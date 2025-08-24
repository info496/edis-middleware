# main.py
from __future__ import annotations

import os
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from edis_pw import refresh_and_download_csv

app = FastAPI(title="e-Distribuzione CSV Middleware", version="2.0")

# --- CORS ---
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOW_ORIGINS if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,
)

# --- Config ---
API_KEY = os.getenv("API_KEY", "CE-eds-2025")
PW_TIMEOUT = int(os.getenv("PW_TIMEOUT_MS", "90000"))
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")


def _require_key(request: Request):
    api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if not api_key or api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/diag")
def diag():
    exists = os.path.exists(STORAGE_STATE)
    size = os.path.getsize(STORAGE_STATE) if exists else 0
    head = {}
    try:
        if exists:
            with open(STORAGE_STATE, "r", encoding="utf-8") as f:
                data = json.load(f)
            head["cookies_count"] = len(data.get("cookies", []))
            head["origins_count"] = len(data.get("origins", []))
    except Exception:
        pass

    return {
        "version": "diag-3",
        "storage_state_path": STORAGE_STATE,
        "exists": exists,
        "size_bytes": size,
        "head": head,
        "allow_origins": [o.strip() for o in ALLOW_ORIGINS if o.strip()],
    }


@app.post("/refresh")
async def refresh(request: Request):
    """
    Usa solo la sessione salvata (use_storage=true) per aprire la pagina
    e cliccare il bottone di download. I campi username/password/pod/date
    nel body vengono ignorati in questa versione.
    """
    _require_key(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    use_storage = bool(body.get("use_storage", True))
    if not use_storage:
        raise HTTPException(
            status_code=400,
            detail="Questo endpoint supporta solo use_storage=true (usa la sessione salvata).",
        )

    res = refresh_and_download_csv(
        storage_state_path=STORAGE_STATE,
        out_dir="/app/tmp",
        headless=True,
        timeout_ms=PW_TIMEOUT,
    )

    if not res.get("ok"):
        return JSONResponse(
            status_code=500,
            content={
                "detail": res.get("detail"),
                "log": list(res.get("log", [])),
            },
        )

    return {
        "ok": True,
        "csv_path": res.get("csv_path"),
        "rows_count": len(res.get("rows", [])),
        "log": list(res.get("log", [])),
    }
