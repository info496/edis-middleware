# main.py
from __future__ import annotations

import os
import json
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --- import robusto dal modulo edis_pw ---------------------------------
# Prova prima l'async; se non c'è, prova la sync e la "asyncifica" in thread.
try:
    from edis_pw import refresh_and_download_csv_async as _async_fn, SessionMissingError  # type: ignore
except Exception as e1:  # noqa: F841
    try:
        from edis_pw import refresh_and_download_csv as _sync_fn, SessionMissingError  # type: ignore
        import asyncio

        async def _async_fn(**kwargs):
            # esegue la versione sync su thread, così FastAPI resta async
            return await asyncio.to_thread(_sync_fn, **kwargs)

    except Exception as e2:
        # Diagnostica dura: mostra cosa c'è nel modulo su Render
        import importlib
        mod = importlib.import_module("edis_pw")
        raise RuntimeError(
            f"Impossibile importare la funzione dal modulo edis_pw. "
            f"dir(edis_pw)={dir(mod)}; primo errore={repr(e1)}; secondo errore={repr(e2)}"
        )

# alias uniforme usato sotto
refresh_and_download_csv_async = _async_fn

APP_TITLE = "e-Distribuzione CSV Middleware"
APP_VERSION = "1.0"

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

# --------------------- CORS ---------------------
ALLOW_ORIGINS_ENV = os.getenv("ALLOW_ORIGINS", "*").strip()
if ALLOW_ORIGINS_ENV in ("", "*"):
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in ALLOW_ORIGINS_ENV.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "Content-Type", "X-API-Key"],
    expose_headers=["*"],
    max_age=86400,
)

# ----------------- API key opzionale -------------
API_KEY = os.getenv("API_KEY")  # es. 'CE-eds-2025'

def _require_api_key(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("X-API-Key")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ----------------- Modello input -----------------
class RefreshBody(BaseModel):
    pod: str = Field(..., description="POD")
    date_from: str = Field(..., description="dd/mm/yyyy")
    date_to: str = Field(..., description="dd/mm/yyyy")
    use_storage: bool = Field(False, description="Usa sessione salvata")
    username: Optional[str] = Field(None)
    password: Optional[str] = Field(None)

# ----------------- Utils -------------------------
def _ok(msg: str) -> Dict[str, Any]:
    return {"ok": True, "msg": msg}

def _err(msg: str, log: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"ok": False, "detail": msg, "log": log or []}

# ----------------- Endpoints ---------------------
@app.get("/healthz")
async def healthz():
    return _ok("ok")

@app.get("/diag")
async def diag():
    storage_state_path = os.getenv("STORAGE_STATE", "/app/storage_state.json")
    return {
        "version": "diag-1",
        "storage_state_path": storage_state_path,
        "exists": os.path.exists(storage_state_path),
        "size_bytes": os.path.getsize(storage_state_path) if os.path.exists(storage_state_path) else 0,
        "allow_origins": allow_origins,
    }

# endpoint diagnostico: mostra cosa vede il server dentro edis_pw
@app.get("/debug/edis")
async def debug_edis():
    import importlib
    m = importlib.import_module("edis_pw")
    return {"ok": True, "dir": dir(m)}

@app.options("/refresh")
async def refresh_options():
    return _ok("ok")

@app.post("/refresh")
async def refresh(request: Request, body: RefreshBody):
    _require_api_key(request)
    log: List[str] = []

    try:
        payload = {
            "pod": body.pod,
            "date_from": body.date_from,
            "date_to": body.date_to,
            "use_storage": body.use_storage,
            "username": body.username,
            "password": body.password,
        }
        masked = dict(payload)
        if masked.get("password"):
            masked["password"] = "***"
        log.append(f"sending={json.dumps(masked, ensure_ascii=False)}")

        result = await refresh_and_download_csv_async(log=log, **payload)
        return {"ok": True, "csv": result.get("csv"), "log": log}

    except SessionMissingError as e:
        return _err(str(e), log)
    except Exception as e:
        return _err(str(e), log)

@app.get("/")
async def root():
    return _ok("see /healthz, /diag, /debug/edis, /refresh")
