from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import tempfile
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout


# =====================================================================
# CONFIG DI BASE
# =====================================================================
BASE_URL = "https://private.e-distribuzione.it/PortaleClienti/s"
CURVE_URL = f"{BASE_URL}/curvedicarico"        # nel portale risulta "curvedicarico"
NAV_TIMEOUT = 45000                             # ms (navigazione)
STEP_TIMEOUT = 20000                            # ms (operazioni singole)
DOWNLOAD_TIMEOUT = 60000                        # ms


# =====================================================================
# FUNZIONI DI SUPPORTO
# =====================================================================

async def _goto_curve_page(page: Page) -> None:
    """Apre la pagina 'Curve di carico' e attende che sia pronta."""
    # prova direttamente l'url "curvedicarico"
    await page.goto(CURVE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")

    # fallback: se non carica la sezione, prova l'homepage e poi "LE MIE MISURE"
    if not await _is_curve_page(page):
        await page.goto(BASE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        # Apri la tile "LE MIE MISURE" (testo presente nello screenshot)
        try:
            tile = page.get_by_text("LE MIE MISURE", exact=False)
            await tile.first.click(timeout=STEP_TIMEOUT)
        except Exception:
            pass

        # riprova a raggiungere "curvedicarico"
        if not await _is_curve_page(page):
            await page.goto(CURVE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")

    # attende presenza di qualcosa che identifichi la pagina
    await _wait_curve_ready(page)


async def _is_curve_page(page: Page) -> bool:
    txts = ["Curve di carico", "Curva di carico", "curvedicarico"]
    page_text = (await page.content()).lower()
    return any(t.lower() in page_text for t in txts)


async def _wait_curve_ready(page: Page) -> None:
    # Qualche etichetta tipica visibile in pagina
    candidates = [
        "Curve di carico",
        "Curva di carico",
        "Periodo di riferimento",
        "Modifica periodo",
        "Scarica il dettaglio quartorario",
    ]
    for _ in range(3):
        html = (await page.content()).lower()
        if any(c.lower() in html for c in candidates):
            return
        await asyncio.sleep(0.8)
    # Se non ha trovato nulla, lascia comunque proseguire (magari i componenti sono shadow)
    return


async def _select_period(page: Page, date_from: date, date_to: date) -> None:
    """
    Imposta il periodo Inizio/Fine. Il portale usa Salesforce Lightning/LWC,
    quindi possono esserci <select> o componenti custom. Tenta varie strategie.
    """

    # 1) Se c'è il bottone "Modifica periodo", cliccalo
    try:
        await page.get_by_role("button", name=re.compile("Modifica periodo", re.I)).click(timeout=STEP_TIMEOUT)
    except Exception:
        pass

    # Prova una strategia con select "Inizio" (Mese/Anno) e "Fine"
    # La pagina nello screenshot mostra due gruppi con label "Inizio" e "Fine".
    strategies = [_try_set_period_by_selects, _try_set_period_by_inputs]

    for strat in strategies:
        try:
            ok = await strat(page, date_from, date_to)
            if ok:
                return
        except Exception:
            pass

    # Non è critico se non riesce ad impostare: molti portali mantengono il default; in caso,
    # il click al pulsante di download spesso usa l’intervallo già impostato (o ultimo usato).
    return


async def _try_set_period_by_selects(page: Page, dfrom: date, dto: date) -> bool:
    """
    Cerca gruppi 'Inizio'/'Fine' con due <select> per Mese e Anno.
    """
    # helper
    def month_it(m: int) -> str:
        # nomi mesi possibili (alcuni portali usano testo)
        mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
        return mesi[m - 1]

    async def _set(label_text: str, dt: date) -> None:
        # Cerca contenitore vicino alla label
        root = page.get_by_text(re.compile(rf"^{label_text}\s*$", re.I)).locator("xpath=..")
        # due select: Mese e Anno
        selects = root.locator("select")
        count = await selects.count()
        if count >= 2:
            # prova prima con testo (mese) poi anno
            try:
                await selects.nth(0).select_option(label=month_it(dt.month))
            except Exception:
                # fallback: value numerico mese
                await selects.nth(0).select_option(value=str(dt.month))
            await selects.nth(1).select_option(value=str(dt.year))
        else:
            # prova a trovare select discendenti
            selects = root.locator("select")
            c2 = await selects.count()
            if c2 >= 2:
                try:
                    await selects.nth(0).select_option(label=month_it(dt.month))
                except Exception:
                    await selects.nth(0).select_option(value=str(dt.month))
                await selects.nth(1).select_option(value=str(dt.year))
            else:
                raise RuntimeError("selects not found")

    try:
        await _set("Inizio", dfrom)
        await _set("Fine", dto)
        return True
    except Exception:
        return False


async def _try_set_period_by_inputs(page: Page, dfrom: date, dto: date) -> bool:
    """
    Alcuni portali usano input 'YYYY-MM-DD' con icona calendario.
    """
    # prova a trovare 2 input con tipo date
    inputs = page.locator("input[type=date]")
    count = await inputs.count()
    if count >= 2:
        await inputs.nth(0).fill(dfrom.isoformat())
        await inputs.nth(1).fill(dto.isoformat())
        return True

    # fallback: 2 input testuali vicino a label "Inizio"/"Fine"
    async def _fill_near(label_text: str, val: str) -> bool:
        try:
            lab = page.get_by_text(re.compile(rf"^{label_text}\s*$", re.I))
            root = lab.locator("xpath=..")
            inp = root.locator("input")
            if await inp.count() == 0:
                inp = root.locator("xpath=.//input")
            await inp.first.fill(val, timeout=STEP_TIMEOUT)
            return True
        except Exception:
            return False

    ok1 = await _fill_near("Inizio", dfrom.isoformat())
    ok2 = await _fill_near("Fine", dto.isoformat())
    return ok1 and ok2


async def _click_download_csv(page: Page) -> str:
    """
    Clicca il pulsante 'Scarica il dettaglio quartorario .csv' e restituisce
    il percorso del file scaricato.
    """
    # Possibili label del bottone (in minuscolo per match case-insensitive)
    labels = [
        "Scarica il dettaglio quartorario .csv",
        "Scarica il dettaglio quartorario",
        "Download CSV",
        "Scarica csv",
    ]

    # Trova e clicca
    for name in labels:
        locs = [
            page.get_by_role("button", name=re.compile(name, re.I)),
            page.get_by_text(re.compile(name, re.I)),
        ]
        for loc in locs:
            try:
                async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as dl:
                    await loc.first.click(timeout=STEP_TIMEOUT)
                download = await dl.value
                tmp = os.path.join(tempfile.gettempdir(), f"edis_{int(time.time())}.csv")
                await download.save_as(tmp)
                return tmp
            except PWTimeout:
                continue
            except Exception:
                continue

    raise RuntimeError("Pulsante download CSV non trovato")


def _parse_number(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    # trasforma 1.234,56 -> 1234.56
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _parse_csv_to_rows(csv_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Parser CSV tollerante. Cerca colonne data/ora/kwh/quality.
    Ritorna sempre una lista (anche vuota).
    """
    text = csv_bytes.decode("utf-8", errors="ignore")
    # sniffer per delimitatore ; o ,
    try:
        dialect = csv.Sniffer().sniff(text[:1024], delimiters=";,")
        delim = dialect.delimiter
    except Exception:
        # default: ;
        delim = ";"

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    if not rows:
        return []

    # trova header (prima riga "vera")
    header = None
    data_rows = []
    for r in rows:
        if any(cell.strip() for cell in r):
            header = [c.strip() for c in r]
            break
    if header is None:
        return []

    # indice colonne
    lower = [h.lower() for h in header]
    idx_date = next((i for i, h in enumerate(lower) if "data" in h or "date" in h), None)
    idx_time = next((i for i, h in enumerate(lower) if "ora" in h or "time" in h), None)
    idx_ts   = next((i for i, h in enumerate(lower) if "timestamp" in h or "data-ora" in h or "datetime" in h), None)
    idx_val  = next((i for i, h in enumerate(lower) if "kwh" in h or "energia" in h or "valore" in h), None)
    idx_q    = next((i for i, h in enumerate(lower) if "qualit" in h or "quality" in h), None)

    # scorri righe dopo header
    started = False
    for r in rows:
        if not started:
            # salta finché non trovi l'header
            if [c.strip() for c in r] == header:
                started = True
            continue
        if not any(c.strip() for c in r):
            continue

        ts: Optional[str] = None
        if idx_ts is not None and idx_ts < len(r) and r[idx_ts].strip():
            ts_raw = r[idx_ts].strip()
            # prova a normalizzare
            ts = _normalize_ts(ts_raw)
        elif idx_date is not None:
            d_raw = r[idx_date].strip() if idx_date < len(r) else ""
            t_raw = r[idx_time].strip() if (idx_time is not None and idx_time < len(r)) else "00:00"
            ts = _normalize_ts(f"{d_raw} {t_raw}")

        val: Optional[float] = None
        if idx_val is not None and idx_val < len(r):
            val = _parse_number(r[idx_val])

        qual: Optional[str] = None
        if idx_q is not None and idx_q < len(r):
            qual = r[idx_q].strip()

        if ts is not None and val is not None:
            data_rows.append({"ts": ts, "kWh": val, "quality": qual})

    return data_rows


def _normalize_ts(s: str) -> Optional[str]:
    s = s.strip()
    if not s:
        return None
    # formati più comuni: "dd/mm/yyyy hh:mm", "yyyy-mm-dd hh:mm"
    candidates = [
        ("%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:00"),
        ("%d/%m/%Y %H.%M", "%Y-%m-%dT%H:%M:00"),
        ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:00"),
        ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:00"),
        ("%d/%m/%Y", "%Y-%m-%dT00:00:00"),
        ("%Y-%m-%d", "%Y-%m-%dT00:00:00"),
    ]
    for fmt_in, fmt_out in candidates:
        try:
            dt = datetime.strptime(s, fmt_in)
            return dt.strftime(fmt_out)
        except Exception:
            continue
    # prova a ripulire separatori strani
    s2 = re.sub(r"[.]", ":", s)
    for fmt_in, fmt_out in candidates:
        try:
            dt = datetime.strptime(s2, fmt_in)
            return dt.strftime(fmt_out)
        except Exception:
            continue
    return None


# =====================================================================
# API PRINCIPALI CHIAMATE DAL MIDDLEWARE
# =====================================================================

async def refresh_with_session(
    storage_state_path: str,
    pod: str,
    date_from: date,
    date_to: date,
    timeout_ms: int = 90000,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    """
    Usa la sessione Playwright (storage_state.json) per evitare il login/CAPTCHA.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx: BrowserContext = await browser.new_context(
            storage_state=storage_state_path,
            accept_downloads=True,
        )
        ctx.set_default_timeout(timeout_ms)
        page = await ctx.new_page()

        # 1) Pagina "Curve di carico"
        await _goto_curve_page(page)

        # 2) Imposta periodo (best-effort; se fallisce usa valori già presenti)
        await _select_period(page, date_from, date_to)

        # 3) Download CSV
        csv_path = await _click_download_csv(page)

        # 4) Parsing CSV -> righe
        with open(csv_path, "rb") as f:
            data = f.read()
        rows = _parse_csv_to_rows(data)

        await browser.close()
        return rows


async def refresh_with_login(
    username: str,
    password: str,
    pod: str,
    date_from: date,
    date_to: date,
    timeout_ms: int = 90000,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    """
    **Opzionale**: se vuoi mantenere anche il login classico (potrebbe scattare il CAPTCHA).
    Completalo con i tuoi passi di autenticazione (in genere form Salesforce/Community).
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx: BrowserContext = await browser.new_context(accept_downloads=True)
        ctx.set_default_timeout(timeout_ms)
        page = await ctx.new_page()

        # TODO: completa i passi di login se vuoi usare questa via
        # Esempio (pseudocodice):
        # await page.goto(BASE_URL, timeout=NAV_TIMEOUT)
        # await page.get_by_label("Username").fill(username)
        # await page.get_by_label("Password").fill(password)
        # await page.get_by_role("button", name=re.compile("Accedi", re.I)).click()
        # ... eventuale 2FA ...
        # Poi prosegui come con la sessione:

        await _goto_curve_page(page)
        await _select_period(page, date_from, date_to)
        csv_path = await _click_download_csv(page)

        with open(csv_path, "rb") as f:
            data = f.read()
        rows = _parse_csv_to_rows(data)

        await browser.close()
        return rows
