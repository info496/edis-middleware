# edis_pw.py
#
# Automazione e-Distribuzione con Playwright **async**.
# Espone: refresh_and_download_csv_async (usato da main.py) e la
# eccezione SessionMissingError.

from __future__ import annotations

import os
from typing import List, Optional, Dict, Any

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeout,
    Error as PWError,
)


class SessionMissingError(Exception):
    """Sollevata se serve una sessione salvata ma non è disponibile."""


def _log(msg: str, buf: Optional[List[str]]):
    if buf is not None:
        buf.append(msg)


async def refresh_and_download_csv_async(
    *,
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool,
    username: Optional[str] = None,
    password: Optional[str] = None,
    headless: bool = True,
    log: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Raggiunge la pagina 'Curve di carico', (se necessario) effettua login,
    imposta POD e date e scarica il CSV.

    Ritorna: {"ok": True, "csv": <string csv>}
    Lancia: RuntimeError se non trova il pulsante, SessionMissingError se
            è richiesta la sessione ma non c’è storage_state.
    """
    _log("=== refresh_and_download_csv: start ===", log)

    storage_state_path = os.environ.get("STORAGE_STATE", "/app/storage_state.json")
    if use_storage and not os.path.exists(storage_state_path):
        raise SessionMissingError(
            f"Storage state non trovato: {storage_state_path}. "
            "Esegui bootstrap login e salva la sessione."
        )

    start_url = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"

    # Argomenti consigliati in container
    launch_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-setuid-sandbox",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            storage_state=storage_state_path if use_storage else None,
            accept_downloads=True,
        )
        page = await context.new_page()

        try:
            _log("Apro la pagina Curve di Carico…", log)
            await page.goto(start_url, wait_until="networkidle", timeout=60_000)

            # Se non usiamo storage, prova login (solo se form presente)
            if not use_storage and username and password:
                # varianti classiche su Salesforce
                user_sel = "input[name='username'], input#username"
                pass_sel = "input[name='password'], input#password"
                login_btn = "button:has-text('Accedi'), button:has-text('Login')"

                if await page.locator(user_sel).count() > 0:
                    _log("Login form rilevato, effettuo login…", log)
                    await page.fill(user_sel, username)
                    await page.fill(pass_sel, password)
                    await page.click(login_btn)
                    await page.wait_for_load_state("networkidle")

            # Se la pagina è incorniciata, scegli il frame giusto
            target = page
            best_score = -1
            for f in page.frames:
                url = f.url or ""
                score = 0
                if "curvedicarico" in url:
                    score += 2
                if await f.locator("text=Curva di carico").count():
                    score += 1
                if score > best_score:
                    best_score = score
                    target = f
            if target is not page:
                _log(f"Frame scelto url='{target.url}' score={best_score}", log)

            # Compila POD se presente
            pod_sel = "input[name='pod'], input[placeholder*='POD']"
            if await target.locator(pod_sel).count() > 0:
                await target.fill(pod_sel, pod)
            else:
                _log("ATTENZIONE: campo POD non trovato (verifica selettori)", log)

            # Compila date (diverse varianti)
            def _sels(label_it: str):
                # piccola utility: generiamo alcuni selettori plausibili
                return [
                    f"input[placeholder*='{label_it}']",
                    f"input[aria-label*='{label_it}']",
                    f"input[name*='{ 'start' if label_it=='Inizio' else 'end' }']",
                ]

            for sel in _sels("Inizio"):
                if await target.locator(sel).count() > 0:
                    await target.fill(sel, date_from)
                    break
            else:
                _log("ATTENZIONE: campo data inizio non trovato (verifica selettori)", log)

            for sel in _sels("Fine"):
                if await target.locator(sel).count() > 0:
                    await target.fill(sel, date_to)
                    break
            else:
                _log("ATTENZIONE: campo data fine non trovato (verifica selettori)", log)

            # Click sul pulsante di download (proviamo più varianti)
            _log("Provo click 'Download CSV' e aspetto il download…", log)
            candidates = [
                # CTA più evidente osservata sul portale
                "button:has-text(\"Scarica il dettaglio del quarto d'ora\")",
                "button:has-text(\"Scarica il dettaglio del quarto d’ora\")",
                # fallback generici
                "button:has-text('Download CSV')",
                "a:has-text('Download CSV')",
                "button:has-text('Scarica CSV')",
                "a:has-text('Scarica CSV')",
                "a[download*='csv']",
            ]

            for sel in candidates:
                if await target.locator(sel).count() > 0:
                    try:
                        async with context.expect_download(timeout=30_000) as dl_info:
                            await target.click(sel)
                        download = await dl_info.value
                        content = await download.content()
                        csv_text = content.decode("utf-8", errors="ignore")
                        _log("Download completato.", log)
                        return {"ok": True, "csv": csv_text}
                    except PWTimeout:
                        # riprova con il prossimo selettore
                        pass
                    except PWError as e:
                        _log(f"Playwright error: {e}", log)
                        pass

            raise RuntimeError("Pulsante download CSV non trovato (verifica selettori)")

        finally:
            await context.close()
            await browser.close()


__all__ = ["refresh_and_download_csv_async", "SessionMissingError"]
