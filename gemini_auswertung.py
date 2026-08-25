"""
gemini_auswertung.py

Automatisierte Auswertung der Neuber Macro & Markets-Ergebnisse durch Gemini
(Ersatz fuer das manuelle Kopieren in den Gem-Chat) - kostenlose
Alternative zu claude_auswertung.py, da die Gemini-API (anders als die
Claude-API) eine dauerhafte kostenlose Nutzungsstufe bietet.

Mit automatischem Retry bei den bekannten, nicht-deterministischen
Sicherheitsfilter-Ablehnungen ("Ich bin nur ein Sprachmodell...", etc.)
und - NEU 30.07.2026 - mit einer eigenen, deutlich laengeren Warte-Staffel
fuer serverseitige Ueberlast (HTTP 503) und Netzwerk-Abbrueche.

Voraussetzungen:
    pip install google-genai

Erwartet folgende Umgebungsvariable (z. B. als GitHub Actions Secret):
    GEMINI_API_KEY

Erwartet im Arbeitsverzeichnis (Pfade/Muster unten in KONFIGURATION anpassen):
    Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md   (Master-Anweisung, reiner Text)
    briefing.txt (oder Briefing(<Datum>).txt)
    Setups(<Datum>).csv
    Performance(<Datum>).csv
    Performance_EU(<Datum>).csv
    Offene Positionen+Check.csv (verbindlich)
    Trendwende_Setups(<Datum>).csv (optional)
    Trendwende_Briefing(<Datum>).txt (optional)

Short_Setups(<Datum>).csv und Short_Briefing(<Datum>).txt (NEU, optional)
werden NICHT lokal erwartet, sondern bei Bedarf automatisch aus Google
Drive nachgeladen (siehe lade_short_dateien_von_drive) - der Short-Scanner
laeuft als eigener, frueherer Workflow (z. B. 04:00 Uhr MESZ) und teilt
sich kein lokales Dateisystem mit diesem Lauf, laedt sein Ergebnis aber
wie die anderen Scanner nach Drive hoch. Dafuer wird zusaetzlich
GDRIVE_TOKEN benoetigt (dasselbe Secret wie bei upload_to_drive.py).

Ergebnis wird nach Auswertung(<Datum>).txt geschrieben (gleicher Dateiname
wie bei claude_auswertung.py, damit upload_to_drive.py nichts anpassen
muss - beide Skripte sind austauschbar, nicht gleichzeitig laufen lassen).
"""

import os
import sys
import glob
import re
import time
import json
import datetime
import csv

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io


# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

MODELL = "gemini-3.5-flash"  # Primaer-Modell
FALLBACK_MODELL = "gemini-3.1-flash-lite"  # Fallback bei Tagesquota des Primaermodells
                              # Das Fallback hat im Free Tier ein separates, hoeheres RPD-Limit.

MAX_VERSUCHE = 5
WARTEZEIT_SEKUNDEN = 10  # Grundwartezeit fuer Sicherheitsfilter-Retries (steigt leicht an)

# NEU (30.07.2026): eigene, deutlich laengere Staffel fuer SERVERSEITIGE
# UEBERLAST (HTTP 503 "This model is currently experiencing high demand")
# und fuer Netzwerk-Abbrueche. Anlass: der Morgenlauf am 30.07. verbrannte
# alle fuenf Versuche in rund zwei Minuten (15/20/25/30/35 s), weil die alte
# Formel WARTEZEIT_SEKUNDEN + versuch*5 fuer JEDEN Fehlertyp galt. Eine
# Nachfragespitze bei einem Gratis-Modell dauert typischerweise laenger als
# zwei Minuten - fuenf Versuche in diesem Fenster sind praktisch fuenf
# Versuche im selben Moment. Exponentiell statt linear:
UEBERLAST_WARTEZEITEN = [60, 120, 240, 480]  # Sekunden, ~15 Min. Gesamtbudget
# Ein GitHub-Actions-Job darf 6 Stunden laufen, 15 Minuten sind also
# unkritisch; laenger ist trotzdem nicht sinnvoll, weil der Lauf sonst den
# ganzen Vormittag blockiert - dann lieber ein spaeterer Handstart.

ANWEISUNG_DATEI = "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"

# Gleicher Drive-Ordner wie in upload_to_drive.py - dort landen alle
# Scanner-Ausgaben, von dort werden ggf. die Short-Dateien nachgeladen.
DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
BEOBACHTUNGSLISTE_DATEI = "einzel_check_beobachtung.json"

# Dateimuster fuer die Eingabedateien (glob-Muster, nimmt jeweils den
# alphabetisch letzten Treffer -> passt zu "Setups(2026-07-19).csv" etc.)
DATEIMUSTER = {
    "briefing.txt": ["briefing.txt", "Briefing(*).txt"],
    "Setups(...).csv": ["Setups(*).csv"],
    "Performance(...).csv": ["Performance(*).csv"],
    "Performance_EU(...).csv": ["Performance_EU(*).csv"],
    "Offene Positionen+Check.csv": ["Offene Positionen+Check.csv"],
    # Backend-Fallback: Der Tracker benötigt die alte Rohdatei weiterhin für
    # Positionsfelder, die bewusst NICHT Teil der festgelegten Check-Struktur
    # sind (z.B. Stop/TP/Richtung/Ideen_Quelle). Sie ist keine technische
    # Quelle; die technische Wahrheit kommt ausschließlich aus der Check-Datei.
    "Offene_Positionen.csv": ["Offene_Positionen.csv", "Offene_Positionen(*).csv"],
    "Trendwende_Setups(...).csv": ["Trendwende_Setups(*).csv"],
    "Trendwende_Briefing(...).txt": ["Trendwende_Briefing(*).txt"],
    # NEU (24.07.2026): zuerst LOKAL suchen - falls short_scan_catchup.py in
    # main.yml den Short-Scan gerade selbst nachgeholt hat (weil short_check.yml
    # heute nicht gefeuert hat), liegen diese Dateien schon lokal vor und
    # muessen nicht extra von Drive geholt werden (siehe sammle_eingabedateien).
    "Short_Setups(...).csv": ["Short_Setups(*).csv"],
    "Short_Briefing(...).txt": ["Short_Briefing(*).txt"],
    "Einzel_Check_Aufstiege(...).txt": ["Einzel_Check_Aufstiege(*).txt"],
    "Edelmetalle_Setups(...).csv": ["Edelmetalle_Setups(*).csv"],
    "Edelmetalle_Briefing(...).txt": ["Edelmetalle_Briefing(*).txt"],
    # NEU 16.08.2026: separates Makro-Datenpaket fuer die mehrhorizontige
    # Zukunftsszenarioanalyse; rein informativ, keine bestehende Trading-Logik.
    "Makro_Briefing(...).txt": ["Makro_Briefing(*).txt"],
    # NEU: Live-Benchmark gegen MSCI World; wird als verbindlicher
    # Datenblock an Gemini uebergeben.
    "Benchmark_Live.txt": ["Benchmark_Live.txt"],
}
# Diese Dateien MUESSEN vorhanden sein, sonst wird abgebrochen. Offene
# Positionen und die beiden Trendwende-Dateien sind optional (siehe
# Abschnitt 7 der Anleitung, die genau diesen Fall vorsieht).
PFLICHT_DATEIEN = {
    "briefing.txt",
    "Setups(...).csv",
    "Performance(...).csv",
    "Performance_EU(...).csv",
    "Offene Positionen+Check.csv",
}

# Ablehnungs-Muster, die einen automatischen Retry ausloesen
# (Kleinschreibung, Substring-Suche im Antworttext)
ABLEHNUNGS_MUSTER = [
    "ich bin nur ein sprachmodell",
    "als sprachmodell kann ich",
    "kann ich in diesem fall nicht helfen",
    "kann ich bei dieser sache nicht helfen",
    "verfüge nicht über die möglichkeit",
    "verfuege nicht ueber die moeglichkeit",
]


# ---------------------------------------------------------------------------
# HILFSFUNKTIONEN
# ---------------------------------------------------------------------------

def get_drive_service():
    """Baut den Drive-Service auf (lesender Zugriff) - identische Auth-Logik
    wie in upload_to_drive.py, damit Refresh-Fehler konsistent behandelt
    werden. Gibt None zurueck (statt zu crashen), falls GDRIVE_TOKEN fehlt
    oder ungueltig ist - das Nachladen der Short-Dateien ist optional, ein
    fehlendes/kaputtes Token darf die eigentliche Gemini-Auswertung nicht
    verhindern."""
    token_str = os.environ.get("GDRIVE_TOKEN")
    if not token_str:
        print("INFO: GDRIVE_TOKEN nicht gesetzt - Short-Dateien werden nicht nachgeladen.")
        return None

    try:
        token_data = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_data)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("WARNUNG: GDRIVE_TOKEN ungueltig, kein Refresh moeglich - Short-Dateien werden uebersprungen.")
                return None
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"WARNUNG: Drive-Verbindung fuer Short-Dateien fehlgeschlagen ({e}) - wird uebersprungen.")
        return None


def lade_short_dateien_von_drive():
    """Sucht im Drive-Ordner nach den heutigen Short_Setups(...).csv und
    Short_Briefing(...).txt (vom separaten, frueheren Short-Scan-Workflow
    hochgeladen) und laedt sie lokal herunter, falls vorhanden. Gibt ein
    Dict {name: lokaler_pfad} zurueck - leer, wenn nichts gefunden wurde
    oder Drive nicht erreichbar ist (kein Fehler, einfach optional)."""
    service = get_drive_service()
    if service is None:
        return {}

    heute = datetime.date.today().isoformat()
    gefunden = {}

    for name_praefix, ziel_key, lokaler_name in [
        ("Short_Setups", "Short_Setups(...).csv", f"Short_Setups({heute}).csv"),
        ("Short_Briefing", "Short_Briefing(...).txt", f"Short_Briefing({heute}).txt"),
    ]:
        try:
            query = (
                f"name contains '{name_praefix}' and name contains '{heute}' "
                f"and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
            )
            ergebnis = service.files().list(q=query, fields="files(id, name)").execute()
            treffer = ergebnis.get("files", [])
            if not treffer:
                print(f"INFO: Keine {name_praefix}-Datei fuer heute ({heute}) in Drive gefunden - Short-Kategorie entfaellt heute.")
                continue

            datei_id = treffer[0]["id"]
            request = service.files().get_media(fileId=datei_id)
            with io.FileIO(lokaler_name, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                fertig = False
                while not fertig:
                    _, fertig = downloader.next_chunk()
            print(f"INFO: {treffer[0]['name']} von Drive nachgeladen -> {lokaler_name}")
            gefunden[ziel_key] = lokaler_name
        except Exception as e:
            print(f"WARNUNG: Nachladen von {name_praefix} fehlgeschlagen ({e}) - wird uebersprungen.")

    return gefunden


def analysiere_api_fehler(fehlertext):
    """NEU (24.07.2026): unterscheidet, ob ein Retry ueberhaupt sinnvoll ist.
    Bei einem TAGES-Kontingent (z. B. quotaId
    'GenerateRequestsPerDayPerProjectPerModel-FreeTier') ist ein Retry am
    selben Tag zwecklos - das Limit resettet erst am naechsten Tag, alle
    weiteren Versuche wuerden nur denselben Fehler wiederholen und den Lauf
    unnoetig in die Laenge ziehen. Bei anderen 429ern (z. B. Anfragen pro
    Minute) oder 503 (kurzzeitige Ueberlastung) IST ein Retry sinnvoll -
    Google liefert dafuer meist ein 'retryDelay' in der Fehlerantwort mit,
    das genauer ist als unsere pauschale WARTEZEIT_SEKUNDEN-Formel.

    ERWEITERT (30.07.2026): unterscheidet zusaetzlich die serverseitige
    UEBERLAST (503 UNAVAILABLE) und Netzwerk-Abbrueche von den uebrigen
    Retry-Faellen, weil diese eine viel laengere Wartezeit brauchen (siehe
    UEBERLAST_WARTEZEITEN oben).
    Gibt (abbrechen: bool, empfohlene_wartezeit_sekunden: float|None,
    kategorie: str) zurueck. Kategorien: "tageskontingent", "ueberlast",
    "netzwerk", "sonstiges"."""
    ist_tages_kontingent = "PerDay" in fehlertext
    if ist_tages_kontingent:
        return True, None, "tageskontingent"

    treffer = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", fehlertext)
    empfohlene_wartezeit = float(treffer.group(1)) if treffer else None

    text_klein = fehlertext.lower()
    if "503" in fehlertext or "unavailable" in text_klein or "high demand" in text_klein:
        return False, empfohlene_wartezeit, "ueberlast"
    if ("connection reset" in text_klein or "connection aborted" in text_klein
            or "timed out" in text_klein or "temporarily unavailable" in text_klein):
        return False, empfohlene_wartezeit, "netzwerk"
    return False, empfohlene_wartezeit, "sonstiges"


def ist_ablehnung(text):
    if not text or not text.strip():
        return True  # leere Antwort werten wir vorsichtshalber auch als Fehlschlag
    text_klein = text.lower()
    return any(muster in text_klein for muster in ABLEHNUNGS_MUSTER)


def lade_beobachtungsliste_von_drive():
    """Lädt die persistente Beobachtungsliste des Einzel-Checks aus Drive.

    Die Liste wird vom separaten manuellen einzel_check.yml-Workflow
    aktualisiert. Fehlt die Datei oder ist Drive nicht erreichbar, wird
    bewusst eine leere Liste geliefert: Die Tagesauswertung darf dadurch
    nicht ausfallen.
    """
    service = get_drive_service()
    if service is None:
        return None

    try:
        query = (
            f"name = '{BEOBACHTUNGSLISTE_DATEI}' "
            f"and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
        )
        ergebnis = service.files().list(
            q=query, fields="files(id, name, modifiedTime)", orderBy="modifiedTime desc"
        ).execute()
        treffer = ergebnis.get("files", [])
        if not treffer:
            print(
                "INFO: Keine Einzel-Check-Beobachtungsliste in Drive gefunden "
                "- Abschnitt wird als leer ausgegeben."
            )
            return {}

        datei_id = treffer[0]["id"]
        request = service.files().get_media(fileId=datei_id)
        lokaler_pfad = BEOBACHTUNGSLISTE_DATEI
        with io.FileIO(lokaler_pfad, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            fertig = False
            while not fertig:
                _, fertig = downloader.next_chunk()

        with open(lokaler_pfad, "r", encoding="utf-8") as f:
            daten = json.load(f)

        if not isinstance(daten, dict):
            print("WARNUNG: Einzel-Check-Beobachtungsliste ist kein JSON-Objekt - leer verwendet.")
            return {}

        print(
            f"INFO: {BEOBACHTUNGSLISTE_DATEI} aus Drive geladen "
            f"({len(daten)} beobachtete Titel)."
        )
        return daten

    except Exception as e:
        print(
            f"WARNUNG: Einzel-Check-Beobachtungsliste konnte nicht aus Drive "
            f"geladen werden ({e}) - Abschnitt wird als leer ausgegeben."
        )
        return None


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

    # Die Einzel-Check-Beobachtungsliste gehört nicht zu den Pflichtdateien.
    # Falls sie im frischen main.yml-Runner noch nicht lokal liegt, wird sie
    # aus Drive nachgeladen und als normale Gemini-Eingabedatei bereitgestellt.
    if gefunden.get("Einzel-Check-Beobachtungsliste") is None:
        daten = lade_beobachtungsliste_von_drive()
        if daten is not None:
            gefunden["Einzel-Check-Beobachtungsliste"] = BEOBACHTUNGSLISTE_DATEI

    if "Einzel-Check-Beobachtungsliste" not in gefunden:
        gefunden["Einzel-Check-Beobachtungsliste"] = None

    fehlend = [n for n in PFLICHT_DATEIEN if gefunden.get(n) is None]
    if fehlend:
        print(f"FEHLER: Pflichtdateien nicht gefunden: {fehlend}")
        sys.exit(1)

    # Short-Dateien: DATEIMUSTER oben hat sie bereits lokal gesucht (Fall:
    # short_scan_catchup.py hat sie in main.yml gerade selbst erzeugt). NUR
    # falls lokal nichts gefunden wurde, zusaetzlich per Drive nachladen
    # (Normalfall: separater frueher short_check.yml-Lauf war erfolgreich).
    # Lokaler Fund hat Vorrang, damit ein frisch nachgeholter Lauf nicht
    # versehentlich durch eine aeltere Drive-Version ersetzt wird.
    if gefunden.get("Short_Setups(...).csv") is None or gefunden.get("Short_Briefing(...).txt") is None:
        for key, pfad in lade_short_dateien_von_drive().items():
            if gefunden.get(key) is None:
                gefunden[key] = pfad

    print("Gefundene Eingabedateien:")
    for name, pfad in gefunden.items():
        print(f"  - {name}: {pfad if pfad else '(nicht vorhanden, wird uebersprungen)'}")

    return {k: v for k, v in gefunden.items() if v is not None}


def lade_anweisung():
    if not os.path.isfile(ANWEISUNG_DATEI):
        print(f"FEHLER: Anweisungs-Datei nicht gefunden: {ANWEISUNG_DATEI}")
        sys.exit(1)
    with open(ANWEISUNG_DATEI, "r", encoding="utf-8-sig") as f:
        return f.read()


# ---------------------------------------------------------------------------
# HAUPTLOGIK
# ---------------------------------------------------------------------------

def gemini_auswertung_starten():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("FEHLER: Umgebungsvariable GEMINI_API_KEY nicht gesetzt.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    anweisung = lade_anweisung()
    eingabedateien = sammle_eingabedateien()

    letzte_antwort = None
    hochgeladene_teile = None  # wird bei Bedarf (neu) befuellt, siehe unten
    aktuelles_modell = MODELL

    # Harte Datenqualitaetskontrolle fuer Punkt 2: Der Makro-Block darf nur
    # dann numerische Base/Bull/Bear-Wahrscheinlichkeiten erzeugen, wenn
    # makro_szenario.py den Gatekeeper freigegeben hat. Die restliche
    # Tagesauswertung bleibt davon unabhaengig.
    # Ausfall oder fehlende Makro-Datei = harte Sperre. Das verhindert, dass
    # Gemini aus den übrigen Markt-/Setup-Dateien trotzdem ein scheinbar
    # quantitatives Makro-Szenario konstruiert.
    makro_gate = "GESPERRT"
    makro_gate_grund = "Makro-Datenpaket fehlt oder konnte nicht verifiziert werden."
    makro_pfad = eingabedateien.get("Makro_Briefing(...).txt")
    if makro_pfad:
        try:
            with open(makro_pfad, "r", encoding="utf-8-sig") as f:
                makro_text = f.read()
            m = re.search(r"MAKRO-SZENARIO-GATE:\s*(FREIGEGEBEN|GESPERRT)", makro_text)
            if m:
                makro_gate = m.group(1)
                makro_gate_grund = "Gate aus Makro-Datenpaket übernommen."
            else:
                makro_gate = "GESPERRT"
                makro_gate_grund = "Makro-Datei vorhanden, aber Gate nicht eindeutig verifiziert."
            print(f"Makro-Szenario-Gate: {makro_gate} | Grund: {makro_gate_grund}")
        except Exception as exc:
            makro_gate = "GESPERRT"
            makro_gate_grund = f"Makro-Gate konnte nicht gelesen werden: {exc}"
            print(f"WARNUNG: {makro_gate_grund}")
    else:
        print(f"WARNUNG: {makro_gate_grund}")

    for versuch in range(1, MAX_VERSUCHE + 1):
        print(f"\nVersuch {versuch}/{MAX_VERSUCHE}...")

        try:
            # GEAENDERT (30.07.2026): Dateien werden nur hochgeladen, wenn
            # noch keine Upload-Referenzen vorliegen. Der frische Upload ist
            # Teil der Retry-Strategie gegen die nicht-deterministischen
            # SICHERHEITSFILTER-Ablehnungen (neue "Sitzung", neuer Kontext) -
            # bei einem technischen Fehler wie 503 ist er dagegen sinnlos:
            # die Anfrage hat das Modell nie erreicht. Vorher wurden bei
            # jedem 503-Retry alle elf Dateien erneut hochgeladen, was den
            # Lauf verlaengert hat, ohne etwas zu verbessern.
            if hochgeladene_teile is None:
                hochgeladene_teile = [
                    client.files.upload(file=pfad)
                    for pfad in eingabedateien.values()
                    if pfad
                ]

            antwort = client.models.generate_content(
                model=aktuelles_modell,
                contents=hochgeladene_teile + [
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben "
                    "und erstelle die vollstaendige Daten-Uebersicht. "
                    "WICHTIGE QUELLE FUER OFFENE POSITIONEN: Verwende fuer den Abschnitt "
                    "Offene Positionen ausschliesslich die Datei 'Offene Positionen+Check.csv'. "
                    "Ihre technischen Check-Felder sind die verbindliche Quelle fuer "
                    "Technischer_Zustand, Trendrichtung, Support/Widerstand, Breakout_Status, "
                    "A-B-C_Status, Fibonacci_Status/Ziele, Trendkanal, Measured Move, Formation, "
                    "Round Number, Major Resistance, Ueberdehnung, Relative Staerke_Sektor, "
                    "Konfluenz, Retest_Support, Technische_Zielzone, Datenqualitaet und Analysehinweis. "
                    "Eine alte Offene_Positionen.csv-Datei darf ausschließlich als "
                    "Backend-Fallback für Positionsfelder verwendet werden, die in der "
                    "festgelegten Check-Struktur nicht enthalten sind (z.B. Stop, TP1, TP2, "
                    "Richtung, Ideen_Quelle, Einstiegsdatum). Sie darf niemals technische "
                    "Check-Werte ersetzen oder widersprechen.",
                    (
                        f"HARTE MAKRO-GATE-VORGABE: Das Makro-Szenario-Gate ist GESPERRT. Grund: {makro_gate_grund} "
                        "Erzeuge in Punkt 2 KEINE Base/Bull/Bear-Wahrscheinlichkeiten, "
                        "keine geschaetzten Ersatzwerte und keine numerischen Makro-Prognosen. "
                        "Benenne stattdessen die konkreten kritischen Datenluecken bzw. den Ausfall des Makro-Datenpakets. "
                         "Verwende dabei NICHT die Bezeichnungen Base Case, Bull Case oder Bear Case, gib KEINE Makro-Trade-Ideen und KEINE qualitative Richtungsprognose aus. "
                        if makro_gate == "GESPERRT" else
                        "HARTE MAKRO-DATENREGEL: Verwende ausschliesslich REAL- oder "
                        "CALCULATED-Werte aus dem Makro-Datenpaket. PROXY-Werte muessen als "
                        "Proxy bezeichnet werden. MODEL_DERIVED-Wahrscheinlichkeiten sind "
                        "nur als Ergebnis der Szenariologik zulaessig; niemals Eingangsdaten schaetzen."
                    ),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=anweisung,
                ),
            )
            text = antwort.text or ""

        except Exception as e:
            fehlertext = str(e)
            print(f"  Technischer Fehler beim API-Call: {e}")
            letzte_antwort = f"[Technischer Fehler] {e}"

            abbrechen, empfohlene_wartezeit, kategorie = analysiere_api_fehler(fehlertext)
            if abbrechen:
                # Das RPD-Free-Tier-Limit ist modellbezogen. Wenn das
                # Primaermodell sein Tageskontingent erreicht hat, wechseln
                # wir genau einmal auf das definierte Fallback-Modell.
                # Ist auch dessen Tageskontingent erschoepft, gibt es keinen
                # weiteren sinnvollen Retry am selben Tag.
                if aktuelles_modell == MODELL and FALLBACK_MODELL and FALLBACK_MODELL != MODELL:
                    aktuelles_modell = FALLBACK_MODELL
                    print(
                        f"  Tages-Kontingent von {MODELL} erschoepft "
                        "(429 RESOURCE_EXHAUSTED, PerDay). "
                        f"Wechsle fuer diesen Lauf auf Fallback-Modell {FALLBACK_MODELL}."
                    )
                    continue

                print(
                    f"  Tages-Kontingent des Gemini-Free-Tiers fuer {aktuelles_modell} ist erschoepft "
                    "(429 RESOURCE_EXHAUSTED, quotaId enthaelt 'PerDay'). "
                    f"Auch das Fallback-Modell kann heute nicht weiter verwendet werden; "
                    f"breche ab statt die restlichen {MAX_VERSUCHE - versuch} Versuche zu verbrennen. "
                    "Naechster sinnvoller Versuch nach dem taeglichen Reset oder mit erweitertem Tier."
                )
                sys.exit(2)

            if kategorie in ("ueberlast", "netzwerk"):
                # Serverseitige Ueberlast/Netzwerkabbruch: lange, exponentiell
                # steigende Pause (siehe UEBERLAST_WARTEZEITEN). Ein von Google
                # mitgeliefertes retryDelay wird beachtet, aber nur wenn es
                # LAENGER ist - kuerzer waere hier kontraproduktiv.
                staffel_index = min(versuch - 1, len(UEBERLAST_WARTEZEITEN) - 1)
                wartezeit = UEBERLAST_WARTEZEITEN[staffel_index]
                if empfohlene_wartezeit is not None:
                    wartezeit = max(wartezeit, empfohlene_wartezeit)
                grund = ("Modell ueberlastet (503)" if kategorie == "ueberlast"
                         else "Netzwerk-Abbruch")
                print(f"  {grund} - warte {wartezeit:.0f}s (lange Staffel, "
                      f"Versuch {versuch}/{MAX_VERSUCHE})...")
            else:
                wartezeit = (empfohlene_wartezeit if empfohlene_wartezeit is not None
                             else WARTEZEIT_SEKUNDEN + versuch * 5)
                print(f"  Warte {wartezeit:.0f}s vor dem naechsten Versuch...")
            time.sleep(wartezeit)
            continue

        if ist_ablehnung(text):
            print("  Sicherheitsfilter-Ablehnung erkannt (oder leere Antwort) - neuer Versuch...")
            print(f"  Antwort war: {text[:200]!r}")
            letzte_antwort = text
            # NUR hier neu hochladen: frischer Kontext ist genau das Mittel
            # gegen diese Art von Ablehnung (siehe Kommentar oben).
            hochgeladene_teile = None
            time.sleep(WARTEZEIT_SEKUNDEN + versuch * 5)
            continue

        print(f"  Erfolgreich mit {aktuelles_modell}!")
        # Strukturelle Vollstaendigkeitspruefung vor dem Speichern. Wenn
        # Punkt 8 unvollstaendig ist, wird NUR dieser Abschnitt einmalig
        # repariert; der restliche Gemini-Output bleibt unveraendert.
        erwartete_namen = _lese_offene_positionen_namen(eingabedateien.get("Offene Positionen+Check.csv"))
        text, vollstaendig, fehlend = _sichere_regionen_und_offene_positionen(text, eingabedateien)
        if not vollstaendig and erwartete_namen:
            raise RuntimeError(
                "Auswertung strukturell unvollstaendig: fehlende/duplizierte offene Positionen: "
                + ", ".join(fehlend or ["unvollstaendiger Offene-Positionen-Block"])
            )
        return text

    print(f"\nFEHLER: Nach {MAX_VERSUCHE} Versuchen weiterhin keine gueltige Antwort.")
    print(f"Letzte Antwort/Fehler:\n{letzte_antwort}")
    sys.exit(1)




def _lese_offene_positionen_namen(csv_pfad):
    """Liest alle offenen Positionslots aus Offene Positionen+Check.csv.
    Die Positionsidentitaet besteht aus Name + Ticker + Einstieg + Einstiegsdatum.
    Es wird niemals nach Ticker oder Name dedupliziert."""
    if not csv_pfad or not os.path.isfile(csv_pfad):
        return []
    try:
        with open(csv_pfad, "r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(8192)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            reader = csv.DictReader(f, dialect=dialect)
            fields = reader.fieldnames or []

            def key_for(*candidates):
                return next((k for k in fields if str(k).strip().lower() in candidates), None)

            name_key = key_for("name", "firmenname", "unternehmen", "company", "titel")
            status_key = key_for("status", "position_status")
            ticker_key = key_for("ticker")
            entry_key = key_for("einstieg", "einstiegskurs", "entry", "entry_price")
            date_key = key_for("einstiegsdatum", "kaufdatum", "einstiegsdatum_utc", "entry_date", "date")

            if not entry_key or not date_key:
                raise ValueError(
                    "Offene Positionen+Check.csv muss 'Einstieg' und 'Einstiegsdatum' enthalten."
                )

            result = []
            for row in reader:
                status = str(row.get(status_key, "") if status_key else "").strip().lower()
                if status and status not in {"offen", "open"}:
                    continue

                name = str(row.get(name_key, "") if name_key else "").strip()
                ticker = str(row.get(ticker_key, "") if ticker_key else "").strip()
                entry = str(row.get(entry_key, "") or "").strip()
                entry_date = str(row.get(date_key, "") or "").strip()

                if name or ticker:
                    result.append({
                        "name": name,
                        "ticker": ticker,
                        "entry": entry,
                        "entry_date": entry_date,
                    })
            return result
    except Exception as exc:
        print(f"WARNUNG: Offene-Positionen-Vollstaendigkeitscheck nicht lesbar: {exc}")
        return []


def _finde_offene_positionen_abschnitt(text):
    """Findet den kompletten offenen-Positionen-Abschnitt ohne andere
    Auswertungsteile anzutasten."""
    m = re.search(r"(?ims)^(?P<head>\s*(?:\d+\.\s*)?offene positionen\s*)$", text)
    if not m:
        return None
    start = m.start()
    tail = text[m.end():]
    nxt = re.search(r"(?im)^\s*(?:\d+\.\s*)?(?:geschlossene positionen|gestoppte positionen|methodik(?: &| und)? lesehilfe)\s*$", tail)
    end = m.end() + (nxt.start() if nxt else len(tail))
    return start, end, m.group('head').strip()


def _offene_positionen_vollstaendig(text, erwartete_positionen):
    """Prueft die offenen Positionen anhand eindeutiger Positionslots.

    Positionsschluessel:
      Name + Ticker + Einstiegskurs + Einstiegsdatum

    Mehrere offene Positionen desselben Tickers sind ausdruecklich erlaubt.
    Die technische Basis ist ausschliesslich Offene Positionen+Check.csv.
    Der Parser akzeptiert:
      1) Firmenname (TICKER) | Markt: ...
      2) TICKER | Firmenname | Markt: ...

    Fuer jeden ausgegebenen Block werden Einstieg und Datum aus dem jeweiligen
    Positionsblock gelesen; Gemini liefert keine technischen Berechnungen.
    """
    if not erwartete_positionen:
        return True, []

    abschnitt = _finde_offene_positionen_abschnitt(text)
    if not abschnitt:
        return False, [
            (p.get("name") or p.get("ticker") or "Unbekannte Position")
            for p in erwartete_positionen
        ]

    block = text[abschnitt[0]:abschnitt[1]]

    def norm_name(value):
        value = str(value or "").lower().strip()
        value = re.sub(r"[^a-z0-9äöüß]+", " ", value)
        value = re.sub(
            r"\b(ag|se|sa|plc|inc|corp|corporation|limited|ltd|nv|spa|srl|"
            r"holding|holdings|company|co|group)\b", " ", value
        )
        return re.sub(r"\s+", " ", value).strip()

    def norm_ticker(value):
        return re.sub(r"[^a-z0-9.=-]+", "", str(value or "").lower())

    def norm_price(value):
        s = str(value or "").strip()
        s = re.sub(r"[^0-9,.\-]+", "", s)
        if not s:
            return ""
        # Deutsche Schreibweise 1.234,56; US-Schreibweise 1234.56
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return f"{float(s):.8f}".rstrip("0").rstrip(".")
        except ValueError:
            return s

    def norm_date(value):
        s = str(value or "").strip()
        if not s:
            return ""
        m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", s)
        if m:
            return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return s.lower()

    def name_match(head, name):
        if not name:
            return False
        return (
            head == name
            or head.startswith(name + " ")
            or name.startswith(head + " ")
            or (len(name) >= 4 and name in head)
            or (len(head) >= 4 and head in name)
        )

    # Positionskoepfe und ihre kompletten Bloecke bestimmen.
    header_matches = list(re.finditer(r"(?im)^\s*(.*?)\s*\|\s*Markt\s*:", block))
    parsed_blocks = []
    for idx, match in enumerate(header_matches):
        end = header_matches[idx + 1].start() if idx + 1 < len(header_matches) else len(block)
        raw_head = match.group(1).strip()
        body = block[match.start():end]
        parsed_blocks.append((raw_head, body))

    def parse_head(raw_head):
        raw = str(raw_head).strip()
        paren = [norm_ticker(x) for x in re.findall(r"\(([^()]+)\)", raw) if norm_ticker(x)]
        if paren:
            ticker = paren[0]
            visible_name = norm_name(re.sub(r"\([^()]+\)", " ", raw, count=1))
            return visible_name, ticker

        parts = [x.strip() for x in raw.split("|") if x.strip()]
        if len(parts) >= 2:
            ticker = norm_ticker(parts[0])
            visible_name = norm_name(parts[1])
            return visible_name, ticker
        return norm_name(raw), ""

    def extract_entry_date(body):
        # Unterstützt z.B. "Einstieg: 131,58 $ (19.08.2026)" sowie
        # getrennte Angaben "Einstieg: ..." und "Einstiegsdatum: ...".
        entry = ""
        date = ""
        m = re.search(r"(?im)\bEinstieg(?:skurs)?\s*:\s*([^\n|;()]+)", body)
        if m:
            entry = m.group(1).strip()
        m = re.search(r"(?im)\bEinstiegsdatum\b\s*:\s*([^\n|;]+)", body)
        if m:
            date = m.group(1).strip()
        if not date:
            # Datum in Klammern direkt hinter dem Einstiegskurs.
            m = re.search(
                r"(?im)\bEinstieg(?:skurs)?\s*:\s*[^\n|]*?\(\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{1,2}-\d{1,2})\s*\)",
                body,
            )
            if m:
                date = m.group(1)
        return norm_price(entry), norm_date(date)

    expected_keys = []
    labels = {}
    for pos in erwartete_positionen:
        key = (
            norm_name(pos.get("name")),
            norm_ticker(pos.get("ticker")),
            norm_price(pos.get("entry")),
            norm_date(pos.get("entry_date")),
        )
        expected_keys.append(key)
        labels[key] = (
            f"{pos.get('name') or pos.get('ticker') or 'Unbekannte Position'} "
            f"({str(pos.get('ticker') or '').upper()})"
        )

    actual_keys = []
    unmatched_headers = []
    for raw_head, body in parsed_blocks:
        name, ticker = parse_head(raw_head)
        entry, date = extract_entry_date(body)
        matched = False
        for expected in expected_keys:
            ename, eticker, eentry, edate = expected
            if ticker == eticker and name_match(name, ename):
                actual_keys.append((ename, eticker, entry, date))
                matched = True
                break
        if not matched:
            unmatched_headers.append(raw_head)

    from collections import Counter
    expected_counts = Counter(expected_keys)
    actual_counts = Counter(actual_keys)

    fehlend = []
    for key, count in expected_counts.items():
        have = actual_counts.get(key, 0)
        if have < count:
            fehlend.extend([labels.get(key, key[0] or key[1] or "Unbekannte Position")] * (count - have))
        elif have > count:
            fehlend.append(
                f"{labels.get(key, key[0] or key[1] or 'Unbekannte Position')} "
                f"(zu viele Ausgabe-Blöcke: {have} statt {count})"
            )

    # Ein nicht zuordenbarer Positionskopf ist ebenfalls ein Strukturfehler.
    if unmatched_headers:
        fehlend.extend([f"Nicht zuordenbarer Positionsblock: {h}" for h in unmatched_headers])

    if re.search(r"(?i)weitere positionen|weitere offene positionen", block):
        return False, fehlend or ["Platzhalter 'Weitere Positionen'"]

    return not fehlend, fehlend


def _extrahiere_regionen_block(briefing_pfad):
    """Uebernimmt den vom analyse.py erzeugten REGIONEN-PERFORMANCE-Block
    wörtlich und macht daraus den sichtbaren Abschnitt 'Blick auf wichtige Indizes'."""
    if not briefing_pfad or not os.path.isfile(briefing_pfad):
        return ""
    try:
        with open(briefing_pfad, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except Exception as exc:
        print(f"WARNUNG: Regionenblock konnte nicht gelesen werden: {exc}")
        return ""
    m = re.search(r"(?ms)^REGIONEN-PERFORMANCE.*?(?=^BENCHMARKS\s*$)", text)
    if not m:
        return ""
    block = m.group(0).strip()
    block = re.sub(r"^REGIONEN-PERFORMANCE[^\n]*$", "Blick auf wichtige Indizes", block, count=1, flags=re.M)
    return block.strip() + "\n"


def _sichere_regionen_und_offene_positionen(text, eingabedateien):
    """Repariert ausschliesslich strukturelle Auslassungen. Markt-/Technik-
    Inhalte werden nicht neu berechnet oder umformuliert."""
    regionen = _extrahiere_regionen_block(eingabedateien.get("briefing.txt"))
    if regionen and not re.search(r"(?im)^\s*Blick auf wichtige Indizes\s*$", text):
        # Direkt nach dem Deckblatt, niemals vor Titel/Datum/Untertitel.
        head = re.search(r"(?ms)^(Neuber Macro & Markets\s*\nDatum der Auswertung:.*?\nTägliche Markt- und Setup-Auswertung\s*\n?)", text)
        if head:
            text = text[:head.end()] + "\n" + regionen + "\n" + text[head.end():].lstrip("\n")
            print("STRUKTUR-FIX: 'Blick auf wichtige Indizes' aus briefing.txt wiederhergestellt.")

    erwartete = _lese_offene_positionen_namen(eingabedateien.get("Offene Positionen+Check.csv"))
    ok, fehlend = _offene_positionen_vollstaendig(text, erwartete)
    return text, ok, fehlend


def _a_aufstiege_block():
    """Liest das tagesaktuelle A-Aufstiegsprotokoll und baut den
    sichtbaren Unterabschnitt fuer Auswertung.txt deterministisch auf.

    Wichtig: Die separate Einzel_Check_Aufstiege-Datei bleibt die Quelle
    fuer das Ereignis. Hier wird sie nur zusaetzlich in die zentrale
    Tagesauswertung gespiegelt; es werden keine historischen Daten geloescht.
    """
    pfad = finde_datei(["Einzel_Check_Aufstiege(*).txt"])
    if not pfad or not os.path.isfile(pfad):
        return ""

    try:
        with open(pfad, "r", encoding="utf-8-sig") as f:
            inhalt = f.read().strip()
    except OSError:
        return ""

    if not inhalt:
        return ""

    zeilen = inhalt.splitlines()
    # Kopfzeilen des Ereignisprotokolls nicht doppelt ausgeben.
    eintraege = []
    for zeile in zeilen:
        z = zeile.strip()
        if not z or z.upper().startswith("EINZEL-CHECK:") or set(z) <= {"="}:
            continue
        eintraege.append(z)

    if not eintraege:
        return ""

    block = [
        "EINZEL-CHECK – A-AUFSTIEGE",
        "",
        "🟢 NEUE KAUFKANDIDAT-A-AUFSTIEGE",
        "",
    ]
    for eintrag in eintraege:
        block.append(f"• {eintrag}")
        block.append("")
    return "\n".join(block).rstrip() + "\n"


def normalisiere_ausgabe(text):
    """Erzwingt formale Layoutregeln und spiegelt echte B/C -> A-Aufstiege
    direkt unter die Einzel-Check-Beobachtungsliste.

    Die A-Aufstiegsdatei bleibt dabei unveraendert als separate Ereignisquelle.
    Wenn Gemini den Abschnitt bereits erzeugt hat, wird er nicht dupliziert.
    """
    if not text:
        return text
    text = re.sub(r"(?m)^[ \t]*(Was muesste technisch passieren, damit das bestehende Setup-System einen konkreten Einstieg bestaetigt\?:)", r"\n\1", text)
    text = re.sub(r"\n{3,}(?=Was muesste technisch passieren, damit das bestehende Setup-System einen konkreten Einstieg bestaetigt\?:)", "\n\n", text)

    # Gemini ist weiterhin fuer die inhaltliche Auswertung verantwortlich.
    # Der bereits separat erzeugte A-Aufstiegs-Report wird hier zusaetzlich
    # deterministisch an der gewuenschten Stelle eingeblendet, damit ein
    # einzelnes Auslassen durch das Sprachmodell nicht zu einer unsichtbaren
    # Meldung in der zentralen Auswertung fuehrt.
    if "Neue KAUFKANDIDAT-A-Aufstiege" not in text and "EINZEL-CHECK – A-AUFSTIEGE" not in text:
        block = _a_aufstiege_block()
        if block:
            marker = re.search(r"(?m)^6\. SHORT-SETUPS\s*$", text)
            if marker:
                text = text[:marker.start()].rstrip() + "\n\n" + block + "\n\n" + text[marker.start():]
            else:
                # Fallback: direkt nach dem Beobachtungslisten-Block.
                marker = re.search(r"(?m)^Einzel-Check-Beobachtungsliste\s*$", text)
                if marker:
                    naechster = re.search(r"(?m)^\d+\. ", text[marker.end():])
                    pos = marker.end() + (naechster.start() if naechster else len(text[marker.end():]))
                    text = text[:pos].rstrip() + "\n\n" + block + "\n\n" + text[pos:].lstrip()
                else:
                    # Sicherheits-Fallback: nichts einfuegen, wenn die
                    # erwartete Struktur nicht erkennbar ist.
                    pass

    return text


def speichere_ergebnis(text):
    heute = datetime.date.today().isoformat()
    ausgabe_datei = f"Auswertung({heute}).txt"
    text = normalisiere_ausgabe(text)
    with open(ausgabe_datei, "w", encoding="utf-8-sig") as f:
        f.write(text)
    print(f"\nGespeichert: {ausgabe_datei}")
    return ausgabe_datei


if __name__ == "__main__":
    print("Gemini-Auswertung gestartet...")
    ergebnis_text = gemini_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(ergebnis_text)
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
