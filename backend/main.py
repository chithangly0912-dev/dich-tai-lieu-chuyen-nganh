"""
FastAPI backend for the PDF -> Vietnamese specialized-document translator.

Flow:
  1. POST /api/jobs           - upload a PDF, get back a job_id, translation
                                 starts running in a background thread.
  2. GET  /api/jobs/{job_id}  - poll progress (stage/current/total/message).
  3. GET  /api/jobs/{job_id}/download - download the finished PDF.

The frontend (a static single-page app) is served from /.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from deepseek_client import TranslationError
from pdf_translator import translate_pdf

BACKEND_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BACKEND_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

# .env can live next to main.py (backend/.env) or at the project root -
# check both so `uvicorn main:app` works whichever directory it's launched
# from. Values already present in the real environment always win.
for _candidate in (BACKEND_DIR / ".env", BACKEND_DIR.parent / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate, override=False)

MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "60"))
DEFAULT_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

app = FastAPI(title="PDF -> Vietnamese Technical Translator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory job registry. Fine for a single-process local/self-hosted app;
# swap for Redis/a DB if you ever need multi-worker deployment.
# ---------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _new_job(filename: str) -> str:
    job_id = uuid.uuid4().hex
    job_dir = STORAGE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "filename": filename,
            "status": "queued",
            "stage": "queued",
            "current": 0,
            "total": 0,
            "message": "Đang chờ xử lý...",
            "error": None,
            "result": None,
            "created_at": time.time(),
        }
    return job_id


def _update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy job.")
        return dict(job)


def _run_job(job_id: str, input_path: str, output_path: str, api_key: str, base_url: str, model: str) -> None:
    _update_job(job_id, status="running", stage="starting")

    def on_progress(stage: str, current: int, total: int, message: str) -> None:
        _update_job(job_id, stage=stage, current=current, total=total, message=message)

    try:
        stats = translate_pdf(
            input_path,
            output_path,
            api_key=api_key,
            on_progress=on_progress,
            deepseek_base_url=base_url,
            deepseek_model=model,
        )
        _update_job(job_id, status="done", stage="done", result=stats, message="Hoàn tất.")
    except TranslationError as exc:
        _update_job(job_id, status="error", stage="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any unexpected error to the UI
        _update_job(job_id, status="error", stage="error", error=f"Lỗi không mong muốn: {exc}")


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    api_key: str | None = Form(None),
    deepseek_base_url: str | None = Form(None),
    deepseek_model: str | None = Form(None),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ file .pdf")

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File quá lớn ({size_mb:.1f} MB). Giới hạn hiện tại là {MAX_UPLOAD_MB:.0f} MB.",
        )

    effective_key = (api_key or "").strip() or DEFAULT_API_KEY
    if not effective_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "Thiếu DeepSeek API key. Đặt biến môi trường DEEPSEEK_API_KEY trên server, "
                "hoặc nhập API key trong giao diện."
            ),
        )

    job_id = _new_job(file.filename)
    job_dir = STORAGE_DIR / job_id
    input_path = job_dir / "input.pdf"
    output_path = job_dir / "translated_vi.pdf"
    input_path.write_bytes(contents)

    thread = threading.Thread(
        target=_run_job,
        args=(
            job_id,
            str(input_path),
            str(output_path),
            effective_key,
            (deepseek_base_url or "").strip() or DEFAULT_BASE_URL,
            (deepseek_model or "").strip() or DEFAULT_MODEL,
        ),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _get_job(job_id)
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    job = _get_job(job_id)
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="Bản dịch chưa hoàn tất.")
    output_path = STORAGE_DIR / job_id / "translated_vi.pdf"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy file kết quả.")

    base_name = Path(job["filename"]).stem
    download_name = f"{base_name}_vi.pdf"
    return FileResponse(str(output_path), media_type="application/pdf", filename=download_name)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    _get_job(job_id)  # 404s if missing
    job_dir = STORAGE_DIR / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    with _jobs_lock:
        _jobs.pop(job_id, None)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "has_default_api_key": bool(DEFAULT_API_KEY)}


# Serve the frontend last so it doesn't shadow the /api/* routes above.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
