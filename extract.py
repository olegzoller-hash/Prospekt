import os
import json
import time
from datetime import datetime
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
from pydantic import BaseModel

# ==========================================
# KONFIGURATION
# ==========================================
BASIS_URL = "https://aldi-nord.de"
AUSGABE_DATEI = "daten/angebote.json"
MAX_SEITEN = 45
SEITEN_PRO_ANFRAGE = 5

# Hauptmodell und Backup-Modell bei Server-Überlastung (503)
HAUPT_MODELL = "gemini-3.8-flash"
BACKUP_MODELL = "gemini-3.5-flash-lite"


# ==========================================
# JSON-STRUKTUR DEFINIEREN
# ==========================================
class Produkt(BaseModel):
    name: str
    marke: str | None
    kategorie: str | None
    preis: float
    streichpreis: float | None
    grundpreis: str | None
    rabatt_badge: str | None
    seite_nr: int


class ProspektPaket(BaseModel):
    produkte: list[Produkt]


# ==========================================
# HAUPTLOGIK MIT RETRY & FALLBACK
# ==========================================
def paket_analysieren(client, bilder_paket):
    inhalte = []
    seiten_nummern = []

    for seite_nr, img_bytes in bilder_paket:
        seiten_nummern.append(str(seite_nr))
        inhalte.append(f"--- Seite {seite_nr} ---")
        inhalte.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    prompt = (
        f"Analysiere diese ALDI-Prospektseiten (Seiten {', '.join(seiten_nummern)}). "
        "Extrahiere ALLE beworbenen Produkte mit Preis und ordne jedem Produkt die korrekte 'seite_nr' zu. "
        "Ignoriere reine Deko-Elemente, Logos und Navigations-Buttons der Webseite."
    )
    inhalte.append(prompt)

    # Bis zu 3 Versuche mit dem Hauptmodell, danach Wechsel auf Backup-Modell
    versuche = [HAUPT_MODELL, HAUPT_MODELL, HAUPT_MODELL, BACKUP_MODELL]

    for versuch_nr, aktuelles_modell in enumerate(versuche, start=1):
        try:
            response = client.models.generate_content(
                model=aktuelles_modell,
                contents=inhalte,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ProspektPaket,
                    temperature=0.1,
                ),
            )
            return json.loads(response.text).get("produkte", [])

        except Exception as e:
            fehler_text = str(e)
            if "503" in fehler_text or "UNAVAILABLE" in fehler_text or "429" in fehler_text:
                wartezeit = 15 * versuch_nr
                print(f"   [Warnung] Server ausgelastet/Limit ({aktuelles_modell}). Warte {wartezeit}s vor Versuch {versuch_nr + 1}...")
                time.sleep(wartezeit)
            else:
                raise e

    print("   [Fehler] Alle Wiederholungsversuche für dieses Paket fehlgeschlagen.")
    return []


def web_prospekt_auslesen():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    gesamte_produkte = []
    alle_screenshots = []
    vorheriger_screenshot = None
    os.makedirs("daten", exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=2,
            locale="de-DE",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        start_url = f"{BASIS_URL}1"
        print(f"Öffne Startseite: {start_url}")
        page.goto(start_url, wait_until="domcontentloaded")
        time.sleep(4)

        # Cookie-Banner schließen
        try:
            uc_btn = page.locator("[data-testid='uc-accept-all-button']")
            if uc_btn.is_visible(timeout=3000):
                uc_btn.click()
                print("ALDI Cookie-Banner geschlossen.")
                time.sleep(2)
        except Exception:
            pass

        # 1. Zuerst alle Screenshots einsammeln
        for seite in range(1, MAX_SEITEN + 1):
            if seite > 1:
                page.goto(f"{BASIS_URL}{seite}", wait_until="domcontentloaded")
                time.sleep(2.5)

            if seite > 1 and f"page={seite}" not in page.url:
                print(f"Umleitung auf {page.url} – Prospektende bei Seite {seite - 1} erreicht.")
                break

            try:
                viewer = page.locator("#content > div.margin-wrapper").first
                if viewer.is_visible(timeout=2000):
                    viewer.scroll_into_view_if_needed()
                    time.sleep(0.5)
            except Exception:
                pass

            aktueller_screenshot = page.screenshot()

            if aktueller_screenshot == vorheriger_screenshot:
                print(f"Keine neue Seite mehr – Prospektende bei Seite {seite - 1} erreicht.")
                break

            vorheriger_screenshot = aktueller_screenshot
            alle_screenshots.append((seite, aktueller_screenshot))
            print(f"Screenshot von Seite {seite} gespeichert.")

        browser.close()

    # 2. Screenshots in 5er-Paketen an die KI senden
    print(f"Starte KI-Analyse für {len(alle_screenshots)} Seiten in {SEITEN_PRO_ANFRAGE}er-Paketen...")
    for i in range(0, len(alle_screenshots), SEITEN_PRO_ANFRAGE):
        paket = alle_screenshots[i:i + SEITEN_PRO_ANFRAGE]
        start_s = paket
        ende_s = paket[-1]
        print(f"Analysiere Paket: Seiten {start_s} bis {ende_s}...")

        try:
            gefundene_produkte = paket_analysieren(client, paket)
            print(f" -> {len(gefundene_produkte)} Produkte erkannt.")
            gesamte_produkte.extend(gefundene_produkte)
        except Exception as e:
            print(f"Fehler bei Paket {start_s}-{ende_s}: {e}")

        time.sleep(4)

    # 3. Ergebnisse speichern
    ergebnis_json = {
        "aktualisiert_am": datetime.now().isoformat(),
        "quelle": f"{BASIS_URL}1",
        "anzahl_produkte": len(gesamte_produkte),
        "produkte": gesamte_produkte
    }

    with open(AUSGABE_DATEI, "w", encoding="utf-8") as f:
        json.dump(ergebnis_json, f, ensure_ascii=False, indent=2)

    print(f"Abgeschlossen! Insgesamt {len(gesamte_produkte)} Produkte gespeichert.")


if __name__ == "__main__":
    web_prospekt_auslesen()
