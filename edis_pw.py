# edis_pw.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Callable, Union

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
)

# -----------------------------------------------------------------------------
# Config da env
# -----------------------------------------------------------------------------

# Timeout di default per le azioni Playwright (ms)
try:
    PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "90000"))
except ValueError:
    PW_TIMEOUT_MS = 90000

# Percorso storage_state per l'uso della sessione salvata
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")

# Directory temporanea per i download
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/edis_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# URL del portale (verifica/aggiorna se necessario)
BASE_URL = "https://private.e-distribuzione.it/PortaleClienti/s"
CURVE_URL = f"{BASE_URL}/curvedicarico"  # TODO: verifica che il path sia questo


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _mklog(log: Optional[Union[Callable[[str], None], list]]):
    """
    Normalizza il parametro log:
      - se è una funzione, la usa
      - se è una lista, aggiunge i messaggi alla lista
      - se è None, crea un logger no-op
    """
    if callable(log):
        return log
    if isinstance(log, list):
        def _f(msg: str):
            try:
                log.append(str(msg))
            except Exception:
                pass
        return _f

    def _noop(msg: str):
        return
    return _noop


def _to_portal_date(yyyy_mm_dd: str) -> str:
    """
    Converte 'YYYY-MM-DD' -> 'DD/MM/YYYY'
    """
    parts = yyyy_mm_dd.split("-")
    if len(parts) == 3:
        y, m, d = parts
        return f"{d}/{m}/{y}"
    return yyyy_mm_dd


async def _fill_if_present(page, selector: str, value: str, log):
    """
    Riempie un campo se presente; ignora in caso di mancanza.
    """
    try:
        loc = page.locator(selector)
        if await loc.count() > 0:
            await loc.click()
            # clear robusto
            try:
                await loc.fill("")
            except Exception:
                await loc.press("Control+A")
                await loc.press("Delete")
            await loc.type(value, delay=20)
            log(f"fill {selector} = {value}")
            return True
    except Exception as e:
        log(f"_fill_if_present({selector}) -> {e}")
    return False


async def _click_if_present(page, selector: str, log, timeout: int = None) -> bool:
    """
    Clicca un elemento se presente/visibile; ignora in caso di mancanza.
    """
    try:
        loc = page.locator(selector)
        if await loc.count() > 0:
            await loc.first().click(timeout=timeout or PW_TIMEOUT_MS)
            log(f"click {selector}")
            return True
    except Exception as e:
        log(f"_click_if_present({selector}) -> {e}")
    return False


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
    Naviga il portale e-distribuzione, imposta query (POD + date),
    e scarica il CSV delle curve di carico.

    Ritorna il percorso assoluto del CSV salvato.

    NOTE:
    - Se `use_storage=True`, prova ad usare STORAGE_STATE già salvato (captcha bypass).
    - Se `use_storage=False`, effettua login con username/password (se forniti) e salva lo storage state.
    - Gli URL e i selettori vanno verificati sul markup corrente del portale.
    """
    log = _mklog(log)
    log("=== refresh_and_download_csv: start ===")

    # Validazioni minime
    if use_storage and not Path(STORAGE_STATE).exists():
        raise RuntimeError("Sessione salvata richiesta ma storage_state non trovato")

    if not use_storage and (not username or not password):
        raise RuntimeError("Username/password mancanti e use_storage=False")

    # Conversione date per il portale
    df_portal = _to_portal_date(date_from)
    dt_portal = _to_portal_date(date_to)

    # Directory download (Playwright salverà i file in memoria; noi li salviamo sul path scelto)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Avvio Playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"]
        )
        context_kwargs = {
            "accept_downloads": True,
        }
        if use_storage and Path(STORAGE_STATE).exists():
            context_kwargs["storage_state"] = STORAGE_STATE

        context = await browser.new_context(**context_kwargs)
        context.set_default_timeout(PW_TIMEOUT_MS)

        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)

        try:
            # 1) Login (solo se non uso sessione salvata)
            if not use_storage:
                log("Navigo al login…")
                await page.goto(BASE_URL, wait_until="domcontentloaded")
                # TODO: verifica selettori
                # Proviamo qualche variante comune per username/password + bottone
                filled_user = await _fill_if_present(page, "input[name='username']", username, log)
                if not filled_user:
                    filled_user = await _fill_if_present(page, "#username", username, log)

                filled_pass = await _fill_if_present(page, "input[name='password']", password, log)
                if not filled_pass:
                    filled_pass = await _fill_if_present(page, "#password", password, log)

                clicked = (
                    await _click_if_present(page, "button:has-text('Accedi')", log)
                    or await _click_if_present(page, "button:has-text('Login')", log)
                    or await _click_if_present(page, "input[type='submit']", log)
                )
                if not clicked:
                    log("Bottone login non trovato; provo invio su password")
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass

                # attendo che la home si carichi (heauristica)
                try:
                    await page.wait_for_url("**/PortaleClienti/**", timeout=PW_TIMEOUT_MS)
                except PWTimeoutError:
                    log("warning: timeout in attesa della home post-login")

                # salvo storage state (per usi futuri)
                try:
                    await context.storage_state(path=STORAGE_STATE)
                    log(f"Storage state salvato: {STORAGE_STATE}")
                except Exception as e:
                    log(f"salvataggio storage state fallito: {e}")

            # 2) Navigo alla pagina “Curve di Carico”
            log("Apro la pagina Curve di Carico…")
            await page.goto(CURVE_URL, wait_until="domcontentloaded")

            # 3) Compilo i campi POD e date (più selettori possibili)
            #    NOTE: i campi data spesso sono DD/MM/YYYY
            #    TODO: verifica i selettori correnti del portale
            filled_pod = (
                await _fill_if_present(page, "input[name='pod']", pod, log)
                or await _fill_if_present(page, "#pod", pod, log)
                or await _fill_if_present(page, "input[placeholder*='POD']", pod, log)
            )
            if not filled_pod:
                log("ATTENZIONE: campo POD non trovato (verifica selettori)")

            filled_from = (
                await _fill_if_present(page, "input[name*='date_from']", df_portal, log)
                or await _fill_if_present(page, "#dateFrom", df_portal, log)
                or await _fill_if_present(page, "input[placeholder*='Inizio']", df_portal, log)
            )
            if not filled_from:
                log("ATTENZIONE: campo data inizio non trovato (verifica selettori)")

            filled_to = (
                await _fill_if_present(page, "input[name*='date_to']", dt_portal, log)
                or await _fill_if_present(page, "#dateTo", dt_portal, log)
                or await _fill_if_present(page, "input[placeholder*='Fine']", dt_portal, log)
            )
            if not filled_to:
                log("ATTENZIONE: campo data fine non trovato (verifica selettori)")

            # 4) Avvio ricerca (se serve) / poi click su “Download CSV”
            #    Spesso il portale richiede una “ricerca” prima di poter scaricare
            _ = (
                await _click_if_present(page, "button:has-text('Cerca')", log)
                or await _click_if_present(page, "button:has-text('Ricerca')", log)
                or await _click_if_present(page, "button:has-text('Aggiorna')", log)
            )

            # attendo eventuale caricamento risultati
            try:
                await page.wait_for_timeout(1500)
            except Exception:
                pass

            # 5) Download CSV – intercetto l’evento di download
            log("Provo click 'Download CSV' e aspetto il download…")
            # selettori possibili
            download_selectors = [
                "a:has-text('Download CSV')",
                "button:has-text('Download CSV')",
                "a:has-text('Scarica CSV')",
                "button:has-text('Scarica CSV')",
                "#downloadCsv",
                "[data-testid='download-csv']",
            ]

            download = None
            for sel in download_selectors:
                if await page.locator(sel).count() > 0:
                    try:
                        async with page.expect_download(timeout=PW_TIMEOUT_MS) as dl_info:
                            await page.locator(sel).first().click()
                        download = await dl_info.value
                        break
                    except PWTimeoutError:
                        log(f"Timeout download con selettore {sel}, riprovo…")
                    except Exception as e:
                        log(f"Errore click/download {sel}: {e}")

            if not download:
                raise RuntimeError("Pulsante download CSV non trovato (verifica selettori)")

            suggested_name = download.suggested_filename or "curva_carico.csv"
            out_path = DOWNLOAD_DIR / suggested_name
            # se esiste già, rigenero un nome
            i = 1
            while out_path.exists():
                stem = Path(suggested_name).stem
                suf = Path(suggested_name).suffix
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
