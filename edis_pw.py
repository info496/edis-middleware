import os
import re
import json
import asyncio
from pathlib import Path
from typing import Optional, Tuple, List

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


class SessionMissingError(RuntimeError):
    pass


CURVES_URL = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"
LOGIN_URL  = "https://private.e-distribuzione.it/PortaleClienti/s/login/?startURL=%2FPortaleClienti%2Fs%2Fcurvedicarico"

# Selettori “elastici” (testo può cambiare leggermente)
CSV_BTN_REGEX = re.compile(r"scarica.*quarto", re.I)

# Tentativi & timeout (puoi alzarli se la pagina è lenta)
NAV_TIMEOUT   = 45_000
CLICK_TIMEOUT = 20_000
DL_TIMEOUT    = 60_000


async def _new_context(pw, use_storage: bool, headless: bool = True):
    storage_state_path = os.getenv("STORAGE_STATE", "/app/storage_state.json")
    if use_storage:
        if not Path(storage_state_path).exists():
            raise SessionMissingError(
                "Sessione salvata non trovata. Esegui il bootstrap da locale e carica lo storage_state.json."
            )
        browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context(storage_state=storage_state_path, accept_downloads=True)
        return browser, context

    # senza sessione salvata
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox"])
    context = await browser.new_context(accept_downloads=True)
    return browser, context


async def _maybe_login(context, username: Optional[str], password: Optional[str], log: List[str]):
    """
    Tenta il login se compaiono i campi nella pagina di login.
    """
    page = await context.new_page()
    await page.goto(LOGIN_URL, wait_until="load", timeout=NAV_TIMEOUT)

    # Se i campi non ci sono vuol dire che siamo già loggati (sessione valida)
    user_sel = "input[name='username'], input[type=email]"
    pass_sel = "input[name='password'], input[type=password]"

    try:
        user_el = page.locator(user_sel)
        pass_el = page.locator(pass_sel)
        btn_el  = page.get_by_role("button", name=re.compile("accedi|login|entra", re.I))

        # Se non ci sono i campi, passo oltre
        if await user_el.count() == 0 or await pass_el.count() == 0:
            await page.close()
            return

        if not username or not password:
            await page.close()
            raise SessionMissingError("Sessione scaduta e credenziali mancanti.")

        await user_el.first.fill(username)
        await pass_el.first.fill(password)
        if await btn_el.count() > 0:
            await btn_el.first.click(timeout=CLICK_TIMEOUT)
        else:
            # fallback generico
            await page.keyboard.press("Enter")

        # attendo redirect alla pagina delle curve
        await page.wait_for_url(re.compile(r"/curvedicarico"), timeout=NAV_TIMEOUT)
        await asyncio.sleep(1)
    finally:
        await page.close()


async def _open_curves_page(context):
    page = await context.new_page()
    await page.goto(CURVES_URL, wait_until="load", timeout=NAV_TIMEOUT)
    return page


async def _set_filters_if_present(page, pod: str, date_from: str, date_to: str, log: List[str]):
    """
    Prova ad impostare POD e date se i campi sono presenti.
    In caso contrario prosegue comunque (spesso non serve).
    """
    async def _fill(sel_list: list[str], value: str, label: str):
        for sel in sel_list:
            loc = page.locator(sel)
            if await loc.count() > 0:
                try:
                    await loc.first.fill(value, timeout=CLICK_TIMEOUT)
                    log.append(f"{label} impostato.")
                    return True
                except Exception:
                    pass
        log.append(f"ATTENZIONE: campo {label} non trovato (verifica selettori)")
        return False

    # Tenta POD
    if pod:
        await _fill(
            [
                "input[name='pod']",
                "input[placeholder*='POD']",
                "input[id*='pod']",
            ],
            pod, "POD"
        )

    # Tenta date
    if date_from:
        await _fill(
            [
                "input[type='date']#date_from",
                "input[name='date_from']",
                "input[placeholder*='Inizio']",
            ],
            date_from, "data inizio"
        )
    if date_to:
        await _fill(
            [
                "input[type='date']#date_to",
                "input[name='date_to']",
                "input[placeholder*='Fine']",
            ],
            date_to, "data fine"
        )


async def _click_download_csv(page, log: List[str]) -> Tuple[str, list]:
    """
    Click su “Scarica il dettaglio del quarto d’ora” e ritorna CSV come testo e
    un parsing basilare (rows).
    """
    # Prova vari modi: role button per accessibilità + fallback testuale
    btn = page.get_by_role("button", name=CSV_BTN_REGEX)
    if await btn.count() == 0:
        # fallback: query generica per testo
        btn = page.locator("button, a").filter(has_text=CSV_BTN_REGEX)

    if await btn.count() == 0:
        raise RuntimeError("Pulsante download CSV non trovato (verifica selettori)")

    async with page.expect_download(timeout=DL_TIMEOUT) as dl_info:
        await btn.first.click(timeout=CLICK_TIMEOUT)
    download = await dl_info.value
    csv_bytes = await download.content()
    csv_text = csv_bytes.decode("utf-8", errors="replace")

    # parse leggero -> rows: [{timestamp,kwh,quality}]
    rows = []
    try:
        lines = [l for l in csv_text.splitlines() if l.strip()]
        # salta header se presente
        body = lines[1:] if "timestamp" in lines[0].lower() or "kwh" in lines[0].lower() else lines
        for line in body:
            cols = line.split(";") if ";" in line else line.split(",")
            if len(cols) >= 2:
                rows.append({
                    "timestamp": cols[0].strip(),
                    "kwh": cols[1].strip(),
                    "quality": cols[2].strip() if len(cols) > 2 else ""
                })
    except Exception:
        pass

    return csv_text, rows


async def refresh_and_download_csv_async(
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Tuple[str, list, List[str]]:
    """
    Funzione principale completamente **async**.
    Ritorna: (csv_text, rows, log)
    """
    log: List[str] = []
    log.append("=== refresh_and_download_csv: start ===")

    # NB: usare SEMPRE Async API
    async with async_playwright() as pw:
        browser, context = await _new_context(pw, use_storage=use_storage, headless=True)

        try:
            # login se serve (solo quando non usi storage/salvata)
            if not use_storage:
                await _maybe_login(context, username, password, log)

            # Apri pagina curve
            page = await _open_curves_page(context)

            # Prova ad impostare filtri (best effort)
            await _set_filters_if_present(page, pod, date_from, date_to, log)

            # Esegui Download
            csv_text, rows = await _click_download_csv(page, log)
            log.append("Download CSV completato.")

            # Se hai appena fatto login, aggiorna la sessione salvata
            if not use_storage:
                storage_state_path = os.getenv("STORAGE_STATE", "/app/storage_state.json")
                # salva storage per i prossimi run
                try:
                    await context.storage_state(path=storage_state_path)
                    log.append(f"Sessione salvata in {storage_state_path}")
                except Exception as e:
                    log.append(f"Salvataggio sessione fallito: {e}")

            return csv_text, rows, log

        finally:
            await context.close()
            await browser.close()
            log.append("=== refresh_and_download_csv: end ===")
