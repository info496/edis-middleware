# edis_pw.py
from typing import Any, Dict, List
from datetime import date
from playwright.async_api import async_playwright

# ------------------------------------------------------------------
# Questo file espone SOLO due funzioni di alto livello:
#   - refresh_with_login(...)      -> login con user/pass (se vuoi mantenerlo)
#   - refresh_with_session(...)    -> usa storage_state (Niente CAPTCHA)
#
# In entrambi i casi devi fare:
#   1) aprire pagina "Curve di carico / Le mie misure"
#   2) impostare intervallo date_from/date_to
#   3) cliccare "Scarica il dettaglio quartorario .csv"
#   4) convertire CSV -> lista di dict: [{"ts": "...", "kWh": float, "quality": "..."}]
#   5) restituire la lista (o un dict con chiave 'rows' / 'data')
# ------------------------------------------------------------------

# ------------------ PUNTO DI INTEGRAZIONE -------------------------
async def _do_scrape(page, pod: str, date_from: date, date_to: date, timeout_ms: int) -> List[Dict[str, Any]]:
    """
    TODO: inserisci qui la TUA logica di scraping che:
      - naviga alla pagina 'Curve di carico'
      - seleziona pod/date
      - scarica il CSV
      - lo trasforma in una lista di righe: [{"ts": "...", "kWh": 0.123, "quality": "..."}, ...]

    DEVE restituire una LISTA (anche vuota se non trovi dati).
    """
    # Se non hai ancora implementato lo scraping, ritorna lista vuota (così il server non va in errore).
    return []


# ------------------ API: LOGIN CLASSICO ---------------------------
async def refresh_with_login(
    username: str,
    password: str,
    pod: str,
    date_from: date,
    date_to: date,
    timeout_ms: int = 90000,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # TODO: la tua sequenza di login (ATTENZIONE: qui potresti incappare nel CAPTCHA)
        # await page.goto("https://private.e-distribuzione.it/PortaleClienti/s")
        # ... fai login ...
        # Dopo il login:
        rows = await _do_scrape(page, pod, date_from, date_to, timeout_ms)

        await browser.close()
        return rows


# ------------------ API: SESSIONE SALVATA -------------------------
async def refresh_with_session(
    storage_state_path: str,
    pod: str,
    date_from: date,
    date_to: date,
    timeout_ms: int = 90000,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    """
    Usa la sessione salvata (storage_state.json) – niente CAPTCHA e niente form di login.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(storage_state=storage_state_path)
        page = await ctx.new_page()

        # Vai diretto alla pagina del portale e fai lo scraping
        # await page.goto("https://private.e-distribuzione.it/PortaleClienti/s")
        rows = await _do_scrape(page, pod, date_from, date_to, timeout_ms)

        await browser.close()
        return rows
