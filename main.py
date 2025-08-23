# main.py
import os
from datetime import date
from typing import Any, List, Dict, Tuple

from fastapi import FastAPI, Header, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- integrazione scraping ---
# Devi avere in edis_pw.py due funzioni:
# - refresh_with_login(username, password, pod, date_from, date_to, timeout_ms) -> List[Dict] | Dict
# - refresh_with_session(storage_state_path, pod, date_from, date_to, timeout_ms) -> List[Dict] | Dict
from edis_pw import refresh_with_login, refresh_with_session  # type: ignore

# --- CONFIG / ENV ---
API_KEY       = os.getenv("API_KEY", "")
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))
STORAGE_STATE = os.getenv("STORAGE_STATE")  # es. /etc/secrets/storage_state.json

# CORS
ALLOW = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]

app = FastAPI(title="e-Distribuzione CSV Middleware", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "Content-Type", "X-API-Key"],
    expose_headers=["*"],
    max_age=86400,
)

# -------- Cache di cortesia (se non usi un tuo storage.py) -------------------
# Se nel tuo repo hai un modulo storage con funzioni fetch/store, viene usato.
# Altrimenti usa una cache in memoria (utile per test).
try:
    from storage import fetch_data as _fetch_data, store_data as _store_data  # type: ignore

    def cache_store(pod: str, dfrom: date, dto: date, rows: List[Dict[str, Any]]) -> None:
        _store_data(pod, dfrom, dto, rows)

    def cache_fetch(pod: str, dfrom: date, dto: date) -> List[Dict[str, Any]]:
        return _fetch_data(pod, dfrom, dto)
except Exception:
    _CACHE: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}

    def _key(pod: str, dfrom: date, dto: date) -> Tuple[str, str, str]:
        return (pod, str(dfrom), str(dto))

    def cache_store(pod: str, dfrom: date, dto: date, rows: List[Dict[str, Any]]) -> None:
        _CACHE[_key(pod, dfrom, dto)] = rows

    def cache_fetch(pod: str, dfrom: date, dto: date) -> List[Dict[str, Any]]:
        return _CACHE.get(_key(pod, dfrom, dto), [])

# --------------------------- Modelli -----------------------------------------
class RefreshReq(BaseModel):
    pod: str
    date_from: date
    date_to: date
    username: str | None = None
    password: str | None = None
    use_storage: bool | None = None  # se true forza uso sessione salvata


# --------------------------- Helpers -----------------------------------------
def _check_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")


def _extract_rows(res: Any) -> List[Dict[str, Any]]:
    """
    Normalizza il risultato delle funzioni di scraping:
    - se tornano direttamente una lista di righe -> la usa
    - se tornano un dict con 'rows_data' o 'data' -> usa quello
    - altrimenti lista vuota
    """
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("rows_data", "data", "rowsList", "rows"):
            v = res.get(k)
            if isinstance(v, list):
                return v
    return []


# --------------------------- Endpoints ----------------------------------------
@app.get("/healthz")
def healthz() -> str:
    return "ok"


@app.post("/refresh")
async def refresh(
    req: RefreshReq,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """
    Aggiorna la cache per il POD/periodo:
    - se use_storage=true (body o query) o se mancano credenziali MA è presente STORAGE_STATE
      => usa la sessione salvata (niente captcha)
    - altrimenti, se ci sono username/password => esegue login classico
    - altrimenti 400
    """
    _check_key(x_api_key)

    # accetta anche ?use_storage=1|true|yes
    use_storage = req.use_storage
    if use_storage is None:
        qs = request.query_params.get("use_storage")
        use_storage = qs in ("1", "true", "True", "yes", "on")

    # 1) Usa sessione salvata
    if use_storage or (
        not req.username and not req.password and STORAGE_STATE and os.path.exists(STORAGE_STATE)
    ):
        if not STORAGE_STATE or not os.path.exists(STORAGE_STATE):
            raise HTTPException(status_code=400, detail="sessione non disponibile sul server")
        try:
            res = await refresh_with_session(
                storage_state_path=STORAGE_STATE,
                pod=req.pod,
                date_from=req.date_from,
                date_to=req.date_to,
                timeout_ms=PW_TIMEOUT_MS,
            )
            rows = _extract_rows(res)
            if rows:
                cache_store(req.pod, req.date_from, req.date_to, rows)
            return {
                "pod": req.pod,
                "from": str(req.date_from),
                "to": str(req.date_to),
                "rows": len(rows),
                "mode": "storage_state",
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"errore sessione: {e}")

    # 2) Login con credenziali
    if req.username and req.password:
        try:
            res = await refresh_with_login(
                username=req.username,
                password=req.password,
                pod=req.pod,
                date_from=req.date_from,
                date_to=req.date_to,
                timeout_ms=PW_TIMEOUT_MS,
            )
            rows = _extract_rows(res)
            if rows:
                cache_store(req.pod, req.date_from, req.date_to, rows)
            return {
                "pod": req.pod,
                "from": str(req.date_from),
                "to": str(req.date_to),
                "rows": len(rows),
                "mode": "login",
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"errore login: {e}")

    # 3) Mancano info sufficienti
    raise HTTPException(
        status_code=400,
        detail="username/password mancanti o sessione non disponibile",
    )


@app.get("/data")
def get_data(
    pod: str = Query(..., description="POD"),
    date_from: date = Query(...),
    date_to: date = Query(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Ritorna le righe quartorarie in cache per POD/periodo."""
    _check_key(x_api_key)
    try:
        rows = cache_fetch(pod, date_from, date_to)
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"errore lettura cache: {e}")
# --- DIAGNOSTICA SEMPLICE (aggiunta) ---
import os, os.path, json, logging
logger = logging.getLogger("uvicorn.error")

@app.get("/diag")
def diag():
    p = os.getenv("STORAGE_STATE")
    exists = bool(p and os.path.exists(p))
    size = os.path.getsize(p) if exists else 0
    # Proviamo anche a contare quanti cookie ci sono nel file
    head = {"cookies_count": None, "origins_count": None}
    try:
        if exists:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                head["cookies_count"] = len(data.get("cookies", []) or [])
                head["origins_count"] = len(data.get("origins", []) or [])
    except Exception as e:
        head["error"] = str(e)

    return {
        "version": "diag-1",
        "storage_state_path": p,
        "exists": exists,
        "size_bytes": size,
        "head": head,
        "allow_origins": ALLOW,
    }
