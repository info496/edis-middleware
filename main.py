# main.py
import os
import json
from typing import List, Optional
from datetime import datetime

import pandas as pd
from fastapi import FastAPI, HTTPException, Header, Request, Depends, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
import anyio

from edis_pw import refresh_and_download_csv

# ------------------------------------------------------------
# Config da ambiente
# ------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "")
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))  # 90s default

CACHE_DIR = os.getenv("CACHE_DIR", "/app/cache")
CSV_CACHE_PATH = os.path.join(CACHE_DIR, "edis_quartorario.csv")
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")

os.makedirs(CACHE_DIR, exist_ok=True)

# ------------------------------------------------------------
# App & CORS
# ------------------------------------------------------------
app = FastAPI(title="e-Distribuzione CSV Middleware", version="1.0")

origins: List[str] = [o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in origins if o],  # evita stringhe vuote
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "X-API-Key"],
    expose_headers=["*", "X-API-Key"],
    max_age=86400,
)

# ------------------------------------------------------------
# Modelli
# ------------------------------------------------------------
class RefreshPayload(BaseModel):
    username: Optional[str] = Field(default=None, description="Ignorato se use_storage=True (reCAPTCHA)")
    password: Optional[str] = Field(default=None, description="Ignorato se use_storage=True (reCAPTCHA)")
    pod: str
    date_from: str = Field(description="Formati ammessi: YYYY-MM-DD oppure DD/MM/YYYY")
    date_to: str = Field(description="Formati ammessi: YYYY-MM-DD oppure DD/MM/YYYY")
    use_storage: bool = Field(default=True, description="Usa cookie salvati in STORAGE_STATE (consigliato)")

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def check_api_key(x_api_key: Optional[str] = Header(None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

def _csv_exists() -> bool:
    return os.path.exists(CSV_CACHE_PATH) and os.path.getsize(CSV_CACHE_PATH) > 0

def _parse_csv_to_json(path: str) -> list:
    if not os.path.exists(path):
        raise FileNotFoundError("CSV cache non presente")

    # Proviamo prima con ; poi con ,
    try:
        df = pd.read_csv(path, sep=";", engine="python")
        # Se ha una sola colonna gigante con ; dentro, riprova con ,
        if df.shape[1] == 1 and ";" in df.columns[0]:
            raise Exception("semicolon wrong")
    except Exception:
        df = pd.read_csv(path, sep=",", engine="python")

    # Normalizzazioni soft (non obbligatorie)
    # Prova a uniformare nomi colonne noti
    cols = {c.lower().strip(): c for c in df.columns}
    # Esempio tipico: "Timestamp", "kWh (15’)", "Quality"
    # Lasciamo invariato se diverso: non conosciamo sempre l'esatto layout

    # Converti eventuale colonna Timestamp in ISO string
    for c in df.columns:
        if "time" in c.lower():
            try:
                df[c] = pd.to_datetime(df[c], dayfirst=True, errors="coerce").astype("datetime64[ns]")
                df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            break

    # NaN -> None
    data = json.loads(df.to_json(orient="records", date_format="iso"))
    return data

def _fmt_error(msg: str) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": msg})

# ------------------------------------------------------------
# Rotte
# ------------------------------------------------------------
@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"

@app.get("/diag")
async def diag():
    exists = os.path.exists(STORAGE_STATE)
    size = os.path.getsize(STORAGE_STATE) if exists else 0
    # Contiamo cookie/origini sommariamente
    head = {}
    if exists:
        try:
            with open(STORAGE_STATE, "r", encoding="utf-8") as f:
                # non parseiamo tutto: basta il "riassunto"
                txt = f.read(2048)
            # non è affidabile al 100% ma dà un'idea
            head = {"preview": txt[:200]}
        except Exception:
            pass
    return {
        "version": "diag-1",
        "storage_state_path": STORAGE_STATE,
        "exists": exists,
        "size_bytes": size,
        "head": head,
        "allow_origins": origins,
    }

@app.post("/refresh")
async def refresh(
    payload: RefreshPayload = Body(...),
    _: None = Depends(check_api_key),
):
    """
    Scarica dal portale (preferibilmente con `use_storage: true`),
    cerca il bottone 'CSV' e salva in cache.
    """
    # Logging "soft"
    def log(msg: str):
        print("[refresh]", msg)

    try:
        # Timeout globale per Playwright
        async with anyio.fail_after(PW_TIMEOUT_MS / 1000.0):
            path = await refresh_and_download_csv(
                username=payload.username,
                password=payload.password,
                pod=payload.pod,
                date_from=payload.date_from,
                date_to=payload.date_to,
                use_storage=payload.use_storage,
                log=log,
            )

        if not os.path.exists(path):
            raise RuntimeError("Download CSV completato ma file non presente")

        return {"status": "ok", "csv_path": path}

    except TimeoutError:
        return _fmt_error("Timeout")
    except Exception as e:
        # Errori "parlanti" già gestiti da edis_pw.py
        return _fmt_error(str(e))

@app.get("/download/csv")
async def download_csv(
    _: None = Depends(check_api_key),
):
    """
    Ritorna il CSV di cache generato da /refresh.
    """
    if not _csv_exists():
        raise HTTPException(status_code=404, detail="CSV non trovato; esegui prima /refresh")
    fname = f"edis_quartorario_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return FileResponse(CSV_CACHE_PATH, filename=fname, media_type="text/csv")

@app.get("/data")
async def data(
    _: None = Depends(check_api_key),
    # (Opzionali) Filtri lato API se vuoi
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
):
    """
    Ritorna i dati del CSV in JSON. Per filtri precisi conviene filtrare lato client,
    perché il layout dei CSV può variare.
    """
    if not _csv_exists():
        raise HTTPException(status_code=404, detail="CSV non trovato; esegui prima /refresh")

    try:
        records = _parse_csv_to_json(CSV_CACHE_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore lettura CSV: {e}")

    # Filtrino leggero sulla colonna che contiene il tempo (se esiste)
    if date_from or date_to:
        df = pd.DataFrame(records)
        time_col = None
        for c in df.columns:
            if "time" in c.lower():
                time_col = c
                break
        if time_col:
            try:
                df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
                if date_from:
                    df = df[df[time_col] >= pd.to_datetime(date_from, errors="coerce")]
                if date_to:
                    df = df[df[time_col] <= pd.to_datetime(date_to, errors="coerce")]
                records = json.loads(df.to_json(orient="records", date_format="iso"))
            except Exception:
                pass

    return {"rows": records, "count": len(records)}
