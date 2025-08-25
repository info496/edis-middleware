# main.py
#
# FastAPI + CORS + endpoint /refresh che usa la funzione async
# da edis_pw.py

from __future__ import annotations

import os
import json
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from edis_pw import refresh_and_download_csv_async, SessionMissingError

APP_TITLE = "e-Distribuzione CSV Middleware"
APP_VERSION = "1.0"

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

# --- CORS -------------------------------------------------------------
ALLOW_ORIGINS_ENV = os.getenv("ALLOW_ORIGINS", "*").strip()
if ALLOW_ORIGINS_ENV == "*" or ALLOW_ORIGINS_ENV == "":
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in ALLOW_ORIGINS_ENV.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in allow_origins if o],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "Content-Type", "X-API-Key"],
    expose_headers=["*"],
    max_age=86400,
)

# --- API key opzionale -----------------------------------------------
API_KEY = os.getenv("API_KEY")  # es. 'CE-eds-2025'

def _require_api_key(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("X-API-Key")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# --- Modello input ----------------------------------------------------
class RefreshBody(BaseModel):
    pod: str = Field(..., description="POD (IT00...)")
    date_from: str = Field(..., description="Data inizio dd/mm/yyyy")
    date_to: str = Field(..., description="Data fine dd/mm/yyyy")
    use_storage: bool = Field(False, description="Usa sessione salvata")
    username: Optional[str] = Field(None, description="User e-Distribuzione")
    password: Optional[str] = Field(None, description="Password")

# --- Utilities --------------------------------------------------------
def _ok(msg: str) -> Dict[str, Any]:
    return {"ok": True, "msg": msg}

def _err(msg: str, log: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"ok": False, "detail": msg, "log": log or []}

# --- Endpoints --------------------------------------------------------
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
        # solo per logging lato client (mascheriamo la password)
        to_client = dict(payload)
        if to_client.get("password"):
            to_client["password"] = "***"
        log.append(f"sending={json.dumps(to_client, ensure_ascii=False)}")

        result = await refresh_and_download_csv_async(log=log, **payload)
        return {"ok": True, "csv": result.get("csv"), "log": log}

    except SessionMissingError as e:
        return _err(str(e), log)
    except Exception as e:
        # qualunque altro errore
        return _err(str(e), log)

# (opzionale) root
@app.get("/")
async def root():
    return _ok("see /healthz, /diag, /refresh")
