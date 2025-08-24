# edis_pw.py
from __future__ import annotations
import os, io, csv, re, json, time
from typing import List, Dict, Any, Optional
from playwright.sync_api import Playwright, sync_playwright, TimeoutError as PWTimeout

E_URL = "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico"

# collezionatore log semplice
class Logger(list):
    def write(self, msg: str) -> None:
        msg = str(msg).rstrip()
        if msg:
            self.append(msg)

def _human_selector_list(locs: List[str]) -> str:
    return " | ".join(locs)

def refresh_and_download_csv(
    storage_state_path: str,
    out_dir: str = "/app/tmp",
    headless: bool = True,
    timeout_ms: int = 45000,
) -> Dict[str, Any]:
    """
    Apre la pagina Curve di carico con la sessione salvata e clicca sul bottone
    “Scarica il dettaglio del quarto d’ora”, intercettando il download CSV.
    Ritorna: { ok, csv_path, rows, log }
    """
    os.makedirs(out_dir, exist_ok=True)

    log = Logger()
    log.write("=== refresh_and_download_csv: start ===")

    # Se non esiste lo storage -> errore esplicativo
    if not os.path.exists(storage_state_path):
        return {"ok": False, "detail": f"storage_state non trovato: {storage_state_path}", "log": log}

    # Selettori robusti per il bottone di download
    DL_SELECTORS = [
        # testuale più probabile sulla tua pagina
        "button:has-text('Scarica il dettaglio del quarto d\\'ora')",
        "button:has-text('Scarica il dettaglio del quarto d’ora')",  # apostrofo tipografico
        # fallback generici
        "button:has-text('Scarica')",
        "button.slds-button_brand.slds-float_right",
        "button.slds-button_brand",
        "button:has-text('CSV')",
    ]
    log.write(f"Selettori download (ordine): {_human_selector_list(DL_SELECTORS)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox"])
        ctx = browser.new_context(storage_state=storage_state_path, accept_downloads=True)
        page = ctx.new_page()

        try:
            log.write(f"Apro URL: {E_URL}")
            page.goto(E_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            time.sleep(1.0)

            # Trova il frame che contiene la UI (titolo ‘Curve di carico’)
            target = None
            frames = page.frames
            log.write(f"Frame totali: {len(frames)}")
            for f in frames:
                url = f.url or ""
                title = (f.title() or "")
                score = 0
                if "curvedicarico" in url:
                    score += 2
                if "Curve di carico" in title or "Curva di carico" in title:
                    score += 1
                log.write(f"- Frame url='{url}' title='{title}' -> score={score}")
                if score >= 2 and target is None:
                    target = f
            if target is None:
                target = page.main_frame
                log.write("ATTENZIONE: frame ‘curvedicarico’ non trovato, uso main_frame")

            # Debug: stampa tutti i bottoni visibili per aiutare il tuning
            try:
                btn_texts = target.locator("button").all_inner_texts()
                sample = btn_texts[:10]
                log.write(f"Bottoni visibili: {len(btn_texts)}  (prime 10) -> {sample}")
            except Exception as e:
                log.write(f"[debug] errore lettura bottoni: {e}")

            # Cerca il bottone download
            dl_btn = None
            for sel in DL_SELECTORS:
                loc = target.locator(sel)
                try:
                    count = loc.count()
                except Exception:
                    count = 0
                log.write(f"Provo selettore '{sel}' -> count={count}")
                if count > 0:
                    dl_btn = loc.first
                    break

            if dl_btn is None:
                # last resort: cerca per nome ARIA che contenga “scarica”
                dl_btn = target.get_by_role("button", name=re.compile(r"scarica", re.I))
                try:
                    if dl_btn.count() == 0:
                        dl_btn = None
                except Exception:
                    dl_btn = None

            if dl_btn is None:
                return {
                    "ok": False,
                    "detail": "Pulsante download CSV non trovato (verifica selettori)",
                    "log": log,
                }

            log.write("Trovato bottone, clic e attendo il download…")

            with page.expect_event("download", timeout=timeout_ms) as dl_wait:
                dl_btn.click()
            download = dl_wait.value

            # salva su file
            file_name = download.suggested_filename or "curva.csv"
            # se manca l’estensione, aggiungi csv
            if not re.search(r"\.csv$", file_name, re.I):
                file_name += ".csv"
            csv_path = os.path.join(out_dir, file_name)
            download.save_as(csv_path)
            log.write(f"CSV salvato in: {csv_path}")

            # carica in memoria e parse (facoltativo)
            rows: List[Dict[str, Any]] = []
            try:
                content = download.content()
                text = content.decode("utf-8", errors="replace")
                reader = csv.reader(io.StringIO(text), delimiter=";")
                header = None
                for i, r in enumerate(reader):
                    if i == 0:
                        header = r
                        continue
                    rows.append({"raw": r})
                log.write(f"Righe CSV (esclusa intestazione): {len(rows)}")
            except Exception as e:
                log.write(f"Parsing CSV fallito (non blocca): {e}")

            return {"ok": True, "csv_path": csv_path, "rows": rows, "log": log}

        except PWTimeout:
            return {"ok": False, "detail": "Timeout durante il caricamento/click", "log": log}
        except Exception as e:
            return {"ok": False, "detail": str(e), "log": log}
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass
