import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from playwright.async_api import async_playwright

from edis_pw import refresh_and_download_csv, EdisError

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "").strip()
ALLOW_ORIGINS = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")

DOWNLOAD_DIR = Path("/app/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="e-Distribuzione CSV Middleware", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS if ALLOW_ORIGINS != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
    expose_headers=["*"],
    max_age=86400,
)

# -----------------------------------------------------------------------------
# Schemi input
# -----------------------------------------------------------------------------
class RefreshIn(BaseModel):
    pod: str = Field(..., min_length=8)
    date_from: str
    date_to: str
    use_storage: bool = True
    username: Optional[str] = ""
    password: Optional[str] = ""

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _check_key(x_api_key: Optional[str]):
    if API_KEY and (not x_api_key or x_api_key != API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/diag")
async def diag():
    p = Path(STORAGE_STATE)
    return {
        "version": "diag-1",
        "storage_state_path": str(p),
        "exists": p.exists(),
        "size_bytes": (p.stat().st_size if p.exists() else 0),
        "allow_origins": ALLOW_ORIGINS,
    }

@app.post("/refresh")
async def refresh(payload: RefreshIn, x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)

    # Avvia Playwright async
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            if payload.use_storage and Path(STORAGE_STATE).exists():
                context = await browser.new_context(
                    storage_state=STORAGE_STATE,
                    accept_downloads=True,
                    viewport={"width": 1366, "height": 900},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
                )
            else:
                context = await browser.new_context(
                    accept_downloads=True,
                    viewport={"width": 1366, "height": 900},
                )

            page = await context.new_page()
            page.set_default_timeout(PW_TIMEOUT_MS)

            try:
                path, log = await refresh_and_download_csv(
                    page=page,
                    pod=payload.pod,
                    date_from=payload.date_from,
                    date_to=payload.date_to,
                    use_storage=payload.use_storage,
                    username=(payload.username or ""),
                    password=(payload.password or ""),
                    download_dir=DOWNLOAD_DIR,
                )
                size = path.stat().st_size if path.exists() else 0
                return {
                    "ok": True,
                    "file": path.name,
                    "path": str(path),
                    "size": size,
                    "log": log,
                }
            except EdisError as e:
                raise HTTPException(status_code=500, detail=str(e), headers=None) from e
            finally:
                await context.close()
        finally:
            await browser.close()
