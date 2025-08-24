# main.py
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Optional, List

import anyio
import pandas as pd
from fastapi import FastAPI, Depends, Header, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse, JSONResponse, FileResponse

from edis_pw import refresh_and_download_csv  # <-- modulo Playwright

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

API_TITLE = "e-Distribuzione CSV Middleware"
API_VERSION = "1.0"

CACHE_DIR = Path("storage")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# CORS
_allow = os.getenv("ALLOW_ORIGINS", "*")
ALLOW_ORIGINS = [x.strip() for x in _allow.split(",")] if _allow else ["*"]

# API-Key
API_KEY = os.getenv("API_KEY", "").strip()

# Timeout Playwright (millisecondi)
try:
    PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))
except ValueError:
    PW_TIMEOUT_MS = 90000

# Percorso storage_state (solo per /diag)
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")

app = FastAPI(title=API_TITLE, version=API_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# -----------------------------------------------------------------------------
# Sicurezza con API Key
# -----------------------------------------------------------------------------
def api_key_guard(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if not API_KEY:
        # se non c'è API_KEY lato server, non applico il guard
        return
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key non valida",
        )

# -----------------------------------------------------------------------------
# Modelli
# -----------------------------------------------------------------------------
class RefreshPayload(BaseModel):
    username: Optional[str] = Field(None, description="Username portale eDistribuzione")
    password: Optional[str] = Field(None, description="Password portale eDistribuzione")
    pod: str = Field(..., description="Codice POD")
    date_from: str = Field(..., description="YYYY-MM-DD")
    date_to: str = Field(..., description="YYYY-MM-DD")
    use_storage: bool = Field(False, description="Usa storage_state esistente")

# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------
def _cache_basename(pod: str, date_from: str, date_to: str) -> str:
    safe_pod = pod.replace("/", "_")
    return f"{safe_pod}_{date_from}_{date_to}"

def _csv_path(pod: str, date_from: str, date_to: str) -> Path:
    return CACHE_DIR / f"{_cache_basename(pod, date_from, date_to)}.csv"

def _json_path(pod: str, date_from: str, date_to: str) -> Path:
    return CACHE_DIR / f"{_cache_basename(pod, date_from, date_to)}.json"

def _csv_to_json(csv_file: Path) -> List[dict]:
    """
    Converte il CSV di e-Distribuzione in JSON semplice:
    [{"timestamp": "...", "kwh_15": <float>, "quality": "..."}]
    """
    df = pd.read_csv(csv_file, sep=";", engine="python")
    cols = [c.lower().strip() for c in df.columns]
    df.columns = cols

    ts_col = None
    for c in ("timestamp", "data/ora", "data ora", "data_ora"):
        if c in df.columns:
            ts_col = c
            break

    kwh_col = None
    for c in ("energia attiva (kwh)", "energia attiva", "kwh", "kwh (15')", "kwh (15m)"):
        if c in df.columns:
            kwh_col = c
            break

    quality_col = None
    for c in ("quality", "qualita", "qualità", "stato", "flag"):
        if c in df.columns:
            quality_col = c
            break

    if ts_col is None or kwh_col is None:
        # fallback: restituisco il CSV come records
        return df.to_dict(orient="records")

    out = []
    for _, row in df.iterrows():
        val = row.get(kwh_col)
        kwh = None
        if pd.notna(val):
            try:
                kwh = float(str(val).replace(",", "."))
            except Exception:
                kwh = None

        out.append(
            {
                "timestamp": str(row.get(ts_col, "")),
                "kwh_15": kwh,
                "quality": (row.get(quality_col) if quality_col else None),
            }
        )
    return out

def _ensure_json_cache(csv_file: Path, json_file: Path) -> None:
    if not json_file.exists():
        data = _csv_to_json(csv_file)
        json_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

@app.get("/diag")
def diag():
    info = {
        "version": "diag-1",
        "storage_state_path": STORAGE_STATE,
        "exists": Path(STORAGE_STATE).exists(),
        "size_bytes": Path(STORAGE_STATE).stat().st_size if Path(STORAGE_STATE).exists() else 0,
        "head": {},
        "allow_origins": ALLOW_ORIGINS,
    }
    if Path(STORAGE_STATE).exists():
        try:
            raw = json.loads(Path(STORAGE_STATE).read_text(encoding="utf-8"))
            cookies = raw.get("cookies", [])
            origins = raw.get("origins", [])
            info["head"] = {
                "cookies_count": len(cookies),
                "origins_count": len(origins),
            }
        except Exception:
            pass
    return info

@app.post("/refresh", dependencies=[Depends(api_key_guard)])
async def refresh(payload: RefreshPayload):
    """
    Avvia Playwright:
      - se use_storage=True usa la sessione salvata (storage_state) e NON richiede credenziali
      - altrimenti richiede username/password
    Salva il CSV in cache + genera JSON.
    """
    # Logger: funzione (callable) + lista per restituire i log al client
    log_messages: List[str] = []

    def log(msg: str):
        try:
            log_messages.append(str(msg))
        except Exception:
            # best-effort
            pass

    if not payload.use_storage:
        if not payload.username or not payload.password:
            raise HTTPException(
                status_code=400,
                detail="username/password mancanti",
            )

    csv_file = _csv_path(payload.pod, payload.date_from, payload.date_to)
    json_file = _json_path(payload.pod, payload.date_from, payload.date_to)

    try:
        # timeout sincrono (fix __aenter__ usato prima in modo errato)
        with anyio.fail_after(PW_TIMEOUT_MS / 1000.0):
            downloaded_path = await refresh_and_download_csv(
                username=payload.username,
                password=payload.password,
                pod=payload.pod,
                date_from=payload.date_from,
                date_to=payload.date_to,
                use_storage=payload.use_storage,
                log=log,  # <--- PASSO UNA FUNZIONE, NON UNA LISTA
            )

        downloaded = Path(downloaded_path)
        if not downloaded.exists():
            raise RuntimeError("Download CSV non trovato")

        # normalizzo posizione/nome in cache
        if downloaded.resolve() != csv_file.resolve():
            csv_file.write_bytes(downloaded.read_bytes())

        _ensure_json_cache(csv_file, json_file)

        return {
            "ok": True,
            "csv": f"/csv?pod={payload.pod}&date_from={payload.date_from}&date_to={payload.date_to}",
            "json": f"/data?pod={payload.pod}&date_from={payload.date_from}&date_to={payload.date_to}",
            "log": log_messages,
        }

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": str(e), "log": log_messages},
        )

@app.get("/data", dependencies=[Depends(api_key_guard)])
def get_data(
    pod: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
):
    json_file = _json_path(pod, date_from, date_to)
    csv_file = _csv_path(pod, date_from, date_to)

    if not json_file.exists():
        if not csv_file.exists():
            raise HTTPException(status_code=404, detail="Cache assente. Esegui /refresh.")
        _ensure_json_cache(csv_file, json_file)

    try:
        data = json.loads(json_file.read_text(encoding="utf-8"))
        return {"ok": True, "count": len(data), "items": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/csv", response_class=FileResponse, dependencies=[Depends(api_key_guard)])
def get_csv(
    pod: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
):
    csv_file = _csv_path(pod, date_from, date_to)
    if not csv_file.exists():
        raise HTTPException(status_code=404, detail="CSV non presente. Esegui /refresh.")

    filename = f"{_cache_basename(pod, date_from, date_to)}.csv"
    return FileResponse(
        csv_file,
        media_type="text/csv; charset=utf-8",
        filename=filename,
    )

# -----------------------------------------------------------------------------
# Avvio locale (non usato su Render)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=True)
