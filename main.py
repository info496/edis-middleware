# main.py
import os
import json
import asyncio
import inspect
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware

# Modulo Playwright “app”
# Deve esportare la funzione refresh_and_download_csv(...)
# (sincrona o async; con parametri flessibili – questo file si adatta)
from edis_pw import refresh_and_download_csv

APP_NAME = "e-Distribuzione CSV Middleware"
APP_VERSION = "1.0.0"

# -----------------------
# Config da environment
# -----------------------
API_KEY = os.getenv("API_KEY", "").strip() or os.getenv("CE_eds_2025", "").strip() or os.getenv("CE-eds-2025", "").strip()
ALLOW_ORIGINS = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")

# -----------------------
# App & CORS
# -----------------------
app = FastAPI(title=APP_NAME, version=APP_VERSION)

# CORS standard (per tutte le rotte)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS if ALLOW_ORIGINS != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,
)

# forza header CORS ANCHE su errori e gestisce preflight
@app.middleware("http")
async def force_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        resp: Response = Response(status_code=204)
    else:
        resp = await call_next(request)

    origin = request.headers.get("origin", "*")
    if ALLOW_ORIGINS == ["*"] or any(origin.startswith(o) for o in ALLOW_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = origin if ALLOW_ORIGINS != ["*"] else "*"
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*, X-API-Key, Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# -----------------------
# Util
# -----------------------
def check_api_key(request: Request):
    """Verifica API key se configurata."""
    if not API_KEY:
        return
    provided = request.headers.get("X-API-Key", "")
    if provided != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

def read_storage_head(path: str) -> Dict[str, Any]:
    """Restituisce info sintetiche sullo storage_state.json (se esiste)."""
    out = {"cookies_count": 0, "origins_count": 0}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out["cookies_count"] = len(data.get("cookies", []))
                out["origins_count"] = len(data.get("origins", []))
    except Exception:
        pass
    return out

async def maybe_await(value):
    """Se value è awaitable, fa await; altrimenti restituisce direttamente."""
    if inspect.isawaitable(value):
        return await value
    return value

def build_flexible_kwargs(func, base_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adatta i nomi/parametri alla signature corrente di refresh_and_download_csv.
    Così evitiamo 'unexpected keyword argument'.
    """
    try:
        sig = inspect.signature(func)
        accepted = set(sig.parameters.keys())
    except Exception:
        # se non riusciamo a leggere la signature, proviamo i nomi più comuni
        accepted = {
            "pod", "date_from", "date_to",
            "username", "password",
            "use_storage", "storage_state_path", "storage_state",
            "timeout_ms", "timeout", "headless", "log"
        }

    out: Dict[str, Any] = {}
    for k, v in base_kwargs.items():
        if k in accepted:
            out[k] = v

    # mapping storage_state_path / storage_state
    if "storage_state_path" in accepted and "storage_state_path" not in out:
        # se l'utente ha passato storage_state -> lo convertiamo se è una stringa
        if "storage_state" in base_kwargs and isinstance(base_kwargs["storage_state"], str):
            out["storage_state_path"] = base_kwargs["storage_state"]
        else:
            out["storage_state_path"] = STORAGE_STATE

    if "storage_state" in accepted and "storage_state" not in out:
        # se serve 'storage_state' ed abbiamo solo path: passiamo il path direttamente
        out["storage_state"] = base_kwargs.get("storage_state", STORAGE_STATE)

    # timeout naming
    if "timeout_ms" in accepted and "timeout_ms" not in out and "timeout" in base_kwargs:
        out["timeout_ms"] = base_kwargs["timeout"]
    if "timeout" in accepted and "timeout" not in out and "timeout_ms" in base_kwargs:
        out["timeout"] = base_kwargs["timeout_ms"]

    return out


# -----------------------
# Routes
# -----------------------
@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"

@app.get("/diag")
async def diag():
    info = {
        "version": "diag-1",
        "storage_state_path": STORAGE_STATE,
        "exists": os.path.exists(STORAGE_STATE),
        "size_bytes": os.path.getsize(STORAGE_STATE) if os.path.exists(STORAGE_STATE) else 0,
        "head": read_storage_head(STORAGE_STATE),
        "allow_origins": ALLOW_ORIGINS,
        "timeout_ms": PW_TIMEOUT_MS,
    }
    return info

@app.post("/refresh")
async def refresh(request: Request):
    """
    Esegue:
      - caricamento pagina 'Curve di Carico'
      - (opz.) login con username/password OPPURE uso sessione salvata
      - inserimento POD e date
      - click 'Download CSV'
    Ritorna JSON con log ed esito.
    """
    check_api_key(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    # parametri accettati dal frontend (tutti opzionali tranne le date/POD se non usi storage)
    username: Optional[str] = body.get("username") or body.get("user")
    password: Optional[str] = body.get("password") or body.get("pass")
    pod: Optional[str] = body.get("pod")
    date_from: Optional[str] = body.get("date_from")
    date_to: Optional[str] = body.get("date_to")
    use_storage: bool = bool(body.get("use_storage", False))
    headless: bool = bool(body.get("headless", True))
    storage_path: str = body.get("storage_state") or STORAGE_STATE

    # log raccolta
    log: List[str] = []

    # validazione minima
    if not use_storage and (not username or not password):
        raise HTTPException(status_code=400, detail="username/password mancanti e use_storage=false")
    if not pod:
        raise HTTPException(status_code=400, detail="pod mancante")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date mancanti")

    # kwargs “base”; saranno adattati alla firma corrente di refresh_and_download_csv
    base_kwargs: Dict[str, Any] = {
        "pod": pod,
        "date_from": date_from,
        "date_to": date_to,
        "username": username,
        "password": password,
        "use_storage": use_storage,
        "storage_state_path": storage_path,
        "storage_state": storage_path,  # se il tuo edis_pw vuole 'storage_state'
        "timeout_ms": PW_TIMEOUT_MS,
        "headless": headless,
        "log": log,
    }
    kwargs = build_flexible_kwargs(refresh_and_download_csv, base_kwargs)

    try:
        result = await maybe_await(refresh_and_download_csv(**kwargs))
        # ci aspettiamo un dict; se non lo è, impacchettiamo
        if not isinstance(result, dict):
            result = {"ok": True, "result": result, "log": log}
        # esito OK
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        # errore controllato: rispondiamo 500 con detail + log
        detail = str(e) or e.__class__.__name__
        return JSONResponse(status_code=500, content={"detail": detail, "log": log})


# -----------------------
# Avvio locale
# -----------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
