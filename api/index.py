"""
Vercel serverless entrypoint. Thin wrapper that mounts backend/main.py's
FastAPI `app` as-is - all translation logic lives in backend/ (shared with
the self-hosted `uvicorn main:app` setup) so there's exactly one copy of it.

vercel.json rewrites every /api/* request to this function; includeFiles
bundles backend/ (code + fonts) into the deployment since those aren't
picked up by Vercel's static import tracing.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from main import app  # noqa: E402
