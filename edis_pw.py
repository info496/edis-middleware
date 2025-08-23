# edis_pw.py
import os
import re
from datetime import datetime
from typing import Optional
from playwright.async_api import async_playwright, Page, BrowserContext, Download

# Percorsi/variabili
STORAGE_STATE = os.getenv("STORAGE_STATE", "/app/storage_state.json")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/tmpdl")
CACHE_DIR = os.getenv("CACHE_DIR", "/app/cache")
HOME_URL = "https://private.e-distribuzione.it/PortaleClienti/s/"

# Alcuni possibili endpoint della pagina curve di carico
CURVES_URL_CANDIDATES = [
    "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico",
    "https://private.e-distribuzione.it/PortaleClienti/s/curve-di-carico",
    "https://private.e-distribuzione.it/PortaleClienti/s/curva-di-carico",
]


# ------------------------- utility -------------------------

def _it_date(d: str) -> str:
    """
    Converte 'YYYY-MM-DD' in 'DD/MM/YYYY'.
    Se è già in formato italiano, lo restituisce com'è.
    """
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return d


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ------------------------- browser/context -------------------------

async def _open_context(pw, use_storage: bool, log=print):
    _ensure_dir(DOWNLOAD_DIR)
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    if use_storage and os.path.exists(STORAGE_STATE):
        context = await browser.new_context(storage_state=STORAGE_STATE)
        log(f"[ctx] storage_state in uso: {STORAGE_STATE}")
    else:
        context = await browser.new_context()
        if use_storage:
            log(f"[ctx] storage_state NON trovato: {STORAGE_STATE}")

    context.set_default_timeout(45_000)
    page = await context.new_page()
    return browser, context, page


# ------------------------- navigazione -------------------------

async def _goto_curves(page: Page, log=print):
    # Prova direttamente gli URL candidati
    for url in CURVES_URL_CANDIDATES:
        try:
            await page.goto(url, wait_until="load")
            await page.wait_for_load_state("networkidle")
            if await page.get_by_text(re.compile(r"curve\s+di\s+carico", re.I)).count() > 0:
                log(f"[nav] pagina curve OK: {url}")
                return
        except Exception as e:
            log(f"[nav] {url}: {e}")

    # Fallback: vai in home e poi clicca i riquadri
    await page.goto(HOME_URL, wait_until="load")
    await page.wait_for_load_state("networkidle")

    # 'LE MIE MISURE'
    try:
        btn = page.get_by_text(re.compile(r"le\s+mie\s+misure", re.I))
        if await btn.count():
            await btn.first.click()
            await page.wait_for_load_state("networkidle")
    except Exception:
        pass

    # 'Curve di carico'
    link = page.get_by_text(re.compile(r"curve\s+di\s+carico", re.I))
    if await link.count() == 0:
        raise RuntimeError("Link 'Curve di carico' non trovato")
    await link.first.click()
    await page.wait_for_load_state("networkidle")


# ------------------------- periodo -------------------------

async def _fill_dates(page: Page, date_from: str, date_to: str, log=print):
    df = _it_date(date_from)
    dt = _it_date(date_to)

    candidates_from = [
        page.get_by_label(re.compile(r"inizio", re.I)),
        page.locator("input[placeholder*='Inizio' i]"),
    ]
    candidates_to = [
        page.get_by_label(re.compile(r"fine", re.I)),
        page.locator("input[placeholder*='Fine' i]"),
    ]

    async def set_input(loc_list, value, name):
        for loc in loc_list:
            try:
                if await loc.count():
                    inp = loc.first
                    await inp.click()
                    await inp.fill("")
                    await inp.type(value, delay=20)
                    return True
            except Exception as e:
                log(f"[date] {name} fallback err: {e}")
        return False

    ok_from = await set_input(candidates_from, df, "Inizio")
    ok_to = await set_input(candidates_to, dt, "Fine")

    if not (ok_from and ok_to):
        # Estremo fallback: prendi i primi due input che sembrano date
        inputs = page.locator("input[type='text'], input[type='date']")
        try:
            if await inputs.count() >= 2:
                await inputs.nth(0).fill(df)
                await inputs.nth(1).fill(dt)
                ok_from = ok_to = True
        except Exception:
            pass

    # Se esiste il pulsante "Modifica periodo" cliccalo
    try:
        btn = page.get_by_role("button", name=re.compile(r"modifica\s+periodo", re.I))
        if await btn.count():
            await btn.first.click()
    except Exception:
        pass

    log(f"[date] periodo impostato {df} → {dt}")


# ------------------------- CSV -------------------------

async def _click_download_csv(page: Page, log=print) -> Download:
    # il link è in fondo, assicuriamoci di vedere il footer
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(700)

    candidates = [
        page.get_by_text(re.compile(r"scarica.*quart.?orario.*csv", re.I)),
        page.get_by_role("link", name=re.compile(r"csv", re.I)),
        page.get_by_role("button", name=re.compile(r"csv", re.I)),
        page.locator("a[href$='.csv']"),
        page.locator("a:has-text('CSV'), button:has-text('CSV')"),
    ]

    for loc in candidates:
        try:
            if await loc.count() > 0:
                with page.expect_download() as dl_info:
                    await loc.first.click()
                return await dl_info.value
        except Exception as e:
            log(f"[csv] tentativo fallito: {e}")

    # Debug utili se non trova nulla
    await page.screenshot(path="/tmp/no_csv.png", full_page=True)
    html = await page.content()
    with open("/tmp/no_csv.html", "w", encoding="utf-8") as f:
        f.write(html)
    raise RuntimeError("Pulsante download CSV non trovato")


# ------------------------- API principale -------------------------

async def refresh_and_download_csv(
    username: Optional[str],
    password: Optional[str],
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool,
    log=print,
) -> str:
    """
    Ritorna il path del CSV salvato su disco.
    Se use_storage=True usa i cookie/sessione di STORAGE_STATE,
    altrimenti (login con credenziali) fallisce causa reCAPTCHA.
    """
    _ensure_dir(CACHE_DIR)
    save_path = os.path.join(CACHE_DIR, "edis_quartorario.csv")

    async with async_playwright() as pw:
        browser, context, page = await _open_context(pw, use_storage, log)

        try:
            if not use_storage:
                # Qui si potrebbe provare un login "manuale", ma è bloccato da reCAPTCHA.
                raise RuntimeError(
                    "Login con credenziali bloccato dal reCAPTCHA. "
                    "Usa la sessione salvata (use_storage=True)."
                )

            await _goto_curves(page, log)

            # Se il campo POD è editabile, prova a impostarlo (sono casi rari)
            try:
                pod_inputs = page.locator("input[value^='IT']")
                if await pod_inputs.count():
                    await pod_inputs.first.fill(pod)
            except Exception:
                pass

            await _fill_dates(page, date_from, date_to, log)

            dl = await _click_download_csv(page, log)
            await dl.save_as(save_path)
            log(f"[csv] salvato in {save_path}")

            return save_path

        finally:
            await context.close()
            await browser.close()
