# edis_pw.py
import os
import json
from contextlib import suppress
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, TimeoutError as PwTimeoutError

VERSION = "v0.8-dom-tries"

CURVE_URL = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"

# possibile pulsante di download CSV (tante varianti)
CSV_SELECTORS = [
    "button:has-text('Scarica il dettaglio dei quarti orari')",
    "a:has-text('Scarica il dettaglio dei quarti orari')",
    "button:has-text('Scarica CSV')",
    "a:has-text('Scarica CSV')",
    "button:has-text('Download CSV')",
    "a:has-text('Download CSV')",
    "[data-testid='download-csv']",
    "#downloadCsv",
    ".slds-button.slds-button_brand.slds-float_right",
]

# login (Salesforce classico)
LOGIN_USER_SELS = ["#username", "input[name='username']", "input#user_email"]
LOGIN_PASS_SELS = ["#password", "input[name='password']", "input#user_password"]
LOGIN_SUBMIT_SELS = ["#Login", "input#Login", "button[type='submit']", "button:has-text('Accedi')", "input[value='Log In']"]


def _storage_state_path() -> Optional[str]:
    p = os.getenv("STORAGE_STATE") or "/app/storage_state.json"
    return p if os.path.exists(p) else None


async def _is_login_page(page) -> bool:
    url = (page.url or "").lower()
    if "/login" in url:
        return True
    with suppress(Exception):
        if await page.locator(",".join(LOGIN_PASS_SELS)).count() > 0:
            return True
    return False


def _score_frame(f) -> int:
    s = 0
    u = (f.url or "").lower()
    t = (f.name or "").lower()
    if "curvedicarico" in u:
        s += 3
    if "login" in u:
        s += 2
    if "widget" in u or "assistant" in u:
        s -= 5
    if t and "login" in t:
        s += 1
    return s


async def _pick_main_frame(page, log: List[str]):
    frames = page.frames
    log.append(f"Frame totali: {len(frames)}")
    ranked = sorted(frames, key=_score_frame, reverse=True)
    for fr in ranked:
        log.append(f"- Frame url='{fr.url}' title='{fr.name}' -> score={_score_frame(fr)}")
    chosen = ranked[0] if ranked else page.main_frame
    log.append(f"Frame scelto url='{chosen.url}' score={_score_frame(chosen)}")
    return chosen


async def _try_click_download(frame, log: List[str]) -> bool:
    for sel in CSV_SELECTORS:
        try:
            cnt = await frame.locator(sel).count()
            log.append(f"download selectors counts: {sel}:{cnt}")
            if cnt > 0:
                btn = frame.locator(sel).first
                with suppress(Exception):
                    await btn.scroll_into_view_if_needed(timeout=2000)
                await btn.click(timeout=5000)
                return True
        except Exception as e:
            log.append(f"click selector '{sel}' errore: {type(e).__name__}: {e}")
    return False


async def _do_login_if_needed(page, username: Optional[str], password: Optional[str], log: List[str]) -> bool:
    if not await _is_login_page(page):
        return True
    if not username or not password:
        log.append("Sei in pagina di login ma non ho credenziali -> impossibile procedere.")
        return False

    log.append("Sono in pagina di login. Provo ad autenticarmi…")

    ok = False
    for s in LOGIN_USER_SELS:
        with suppress(Exception):
            if await page.locator(s).count():
                await page.locator(s).fill(username, timeout=5000)
                ok = True
                break
    if not ok:
        log.append("Campo username non trovato in login.")
        return False

    ok = False
    for s in LOGIN_PASS_SELS:
        with suppress(Exception):
            if await page.locator(s).count():
                await page.locator(s).fill(password, timeout=5000)
                ok = True
                break
    if not ok:
        log.append("Campo password non trovato in login.")
        return False

    clicked = False
    for s in LOGIN_SUBMIT_SELS:
        with suppress(Exception):
            if await page.locator(s).count():
                await page.locator(s).first.click(timeout=5000)
                clicked = True
                break
    if not clicked:
        log.append("Bottone di login non trovato.")
        return False

    with suppress(PwTimeoutError):
        await page.wait_for_load_state("domcontentloaded", timeout=15000)

    if await _is_login_page(page):
        log.append("Sembra che la login non sia andata a buon fine (captcha?).")
        return False

    log.append("Login eseguita.")
    return True


async def _navigate_with_fallbacks(page, log: List[str]) -> None:
    """
    3 tentativi:
     1) goto(wait_until='domcontentloaded', timeout 120s)
     2) goto(wait_until='load',            timeout 120s)
     3) goto senza wait_until + attesa selettori noti
    """
    page.set_default_timeout(60_000)
    page.context.set_default_timeout(60_000)
    page.set_default_navigation_timeout(60_000)

    # 1 – domcontentloaded
    try:
        log.append("NAV attempt #1: domcontentloaded (120s)")
        await page.goto(CURVE_URL, wait_until="domcontentloaded", timeout=120_000)
        with suppress(PwTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=3_000)
        return
    except Exception as e:
        log.append(f"Attempt #1 fallito: {type(e).__name__}: {e}")

    # 2 – load
    try:
        log.append("NAV attempt #2: load (120s)")
        await page.goto(CURVE_URL, wait_until="load", timeout=120_000)
        with suppress(PwTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=3_000)
        return
    except Exception as e:
        log.append(f"Attempt #2 fallito: {type(e).__name__}: {e}")

    # 3 – senza wait_until + attesa euristica
    log.append("NAV attempt #3: no wait_until + attesa euristica (15s)")
    await page.goto(CURVE_URL, timeout=120_000)
    # attendo che l’URL contenga la route corretta o la login
    for _ in range(15):
        u = (page.url or "").lower()
        if "curvedicarico" in u or "/login" in u:
            break
        with suppress(PwTimeoutError):
            await page.wait_for_timeout(1000)
    # in ogni caso ritorno al chiamante; eventuali login/frames vengono gestiti dopo


async def refresh_and_download_csv_async(
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool = True,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    log: List[str] = []
    log.append(f"edis_pw version: {VERSION}")
    sending = {
        "pod": pod,
        "date_from": date_from,
        "date_to": date_to,
        "use_storage": use_storage,
        "username": username,
        "password": "***" if password else None,
    }
    log.append(f"sending={json.dumps(sending)}")
    log.append("=== refresh_and_download_csv: start ===")

    storage_path = _storage_state_path() if use_storage else None
    if use_storage and not storage_path:
        log.append("ATTENZIONE: use_storage=True ma storage_state non trovato/valido.")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-features=site-per-process",
                ],
            )

            context_args: Dict[str, Any] = {
                "accept_downloads": True,
                "java_script_enabled": True,
                # User-Agent reale di Chrome stabile
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            if storage_path:
                context_args["storage_state"] = storage_path

            context = await browser.new_context(**context_args)
            page = await context.new_page()

            # navigazione con fallbacks
            try:
                await _navigate_with_fallbacks(page, log)
            except Exception as e:
                await context.close(); await browser.close()
                return {
                    "ok": False,
                    "detail": f"Goto error: {type(e).__name__}: {e}",
                    "log": log,
                }

            # se sessione salvata ma ci porta alla login -> storage scaduto
            if use_storage and await _is_login_page(page):
                await context.close(); await browser.close()
                return {
                    "ok": False,
                    "detail": "Sessione salvata non valida/scaduta. Disattiva 'Usa sessione salvata' oppure rigenera lo storage_state.",
                    "log": log,
                }

            # senza storage: se serve, prova login
            if not use_storage:
                ok = await _do_login_if_needed(page, username, password, log)
                if not ok:
                    await context.close(); await browser.close()
                    return {
                        "ok": False,
                        "detail": "Impossibile autenticarsi (mancano campi o captcha).",
                        "log": log,
                    }
                with suppress(Exception):
                    await _navigate_with_fallbacks(page, log)

            # trova frame e click
            frame = await _pick_main_frame(page, log)
            log.append("Provo click 'Download CSV' e aspetto il download…")

            downloaded = False
            try:
                async with page.expect_download(timeout=25_000) as dl_info:
                    clicked = await _try_click_download(frame, log)
                    if not clicked:
                        await context.close(); await browser.close()
                        return {
                            "ok": False,
                            "detail": "Pulsante download CSV non trovato (verifica selettori).",
                            "log": log,
                        }
                download = await dl_info.value
                with suppress(Exception):
                    tmp = await download.path()
                    log.append(f"Download file: {download.suggested_filename}, path={tmp}")
                downloaded = True
            except PwTimeoutError:
                # a volte è XHR, non un vero "download"
                if await frame.locator("|".join(CSV_SELECTORS)).count() > 0:
                    log.append("Click eseguito, ma non ho intercettato un evento 'download' (probabile XHR).")
                    downloaded = True
                else:
                    downloaded = False
            except Exception as e:
                await context.close(); await browser.close()
                return {
                    "ok": False,
                    "detail": f"Errore durante il tentativo di download: {type(e).__name__}: {e}",
                    "log": log,
                }

            await context.close()
            await browser.close()

            if downloaded:
                return {"ok": True, "detail": "Download avviato/ottenuto.", "log": log}
            else:
                return {
                    "ok": False,
                    "detail": "Non sono riuscito ad avviare/ottenere il download del CSV.",
                    "log": log,
                }

    except Exception as e:
        # messaggio raw, senza la dicitura "waiting until networkidle"
        return {
            "ok": False,
            "detail": f"{type(e).__name__}: {e}",
            "log": log,
        }
