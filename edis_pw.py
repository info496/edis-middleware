import re
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import Page, Frame, expect


CURVE_URL = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"
LOGIN_URL = "https://private.e-distribuzione.it/PortaleClienti/s/login/"

# pattern robusto: gestisce “Scarica il dettaglio del quarto d’ora” e “Download CSV”
_DL_REGEX = re.compile(
    r"(scarica(?:\s+il)?\s+(?:dettaglio\s+del\s+)?quarto\s+d[’'`]ora|download\s*csv)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def _to_ddmmyyyy(s: str) -> str:
    """
    Converte '2025-08-24' -> '24/08/2025' oppure lascia invariato se già dd/mm/yyyy.
    """
    s = (s or "").strip()
    if not s:
        return s
    if "/" in s:
        return s  # già dd/mm/yyyy
    # case 'YYYY-MM-DD'
    parts = s.split("-")
    if len(parts) == 3 and all(parts):
        y, m, d = parts
        return f"{d.zfill(2)}/{m.zfill(2)}/{y}"
    return s


async def _set_text(loc, value: str):
    """Se esiste, forza un valore in un input text."""
    try:
        await loc.scroll_into_view_if_needed()
        await loc.wait_for(state="visible", timeout=4000)
        await loc.click()
        # seleziona tutto e sovrascrivi
        await loc.fill("")  # per sicurezza
        await loc.type(value, delay=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Frame e selettori
# ---------------------------------------------------------------------------

async def _find_curve_frame(page: Page) -> Frame:
    """
    Trova il frame “giusto” che contiene la pagina Curve di Carico.
    Priorità a URL contenente 'curvedicarico' oppure title 'Curve di carico'.
    """
    # prima prova: frame attuale se punta già alla pagina
    for fr in page.frames:
        url = (fr.url or "").lower()
        try:
            title = ((await fr.title()) or "").lower()
        except Exception:
            title = ""
        if "curvedicarico" in url or "curve di carico" in title:
            return fr

    # fallback: main frame
    return page.main_frame


async def _find_download_button(frame: Frame):
    """
    Cerca pulsante/ancora 'Scarica il dettaglio del quarto d’ora' / 'Download CSV'
    con varie strategie robuste.
    """
    candidates = [
        frame.get_by_role("button", name=_DL_REGEX),
        frame.get_by_role("link", name=_DL_REGEX),
        frame.locator("button", has_text=_DL_REGEX),
        frame.locator("a", has_text=_DL_REGEX),
        # classi Salesforce ricorrenti
        frame.locator("button.slds-button_brand"),
        frame.locator("button.slds-button.slds-button_brand.slds-float_right"),
    ]

    for loc in candidates:
        try:
            count = await loc.count()
        except Exception:
            continue
        for i in range(count):
            el = loc.nth(i)
            try:
                txt = (await el.inner_text()).strip().lower()
            except Exception:
                continue
            if _DL_REGEX.search(txt):
                try:
                    await el.scroll_into_view_if_needed()
                    await el.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                return el
    return None


# ---------------------------------------------------------------------------
# Compilazione filtri (best effort — non blocca se i campi non esistono)
# ---------------------------------------------------------------------------

async def _fill_filters_best_effort(
    frame: Frame, pod: str, date_from: str, date_to: str, log: list
):
    """Prova a compilare POD e date. Se i campi non ci sono, logga e prosegue."""
    dfrom = _to_ddmmyyyy(date_from)
    dto = _to_ddmmyyyy(date_to)

    # POD
    try:
        pod_loc = (
            frame.get_by_label(re.compile("pod", re.I))
            .or_(frame.get_by_placeholder(re.compile("pod", re.I)))
            .or_(frame.locator("input[name*='pod' i]"))
        )
        if await pod_loc.count():
            await _set_text(pod_loc.first, pod)
        else:
            log.append("ATTENZIONE: campo POD non trovato (best effort)")
    except Exception:
        log.append("ATTENZIONE: campo POD non trovato (best effort)")

    # date from
    try:
        df_loc = (
            frame.get_by_label(re.compile("inizio|da|from", re.I))
            .or_(frame.get_by_placeholder(re.compile(r"\d{2}/\d{2}/\d{4}")))
        )
        if await df_loc.count():
            await _set_text(df_loc.first, dfrom)
        else:
            log.append("ATTENZIONE: campo data inizio non trovato (best effort)")
    except Exception:
        log.append("ATTENZIONE: campo data inizio non trovato (best effort)")

    # date to
    try:
        dt_loc = (
            frame.get_by_label(re.compile("fine|a|to", re.I))
            .or_(frame.get_by_placeholder(re.compile(r"\d{2}/\d{2}/\d{4}")))
        )
        if await dt_loc.count():
            await _set_text(dt_loc.first, dto)
        else:
            log.append("ATTENZIONE: campo data fine non trovato (best effort)")
    except Exception:
        log.append("ATTENZIONE: campo data fine non trovato (best effort)")


# ---------------------------------------------------------------------------
# Login best-effort (solo se non si usa storage; soggetto a captcha)
# ---------------------------------------------------------------------------

async def _maybe_login(page: Page, username: str, password: str, log: list) -> bool:
    """
    Se siamo sulla pagina di login, prova a loggarsi. Ritorna True se pensa di esserci riuscito.
    NB: il sito può richiedere captcha => non garantito.
    """
    try:
        url = page.url.lower()
    except Exception:
        url = ""

    if "login" not in url:
        return True  # non sembra in login

    if not username or not password:
        log.append("Sei in login ma non hai username/password: impossibile procedere.")
        return False

    log.append("Sono in login: provo a compilare le credenziali…")

    # campi possibili
    user_loc = (
        page.get_by_label(re.compile("email|user|username", re.I))
        .or_(page.get_by_placeholder(re.compile("email|user", re.I)))
        .or_(page.locator("input[type='email'], input[name*='user' i]"))
    )
    pass_loc = (
        page.get_by_label(re.compile("password", re.I))
        .or_(page.get_by_placeholder(re.compile("password", re.I)))
        .or_(page.locator("input[type='password']"))
    )

    try:
        if await user_loc.count():
            await _set_text(user_loc.first, username)
        if await pass_loc.count():
            await _set_text(pass_loc.first, password)
        # bottone “Accedi” / “Login”
        accedi = page.get_by_role("button", name=re.compile("accedi|login|entra", re.I))
        if not await accedi.count():
            accedi = page.locator("button.slds-button_brand")
        if await accedi.count():
            async with page.expect_navigation():
                await accedi.first.click()
        else:
            # prova Invio direttamente sul campo password
            if await pass_loc.count():
                await pass_loc.first.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # verifica “grossolana”: se URL contiene curvedicarico
    await page.wait_for_load_state("domcontentloaded", timeout=20000)
    ok = "curvedicarico" in page.url.lower()
    if ok:
        log.append("Login completato.")
    else:
        log.append("Login fallito o bloccato da captcha.")
    return ok


# ---------------------------------------------------------------------------
# Funzione principale invocata da main.py
# ---------------------------------------------------------------------------

async def refresh_and_download_csv(
    page: Page,
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool,
    username: str = "",
    password: str = "",
    download_dir: Optional[Path] = None,
) -> Path:
    """
    Naviga a 'Curve di Carico', imposta (best effort) POD e date, clicca il pulsante
    “Scarica il dettaglio del quarto d’ora” / “Download CSV” e ritorna il path del file.

    Parametri:
      - page: Page async già aperta (il contesto può essere con storage state).
      - use_storage: se True si assume che l’utente sia già autenticato.
      - username/password: usati solo se use_storage=False (login best effort).
    """
    log: list[str] = []

    # 1) vai alla pagina “curve di carico”
    await page.goto(CURVE_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # 2) se risulto in una pagina di login, prova a loggarti (solo se non usi storage)
    if not use_storage and "login" in page.url.lower():
        ok = await _maybe_login(page, username, password, log)
        if not ok:
            raise RuntimeError("Login non riuscito (probabile reCAPTCHA).")

        # dopo login torna alla pagina curve
        await page.goto(CURVE_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

    # 3) trova frame
    frame = await _find_curve_frame(page)

    # 4) scorri verso il basso (il bottone è spesso in fondo a dx)
    try:
        await frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await frame.wait_for_timeout(400)
    except Exception:
        pass

    # 5) compila filtri (best effort)
    await _fill_filters_best_effort(frame, pod=pod, date_from=date_from, date_to=date_to, log=log)

    # 6) trova pulsante download
    btn = await _find_download_button(frame)
    if not btn:
        # un altro tentativo dopo scroll up
        try:
            await frame.evaluate("window.scrollTo(0, 0)")
            await frame.wait_for_timeout(300)
        except Exception:
            pass
        btn = await _find_download_button(frame)

    if not btn:
        raise RuntimeError("Pulsante download CSV non trovato (verifica selettori)")

    # 7) configurazione cartella download
    if download_dir is None:
        download_dir = Path("/tmp")
    download_dir.mkdir(parents=True, exist_ok=True)

    safe_from = _to_ddmmyyyy(date_from).replace("/", "-")
    safe_to = _to_ddmmyyyy(date_to).replace("/", "-")
    out_path = download_dir / f"curve_{pod}_{safe_from}_{safe_to}.csv"

    # 8) click con attesa del download reale
    async with page.expect_download() as dl_info:
        await btn.click()
    dl = await dl_info.value
    await dl.save_as(out_path.as_posix())

    return out_path
