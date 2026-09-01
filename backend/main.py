"""
FastAPI backend for the PDF -> Vietnamese specialized-document translator.

Single synchronous endpoint: POST /api/translate takes a PDF, translates it
end-to-end, and returns the translated PDF directly in the response. This
shape works identically whether self-hosted with `uvicorn main:app` or
deployed as a Vercel serverless function (see ../api/index.py, which shares
this same backend/ code) - no background jobs, no in-memory/disk state that
needs to survive across requests.

The frontend (a static single-page app) is served from /.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from deepseek_client import TranslationError
from pdf_translator import translate_pdf

BACKEND_DIR = Path(__file__).resolve().parent
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


@app.get("/api/health")
async def health():
    return {"ok": True, "has_default_api_key": bool(DEFAULT_API_KEY)}


@app.post("/api/translate")
async def translate(
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

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, "input.pdf")
        output_path = os.path.join(tmp_dir, "translated_vi.pdf")
        with open(input_path, "wb") as f:
            f.write(contents)

        try:
            translate_pdf(
                input_path,
                output_path,
                api_key=effective_key,
                deepseek_base_url=(deepseek_base_url or "").strip() or DEFAULT_BASE_URL,
                deepseek_model=(deepseek_model or "").strip() or DEFAULT_MODEL,
            )
        except TranslationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface any unexpected error to the UI
            raise HTTPException(status_code=500, detail=f"Lỗi không mong muốn: {exc}") from exc

        with open(output_path, "rb") as f:
            translated_bytes = f.read()

    download_name = f"{Path(file.filename).stem}_vi.pdf"
    # HTTP headers must be latin-1; the filename can contain Vietnamese/
    # Chinese characters (from the uploaded file's own name), so send both
    # an ASCII fallback and an RFC 6266 UTF-8 filename* per the header spec.
    ascii_fallback = download_name.encode("ascii", "ignore").decode("ascii") or "translated_vi.pdf"
    content_disposition = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(download_name)}"
    return Response(
        content=translated_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition},
    )


# Serve the frontend last so it doesn't shadow the /api/* routes above.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
