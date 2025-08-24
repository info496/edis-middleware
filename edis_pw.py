# edis_pw.py
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Callable, Union, Iterable

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# -----------------------------------------------------------------------------
# Config da env
# -----------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

PW_TIMEOUT_MS = _env_int("PW_TIMEOUT_MS", 90000)
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/edis_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://private.e-distribuzione.it/PortaleClienti/s"
# In genere “curvedicarico” o simile. Se cambia, basta aggiornare questa costante.
CURVE_URL = f"{BASE_URL}/curvedicarico"

# -----------------------------------------------------------------------------
# Log helper (accetta callable, lista o None)
# -----------------------------------------------------------------------------

def _mklog(log: Optional[Union[Callable[[str], None], list]]):
    if callable(log):
        return log
    if isinstance(log, list):
        def _f(msg: str):
            try:
                log.append(str(msg))
            except Exception:
                pass
        return _f
    def _noop(_msg: str):
        return
    return _noop

# -----------------------------------------------------------------------------
# Util
# -----------------------------------------------------------------------------

def _to_portal_date(yyyy_mm_dd: str) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY (accettata normalmente dal portale)"""
    parts = yyyy_mm_dd.split("-")
    if len(parts) == 3:
        y, m, d = parts
        return f"{d}/{m}/{y}"
    return yyyy_mm_dd

async def _fill(locator, value: str):
    await locator.click()
    try:
        await locator.fill("")
    except Exception:
        await locator.press("Control+A")
        await locator.press("Delete")
    await locator.type(value, delay=20)

async def _first_count(frame, selector: str) -> int:
    try:
        return await frame.locator(selector).count()
    except Exception:
        return 0

async def _click_if_any(frame, selectors: Iterable[str], timeout: int) -> bool:
    for sel in selectors:
        try:
            loc = frame.locator(sel)
            if await loc.count() > 0:
                await loc.first().click(timeout=timeout)
                return True
        except Exception:
            pass
    return False

async def _fill_first_available(frame, value: str, selectors: Iterable[str]) -> bool:
    for sel in selectors:
        loc = frame.locator(sel)
        try:
            if await loc.count() > 0:
                await _fill(loc.first(), value)
                return True
        except Exception:
            pass
    return False

# -----------------------------------------------------------------------------
# Frame discovery: trova il frame che contiene i controlli di POD/CSV
# -----------------------------------------------------------------------------

async def _pick_work_frame(page, log) -> "Frame":
    await page.wait_for_load_state("domcontentloaded")
    frames = page.frames
    log(f"Frame totali: {len(frames)}")

    # pattern testuali tipici
    pat_pod = re.compile(r"\bPOD\b|\bCodice\s*POD\b", re.I)
    pat_csv = re.compile(r"Download\s*CSV|Scarica\s*CSV", re.I)

    best = None
    best_score = -1

    for fr in frames:
        score = 0
        try:
            # prova: match su inner text (veloce ma non sempre permesso)
            txt = (await fr.title()) or ""
            url = fr.url or ""
            # punteggio base se l'URL fa pensare alla pagina corretta
            if "curvedicarico" in url.lower():
                score += 2

            # euristiche: conta occorrenze di scritte POD/CSV
            c1 = await fr.locator(f"text=/{pat_pod.pattern}/i").count()
            c2 = await fr.locator(f"text=/{pat_csv.pattern}/i").count()
            score += c1 + c2

            # se non ha nulla, prova a cercare input tipici
            if score == 0:
                c3 = await fr.locator("input[name*='pod'], #pod, input[placeholder*='POD']").count()
                c4 = await fr.locator("button:has-text('CSV'), a:has-text('CSV')").count()
                score += c3 + c4

            log(f"- Frame url={url!r} title={txt!r} -> score={score} (POD={c1 if 'c1' in locals() else '?'} CSV={c2 if 'c2' in locals() else '?'})")
        except Exception as e:
            log(f"- Frame error: {e}")

        if score > best_score:
            best_score = score
            best = fr

    if best is None:
        # fallback al main frame, comunque
        log("Nessun frame 'migliore' trovato; uso page.main_frame")
        return page.main_frame

    log(f"Frame scelto url={best.url!r} score={best_score}")
    return best

# -----------------------------------------------------------------------------
# Flusso principale
# -----------------------------------------------------------------------------

async def refresh_and_download_csv(
    *,
    username: Optional[str],
    password: Optional[str],
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool,
    log: Optional[Union[Callable[[str], None], list]] = None,
) -> str:
    """
    Scarica il CSV da e-Distribuzione. Ritorna il path assoluto del file scaricato.
    """
    log = _mklog(log)
    log("=== refresh_and_download_csv: start ===")

    if use_storage and not Path(STORAGE_STATE).exists():
        raise RuntimeError("Sessione salvata richiesta ma storage_state non trovato")
    if not use_storage and (not username or not password):
        raise RuntimeError("Username/password mancanti e use_storage=False")

    df = _to_portal_date(date_from)
    dt = _to_portal_date(date_to)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx_kwargs = {"accept_downloads": True}
        if use_storage and Path(STORAGE_STATE).exists():
            ctx_kwargs["storage_state"] = STORAGE_STATE

        context = await browser.new_context(**ctx_kwargs)
        context.set_default_timeout(PW_TIMEOUT_MS)
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)

        try:
            # Login solo se non uso storage
            if not use_storage:
                log("Navigo al login…")
                await page.goto(BASE_URL, wait_until="domcontentloaded")

                # username/password
                user_selectors = ["input[name='username']", "#username", "input[placeholder*='mail']"]
                pass_selectors = ["input[name='password']", "#password", "input[type='password']"]

                await _fill_first_available(page, username, user_selectors)
                await _fill_first_available(page, password, pass_selectors)

                # bottone
                await _click_if_any(
                    page,
                    [
                        "button:has-text('Accedi')",
                        "button:has-text('Login')",
                        "input[type='submit']",
                    ],
                    timeout=PW_TIMEOUT_MS,
                )
                try:
                    await page.wait_for_url("**/PortaleClienti/**", timeout=PW_TIMEOUT_MS)
                except PWTimeoutError:
                    pass

                try:
                    await context.storage_state(path=STORAGE_STATE)
                    log(f"Storage state salvato in {STORAGE_STATE}")
                except Exception as e:
                    log(f"Salvataggio storage state fallito: {e}")

            # Pagina curve
            log("Apro la pagina Curve di Carico…")
            await page.goto(CURVE_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Trova frame “utile”
            work = await _pick_work_frame(page, log)

            # --- Compila POD ---
            filled_pod = await _fill_first_available(
                work,
                pod,
                [
                    "input[name*='pod']",
                    "#pod",
                    "input[placeholder*='POD']",
                    "input[aria-label*='POD']",
                    "input:below(:text('POD'))",
                ],
            )
            log(f"POD filled={filled_pod}")

            # --- Data inizio ---
            filled_from = await _fill_first_available(
                work,
                df,
                [
                    "input[name*='from']",
                    "#dateFrom",
                    "input[placeholder*='Inizio']",
                    "input[aria-label*='Inizio']",
                    "input:below(:text('Inizio'))",
                    "input:below(:text('Dal'))",
                ],
            )
            log(f"date_from filled={filled_from}")

            # --- Data fine ---
            filled_to = await _fill_first_available(
                work,
                dt,
                [
                    "input[name*='to']",
                    "#dateTo",
                    "input[placeholder*='Fine']",
                    "input[aria-label*='Fine']",
                    "input:below(:text('Fine'))",
                    "input:below(:text('Al'))",
                ],
            )
            log(f"date_to filled={filled_to}")

            # Possibile bottone “Cerca/Aggiorna”
            _ = await _click_if_any(
                work,
                [
                    "button:has-text('Cerca')",
                    "button:has-text('Ricerca')",
                    "button:has-text('Aggiorna')",
                    "button:has-text('Calcola')",
                    "button:has-text('Visualizza')",
                ],
                timeout=PW_TIMEOUT_MS,
            )
            try:
                await work.wait_for_timeout(1200)
            except Exception:
                pass

            # Download CSV
            # Usa get_by_role/text dove possibile
            download_try = [
                "button:has-text('Download CSV')",
                "a:has-text('Download CSV')",
                "button:has-text('Scarica CSV')",
                "a:has-text('Scarica CSV')",
                "[data-testid='download-csv']",
                "#downloadCsv",
            ]
            # prova anche ARIA role name “CSV”
            try:
                if await work.get_by_role("button", name=re.compile("CSV", re.I)).count() > 0:
                    download_try.insert(0, "role=button[name=/CSV/i]")
            except Exception:
                pass

            # log quante corrispondenze vediamo per debug
            counts = []
            for sel in download_try:
                try:
                    counts.append((sel, await work.locator(sel).count()))
                except Exception:
                    counts.append((sel, 0))
            log("download selectors counts: " + ", ".join([f"{s}:{c}" for s, c in counts]))

            download = None
            for sel in download_try:
                if await work.locator(sel).count() > 0:
                    try:
                        async with work.expect_download(timeout=PW_TIMEOUT_MS) as dl_info:
                            await work.locator(sel).first().click()
                        download = await dl_info.value
                        break
                    except PWTimeoutError:
                        log(f"Timeout download con {sel}, riprovo…")
                    except Exception as e:
                        log(f"Errore download {sel}: {e}")

            if not download:
                raise RuntimeError("Pulsante download CSV non trovato (verifica selettori)")

            suggested = download.suggested_filename or "curva_carico.csv"
            out_path = DOWNLOAD_DIR / suggested
            i = 1
            while out_path.exists():
                stem, suf = Path(suggested).stem, Path(suggested).suffix
                out_path = DOWNLOAD_DIR / f"{stem}_{i}{suf}"
                i += 1

            await download.save_as(out_path)
            log(f"CSV salvato in: {out_path}")
            return str(out_path)

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            log("=== refresh_and_download_csv: end ===")
