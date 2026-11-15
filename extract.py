import os
import json
import time
from datetime import datetime
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
from pydantic import BaseModel

# ==========================================
# KONFIGURATION (Hier deine Werte eintragen)
# ==========================================
PROSPEKT_URL = "https://beispiel.de"
AUSGABE_DATEI = "daten/angebote.json"
MAX_SEITEN = 50  # Sicherheitslimit

# Optional: Falls Pfeiltaste "Rechts" nicht blättert, hier CSS-Selektor des Weiter-Buttons eintragen
# Beispiel: WEITER_BUTTON_SELEKTOR = "button[aria-label='Nächste Seite']"
WEITER_BUTTON_SELEKTOR = None 

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

class ProspektSeite(BaseModel):
    produkte: list[Produkt]

# ==========================================
# HAUPTLOGIK
# ==========================================
def bild_analysieren(client, img_bytes):
    prompt = (
        "Analysiere diese Prospektseite. Extrahiere ALLE beworbenen Produkte mit Preis. "
        "Ignoriere reine Deko-Elemente, Logos, Seitenzahlen und Navigations-Buttons des Web-Viewers. "
        "Achte bei Doppelseiten darauf, sowohl die linke als auch die rechte Seite vollständig zu erfassen."
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
            prompt
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ProspektSeite,
            temperature=0.1,
        ),
    )
    return json.loads(response.text).get("produkte", [])

def web_prospekt_auslesen():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    gesamte_produkte = []
    vorheriger_screenshot = None

    with sync_playwright() as p:
        # Hohe Auflösung (Retina-Faktor 2), damit auch kleingedruckte Grundpreise lesbar sind
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=2,
            locale="de-DE"
        )
        page = context.new_page()

        print(f"Lade Webseite: {PROSPEKT_URL}")
        page.goto(PROSPEKT_URL, wait_until="networkidle")
        time.sleep(3)

        # 1. Cookie-Banner automatisch schließen
        cookie_texte = ["Alle akzeptieren", "Akzeptieren", "Zustimmen", "Alle annehmen", "Einverstanden"]
        for text in cookie_texte:
            try:
                btn = page.get_by_role("button", name=text, exact=False).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    print(f"Cookie-Banner geschlossen ({text}).")
                    time.sleep(1.5)
                    break
            except Exception:
                pass

        # 2. Seite für Seite durchblättern
        for seite in range(1, MAX_SEITEN + 1):
            print(f"Erfasse Ansicht {seite}...")
            
            # Kurz warten, falls Bilder nach dem Blättern noch scharf gerechnet werden
            time.sleep(2)
            aktueller_screenshot = page.screenshot()

            # Abbruchbedingung: Bild ist identisch zum vorherigen -> Prospektende erreicht
            if aktueller_screenshot == vorheriger_screenshot:
                print("Keine Änderung mehr erkannt – Ende des Prospekts erreicht.")
                break

            vorheriger_screenshot = aktueller_screenshot

            # KI-Extraktion starten
            try:
                gefundene_produkte = bild_analysieren(client, aktueller_screenshot)
                print(f" -> {len(gefundene_produkte)} Produkte erkannt.")
                for prod in gefundene_produkte:
                    prod["ansicht_nr"] = seite
                    gesamte_produkte.append(prod)
            except Exception as e:
                print(f"Fehler bei KI-Analyse auf Seite {seite}: {e}")

            # 3. Nächste Seite aufrufen
            if WEITER_BUTTON_SELEKTOR:
                try:
                    page.locator(WEITER_BUTTON_SELEKTOR).first.click(timeout=3000)
                except Exception:
                    print("Weiter-Button nicht mehr klickbar – Ende erreicht.")
                    break
            else:
                page.keyboard.press("ArrowRight")

        browser.close()

    # 4. Metadaten & Ergebnisse als JSON speichern
    ergebnis_json = {
        "aktualisiert_am": datetime.now().isoformat(),
        "quelle": PROSPEKT_URL,
        "anzahl_produkte": len(gesamte_produkte),
        "produkte": gesamte_produkte
    }

    os.makedirs(os.path.dirname(AUSGABE_DATEI), exist_ok=True)
    with open(AUSGABE_DATEI, "w", encoding="utf-8") as f:
        json.dump(ergebnis_json, f, ensure_ascii=False, indent=2)

    print(f"Abgeschlossen! Insgesamt {len(gesamte_produkte)} Produkte gespeichert.")

if __name__ == "__main__":
    web_prospekt_auslesen()

