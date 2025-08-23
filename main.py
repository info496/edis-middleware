import io
import os
import logging
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Body, Header, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from storage import init_db, upsert_readings, select_readings
from edis_pw import EDisPWClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("edis")

app = FastAPI(title="e-Distribuzione CSV Middleware", version="1.0")

# --- CORS ---
from fastapi.middleware.cors import CORSMiddleware

ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOW_ORIGINS if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "Content-Type", "X-API-Key"],
    expose_headers=["*"],
    max_age=86400,
)

# --- API Key guard (opzionale) ---
API_KEY = os.getenv("API_KEY")  # imposta su Render, es. 'supersegreta123'

def api_key_guard(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

init_db()

def parse_quarterhour_csv(csv_bytes: bytes, pod: str) -> pd.DataFrame:
    """
    Normalizza un CSV quartorario nei campi:
      ts (ISO 8601), value_kwh (float), quality (str)
    Gestisce colonne tipiche 'DATA','ORA','kWh','VALORE'.
    """
    import pandas as pd
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes), sep=";", dtype=str).fillna("")
    except Exception:
        df = pd.read_csv(io.BytesIO(csv_bytes), sep=",", dtype=str).fillna("")

    val_col = next((c for c in df.columns if "kwh" in c.lower() or "valore" in c.lower()), None)
    if not val_col:
        for c in df.columns:
            try:
                pd.to_numeric(df[c].str.replace(",", ".", regex=False), errors="raise")
                val_col = c
                break
            except Exception:
                continue
    if not val_col:
        raise RuntimeError("Colonna valore kWh non trovata.")

    if {"DATA", "ORA"}.issubset(df.columns):
        ts = pd.to_datetime(df["DATA"] + " " + df["ORA"], dayfirst=True, errors="coerce")
    else:
        ts_col = next((c for c in df.columns if "time" in c.lower() or "data" in c.lower() or "ora" in c.lower()), None)
        if not ts_col:
            raise RuntimeError("Colonna timestamp non trovata.")
        ts = pd.to_datetime(df[ts_col], dayfirst=True, errors="coerce")

    out = pd.DataFrame({
        "pod": pod,
        "ts": ts.dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "value_kwh": pd.to_numeric(df[val_col].str.replace(",", ".", regex=False), errors="coerce"),
        "quality": "",
    }).dropna(subset=["ts", "value_kwh"])

    out["ts"] = pd.to_datetime(out["ts"]).dt.floor("15min").dt.strftime("%Y-%m-%dT%H:%M:%S")
    return out

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

@app.post("/refresh")
def refresh(payload: dict = Body(...), _: bool = Depends(api_key_guard)):
    """
    Body JSON: { username, password, pod, date_from, date_to }
    """
    try:
        username = payload.get("username"); password = payload.get("password")
        pod = payload["pod"]
        date_from = payload["date_from"]
        date_to = payload["date_to"]

        datetime.strptime(date_from, "%Y-%m-%d")
        datetime.strptime(date_to, "%Y-%m-%d")
        if date_to <= date_from:
            raise ValueError("date_to deve essere > date_from")
        if not username or not password:
            raise ValueError("username/password mancanti")

        client = EDisPWClient()
        csv_bytes = client.download_csv(username, password, pod, date_from, date_to)

        df = parse_quarterhour_csv(csv_bytes, pod)
        rows = [(pod, r.ts, float(r.value_kwh), str(r.quality)) for r in df.itertuples(index=False)]
        upsert_readings(rows)
        return {"status":"ok","pod":pod,"from":date_from,"to":date_to,"rows":len(rows)}
    except Exception as e:
        log.exception("refresh error")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/data")
def data(pod: str, date_from: str, date_to: str, _: bool = Depends(api_key_guard)):
    try:
        return JSONResponse(select_readings(pod, date_from, date_to))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
