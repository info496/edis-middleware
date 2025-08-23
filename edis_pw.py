import os, asyncio
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

ITALIAN_MONTHS = {
    1:"Gennaio", 2:"Febbraio", 3:"Marzo", 4:"Aprile", 5:"Maggio", 6:"Giugno",
    7:"Luglio", 8:"Agosto", 9:"Settembre", 10:"Ottobre", 11:"Novembre", 12:"Dicembre"
}

# Timeout di default (ms) – puoi cambiarlo da env su Render: PW_TIMEOUT_MS=90000
DEFAULT_TIMEOUT = int(os.getenv("PW_TIMEOUT_MS", "90000"))

class EDisPWClient:
    def __init__(self, base_url: str = "https://private.e-distribuzione.it/PortaleClienti/s/"):
        self.base_url = base_url
        self.storage_state_path = os.getenv("STORAGE_STATE")  # opzionale (sessione salvata)

    async def _login_and_download(self, username: str, password: str, pod: str, date_from: str, date_to: str) -> bytes:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            use_state = self.storage_state_path if self.storage_state_path and os.path.exists(self.storage_state_path) else None
            context = await browser.new_context(accept_downloads=True, storage_state=use_state)
            page = await context.new_page()

            # Applica timeouts estesi
            page.set_default_timeout(DEFAULT_TIMEOUT)
            page.set_default_navigation_timeout(DEFAULT_TIMEOUT)

            # Vai al portale
            await page.goto(self.base_url, wait_until="load")

            # Se non ho uno storage_state, provo il login
            if not use_state:
                try:
                    email_sel = "input[type='email'], input[name='username'], input[name='j_username']"
                    pwd_sel   = "input[type='password'], input[name='password'], input[name='j_password']"
                    await page.wait_for_selector(email_sel)
                    await page.fill(email_sel, username)
                    await page.fill(pwd_sel, password)
                    await page.keyboard.press("Enter")
                    await page.wait_for_load_state("networkidle")
                except PWTimeoutError:
                    # Se sto ancora in pagina login, probabile reCAPTCHA
                    html = (await page.content()).lower()
                    if "recaptcha" in html or "captcha" in html:
                        raise RuntimeError("Login bloccato da reCAPTCHA: esegui l’accesso una volta da browser e salva la sessione (storage_state).")
                    raise RuntimeError("Timeout durante il login al portale.")

            # Controllo reCAPTCHA anche dopo il redirect
            html = (await page.content()).lower()
            if "recaptcha" in html or "captcha" in html or "login" in page.url.lower():
                raise RuntimeError("Accesso non riuscito (reCAPTCHA o pagina di login). Usa una sessione salvata.")

            # Entra in “LE MIE MISURE”
            try:
                await page.get_by_text("LE MIE MISURE", exact=False).click()
            except Exception:
                await page.locator("a:has-text('MISURE'), div:has-text('LE MIE MISURE')").first.click()
            await page.wait_for_load_state("networkidle")

            # Seleziona il periodo
            start = datetime.fromisoformat(date_from)
            end   = datetime.fromisoformat(date_to)
            smonth, syear = ITALIAN_MONTHS[start.month], str(start.year)
            emonth, eyear = ITALIAN_MONTHS[end.month], str(end.year)

            selects = page.locator("select")
            await selects.nth(0).select_option(label=smonth)
            await selects.nth(1).select_option(label=syear)
            await selects.nth(2).select_option(label=emonth)
            await selects.nth(3).select_option(label=eyear)

            # Applica periodo (se presente)
            try:
                await page.get_by_role("button", name="Modifica periodo").click()
            except Exception:
                try:
                    await page.get_by_text("Modifica periodo", exact=False).click()
                except Exception:
                    pass
            await page.wait_for_load_state("networkidle")

            # Scarica CSV (timeout esteso)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                async with page.expect_download(timeout=DEFAULT_TIMEOUT) as dl_info:
                    await page.get_by_text("Scarica il dettaglio quartorario", exact=False).click()
                download = await dl_info.value
                csv_bytes = await download.content()
            except PWTimeoutError:
                raise RuntimeError("Timeout nello scaricare il CSV (non trovato il bottone o pagina lenta).")

            await context.close()
            await browser.close()
            return csv_bytes

    def download_csv(self, username: str, password: str, pod: str, date_from: str, date_to: str) -> bytes:
        return asyncio.run(self._login_and_download(username, password, pod, date_from, date_to))
