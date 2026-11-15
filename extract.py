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
BASIS_URL = "https://www.aldi-nord.de/prospekte/aldi-aktuell.html?page="
AUSGABE_DATEI = "daten/angebote.json"
MAX_SEITEN = 45


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
        "Analysiere diese ALDI-Prospektseite. Extrahiere ALLE beworbenen Produkte mit Preis. "
        "Falls es ein Titelblatt ohne konkrete Produktpreise ist, gib eine leere Liste zurück. "
        "Ignoriere reine Deko-Elemente, Logos, Seitenzahlen und Navigations-Buttons der Webseite."
    )
    response = client.models.generate_content(
        model="gemini-3.8-flash",
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
    os.makedirs("daten", exist_ok=True)

    with sync_playwright() as p:
        # User-Agent eines normalen Windows-Chrome setzen (verhindert Bot-Blockaden)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=2,
            locale="de-DE",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Seite 1 öffnen und Cookie-Banner erledigen
        start_url = f"{BASIS_URL}1"
        print(f"Öffne Startseite: {start_url}")
        page.goto(start_url, wait_until="domcontentloaded")
        time.sleep(4)

        # Usercentrics / ALDI Cookie-Banner schließen
        try:
            uc_btn = page.locator("[data-testid='uc-accept-all-button']")
            if uc_btn.is_visible(timeout=3000):
                uc_btn.click()
                print("ALDI Cookie-Banner (Usercentrics) geschlossen.")
                time.sleep(2)
        except Exception:
            for text in ["Alle bestätigen", "Alle akzeptieren", "Akzeptieren"]:
                try:
                    btn = page.get_by_role("button", name=text, exact=False).first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        print(f"Cookie-Banner geschlossen ({text}).")
                        time.sleep(2)
                        break
                except Exception:
                    pass

        # Durch die Seiten iterieren (?page=1, ?page=2, ...)
        for seite in range(1, MAX_SEITEN + 1):
            ziel_url = f"{BASIS_URL}{seite}"
            if seite > 1:
                page.goto(ziel_url, wait_until="domcontentloaded")
                time.sleep(3)

            # Prüfen, ob ALDI zurück auf Seite 1 oder eine andere URL umgeleitet hat (Prospektende)
            if seite > 1 and f"page={seite}" not in page.url:
                print(f"Umleitung auf {page.url} erkannt – Ende des Prospekts erreicht.")
                break

            # Zum Prospekt-Viewer scrollen, damit er mittig im Bild ist
            try:
                viewer = page.locator("#content > div.margin-wrapper").first
                if viewer.is_visible(timeout=2000):
                    viewer.scroll_into_view_if_needed()
                    time.sleep(1)
            except Exception:
                pass

            print(f"Erfasse Seite {seite} ({page.url})...")
            aktueller_screenshot = page.screenshot()

            # Debug-Screenshot von Seite 2 speichern (erste Innenseite mit Produkten)
            if seite == 2:
                with open("daten/debug_seite2.png", "wb") as f:
                    f.write(aktueller_screenshot)

            # KI-Extraktion starten
            try:
                gefundene_produkte = bild_analysieren(client, aktueller_screenshot)
                print(f" -> {len(gefundene_produkte)} Produkte auf Seite {seite} erkannt.")
                for prod in gefundene_produkte:
                    prod["seite_nr"] = seite
                    gesamte_produkte.append(prod)
            except Exception as e:
                print(f"Fehler bei KI-Analyse auf Seite {seite}: {e}")

        browser.close()

    # Ergebnisse speichern
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
