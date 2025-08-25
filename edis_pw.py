# edis_pw.py
import os
import json
from contextlib import suppress
from typing import List, Dict, Any, Optional

from playwright.async_api import (
    async_playwright,
    TimeoutError as PwTimeoutError,
)

CURVE_URL = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"

# domini “rumorosi” che non servono alla pagina e tengono viva la rete
BLOCKED_DOMAINS = [
    "adobedtm.com",
    "demdex.net",
    "everesttech.net",
    "doubleclick.net",
    "g.doubleclick.net",
    "google-analytics.com",
    "mookie1.com",
    "scorecardresearch.com",
    "googletagmanager.com",
]

# selettori possibili per il pulsante di download CSV
CSV_SELECTORS = [
    # etichette testuali più probabili
    "button:has-text('Scarica il dettaglio dei quarti orari')",
    "a:has-text('Scarica il dettaglio dei quarti orari')",
    "button:has-text('Scarica CSV')",
    "a:has-text('Scarica CSV')",
    "button:has-text('Download CSV')",
    "a:has-text('Download CSV')",
    # data-testid / id
    "[data-testid='download-csv']",
    "#downloadCsv",
    # variante generica vista nello screenshot (Salesforce Lightning)
    ".slds-button.slds-button_brand.slds-float_right",
]

# selettori tipici dei campi login (Salesforce)
LOGIN_USER_SELS = ["#username", "input[name='username']", "input#user_email"]
LOGIN_PASS_SELS = ["#password", "input[name='password']", "input#user_password"]
LOGIN_SUBMIT_SELS = ["#Login", "input#Login", "button[type='submit']", "button:has-text('Accedi')", "input[value='Log In']"]


# -----------------------------------------------------------------------------
# Utilità
# -----------------------------------------------------------------------------
def _should_block(url: str) -> bool:
    u = url.lower()
    return any(d in u for d in BLOCKED_DOMAINS)


def _storage_state_path() -> Optional[str]:
    """
    Percorso del file storage_state.json impostato in ENV STORAGE_STATE,
    o '/app/storage_state.json' come default se esiste.
    """
    p = os.getenv("STORAGE_STATE") or "/app/storage_state.json"
    return p if os.path.exists(p) else None


async def _is_login_page(page) -> bool:
    url = (page.url or "").lower()
    if "/login" in url:
        return True
    # cerca campi password tipici
    with suppress(Exception):
        if await page.locator(",".join(LOGIN_PASS_SELS)).count() > 0:
            return True
    return False


async def _safe_goto(page, url: str, log: List[str], timeout_ms: int = 120_000):
    """
    Navigazione robusta: evita il blocco su 'networkidle' che su Lightning spesso non arriva.
    """
    log.append("Apro la pagina Curve di Carico…")
    page.set_default_timeout(60_000)
    page.context.set_default_timeout(60_000)
    page.set_default_navigation_timeout(60_000)
    page.context.set_default_navigation_timeout(60_000)

    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    # breve tentativo 'networkidle' (non bloccante)
    with suppress(PwTimeoutError):
        await page.wait_for_load_state("networkidle", timeout=3_000)


def _score_frame(f) -> int:
    """
    Heuristica per scegliere il frame giusto.
    """
    score = 0
    u = (f.url or "").lower()
    t = (f.name or "").lower()
    if "curvedicarico" in u:
        score += 3
    if "login" in u:
        score += 2
    if "widget" in u or "assistant" in u:
        score -= 5
    if t and "login" in t:
        score += 1
    return score


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
    """
    Prova a trovare/cliccare il pulsante di download CSV.
    Restituisce True se è riuscito a fare click su qualcosa.
    """
    for sel in CSV_SELECTORS:
        try:
            cnt = await frame.locator(sel).count()
            log.append(f"download selectors counts: {sel}:{cnt}")
            if cnt > 0:
                btn = frame.locator(sel).first
                # assicurati che sia visibile/cliccabile
                with suppress(Exception):
                    await btn.scroll_into_view_if_needed(timeout=2000)
                await btn.click(timeout=5000)
                return True
        except Exception as e:
            log.append(f"click selector '{sel}' errore: {type(e).__name__}: {e}")
    return False


async def _do_login_if_needed(page, username: Optional[str], password: Optional[str], log: List[str]) -> bool:
    """
    Se ci porta alla pagina di login e ho le credenziali, prova a loggarsi.
    Ritorna True se dopo il tentativo non siamo più in login.
    """
    if not await _is_login_page(page):
        return True

    if not username or not password:
        log.append("Sei in pagina di login ma non ho credenziali -> impossibile procedere.")
        return False

    log.append("Sono in pagina di login. Provo ad autenticarmi…")

    # compila utente
    filled = False
    for s in LOGIN_USER_SELS:
        try:
            if await page.locator(s).count():
                await page.locator(s).fill(username, timeout=5000)
                filled = True
                break
        except Exception:
            pass
    if not filled:
        log.append("Campo username non trovato in login.")
        return False

    # compila password
    filled = False
    for s in LOGIN_PASS_SELS:
        try:
            if await page.locator(s).count():
                await page.locator(s).fill(password, timeout=5000)
                filled = True
                break
        except Exception:
            pass
    if not filled:
        log.append("Campo password non trovato in login.")
        return False

    # submit
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

    # attende che esca dalla login
    with suppress(PwTimeoutError):
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)

    # se ancora login, ritenta una volta
    if await _is_login_page(page):
        log.append("Sembra che la login non sia andata a buon fine (captcha?).")
        return False

    log.append("Login eseguita.")
    return True


# -----------------------------------------------------------------------------
# Funzione esportata
# -----------------------------------------------------------------------------
async def refresh_and_download_csv_async(
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool = True,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Flusso:
      - apre browser Chromium
      - usa storage_state se richiesto
      - va alla pagina curve di carico (senza bloccare su networkidle)
      - se è login e ho credenziali, prova ad autenticarsi
      - sceglie il frame giusto
      - prova a cliccare il pulsante di download
      - ritorna esito + log
    """
    log: List[str] = []
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
                headless=True,  # su Render/Serverless è la scelta più stabile
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-features=site-per-process",
                ],
            )

            context_args: Dict[str, Any] = {
                "accept_downloads": True,
                "java_script_enabled": True,
            }
            if storage_path:
                context_args["storage_state"] = storage_path

            context = await browser.new_context(**context_args)

            # blocca domini rumorosi
            await context.route(
                "**/*",
                lambda route, request: (
                    route.abort() if _should_block(request.url) else route.continue_()
                ),
            )

            page = await context.new_page()

            # vai alla pagina
            try:
                await _safe_goto(page, CURVE_URL, log)
            except Exception as e:
                return {
                    "ok": False,
                    "detail": f"Errore in goto: {type(e).__name__}: {e}",
                    "log": log,
                }

            # se stai usando storage e sei in login -> sessione scaduta
            if use_storage and await _is_login_page(page):
                return {
                    "ok": False,
                    "detail": "Sessione salvata non valida/scaduta. Disattiva 'Usa sessione salvata' oppure rigenera lo storage_state.",
                    "log": log,
                }

            # se non uso storage, prova login se necessario
            if not use_storage:
                ok = await _do_login_if_needed(page, username, password, log)
                if not ok:
                    return {
                        "ok": False,
                        "detail": "Impossibile autenticarsi (mancano campi o captcha).",
                        "log": log,
                    }
                # dopo login torna alla pagina target (evita rimanere su home)
                with suppress(Exception):
                    await _safe_goto(page, CURVE_URL, log)

            # scegli il frame e prova clic
            frame = await _pick_main_frame(page, log)
            log.append("Provo click 'Download CSV' e aspetto il download…")

            # aspetto un eventuale download
            downloaded = False
            try:
                async with page.expect_download(timeout=20_000) as dl_info:
                    clicked = await _try_click_download(frame, log)
                    if not clicked:
                        return {
                            "ok": False,
                            "detail": "Pulsante download CSV non trovato (verifica selettori).",
                            "log": log,
                        }
                download = await dl_info.value
                # salvataggio temporaneo (opzionale)
                with suppress(Exception):
                    tmp = await download.path()
                    log.append(f"Download file: {download.suggested_filename}, path={tmp}")
                downloaded = True
            except PwTimeoutError:
                # potrebbe non attivare un vero oggetto Download (pop-up o XHR);
                # segnala comunque il click effettuato
                if await frame.locator("|".join(CSV_SELECTORS)).count() > 0:
                    log.append("Click eseguito, ma non ho intercettato un 'download' Playwright (possibile XHR).")
                    downloaded = True
                else:
                    downloaded = False
            except Exception as e:
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
        # errori generali (anche timeout goto)
        msg = str(e)
        if isinstance(e, PwTimeoutError) or "Timeout" in msg:
            return {
                "ok": False,
                "detail": f"Page.goto: Timeout 60000ms exceeded.\nCall log:\nnavigating to \"{CURVE_URL}\", waiting until \"networkidle\"\n",
                "log": log,
            }
        return {
            "ok": False,
            "detail": f"{type(e).__name__}: {e}",
            "log": log,
        }
