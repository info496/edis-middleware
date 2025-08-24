import os
import json
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from edis_pw import refresh_and_download_csv_async, SessionMissingError

APP_NAME = "e-Distribuzione CSV Middleware"
API_KEY = os.getenv("API_KEY", "CE-eds-2025")

# CORS
allow_origins_env = os.getenv("ALLOW_ORIGINS", "*")
ALLOW_ORIGINS: List[str] = [o.strip() for o in allow_origins_env.split(",") if o.strip()]

app = FastAPI(title=APP_NAME, version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# ---- Modelli I/O -------------------------------------------------------------

class RefreshIn(BaseModel):
    pod: str = Field(..., description="POD IT00…")
    date_from: str = Field(..., description="YYYY-MM-DD")
    date_to: str = Field(..., description="YYYY-MM-DD")
    use_storage: bool = Field(False, description="Usa sessione salvata")
    username: Optional[str] = None
    password: Optional[str] = None


class RefreshOut(BaseModel):
    ok: bool = True
    detail: Optional[str] = None
    log: List[str] = []
    csv_text: Optional[str] = None
    rows: Optional[list] = None


# ---- Helpers ----------------------------------------------------------------

def check_api_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---- Routes -----------------------------------------------------------------

@app.get("/healthz")
async def healthz(x_api_key: Optional[str] = Header(None)):
    # opzionale: proteggi anche /healthz
    return {"status": "ok"}

@app.get("/diag")
async def diag(x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    storage_path = os.getenv("STORAGE_STATE", "/app/storage_state.json")
    p = Path(storage_path)
    head = {}
    if p.exists():
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            head = {
                "cookies_count": len(data.get("cookies", [])),
                "origins_count": len(data.get("origins", [])),
            }
        except Exception:
            head = {"cookies_count": 0, "origins_count": 0}
    return {
        "version": "diag-1",
        "storage_state_path": storage_path,
        "exists": p.exists(),
        "size_bytes": p.stat().st_size if p.exists() else 0,
        "head": head,
        "allow_origins": ALLOW_ORIGINS,
    }


@app.post("/refresh", response_model=RefreshOut)
async def refresh(payload: RefreshIn, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)

    log: list[str] = []

    # Validazioni base
    if payload.use_storage is False:
        if not payload.username or not payload.password:
            raise HTTPException(status_code=400, detail="Missing username/password")

    try:
        csv_text, rows, more_log = await refresh_and_download_csv_async(
            pod=payload.pod.strip(),
            date_from=payload.date_from,
            date_to=payload.date_to,
            use_storage=payload.use_storage,
            username=payload.username,
            password=payload.password,
        )
        log.extend(more_log or [])

        return RefreshOut(
            ok=True,
            log=log,
            csv_text=csv_text,
            rows=rows,
        )

    except SessionMissingError as e:
        log.append(str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        log.append(f"Unexpected error: {repr(e)}")
        raise HTTPException(status_code=500, detail=str(e))
