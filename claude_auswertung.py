"""
claude_auswertung.py

Automatisierte Auswertung der Sektor-Analyse-Ergebnisse durch Claude
(Ersatz fuer das manuelle Kopieren in das Gemini-Gem). Liest die Master-
Anweisung sowie alle Tagesdateien lokal ein und schickt sie in EINER
Anfrage an die Claude-API; das Ergebnis wird als Auswertung(<Datum>).txt
gespeichert und von upload_to_drive.py automatisch mit hochgeladen.

Wichtig: CSV/TXT/MD-Dateien werden laut Anthropic-Doku NICHT ueber die
Files API als Dokument-Block eingebunden, sondern direkt als Klartext in
die Nachricht eingebettet - das ist hier bereits so umgesetzt (kein
Datei-Upload-Mechanismus wie bei Gemini, der Kodierungsprobleme
einschleppen koennte).

Voraussetzungen:
    pip install anthropic

Erwartet folgende Umgebungsvariable (z. B. als GitHub Actions Secret):
    ANTHROPIC_API_KEY

Erwartet im Arbeitsverzeichnis (Pfade/Muster unten in KONFIGURATION anpassen):
    Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md   (Master-Anweisung, reiner Text)
    briefing.txt (oder Briefing(<Datum>).txt)
    Setups(<Datum>).csv
    Performance(<Datum>).csv
    Performance_EU(<Datum>).csv
    Offene_Positionen.csv (optional)
    Trendwende_Setups(<Datum>).csv (optional)
    Trendwende_Briefing(<Datum>).txt (optional)

Ergebnis wird nach Auswertung(<Datum>).txt geschrieben.
"""

import os
import sys
import glob
import datetime

from anthropic import Anthropic


# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

MODELL = "claude-sonnet-5"
MAX_TOKENS = 8000

ANWEISUNG_DATEI = "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"

# Dateimuster fuer die Eingabedateien (glob-Muster, nimmt jeweils den
# alphabetisch letzten Treffer -> passt zu "Setups(2026-07-19).csv" etc.)
DATEIMUSTER = {
    "briefing.txt": ["briefing.txt", "Briefing(*).txt"],
    "Setups(...).csv": ["Setups(*).csv"],
    "Performance(...).csv": ["Performance(*).csv"],
    "Performance_EU(...).csv": ["Performance_EU(*).csv"],
    "Offene_Positionen.csv": ["Offene_Positionen.csv", "Offene_Positionen(*).csv"],
    "Trendwende_Setups(...).csv": ["Trendwende_Setups(*).csv"],
    "Trendwende_Briefing(...).txt": ["Trendwende_Briefing(*).txt"],
    "Short_Setups(...).csv": ["Short_Setups(*).csv"],
    "Short_Briefing(...).txt": ["Short_Briefing(*).txt"],
    "Edelmetalle_Setups(...).csv": ["Edelmetalle_Setups(*).csv"],
    "Edelmetalle_Briefing(...).txt": ["Edelmetalle_Briefing(*).txt"],
}
# Diese Dateien MUESSEN vorhanden sein, sonst wird abgebrochen. Offene
# Positionen und die beiden Trendwende-Dateien sind optional (koennten an
# einem Tag fehlen, z. B. wenn ein Scanner-Schritt mal deaktiviert ist -
# siehe Abschnitt 7 der Anleitung, die genau diesen Fall vorsieht).
PFLICHT_DATEIEN = {"briefing.txt", "Setups(...).csv", "Performance(...).csv", "Performance_EU(...).csv"}


def finde_datei(muster_liste):
    for muster in muster_liste:
        treffer = sorted(glob.glob(muster))
        if treffer:
            return treffer[-1]
    return None


def sammle_eingabedateien():
    gefunden = {}
    for name, muster_liste in DATEIMUSTER.items():
        gefunden[name] = finde_datei(muster_liste)

    fehlend = [n for n in PFLICHT_DATEIEN if gefunden.get(n) is None]
    if fehlend:
        print(f"FEHLER: Pflichtdateien nicht gefunden: {fehlend}")
        sys.exit(1)

    print("Gefundene Eingabedateien:")
    for name, pfad in gefunden.items():
        print(f"  - {name}: {pfad if pfad else '(nicht vorhanden, wird uebersprungen)'}")

    return {k: v for k, v in gefunden.items() if v is not None}


def lade_text(pfad):
    # utf-8-sig entfernt eine eventuell vorhandene BOM automatisch - genau
    # das Kodierungsformat, das positionen_tracker.py/analyse.py beim
    # Schreiben verwenden (encoding='utf-8-sig').
    with open(pfad, "r", encoding="utf-8-sig") as f:
        return f.read()


def lade_anweisung():
    if not os.path.isfile(ANWEISUNG_DATEI):
        print(f"FEHLER: Anweisungs-Datei nicht gefunden: {ANWEISUNG_DATEI}")
        sys.exit(1)
    return lade_text(ANWEISUNG_DATEI)


def baue_nachricht(eingabedateien):
    """Baut EINE Textnachricht mit allen Dateien, klar durch Trennzeilen
    markiert - Claude erhaelt so alle Rohdaten direkt als Text, ohne
    separaten Datei-Upload-Mechanismus (siehe Modul-Docstring)."""
    teile = [
        "Verarbeite die folgenden Dateien wie in der System-Anweisung beschrieben "
        "und erstelle die vollstaendige Daten-Uebersicht.\n"
    ]
    for name, pfad in eingabedateien.items():
        inhalt = lade_text(pfad)
        teile.append(f"\n=== DATEI: {name} ===\n{inhalt}\n=== ENDE {name} ===\n")
    return "".join(teile)


# ---------------------------------------------------------------------------
# HAUPTLOGIK
# ---------------------------------------------------------------------------

def claude_auswertung_starten():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("FEHLER: Umgebungsvariable ANTHROPIC_API_KEY nicht gesetzt.")
        sys.exit(1)

    client = Anthropic(api_key=api_key)
    anweisung = lade_anweisung()
    eingabedateien = sammle_eingabedateien()
    nachricht = baue_nachricht(eingabedateien)

    print(f"\nSende Anfrage an {MODELL}...")
    try:
        antwort = client.messages.create(
            model=MODELL,
            max_tokens=MAX_TOKENS,
            system=anweisung,
            messages=[{"role": "user", "content": nachricht}],
        )
    except Exception as e:
        print(f"FEHLER beim API-Call: {e}")
        sys.exit(1)

    text_teile = [block.text for block in antwort.content if block.type == "text"]
    text = "\n".join(text_teile).strip()

    if not text:
        print("FEHLER: Leere Antwort erhalten.")
        sys.exit(1)

    print("Antwort erhalten.")
    return text


def speichere_ergebnis(text):
    heute = datetime.date.today().isoformat()
    ausgabe_datei = f"Auswertung({heute}).txt"
    with open(ausgabe_datei, "w", encoding="utf-8-sig") as f:
        f.write(text)
    print(f"Gespeichert: {ausgabe_datei}")
    return ausgabe_datei


if __name__ == "__main__":
    print("Claude-Auswertung gestartet...")
    ergebnis_text = claude_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(ergebnis_text)
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
