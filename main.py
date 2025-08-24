# main.py
import os
import json
import datetime as dt
from typing import List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- Import dal modulo Playwright (async) ----
# Assumiamo che nel repo sia presente edis_pw.py con questa funzione async.
# Se nel tuo repo il nome differisce, adegua QUI l'import.
from edis_pw import refresh_and_download_csv_async, SessionMissingError

# --------------------------------------------------------------------
# Config da ENV
# --------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "").strip()  # es.: "CE-eds-2025"
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))  # default 90s
STORAGE_STATE_PATH = os.getenv("STORAGE_STATE", "/app/storage_state.json")

# CORS
_ALLOW_ORIGINS_ENV = os.getenv("ALLOW_ORIGINS", "*").strip()
ALLOW_ORIGINS: List[str] = (
    ["*"]
    if _ALLOW_ORIGINS_ENV == "*" or _ALLOW_ORIGINS_ENV == ""
    else [o.strip() for o in _ALLOW_ORIGINS_ENV.split(",") if o.strip()]
)

# --------------------------------------------------------------------
# FastAPI app & CORS
# --------------------------------------------------------------------
app = FastAPI(title="e-Distribuzione CSV Middleware", version="1.0")

# Nota importante:
# - Se allow_credentials=True, non si può usare "*" come allow_origins.
#   Manteniamo allow_credentials=False così puoi usare anche "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "X-API-Key", "Content-Type"],
    expose_headers=["Content-Type", "X-API-Key"],
    max_age=86400,
)

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _iso_date(s: str) -> str:
    """
    Normalizza una data in input ("dd/mm/yyyy", "yyyy-mm-dd", ecc.)
    restituendo 'YYYY-MM-DD'. Lancia ValueError se non riconosciuta.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("data vuota")

    # Prova ISO diretto
    try:
        return dt.date.fromisoformat(s).isoformat()
    except Exception:
        pass

    # Prova dd/mm/yyyy
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue

    raise ValueError(f"Formato data non riconosciuto: {s!r}")

def _read_storage_head(path: str) -> dict:
    """
    Legge velocemente alcune info dal file di storage_state Playwright
    (conteggio cookie, numero origini).
    """
    if not os.path.exists(path):
        return {"exists": False, "size_bytes": 0, "head": None}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", []) or []
        origins = data.get("origins", []) or []
        return {
            "exists": True,
            "size_bytes": os.path.getsize(path),
            "head": {
                "cookies_count": len(cookies),
                "origins_count": len(origins),
            },
        }
    except Exception as e:
        return {"exists": True, "size_bytes": os.path.getsize(path), "error": str(e)}

# --------------------------------------------------------------------
# Security (API Key opzionale)
# --------------------------------------------------------------------
async def require_api_key(x_api_key: Optional[str] = Header(None)):
    """
    Se API_KEY è settata, richiede che l'header X-API-Key la corrisponda.
    Se API_KEY è vuota, non impone alcun controllo.
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# --------------------------------------------------------------------
# Schemi Pydantic
# --------------------------------------------------------------------
class RefreshPayload(BaseModel):
    username: Optional[str] = ""   # usato solo se use_storage=False
    password: Optional[str] = ""   # usato solo se use_storage=False
    pod: str
    date_from: str                 # accetta dd/mm/yyyy o yyyy-mm-dd
    date_to: str                   # accetta dd/mm/yyyy o yyyy-mm-dd
    use_storage: bool = False      # se True usa solo la sessione salvata

# --------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/diag")
async def diag():
    """
    Ritorna info sintetiche sulla sessione Playwright salvata e CORS correnti.
    """
    head = _read_storage_head(STORAGE_STATE_PATH)
    return {
        "version": "diag-1",
        "storage_state_path": STORAGE_STATE_PATH,
        **head,
        "allow_origins": ALLOW_ORIGINS,
    }

@app.post("/refresh", dependencies=[Depends(require_api_key)])
async def refresh(body: RefreshPayload):
    """
    Esegue il flusso Playwright per aggiornare/leggere le curve e
    scaricare il CSV. Ritorna un JSON con ok/detail/log (non streamma direttamente il CSV).
    """
    # Normalizza date
    try:
        f_iso = _iso_date(body.date_from)
        t_iso = _iso_date(body.date_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Log in uscita (ritornato al client per debug)
    out_log: List[str] = []
    out_log.append("=== refresh: start ===")
    out_log.append(f"use_storage={body.use_storage}")
    out_log.append(f"POD={body.pod}")
    out_log.append(f"date_from={f_iso}")
    out_log.append(f"date_to={t_iso}")

    # Controlli minimi
    if not body.use_storage:
        # Richiesto login: username/password obbligatori
        if not (body.username and body.password):
            raise HTTPException(status_code=400, detail="Missing username/password")
    else:
        # flusso da sessione salvata
        if not os.path.exists(STORAGE_STATE_PATH):
            raise HTTPException(status_code=500, detail="Sessione salvata non presente")

    # Chiamata Playwright (async)
    try:
        csv_bytes, pw_log = await refresh_and_download_csv_async(
            pod=body.pod,
            date_from=f_iso,
            date_to=t_iso,
            use_storage=body.use_storage,
            username=(body.username or None),
            password=(body.password or None),
            timeout_ms=PW_TIMEOUT_MS,
            storage_state_path=STORAGE_STATE_PATH,
        )
        # Unisce i log provenienti dal layer Playwright
        if pw_log:
            out_log.extend(pw_log)

        out_log.append("=== refresh: end ===")
        # Risposta standard JSON (il front-end stampa `body.detail`/`log`)
        return {"ok": True, "detail": "CSV scaricato/aggiornato", "log": out_log}

    except SessionMissingError as e:
        out_log.append(f"[error] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    except Exception as e:
        # Risposta d’errore generica con log per il front-end
        out_log.append(f"[exception] {repr(e)}")
        raise HTTPException(status_code=500, detail=str(e), headers={"X-Error": "refresh-failed"})

# --------------------------------------------------------------------
# (Niente if __name__ == '__main__') -> avvio gestito da start.sh/uvicorn
# --------------------------------------------------------------------
