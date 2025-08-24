import re
from pathlib import Path
from typing import Optional, Tuple, List

from playwright.async_api import Page, Frame

CURVE_URL = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"
LOGIN_URL = "https://private.e-distribuzione.it/PortaleClienti/s/login/"

# Riconosce sia "Scarica il dettaglio del quarto d’ora" che "Download CSV"
_DL_REGEX = re.compile(
    r"(scarica(?:\s+il)?\s+(?:dettaglio\s+del\s+)?quarto\s+d[’'`]ora|download\s*csv)",
    re.IGNORECASE,
)


class EdisError(Exception):
    """Eccezione con log allegato."""
    def __init__(self, message: str, log: Optional[List[str]] = None):
        super().__init__(message)
        self.log = log or []


def _to_ddmmyyyy(s: str) -> str:
    """'2025-08-24' -> '24/08/2025'; lascia dd/mm/yyyy invariato."""
    s = (s or "").strip()
    if not s:
        return s
    if "/" in s:
        return s
    parts = s.split("-")
    if len(parts) == 3 and all(parts):
        y, m, d = parts
        return f"{d.zfill(2)}/{m.zfill(2)}/{y}"
    return s


async def _set_text(loc, value: str):
    try:
        await loc.scroll_into_view_if_needed()
        await loc.wait_for(state="visible", timeout=4000)
        await loc.click()
        await loc.fill("")
        await loc.type(value, delay=10)
    except Exception:
        pass


async def _find_curve_frame(page: Page) -> Frame:
    # prova ogni frame
    for fr in page.frames:
        url = (fr.url or "").lower()
        try:
            title = ((await fr.title()) or "").lower()
        except Exception:
            title = ""
        if "curvedicarico" in url or "curve di carico" in title:
            return fr
    # fallback
    return page.main_frame


async def _find_download_button(frame: Frame):
    candidates = [
        frame.get_by_role("button", name=_DL_REGEX),
        frame.get_by_role("link", name=_DL_REGEX),
        frame.locator("button", has_text=_DL_REGEX),
        frame.locator("a", has_text=_DL_REGEX),
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


async def _fill_filters_best_effort(frame: Frame, pod: str, date_from: str, date_to: str, log: List[str]):
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

    # da / inizio
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

    # a / fine
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


async def _maybe_login(page: Page, username: str, password: str, log: List[str]) -> bool:
    url = (page.url or "").lower()
    if "login" not in url:
        return True

    if not username or not password:
        log.append("Sei in login ma non hai username/password: impossibile procedere.")
        return False

    log.append("Sono in login: provo a compilare le credenziali…")

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
        accedi = page.get_by_role("button", name=re.compile("accedi|login|entra", re.I))
        if not await accedi.count():
            accedi = page.locator("button.slds-button_brand")
        if await accedi.count():
            async with page.expect_navigation():
                await accedi.first.click()
        else:
            if await pass_loc.count():
                await pass_loc.first.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    await page.wait_for_load_state("domcontentloaded", timeout=20000)
    ok = "curvedicarico" in (page.url or "").lower()
    if ok:
        log.append("Login completato.")
    else:
        log.append("Login fallito o bloccato da captcha.")
    return ok


async def refresh_and_download_csv(
    page: Page,
    pod: str,
    date_from: str,
    date_to: str,
    use_storage: bool,
    username: str = "",
    password: str = "",
    download_dir: Optional[Path] = None,
) -> Tuple[Path, List[str]]:
    """
    Raggiunge la pagina, compila i filtri (best effort), clicca il bottone download e
    salva il CSV. Ritorna (path_file, log). Lancia EdisError in caso di problemi.
    """
    log: List[str] = []

    # 1) vai alla pagina “curve di carico”
    await page.goto(CURVE_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # 2) se serve login (senza storage)
    if not use_storage and "login" in (page.url or "").lower():
        ok = await _maybe_login(page, username, password, log)
        if not ok:
            raise EdisError("Login non riuscito (probabile reCAPTCHA).", log)
        await page.goto(CURVE_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

    # 3) frame
    frame = await _find_curve_frame(page)

    # 4) scorri in basso (bottone spesso in basso a dx)
    try:
        await frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await frame.wait_for_timeout(400)
    except Exception:
        pass

    # 5) filtri
    await _fill_filters_best_effort(frame, pod=pod, date_from=date_from, date_to=date_to, log=log)

    # 6) bottone download
    btn = await _find_download_button(frame)
    if not btn:
        try:
            await frame.evaluate("window.scrollTo(0, 0)")
            await frame.wait_for_timeout(300)
        except Exception:
            pass
        btn = await _find_download_button(frame)

    if not btn:
        raise EdisError("Pulsante download CSV non trovato (verifica selettori)", log)

    # 7) cartella
    if download_dir is None:
        download_dir = Path("/tmp")
    download_dir.mkdir(parents=True, exist_ok=True)

    safe_from = _to_ddmmyyyy(date_from).replace("/", "-")
    safe_to = _to_ddmmyyyy(date_to).replace("/", "-")
    out_path = download_dir / f"curve_{pod}_{safe_from}_{safe_to}.csv"

    # 8) click + attesa download
    async with page.expect_download() as dl_info:
        await btn.click()
    dl = await dl_info.value
    await dl.save_as(out_path.as_posix())

    return out_path, log
