import asyncio
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

ITALIAN_MONTHS = {
    1:"Gennaio", 2:"Febbraio", 3:"Marzo", 4:"Aprile", 5:"Maggio", 6:"Giugno",
    7:"Luglio", 8:"Agosto", 9:"Settembre", 10:"Ottobre", 11:"Novembre", 12:"Dicembre"
}

class EDisPWClient:
    """
    Automazione 'best effort' con Playwright per:
    - login
    - click 'LE MIE MISURE'
    - selezione periodo (Inizio/Fine)
    - click 'Scarica il dettaglio quartorario .csv'

    NOTA: i selettori potrebbero richiedere piccoli aggiustamenti se il portale cambia.
    """

    def __init__(self, base_url: str = "https://private.e-distribuzione.it/PortaleClienti/s/"):
        self.base_url = base_url

    async def _login_and_download(self, username: str, password: str, pod: str, date_from: str, date_to: str) -> bytes:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            # 1) Login
            await page.goto(self.base_url, wait_until="load")
            try:
                email_sel = "input[type='email'], input[name='username'], input[name='j_username']"
                pwd_sel = "input[type='password'], input[name='password'], input[name='j_password']"
                await page.wait_for_selector(email_sel, timeout=15000)
                await page.fill(email_sel, username)
                await page.fill(pwd_sel, password)
                await page.keyboard.press("Enter")
            except PWTimeoutError:
                pass

            await page.wait_for_load_state("networkidle", timeout=30000)

            # 2) Entra in "LE MIE MISURE"
            try:
                await page.get_by_text("LE MIE MISURE", exact=False).click(timeout=20000)
            except Exception:
                await page.locator("a:has-text('MISURE'), div:has-text('LE MIE MISURE')").first.click(timeout=20000)

            await page.wait_for_load_state("networkidle")

            # 3) Seleziona periodo
            start = datetime.fromisoformat(date_from)
            end = datetime.fromisoformat(date_to)
            smonth, syear = ITALIAN_MONTHS[start.month], str(start.year)
            emonth, eyear = ITALIAN_MONTHS[end.month], str(end.year)

            selects = page.locator("select")
            await selects.nth(0).select_option(label=smonth)
            await selects.nth(1).select_option(label=syear)
            await selects.nth(2).select_option(label=emonth)
            await selects.nth(3).select_option(label=eyear)

            try:
                await page.get_by_role("button", name="Modifica periodo").click(timeout=10000)
            except Exception:
                try:
                    await page.get_by_text("Modifica periodo", exact=False).click(timeout=10000)
                except Exception:
                    pass

            await page.wait_for_load_state("networkidle")

            # 4) Scarica CSV
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            async with page.expect_download(timeout=30000) as dl_info:
                await page.get_by_text("Scarica il dettaglio quartorario", exact=False).click()
            download = await dl_info.value
            csv_bytes = await download.content()

            await context.close()
            await browser.close()
            return csv_bytes

    def download_csv(self, username: str, password: str, pod: str, date_from: str, date_to: str) -> bytes:
        return asyncio.run(self._login_and_download(username, password, pod, date_from, date_to))
