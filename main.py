# main.py
import os
import json
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from edis_pw import refresh_and_download_csv_async, VERSION  # <-- niente SessionMissingError

# -------- util --------

def _get_allowed_origins() -> List[str]:
    """
    ALLOW_ORIGINS può essere:
      - JSON: ["https://dominio1", "https://dominio2"]
      - CSV:  https://dom1,https://dom2
      - "*"  : tutti
    """
    raw = os.getenv("ALLOW_ORIGINS", "*").strip()
    try:
        if raw.startswith("["):
            arr = json.loads(raw)
            if isinstance(arr, list):
                return arr
    except Exception:
        pass
    if raw == "*" or raw == "":
        return ["*"]
    return [x.strip() for x in raw.split(",") if x.strip()]


ALLOW_ORIGINS = _get_allowed_origins()

def _storage_state_path() -> str:
    return os.getenv("STORAGE_STATE", "/app/storage_state.json")

# -------- app --------

app = FastAPI(title="edis-middleware")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,  # con "*" non si può tenere True
    allow_methods=["*"],
    allow_headers=["*"],
)

class RefreshPayload(BaseModel):
    pod: str
    date_from: str
    date_to: str
    use_storage: bool = True
    username: Optional[str] = None
    password: Optional[str] = None


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION}


@app.get("/diag")
async def diag():
    path = _storage_state_path()
    exists = os.path.exists(path)
    size = os.path.getsize(path) if exists else 0
    return {
        "version": "diag-1",
        "storage_state_path": path,
        "exists": exists,
        "size_bytes": size,
        "allow_origins": ALLOW_ORIGINS,
    }


@app.post("/refresh")
async def refresh(payload: RefreshPayload, request: Request):
    # non logghiamo la password in chiaro
    sending: Dict[str, Any] = payload.dict()
    if sending.get("password"):
        sending["password"] = "***"

    log: List[str] = [f"sending={json.dumps(sending)}"]

    try:
        res = await refresh_and_download_csv_async(
            pod=payload.pod,
            date_from=payload.date_from,
            date_to=payload.date_to,
            use_storage=payload.use_storage,
            username=payload.username,
            password=payload.password,
        )
        # includo il log lato server per facilitarne la lettura dal front-end
        if "log" in res:
            res["log"] = res["log"]
        else:
            res["log"] = log
        return res
    except Exception as e:
        log.append(f"server exception: {type(e).__name__}: {e}")
        return {"ok": False, "detail": str(e), "log": log}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
