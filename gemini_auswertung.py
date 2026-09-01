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
import csv
import time
import json
import datetime

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
FALLBACK_MODELL = "gemini-3.1-flash-lite"  # Erster Fallback
DRITTER_FALLBACK_MODELL = "gemini-2.5-flash"  # Zweiter Fallback bei 503-Ueberlast
                              # Das dritte Modell wird nur verwendet, wenn auch der erste
                              # Fallback weiterhin serverseitig ueberlastet ist.

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
UEBERLAST_WARTEZEITEN = [30, 60, 60, 60]  # Sekunden; kurze Staffel vor dem Fallback
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
    # Qualitative externe YouTube-Marktquellen; niemals technische/CRV-Werte ersetzen.
    "Bitcoin_Trading_DE_Briefing.txt": ["Bitcoin_Trading_DE_Briefing.txt"],
    "Gold_Trading_DE_Briefing.txt": ["Gold_Trading_DE_Briefing.txt"],
    "Silber_Trading_DE_Briefing.txt": ["Silber_Trading_DE_Briefing.txt"],
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



def _positionsfeld_schluessel(value):
    """Normalisiert nur die vier Felder des eindeutigen Positionsschluessels,
    damit z.B. 66,32 und 66,32$ dieselbe Position referenzieren."""
    text = str(value or "").strip()
    text = text.replace("€", "").replace("$", "").replace("£", "")
    date_match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if date_match:
        a, b, y = date_match.groups()
        if len(a) == 4:
            return f"{a}-{b.zfill(2)}-{y.zfill(2)}"
        return f"{y}-{b.zfill(2)}-{a.zfill(2)}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", text):
        return text[:10]
    text = text.replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return f"{float(text):.12g}"
    except Exception:
        return text.lower()


def _offene_positionen_quellblock(csv_pfad):
    """Erstellt eine unveränderte, autoritative Positionsliste aus der Check-Datei.
    Nur Name/Ticker/Einstieg/Einstiegsdatum werden hier als Stammdaten vorgegeben."""
    if not csv_pfad or not os.path.isfile(csv_pfad):
        return ""
    try:
        with open(csv_pfad, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            fields = reader.fieldnames or []
            def key(name):
                return next((k for k in fields if str(k).strip().lower() == name.lower()), None)
            name_k = key("Name")
            ticker_k = key("Ticker")
            status_k = key("Status")
            entry_k = key("Einstieg")
            date_k = key("Einstiegsdatum")
            if not all((name_k, ticker_k, entry_k, date_k)):
                raise ValueError("Check-Datei benötigt Name, Ticker, Einstieg und Einstiegsdatum.")
            rows = []
            for row in reader:
                status = str(row.get(status_k, "")).strip().lower() if status_k else ""
                if status and status not in {"offen", "open"}:
                    continue
                name = str(row.get(name_k, "")).strip()
                ticker = str(row.get(ticker_k, "")).strip()
                entry = str(row.get(entry_k, "")).strip()
                date = str(row.get(date_k, "")).strip()
                if name or ticker:
                    rows.append(f"- {name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}")
            return "\n".join(rows)
    except Exception as exc:
        raise RuntimeError(f"Offene Positionen+Check.csv konnte nicht als verbindliche Quelle gelesen werden: {exc}")

def _technische_zielzonen_quelle(csv_pfad):
    """Liest die technischen Check-Felder verbindlich aus der Check-Datei.

    Die Positionsidentitaet ist ausschließlich:
        Name + Ticker + Einstiegskurs + Einstiegsdatum

    Die Check-Datei ist Master. Insbesondere Technische_Zielzone wird
    ausschließlich als bereits vorhandener CSV-String übernommen.
    """
    if not csv_pfad or not os.path.isfile(csv_pfad):
        raise RuntimeError(
            "Offene Positionen+Check.csv fehlt; technische Werte koennen "
            "nicht verbindlich aus der Master-Datei uebernommen werden."
        )

    technische_felder = [
        "Technischer_Zustand",
        "Trendrichtung",
        "Support/Widerstand",
        "Breakout_Status",
        "A-B-C_Status",
        "Fibonacci_Status/Ziele",
        "Trendkanal",
        "Measured Move",
        "Formation",
        "Round Number",
        "Major Resistance",
        "Ueberdehnung",
        "Relative Staerke_Sektor",
        "Konfluenz",
        "Retest_Support",
        "Technische_Zielzone",
        "Datenqualitaet",
        "Analysehinweis",
    ]

    try:
        with open(csv_pfad, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            fields = reader.fieldnames or []

            def key(name):
                wanted = name.strip().lower()
                return next(
                    (k for k in fields if str(k).strip().lower() == wanted),
                    None,
                )

            name_k = key("Name")
            ticker_k = key("Ticker")
            status_k = key("Status")
            entry_k = key("Einstieg")
            date_k = key("Einstiegsdatum")
            technical_keys = {field: key(field) for field in technische_felder}

            missing = [
                field for field, column in [
                    ("Name", name_k),
                    ("Ticker", ticker_k),
                    ("Einstieg", entry_k),
                    ("Einstiegsdatum", date_k),
                    ("Technische_Zielzone", technical_keys["Technische_Zielzone"]),
                ]
                if not column
            ]
            if missing:
                raise ValueError(
                    "Check-Datei benoetigt folgende Felder: " + ", ".join(missing)
                )

            result = {}
            for row in reader:
                status = str(row.get(status_k, "")).strip().lower() if status_k else ""
                if status and status not in {"offen", "open"}:
                    continue

                name = str(row.get(name_k, "") or "").strip()
                ticker = str(row.get(ticker_k, "") or "").strip()
                entry = str(row.get(entry_k, "") or "").strip()
                date = str(row.get(date_k, "") or "").strip()

                if not (name or ticker):
                    continue

                # Wichtig: Der Wert der Zielzone wird NICHT normalisiert.
                # Er wird exakt so gespeichert, wie er in der CSV steht.
                technical_values = {
                    field: (
                        str(row.get(column, "") or "").strip()
                        if column else None
                    )
                    for field, column in technical_keys.items()
                }

                pos_key = (
                    _normalisiere_positionsname(name),
                    _normalisiere_ticker(ticker),
                    _positionsfeld_schluessel(entry),
                    _positionsfeld_schluessel(date),
                )

                if pos_key in result:
                    raise ValueError(
                        "Doppelter Positionsschlüssel in Offene Positionen+Check.csv: "
                        f"{name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}"
                    )

                result[pos_key] = {
                    "name": name,
                    "ticker": ticker,
                    "entry": entry,
                    "date": date,
                    "technical": technical_values,
                }

            return result

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "Technische Check-Werte konnten nicht verbindlich aus "
            f"Offene Positionen+Check.csv gelesen werden: {exc}"
        )


def _normalisiere_datum(value):
    """Normalisiert ein Datum fuer den Positionsschluessel."""
    return _positionsfeld_schluessel(value)


def _normalisiere_positionsname(value):
    """Robuste Namensnormalisierung fuer die Zuordnung Gemini -> CSV."""
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9äöüß]+", " ", value)
    value = re.sub(
        r"\b(ag|se|sa|plc|inc|corp|corporation|limited|ltd|nv|spa|srl|"
        r"holding|holdings|company|co|group)\b",
        " ",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def _normalisiere_ticker(value):
    return re.sub(r"[^a-z0-9.=-]+", "", str(value or "").strip().lower())


def _finde_quellposition(ziel_key, quellpositionen):
    """Findet genau eine CSV-Position.

    Primär wird der vollständige Schlüssel verwendet. Wenn Gemini den
    Firmennamen leicht anders schreibt, wird ausschließlich über
    Ticker + Einstieg + Datum aufgelöst. Das ist bei mehreren gleichen
    Tickern sicher, weil Einstieg und Datum Bestandteil des Schlüssels sind.
    """
    if ziel_key in quellpositionen:
        return quellpositionen[ziel_key]

    name, ticker, entry, date = ziel_key

    # 1. Vollständiger Schlüssel mit Ticker + Einstieg + Datum.
    kandidaten = [
        pos for key, pos in quellpositionen.items()
        if key[1] == ticker and key[2] == entry and key[3] == date
    ]
    if len(kandidaten) == 1:
        return kandidaten[0]
    if len(kandidaten) > 1:
        raise RuntimeError(
            "Position nicht eindeutig zuordenbar: "
            f"{name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}"
        )

    # 2. CSV ist Master: Wenn Name+Ticker in der CSV eindeutig sind, darf
    # der Gemini-Block auch bei abweichendem/fehlendem Einstieg oder Datum
    # dieser eindeutigen CSV-Position zugeordnet werden. Anschließend werden
    # Einstieg und Datum aus der CSV eingesetzt.
    kandidaten = [
        pos for key, pos in quellpositionen.items()
        if key[0] == name and key[1] == ticker
    ]
    if len(kandidaten) == 1:
        return kandidaten[0]

    # 3. Falls der Firmenname durch Gemini leicht abweicht, ist ein eindeutiger
    # Ticker ebenfalls ausreichend. Bei mehreren gleichen Tickern wird ohne
    # Einstieg+Datum niemals geraten.
    kandidaten = [
        pos for key, pos in quellpositionen.items()
        if key[1] == ticker
    ]
    if len(kandidaten) == 1:
        return kandidaten[0]

    if len(kandidaten) > 1:
        raise RuntimeError(
            "Position nicht eindeutig zuordenbar; gleicher Ticker mehrfach "
            "vorhanden, Einstieg und Einstiegsdatum fehlen oder passen nicht: "
            f"{name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}"
        )
    return None

# ---------------------------------------------------------------------------
# HAUPTLOGIK
# ---------------------------------------------------------------------------

def _enthaelt_abschnitt_8(text):
    """Prüft strikt, ob Gemini den vollständigen Abschnitt 7 begonnen hat."""
    return bool(re.search(r"(?im)^\s*7\. OFFENE POSITIONEN\s*$", text or ""))


def _gemini_finish_reason(antwort):
    """Liest den Finish-Reason robust aus der Gemini-Antwort."""
    try:
        candidates = getattr(antwort, "candidates", None) or []
        if not candidates:
            return "UNBEKANNT"
        reason = getattr(candidates[0], "finish_reason", None)
        if reason is None:
            return "UNBEKANNT"
        return str(reason)
    except Exception:
        return "UNBEKANNT"


def _abschnitt_8_vollstaendig(text, csv_pfad):
    """Prüft, ob Punkt 7 alle offenen CSV-Positionen eindeutig enthält.

    Diese Prüfung ist bewusst nur eine Vollständigkeitsprüfung. Die bestehende
    harte technische/CSV-Kanonisierung in normalisiere_ausgabe() bleibt danach
    unverändert und ist weiterhin die letzte Instanz.
    """
    if not _enthaelt_abschnitt_8(text):
        return False

    expected = _technische_zielzonen_quelle(csv_pfad)
    match = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*\d+\.\s+|\Z)",
        text,
    )
    if not match:
        return False

    block = match.group(0)
    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\(([^()]+)\)\s*\|\s*Markt:\s*[^\n]+$"
    )
    headers = list(header_re.finditer(block))
    if not headers:
        return False

    seen = set()
    for idx, header in enumerate(headers):
        start = header.start()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(block)
        pos_block = block[start:end]

        name = header.group(1).strip()
        ticker = header.group(2).strip()
        entry_match = re.search(
            r"(?im)^\s*Einstieg(?:skurs)?\s*:\s*([^\n(]+?)(?:\s*\(([^)]+)\))?\s*$",
            pos_block,
        )
        if not entry_match:
            return False

        entry = _positionsfeld_schluessel(entry_match.group(1).strip())
        if entry_match.group(2):
            date = _normalisiere_datum(entry_match.group(2).strip())
        else:
            date_match = re.search(
                r"(?im)^\s*Einstiegsdatum\s*:\s*([^\n]+)\s*$",
                pos_block,
            )
            if not date_match:
                return False
            date = _normalisiere_datum(date_match.group(1).strip())

        key = (
            _normalisiere_positionsname(name),
            _normalisiere_ticker(ticker),
            entry,
            date,
        )
        try:
            source = _finde_quellposition(key, expected)
        except Exception:
            return False
        if source is None:
            return False

        source_key = (
            _normalisiere_positionsname(source["name"]),
            _normalisiere_ticker(source["ticker"]),
            source["entry"],
            source["date"],
        )
        if source_key in seen:
            return False
        seen.add(source_key)

    return len(seen) == len(expected)


def _fuege_abschnitt_8_ein(original_text, abschnitt_8):
    """Fügt einen ausschließlich für Punkt 7 angeforderten Gemini-Block ein.

    Der Reparatur-Call darf nur Punkt 7 liefern. Der Block wird deshalb nicht
    als komplette neue Auswertung verwendet, sondern deterministisch in die
    bestehende Antwort vor den nächsten nummerierten Hauptabschnitt eingesetzt.
    """
    if not _enthaelt_abschnitt_8(abschnitt_8):
        raise RuntimeError(
            "Gezielter Reparaturversuch lieferte ebenfalls keinen Abschnitt "
            "'7. OFFENE POSITIONEN'."
        )

    block_match = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*\d+\.\s+|\Z)",
        abschnitt_8,
    )
    if not block_match:
        raise RuntimeError(
            "Gezielter Reparaturversuch lieferte keinen verwertbaren "
            "Abschnitt '7. OFFENE POSITIONEN'."
        )

    block = block_match.group(0).strip("\n")
    # Ersetze den bereits vorhandenen Punkt-7-Block vollständig durch
    # den erfolgreich reparierten Punkt-7-Block.
    vorhandener_abschnitt = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*9\.\s+|\Z)",
        original_text,
    )
    if vorhandener_abschnitt:
        return (
            original_text[:vorhandener_abschnitt.start()].rstrip()
            + "\n\n"
            + block
            + "\n\n"
            + original_text[vorhandener_abschnitt.end():].lstrip()
        )
    return original_text.rstrip() + "\n\n" + block + "\n"



def pruefe_makro_gate_konsistenz(text, quell_gate):
    """Der Gate-Status des Makro-Datenpakets ist autoritativ.

    Bei FREIGEGEBEN darf Gemini das Szenario nicht wegen TIER-2/TIER-3-Luecken
    nachtraeglich als GESPERRT darstellen. Bei GESPERRT greift weiterhin die
    bestehende harte Sperrlogik.
    """
    if quell_gate != "FREIGEGEBEN":
        return True
    t = text or ""
    if re.search(r"(?is)MAKRO[- ]?SZENARIO[- ]?GATE\s*[:=]?\s*(?:ist\s+)?GESPERRT", t):
        print(
            "WARNUNG: MAKRO-GATE-KONSISTENZFEHLER: Quelldatei meldet FREIGEGEBEN, "
            "Gemini-Ausgabe meldet GESPERRT."
        )
        return False
    return True


def gemini_auswertung_starten():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("FEHLER: Umgebungsvariable GEMINI_API_KEY nicht gesetzt.")
        sys.exit(1)

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=600000))
    anweisung = lade_anweisung()
    eingabedateien = sammle_eingabedateien()

    letzte_antwort = None
    hochgeladene_teile = None  # wird bei Bedarf (neu) befuellt, siehe unten
    aktuelles_modell = MODELL

    # Harte Datenqualitaetskontrolle fuer Punkt 4: Der Makro-Block darf nur
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

    makro_datenqualitaet = _lese_makro_datenqualitaet(makro_text if makro_pfad else "")
    if makro_datenqualitaet:
        print(f"Makro-Datenqualitaet: {makro_datenqualitaet} | Quelle: Makro-Datenpaket")

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

            offene_quelle = _offene_positionen_quellblock(eingabedateien.get("Offene Positionen+Check.csv"))
            antwort = client.models.generate_content(
                model=aktuelles_modell,
                contents=hochgeladene_teile + [
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. Die Dateien Bitcoin_Trading_DE_Briefing.txt, Gold_Trading_DE_Briefing.txt und Silber_Trading_DE_Briefing.txt sind ausschließlich qualitative externe YouTube-Quellen. Nutze sie nur als Kontext/Abgleich; sie dürfen niemals objektive Kursdaten, technische Check-Felder, CRV, Setup-Scores, Filter, Setup-Qualität oder Handelsentscheidungen verändern. Wenn eine solche Datei fehlt, ist das kein Fehler und es darf nichts daraus erfunden werden. "
                    "ERSTELLE in der fertigen Auswertung zusätzlich eine feste Sektion mit exakt der Überschrift 'EXTERNE MARKTQUELLEN'. Gliedere sie getrennt nach 'Bitcoin', 'Gold' und 'Silber'. Für jeden Markt nenne die Anzahl der tatsächlich in der jeweiligen bereitgestellten Briefing-Datei enthaltenen relevanten Videos. WICHTIG: Zähle und verarbeite jedes vorhandene Video einzeln anhand jedes einzelnen 'Titel:'-Blocks bzw. Video-Blocks. Wenn die Briefing-Datei beispielsweise 3 relevante Videos enthält, müssen in der fertigen Auswertung genau diese 3 Videos einzeln erscheinen. Kein Video darf wegen Kürze, Ähnlichkeit, Redundanz oder eigener Auswahl des Modells weggelassen, zusammengefasst oder durch ein anderes ersetzt werden. Führe für JEDES vorhandene relevante Video separat Titel und eine kurze Kernaussage auf und ordne JEDE einzelne Aussage ausschließlich im Verhältnis zur bestehenden Systemanalyse als 'BESTÄTIGT', 'WIDERSPRICHT' oder 'NEUTRAL' ein. Die Anzahl muss mit der Zahl der tatsächlich einzeln aufgeführten Videos übereinstimmen. Ergänze bei jedem Markt ausdrücklich 'Technische Auswirkung: KEINE'. Wenn für einen Markt keine relevanten Videos in der bereitgestellten Briefing-Datei vorhanden sind oder die Datei fehlt, schreibe ausdrücklich 'Keine neuen relevanten Videos verarbeitet'. Verwende für Titel und Kernaussagen ausschließlich die Inhalte der bereitgestellten YouTube-Briefing-Dateien; ergänze nichts aus allgemeinem Modellwissen und erfinde nichts. Die Einordnung darf keine technische Berechnung oder Entscheidung verändern. Die externe Quelle ist ausschließlich qualitativer Kontext. Eine Übereinstimmung mit der externen Quelle ist keine technische Bestätigung; eine Abweichung ist kein technischer Ausschluss. Eine Aussage wie '1 Video' ist nur zulässig, wenn tatsächlich genau 1 relevanter Video-Block in der betreffenden Briefing-Datei vorhanden ist. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben "
                    "und erstelle die vollstaendige Daten-Uebersicht. "
                    "AUTORITATIVE OFFENE-POSITIONEN-LISTE (ausschließlich aus Offene Positionen+Check.csv):\n"
                    + (offene_quelle or "(keine offenen Positionen gefunden)") + "\n"
                    "Diese Liste ist für Firmenname, Ticker, Einstiegskurs und Einstiegsdatum verbindlich. "
                    "Übernimm diese vier Werte exakt; erfinde, schätze oder ändere sie nicht. "
                    "WICHTIGE QUELLE FUER OFFENE POSITIONEN: Verwende fuer den Abschnitt "
                    "Offene Positionen ausschliesslich die Datei 'Offene Positionen+Check.csv'. "
                    "Ihre technischen Check-Felder sind die verbindliche Quelle fuer "
                    "Technischer_Zustand, Trendrichtung, Support/Widerstand, Breakout_Status, "
                    "A-B-C_Status, Fibonacci_Status/Ziele, Trendkanal, Measured Move, Formation, "
                    "Round Number, Major Resistance, Ueberdehnung, Relative Staerke_Sektor, "
                    "Konfluenz, Retest_Support, Technische_Zielzone, Datenqualitaet und Analysehinweis. "
                    "ALLE technischen Check-Felder sind bereits berechnete Quellwerte. "
                    "Uebernimm sie aus Offene Positionen+Check.csv und berechne, interpretiere, "
                    "priorisiere, kuerze, ergaenze oder ersetze keinen technischen Wert selbst. "
                    "Technische_Zielzone ist dabei besonders streng: Uebernimm den Wert "
                    "aus Offene Positionen+Check.csv exakt 1:1. Berechne, priorisiere, kuerze, "
                    "ergaenze oder ersetze die Technische_Zielzone niemals selbst. Auch wenn "
                    "Widerstand_1, Fibonacci, Trendkanal, Measured Move, Round Number oder "
                    "Major Resistance andere Werte enthalten, hat der bereits berechnete Wert "
                    "in Technische_Zielzone Vorrang und muss unveraendert ausgegeben werden. "
                    "Für offene Positionen sind Firmenname, Ticker, Einstiegskurs und Einstiegsdatum "
                    "ausschließlich aus 'Offene Positionen+Check.csv' zu übernehmen. "
                    "Die alte Offene_Positionen.csv darf für diese vier Felder niemals als "
                    "Quelle oder Fallback verwendet werden. Gemini darf diese Werte nicht "
                    "erfinden, schätzen oder verändern. Mehrere offene Positionen desselben "
                    "Tickers sind ausdrücklich zulässig; jede Kombination aus Name + Ticker + "
                    "Einstiegskurs + Einstiegsdatum ist eine eigene Position. "
                    "Jeder offene Positionskopf muss ausschließlich im Format "
                    "'Firmenname (Ticker) | Markt: ...' ausgegeben werden. "
                    "Keine alternativen Kopfzeilenformate und keine Positionsköpfe ohne Ticker.",
                    (
                        f"HARTE MAKRO-GATE-VORGABE: Das Makro-Szenario-Gate ist GESPERRT. Grund: {makro_gate_grund} "
                        "Erzeuge in Punkt 4 KEINE Base/Bull/Bear-Wahrscheinlichkeiten, "
                        "keine geschaetzten Ersatzwerte und keine numerischen Makro-Prognosen. "
                        "Benenne stattdessen die konkreten kritischen Datenluecken bzw. den Ausfall des Makro-Datenpakets. "
                         "Verwende dabei NICHT die Bezeichnungen Base Case, Bull Case oder Bear Case, gib KEINE Makro-Trade-Ideen und KEINE qualitative Richtungsprognose aus. "
                        if makro_gate == "GESPERRT" else
                        "HARTE MAKRO-GATE-VORGABE: Das Makro-Datenpaket ist autoritativ. "
                        "Sein MAKRO-SZENARIO-GATE hat Vorrang vor jeder eigenen Bewertung der "
                        "Datenvollstaendigkeit. Das Gate lautet FREIGEGEBEN. Punkt 4 MUSS daher "
                        "als freigegeben behandelt werden. TIER-2- oder TIER-3-Luecken, insbesondere "
                        "fehlende ISM-EXTENDED-Unterkomponenten oder fehlende LME-Preise, duerfen das "
                        "Gate NICHT nachtraeglich sperren. Sie duerfen hoechstens die DATENQUALITAET "
                        "auf EINGESCHRAENKT halten bzw. die Staerke der Bestaetigung reduzieren. Schreibe "
                        "NICHT, das Makro-Szenario sei gesperrt, wenn die Quelldatei FREIGEGEBEN meldet. "
                        "TIER 1 CORE = gate-relevant; TIER 2 CONFIRMATION = Szenarioverstaerkung, "
                        "niemals alleiniger Gate-Blocker; TIER 3 CONTEXT = zusaetzliche Information "
                        "ohne Gate-Einfluss. Verwende fuer sichtbare Datenqualitaet ausschliesslich VOLLSTAENDIG, "
                        "EINGESCHRAENKT oder UNZUREICHEND sowie die Bezeichnungen TIER-2-DATENLUECKEN "
                        "und TIER-3-DATENLUECKEN. "
                        "Verwende ausschliesslich REAL-, REAL_CACHED-, "
                        "REAL_PUBLIC_SECONDARY- oder zulaessige CALCULATED-Werte aus dem Makro-Datenpaket. "
                        "PROXY-Werte muessen als Proxy bezeichnet werden. MODEL_DERIVED-Wahrscheinlichkeiten "
                        "sind nur als Ergebnis der Szenariologik zulaessig; niemals Eingangsdaten schaetzen."
                        + (
                            f" ZUSAETZLICHE HARTE DATENQUALITAETS-VORGABE: Das Makro-Datenpaket meldet "
                            f"MAKRO-DATENQUALITAET={makro_datenqualitaet}. Uebernimm diesen Wert in Punkt 4 "
                            f"exakt. Wenn der Wert VOLLSTAENDIG ist, darf Punkt 2 nicht auf EINGESCHRAENKT "
                            f"oder UNZUREICHEND herabgestuft werden und darf keine TIER-2-DATENLUECKE als "
                            f"Grund fuer eine Herabstufung nennen."
                            if makro_datenqualitaet else ""
                        )
                    ),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=anweisung,
                ),
            )
            text = antwort.text or ""
            print(f"  Gemini finish_reason (Hauptantwort): {_gemini_finish_reason(antwort)}")

            if not pruefe_makro_gate_konsistenz(text, makro_gate):
                print("WARNUNG: Gemini widerspricht dem autoritativen Makro-Gate - starte gezielte Makro-Reparatur.")
                reparatur = client.models.generate_content(
                    model=aktuelles_modell,
                    contents=hochgeladene_teile + [
                        "REPARATUR NUR FÜR PUNKT 2: Das Makro-Datenpaket meldet MAKRO-SZENARIO-GATE=FREIGEGEBEN. "
                        "Überarbeite ausschließlich Punkt 4. Eine Sperrung ist unzulässig, wenn nur TIER-2- oder TIER-3-Daten fehlen. "
                        "TIER 1 KERN entscheidet über das Gate; TIER 2 BESTAETIGUNG und TIER 3 KONTEXT sind Ergänzungen. "
                        "Verwende die deutsche Terminologie VOLLSTAENDIG/EINGESCHRAENKT/UNZUREICHEND und nenne "
                        "TIER-2-DATENLUECKEN bzw. TIER-3-DATENLUECKEN. Erhalte alle übrigen Abschnitte unverändert soweit möglich. "
                        "Gib die vollständige Auswertung erneut aus."
                    ],
                    config=types.GenerateContentConfig(system_instruction=anweisung),
                )
                reparatur_text = reparatur.text or ""
                if pruefe_makro_gate_konsistenz(reparatur_text, makro_gate):
                    text = reparatur_text
                    print("INFO: Makro-Gate-Konsistenz nach Reparatur hergestellt.")
                else:
                    raise RuntimeError("Gemini widerspricht weiterhin dem autoritativen MAKRO-SZENARIO-GATE=FREIGEGEBEN.")

            # KONTROLLIERTER REPARATURVERSUCH:
            # Gemini kann trotz der Hauptvorgabe die komplette Auswertung liefern,
            # aber Punkt 7 auslassen. In diesem Fall wird NICHT aus anderen Dateien
            # geraten und NICHT der Parser gelockert. Stattdessen erhält Gemini genau
            # einen gezielten zweiten Versuch, ausschließlich Punkt 7 vollständig
            # nachzuliefern. Erst danach darf normalisiere_ausgabe() den CSV-Master
            # anwenden.
            if not _abschnitt_8_vollstaendig(
                text, eingabedateien.get("Offene Positionen+Check.csv")
            ):
                if _enthaelt_abschnitt_8(text):
                    print(
                        "  Abschnitt '7. OFFENE POSITIONEN' ist vorhanden, "
                        "aber unvollstaendig - starte gezielten Reparaturversuch "
                        "fuer Punkt 7..."
                    )
                else:
                    print(
                        "  Abschnitt '7. OFFENE POSITIONEN' fehlt - "
                        "starte gezielten Reparaturversuch fuer Punkt 7..."
                    )
                reparatur_prompt = (
                    "REPARATURVERSUCH - NUR ABSCHNITT 7 NACHLIEFERN.\n"
                    "Deine vorherige Antwort enthielt den erforderlichen Abschnitt "
                    "'7. OFFENE POSITIONEN' nicht. Erstelle deshalb jetzt "
                    "AUSSCHLIESSLICH den vollständigen Abschnitt 7.\n\n"
                    "Beginne zwingend mit exakt:\n"
                    "7. OFFENE POSITIONEN\n\n"
                    "Gib danach ALLE offenen Positionen aus der verbindlichen Datei "
                    "'Offene Positionen+Check.csv' vollständig und genau einmal aus. "
                    "Verwende ausschließlich diese Datei für Firmenname, Ticker, "
                    "Einstiegskurs und Einstiegsdatum. Die Kombination aus Name + "
                    "Ticker + Einstiegskurs + Einstiegsdatum identifiziert eine "
                    "Position eindeutig; mehrere Positionen mit demselben Ticker "
                    "sind zulässig.\n\n"
                    "Jeder Positionskopf muss exakt dem Format "
                    "'Firmenname (Ticker) | Markt: ...' entsprechen. "
                    "Für jede Position müssen die in der Hauptanweisung geforderten "
                    "Positionsdaten und technischen Check-Felder ausgegeben werden. "
                    "Übernimm technische Check-Felder aus 'Offene Positionen+Check.csv' "
                    "und erfinde, berechne, kürze oder interpretiere sie nicht. "
                    "Insbesondere 'Technische Zielzone' darf ausschließlich als "
                    "bereits vorhandener CSV-Wert übernommen werden.\n\n"
                    "WICHTIG: Antworte ausschließlich mit Abschnitt 7 und dessen "
                    "vollständigem Inhalt. Keine Einleitung, keine Erklärung, "
                    "keine Abschnitte 1-7 oder 9 ff."
                )
                reparatur_antwort = client.models.generate_content(
                    model=aktuelles_modell,
                    contents=hochgeladene_teile + [
                        reparatur_prompt,
                        "VERBINDLICHE OFFENE-POSITIONEN-LISTE AUS "
                        "'Offene Positionen+Check.csv':\n"
                        + (offene_quelle or "(keine offenen Positionen gefunden)")
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=anweisung,
                    ),
                )
                reparatur_text = reparatur_antwort.text or ""
                print(
                    f"  Gemini finish_reason (Punkt-7-Reparatur): "
                    f"{_gemini_finish_reason(reparatur_antwort)}"
                )

                if _abschnitt_8_vollstaendig(
                    reparatur_text, eingabedateien.get("Offene Positionen+Check.csv")
                ):
                    text = _fuege_abschnitt_8_ein(text, reparatur_text)
                    print(
                        "  Reparatur erfolgreich: Abschnitt "
                        "'7. OFFENE POSITIONEN' nachgeliefert."
                    )
                else:
                    print(
                        "  Reparatur fehlgeschlagen: Abschnitt "
                        "'7. OFFENE POSITIONEN' weiterhin unvollstaendig."
                    )
                    letzte_antwort = reparatur_text or text
                    # Kein Parser-Fallback. Der äußere Retry startet eine neue
                    # vollständige Gemini-Anfrage.
                    hochgeladene_teile = None
                    continue

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
                # Bei serverseitiger Ueberlast (503) nicht alle fuenf Versuche
                # am selben Modell verbrauchen: Nach drei erfolglosen 503-
                # Versuchen wird auf das definierte Fallback-Modell gewechselt.
                # Netzwerk-Abbrueche behalten die bisherige Retry-Logik.
                if kategorie == "ueberlast":
                    if (aktuelles_modell == MODELL and
                            FALLBACK_MODELL and FALLBACK_MODELL != MODELL):
                        aktuelles_modell = FALLBACK_MODELL
                        print(
                            f"  503-Overload nach Versuch {versuch}/{MAX_VERSUCHE}. "
                            f"Wechsle fuer den naechsten Versuch von {MODELL} "
                            f"auf Fallback-Modell {FALLBACK_MODELL}."
                        )
                        continue

                    if (aktuelles_modell == FALLBACK_MODELL and
                            DRITTER_FALLBACK_MODELL and
                            DRITTER_FALLBACK_MODELL not in (MODELL, FALLBACK_MODELL)):
                        aktuelles_modell = DRITTER_FALLBACK_MODELL
                        print(
                            f"  503-Overload nach Versuch {versuch}/{MAX_VERSUCHE}. "
                            f"Wechsle fuer den naechsten Versuch von {FALLBACK_MODELL} "
                            f"auf dritten Fallback {DRITTER_FALLBACK_MODELL}."
                        )
                        continue

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
                print(f"  {grund} - warte {wartezeit:.0f}s (kurze Staffel, "
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
        text = _normalisiere_makro_datenqualitaet(text, makro_datenqualitaet)
        return text

    print(f"\nFEHLER: Nach {MAX_VERSUCHE} Versuchen weiterhin keine gueltige Antwort.")
    print(f"Letzte Antwort/Fehler:\n{letzte_antwort}")
    sys.exit(1)



def _read_latest_local(patterns):
    """Liest eine bereits vorhandene Ausgabedatei fuer die Darstellungsebene."""
    pfad = finde_datei(patterns)
    if not pfad:
        return None
    try:
        return Path(pfad).read_text(encoding="utf-8-sig")
    except Exception:
        return None


def _legacy_sections(text):
    """Zerlegt Gemini-Ausgabe anhand ihrer Hauptueberschriften.

    Nur Darstellung: Es werden keine Scannerwerte berechnet oder bewertet.
    """
    headings = list(re.finditer(r"(?m)^(?:\ufeff)?(?:1\. MARKTUMFELD & GLOBALE RISIKOLAGE|2\. MAKRO-ZUKUNFTSSZENARIO|3\. TRENDFOLGE-SETUPS|4\. TRENDWENDE-SETUPS[^\n]*|5\. HEBELTRADER-SETUPS|6\. SHORT-SETUPS[^\n]*|7\. EDELMETALLE-SETUPS|8\. OFFENE POSITIONEN[^\n]*|9\. GESCHLOSSENE POSITIONEN[^\n]*|METHODIK & LESEHILFE|EXTERNE MARKTQUELLEN|PERSPEKTIVISCHE TRADE-IDEEN|LIVE-PERFORMANCE vs\. MSCI WORLD|KURZ-ZUSAMMENFASSUNG|RISIKO-WATCH|WOCHENAUSBLICK|SYSTEM-STATISTIK)\s*$", text, re.I))
    result = {}
    for i, m in enumerate(headings):
        key = m.group(0).strip().upper()
        end = headings[i+1].start() if i+1 < len(headings) else len(text)
        result[key] = text[m.start():end].strip()
    return result


def _strip_watchlists(block):
    if not block:
        return ""
    patterns = [
        r"(?ims)^\s*WATCHLIST(?:\s*\([^\n]*\))?\s*$.*?(?=^\s*(?:[A-ZÄÖÜ][^\n]*:|$)|\Z)",
        r"(?ims)^\s*DIVERGENZ-WATCHLIST[^\n]*\s*$.*?(?=^\s*(?:[A-ZÄÖÜ][^\n]*:|$)|\Z)",
        r"(?ims)^\s*Beinahe-Kandidaten[^\n]*\s*$.*?(?=^\s*(?:[A-ZÄÖÜ][^\n]*:|$)|\Z)",
    ]
    for pat in patterns:
        block = re.sub(pat, "", block)
    # Scanner-Ablehnungsbegruendungen sind ebenfalls keine validen Setups.
    block = re.sub(r"(?im)^\s*Engstelle des Filters:[^\n]*\n?", "", block)
    block = re.sub(r"\n{3,}", "\n\n", block)
    return block.strip()


def _extract_summary_without_watchlist(text):
    m = re.search(r"(?ims)^KURZ-ZUSAMMENFASSUNG\s*$.*?(?=^\s*WATCHLIST\s*$|^\s*SYSTEM-STATISTIK\s*$)", text)
    if not m:
        return ""
    return m.group(0).strip()


def _extract_between(text, start_pat, end_pats):
    m = re.search(start_pat, text, re.I | re.M)
    if not m:
        return ""
    start = m.start()
    end = len(text)
    for ep in end_pats:
        em = re.search(ep, text[m.end():], re.I | re.M)
        if em:
            end = min(end, m.end() + em.start())
    return text[start:end].strip()


def _macro_status_block():
    """Uebernimmt die drei autoritativen Statuszeilen 1:1 aus Makro_Briefing."""
    raw = _read_latest_local(["Makro_Briefing(*).txt"])
    if not raw:
        return ""
    lines = raw.splitlines()
    wanted = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^(MAKRO-SZENARIO-GATE|MAKRO-DATENQUALITAET|DATENQUALITAET|SEKUNDAERE DATENLUECKEN|SEKUNDAERE_DATENLUECKEN)\s*[:=]", stripped, re.I):
            # Genau die Originalzeile, ohne inhaltliche Umformulierung.
            wanted.append(stripped)
    # Quelle kann DATENQUALITAET statt MAKRO-DATENQUALITAET verwenden.
    gate = next((x for x in wanted if x.upper().startswith("MAKRO-SZENARIO-GATE")), None)
    quality = next((x for x in wanted if x.upper().startswith("MAKRO-DATENQUALITAET")), None)
    if quality is None:
        quality = next((x for x in wanted if x.upper().startswith("DATENQUALITAET")), None)
    gaps = next((x for x in wanted if x.upper().startswith("SEKUNDAERE DATENLUECKEN") or x.upper().startswith("SEKUNDAERE_DATENLUECKEN")), None)
    return "\n".join(x for x in (gate, quality, gaps) if x)


def _metals_information_block():
    raw = _read_latest_local(["Edelmetalle_Briefing(*).txt"])
    if not raw:
        return ""
    # Vollstaendige Markt-/Diagnoseinformationen, aber ohne die drei
    # Strategie-Funnel als vermeintliche Setups. Die Quelle selbst bleibt
    # unveraendert; hier wird nur der relevante Lageblock uebernommen.
    start = raw.find("LAGE JE METALL")
    end = raw.find("==================================================\nSTRATEGIE: TRENDFOLGE")
    if start < 0:
        return raw.strip()
    if end < 0:
        end = len(raw)
    return raw[start:end].strip()


def _a_meldungen_block():
    pfad = finde_datei(["Einzel_Check_A_Meldungen(*).txt"])
    if not pfad:
        return ""
    try:
        raw = Path(pfad).read_text(encoding="utf-8-sig").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    # Nur dann sichtbar, wenn nach dem Kopf mindestens eine Meldung existiert.
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    if len(lines) <= 2:
        return ""
    return raw


def _remove_duplicate_open_position_blocks(text):
    matches = list(re.finditer(r"(?ims)^\s*(?:7|8)\. OFFENE POSITIONEN[^\n]*\s*$", text))
    if len(matches) <= 1:
        return text
    first = matches[0]
    # Der erste Block ist der vollstaendige, aus der Hauptantwort stammende
    # Positionsblock. Spaetere 7/8-Bloecke sind Reparaturduplikate.
    second = matches[1]
    next_main = re.search(r"(?m)^\s*9\.\s+|^\s*METHODIK & LESEHILFE\s*$", text[second.end():], re.I)
    end = second.end() + next_main.start() if next_main else len(text)
    return text[:second.start()].rstrip() + "\n\n" + text[end:].lstrip()


def _ensure_ki_position_fazit_instruction(text):
    """Kein Inhalt wird erfunden; vorhandene KI-Fazits bleiben unveraendert."""
    return text


def _kanonisiere_ausgabestruktur(text):
    """Kanonisiert ausschliesslich die Newsletter-Darstellung 1-9."""
    if not text:
        return text
    # Bereits kanonisierte Ausgaben werden nicht ein zweites Mal zerlegt.
    if re.search(r"(?m)^1\. DAS WICHTIGSTE AUF EINEN BLICK$", text) and re.search(
        r"(?m)^7\. OFFENE POSITIONEN$", text
    ) and re.search(r"(?m)^9\. METHODIK & DATENHINWEISE$", text):
        return text
    text = _remove_duplicate_open_position_blocks(text)
    sec = _legacy_sections(text)

    header = text[: min([m.start() for m in re.finditer(r"(?m)^(?:LIVE-PERFORMANCE vs\. MSCI WORLD|KURZ-ZUSAMMENFASSUNG|1\. MARKTUMFELD)", text)] or [len(text)])].strip()
    if not header:
        header = re.search(r"(?s)^.*?(?=^LIVE-PERFORMANCE vs\. MSCI WORLD|^KURZ-ZUSAMMENFASSUNG|^1\. MARKTUMFELD)", text, re.M).group(0).strip() if re.search(r"(?m)^LIVE-PERFORMANCE vs\. MSCI WORLD|^KURZ-ZUSAMMENFASSUNG|^1\. MARKTUMFELD", text) else ""

    summary = _extract_summary_without_watchlist(text)
    system = sec.get("SYSTEM-STATISTIK", "")
    risk = sec.get("RISIKO-WATCH", "")
    handlungsbedarf = sec.get("KURZ-ZUSAMMENFASSUNG", "")
    if handlungsbedarf:
        hm = re.search(r"(?ims)^SOFORT BEACHTEN\s*$.*?(?=^\s*WATCHLIST\s*$|^\s*SYSTEM-STATISTIK\s*$)", handlungsbedarf)
        handlungsbedarf = hm.group(0).strip() if hm else ""

    live = sec.get("LIVE-PERFORMANCE VS. MSCI WORLD", "")
    market = sec.get("1. MARKTUMFELD & GLOBALE RISIKOLAGE", "")
    macro = sec.get("2. MAKRO-ZUKUNFTSSZENARIO", "")
    perspective = sec.get("PERSPEKTIVISCHE TRADE-IDEEN", "")
    trend = sec.get("3. TRENDFOLGE-SETUPS", "")
    reversal = sec.get("4. TRENDWENDE-SETUPS (SEPARATES RISIKO)", "")
    leverage = sec.get("5. HEBELTRADER-SETUPS", "")
    short = sec.get("6. SHORT-SETUPS (FALLENDE KURSE)", "")
    metals = sec.get("7. EDELMETALLE-SETUPS", "")
    openpos = sec.get("8. OFFENE POSITIONEN (MANUELL BESTÄTIGT)", "")
    closed = sec.get("9. GESCHLOSSENE POSITIONEN (LETZTE 10 WERKTAGE)", "")
    external = sec.get("EXTERNE MARKTQUELLEN", "")
    method = sec.get("METHODIK & LESEHILFE", "")
    outlook = sec.get("WOCHENAUSBLICK", "")

    # Punkt 1: keine Watchlist.
    p1_parts = [summary, system, risk]
    p1 = "\n\n".join(x for x in p1_parts if x)

    # Punkt 2: Marktinformation aus dem alten Marktblock, ohne die
    # zeitlichen Zukunftsunterpunkte, die unter Punkt 5 gehoeren.
    p2 = re.sub(r"(?m)^1\. MARKTUMFELD & GLOBALE RISIKOLAGE\s*$", "", market, count=1).strip()
    # Punkt 2 ist bewusst kurz: nur Index-/Regionenlage und das vorhandene
    # Score-Fazit. Die ausfuehrlichen Risikoindikatoren bleiben aus der
    # kompakten Marktinformation heraus.
    risk_start = p2.find("GLOBALE RISIKO-BENCHMARKS UND INDIKATOREN")
    if risk_start >= 0:
        p2 = p2[:risk_start].rstrip()

    # Punkt 5: alte Makro-Zukunftssektion als Perspektive.
    p5 = macro
    p5 = re.sub(r"(?m)^2\. MAKRO-ZUKUNFTSSZENARIO\s*$", "5. MARKTPERSPEKTIVE", p5)
    p5 = re.sub(r"(?m)^2\.1\s+", "5.1 ", p5)
    p5 = re.sub(r"(?m)^2\.2\s+", "5.2 ", p5)
    p5 = re.sub(r"(?m)^2\.3\s+", "5.3 ", p5)
    p5 = re.sub(r"(?m)^2\.4\s+", "5.3 ", p5)
    # Beide langfristigen Horizonte werden unter einem gemeinsamen 5.3-Kopf gefuehrt.
    p5 = re.sub(r"(?m)^5\.3 WEITERER HORIZONT:[^\n]*\n", "5.3 LANGFRISTIG / STRUKTURELL\n\n", p5, count=1)
    p5 = re.sub(r"(?m)^5\.3 STRUKTURELL:[^\n]*\n", "", p5, count=1)
    # Falls keine explizite Matrix/Chancen-Risiken vorhanden sind, werden sie
    # nicht erfunden; die vorhandenen Matrixdaten bleiben im Block erhalten.
    p5 = re.sub(r"(?m)^SZENARIO-MATRIX", "5.4 Szenario-Matrix", p5)
    p5 = re.sub(r"(?m)^CHANCEN & RISIKEN", "5.5 Chancen & Risiken", p5)
    if "5.5 Chancen & Risiken" not in p5:
        chance = re.search(r"(?im)^Bevorzugte Trading-Themen:.*$", p5)
        riskline = re.search(r"(?im)^Regime-Killer:.*$", p5)
        if chance or riskline:
            cr = ["5.5 Chancen & Risiken", ""]
            if chance:
                cr.append("Chancen: " + chance.group(0).split(":", 1)[1].strip())
            if riskline:
                cr.append("Risiken: " + riskline.group(0).split(":", 1)[1].strip())
            p5 = p5.rstrip() + "\n\n" + "\n".join(cr)

    # Setup-Bloecke: nur valide Inhalte; Watchlists/Filterengstellen werden entfernt.
    def setup_block(title, body):
        body = _strip_watchlists(body)
        # Manche Gemini-Antworten verlieren die Watchlist-Ueberschrift, lassen
        # aber deren Kandidaten stehen. In einem einzelnen Setup-Block ist
        # alles ab dem ersten Watchlist-/Beinahe-Kandidaten-Marker nicht mehr
        # Teil der validen Setup-Ausgabe.
        cut = re.search(r"(?im)^\s*(?:WATCHLIST|Beinahe-Kandidaten|DIVERGENZ-WATCHLIST)\b", body)
        if cut:
            body = body[:cut.start()].rstrip()
        # Wenn der Scanner ausdruecklich 0 valide Kandidaten meldet, duerfen
        # danach verbliebene alte Beinahe-Kandidaten nicht in den Setup-Block
        # hineinrutschen. Die Nullmeldung selbst bleibt erhalten.
        zero = re.search(r"(?im)^\s*Keine (?:neuen )?(?:valide[n]?|validen)?\s*(?:Trendfolge|Trendwende|Short|Edelmetalle)[^\n]*gefunden\.\s*$", body)
        if zero:
            body = body[:zero.end()].rstrip()
        return (title + "\n\n" + body.strip()).strip() if body.strip() else ""

    p6_parts = []
    if perspective:
        p6_parts.append(setup_block("6.1 PERSPEKTIVISCHE TRADE-IDEEN", perspective.replace("PERSPEKTIVISCHE TRADE-IDEEN", "", 1).strip()))
    p6_parts.append(setup_block("6.2 TRENDFOLGE", trend.replace("3. TRENDFOLGE-SETUPS", "", 1).strip()))
    p6_parts.append(setup_block("6.3 TRENDWENDE", reversal.replace(reversal.splitlines()[0], "", 1).strip() if reversal else ""))
    # Langfrist wird nur ausgegeben, wenn im Gemini-Ergebnis ein eigener Block existiert.
    long_block = ""
    for k,v in sec.items():
        if "LANGFRIST" in k and "POSITIONEN" not in k:
            long_block = v
            break
    if long_block:
        p6_parts.append(setup_block("6.4 LANGFRIST", long_block))
    p6_parts.append(setup_block("6.5 HEBELTRADER", leverage.replace("5. HEBELTRADER-SETUPS", "", 1).strip()))
    a_block = _a_meldungen_block()
    if a_block:
        p6_parts.append("6.5.1 A-KANDIDATEN / EINZEL-CHECK-MELDUNGEN\n\n" + a_block)
    p6_parts.append(setup_block("6.6 SHORT", short.replace("6. SHORT-SETUPS (fallende Kurse)", "", 1).strip()))
    metals_body = _metals_information_block()
    metals_setup = _strip_watchlists(metals.replace("7. EDELMETALLE-SETUPS", "", 1).strip())
    p6_parts.append("6.7 EDELMETALLE\n\n" + "\n\n".join(x for x in (metals_body, metals_setup) if x).strip())
    if external:
        p6_parts.append(setup_block("6.8 EXTERNE QUELLEN / WEITERE ANSÄTZE", external.replace("EXTERNE MARKTQUELLEN", "", 1).strip()))
    p6 = "\n\n".join(x for x in p6_parts if x)

    # Punkt 7: erster vollstaendiger Positionsblock; keine zweite Reparaturkopie.
    p7_body = openpos
    p7_body = re.sub(r"(?m)^8\. OFFENE POSITIONEN[^\n]*\s*$", "", p7_body, count=1).strip()
    # Portfolio-Uebersicht wird nach 7.1 verschoben; Handlungsbedarf kommt aus
    # dem bereits vorhandenen SOFORT-BEACHTEN-Block.
    po = re.search(r"(?im)^Portfolio[- ]Übersicht:.*$", p7_body)
    portfolio = po.group(0).strip() if po else ""
    if po:
        p7_body = p7_body[:po.start()] + p7_body[po.end():]
    p7 = "7. OFFENE POSITIONEN\n\n"
    p7 += "7.1 PORTFOLIO-ÜBERSICHT\n\n" + (portfolio or "Keine Portfolio-Übersicht im bereitgestellten Positionsblock.")
    p7 += "\n\n7.2 HANDLUNGSBEDARF\n\n" + (handlungsbedarf or "Kein separater Handlungsbedarf im bereitgestellten Output ausgewiesen.")
    p7 += "\n\n7.3 EINZELPOSITIONEN\n\n" + p7_body.strip()

    # Geschlossene Positionen: nur die letzten 3 Tage. Die alte 10-Tage-Formulierung
    # wird nicht als Quelle verwendet; wenn der Block konkrete ältere Daten enthaelt,
    # wird er nicht blind uebernommen.
    p74 = ""
    if closed:
        content = re.sub(r"(?m)^9\. GESCHLOSSENE POSITIONEN[^\n]*\s*$", "", closed, count=1).strip()
        if re.search(r"(?i)keine position|keine geschlossene", content):
            p74 = ""
        else:
            p74 = "7.4 GESCHLOSSENE POSITIONEN – LETZTE 3 TAGE\n\n" + content
    if p74:
        p7 += "\n\n" + p74

    # Punkt 8: Ausblick/Events.
    p8 = outlook
    if p8:
        p8 = re.sub(r"(?m)^WOCHENAUSBLICK\s*$", "8. AUSBLICK & KEY EVENTS", p8, count=1)
    else:
        p8 = "8. AUSBLICK & KEY EVENTS"

    # Punkt 9.
    p9 = method
    if p9:
        p9 = re.sub(r"(?m)^METHODIK & LESEHILFE\s*$", "9. METHODIK & DATENHINWEISE", p9, count=1)
    else:
        p9 = "9. METHODIK & DATENHINWEISE"

    p3 = live
    p3 = re.sub(r"(?m)^LIVE-PERFORMANCE vs\. MSCI WORLD\s*$", "3. SYSTEMPERFORMANCE & BENCHMARK", p3, count=1)

    p4 = _macro_status_block()
    if not p4:
        p4 = "4. DATEN- & SZENARIOSTATUS"
    else:
        p4 = "4. DATEN- & SZENARIOSTATUS\n\n" + p4

    parts = [header, "1. DAS WICHTIGSTE AUF EINEN BLICK\n\n" + p1, "2. MAKRO & MARKT\n\n" + p2,
             p3, p4, p5, "6. TRADING-IDEEN & SETUPS\n\n" + p6, p7, p8, p9]
    result = "\n\n".join(x.strip() for x in parts if x and x.strip())
    return result.strip() + "\n"

def normalisiere_ausgabe(text, zielzonen=None):
    """Erzwingt formale Regeln und macht die Check-Datei zum Master.

    Gemini liefert die Analyse, aber offene Positions-Stammdaten und
    technische Check-Felder werden deterministisch aus
    Offene Positionen+Check.csv übernommen. Keine technische Berechnung
    findet hier statt.
    """
    if not text:
        return text

    text = re.sub(
        r"(?m)^[ \t]*(Was muesste technisch passieren, damit das bestehende "
        r"Setup-System einen konkreten Einstieg bestaetigt\?:)",
        r"\n\1",
        text,
    )
    text = re.sub(
        r"\n{3,}(?=Was muesste technisch passieren, damit das bestehende "
        r"Setup-System einen konkreten Einstieg bestaetigt\?:)",
        "\n\n",
        text,
    )

    if not zielzonen:
        raise RuntimeError(
            "Keine verbindlichen technischen Positionsdaten aus "
            "Offene Positionen+Check.csv vorhanden."
        )

    match = re.search(
        r"(?ims)^7\. OFFENE POSITIONEN\s*$.*?(?=^\s*\d+\.\s+|\Z)",
        text,
    )
    if not match:
        raise RuntimeError(
            "Abschnitt '7. OFFENE POSITIONEN' fehlt; "
            "CSV-Masterwerte koennen nicht verbindlich eingesetzt werden."
        )

    block = match.group(0)
    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\(([^()]+)\)\s*\|\s*Markt:\s*[^\n]+$"
    )
    headers = list(header_re.finditer(block))
    if not headers:
        raise RuntimeError(
            "Keine gueltigen Positionskoepfe im Abschnitt "
            "'7. OFFENE POSITIONEN' gefunden."
        )

    expected = dict(zielzonen)
    seen = {}
    errors = []

    # Gemini darf die technischen Werte nur darstellen; die CSV ersetzt sie
    # nach der Zuordnung. Die Zielzone ist dabei besonders streng: 1:1.
    technical_labels = {
        "Technischer_Zustand": re.compile(r"(?im)^(\s*Technischer Zustand\s*:\s*)[^\n]*$"),
        "Trendrichtung": re.compile(r"(?im)^(\s*Trendrichtung\s*:\s*)[^\n]*$"),
        "Support/Widerstand": re.compile(r"(?im)^(\s*Support/Widerstand\s*:\s*)[^\n]*$"),
        "Breakout_Status": re.compile(r"(?im)^(\s*Breakout Status\s*:\s*)[^\n]*$"),
        "A-B-C_Status": re.compile(r"(?im)^(\s*A-B-C Status\s*:\s*)[^\n]*$"),
        "Fibonacci_Status/Ziele": re.compile(r"(?im)^(\s*Fibonacci(?: Status/Ziele)?\s*:\s*)[^\n]*$"),
        "Trendkanal": re.compile(r"(?im)^(\s*Trendkanal\s*:\s*)[^\n]*$"),
        "Measured Move": re.compile(r"(?im)^(\s*Measured Move\s*:\s*)[^\n]*$"),
        "Formation": re.compile(r"(?im)^(\s*Formation\s*:\s*)[^\n]*$"),
        "Round Number": re.compile(r"(?im)^(\s*Round Number\s*:\s*)[^\n]*$"),
        "Major Resistance": re.compile(r"(?im)^(\s*Major Resistance\s*:\s*)[^\n]*$"),
        "Ueberdehnung": re.compile(r"(?im)^(\s*(?:Ueberdehnung|Überdehnung)\s*:\s*)[^\n]*$"),
        "Relative Staerke_Sektor": re.compile(r"(?im)^(\s*Relative Staerke(?:_Sektor)?\s*:\s*)[^\n]*$"),
        "Konfluenz": re.compile(r"(?im)^(\s*Konfluenz\s*:\s*)[^\n]*$"),
        "Retest_Support": re.compile(r"(?im)^(\s*Retest_Support\s*:\s*)[^\n]*$"),
        "Technische_Zielzone": re.compile(r"(?im)^(\s*Technische Zielzone\s*:\s*)[^\n]*$"),
        "Datenqualitaet": re.compile(r"(?im)^(\s*Datenqualitaet\s*:\s*)[^\n]*$"),
        "Analysehinweis": re.compile(r"(?im)^(\s*Analysehinweis\s*:\s*)[^\n]*$"),
    }

    replacements = []

    for idx, header in reversed(list(enumerate(headers))):
        start = header.start()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(block)
        pos_block = block[start:end]

        name = header.group(1).strip()
        ticker = header.group(2).strip()

        # Unterstützt sowohl "Einstieg: 108,04€ (02.02.2022)" als auch
        # getrennte Einstieg/Einstiegsdatum-Zeilen.
        entry_match = re.search(
            r"(?im)^\s*Einstieg(?:skurs)?\s*:\s*([^\n(]+?)(?:\s*\(([^)]+)\))?\s*$",
            pos_block,
        )
        if not entry_match:
            errors.append(f"{name} ({ticker}): Einstiegszeile fehlt")
            continue

        entry = entry_match.group(1).strip()
        inline_entry_date = bool(entry_match.group(2))
        date = entry_match.group(2).strip() if inline_entry_date else ""
        if not date:
            date_match = re.search(
                r"(?im)^\s*Einstiegsdatum\s*:\s*([^\n|]+?)\s*$",
                pos_block,
            )
            if date_match:
                date = date_match.group(1).strip()

        pos_key = (
            _normalisiere_positionsname(name),
            _normalisiere_ticker(ticker),
            _positionsfeld_schluessel(entry),
            _positionsfeld_schluessel(date),
        )

        try:
            source = _finde_quellposition(pos_key, expected)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue

        if source is None:
            errors.append(
                f"{name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}: "
                "kein passender Positionsschluessel in Offene Positionen+Check.csv"
            )
            continue

        source_key = (
            _normalisiere_positionsname(source["name"]),
            _normalisiere_ticker(source["ticker"]),
            _positionsfeld_schluessel(source["entry"]),
            _positionsfeld_schluessel(source["date"]),
        )
        seen[source_key] = seen.get(source_key, 0) + 1
        if seen[source_key] > 1:
            errors.append(
                f"{source['name']} ({source['ticker']}) | Einstieg: {source['entry']} | "
                f"Einstiegsdatum: {source['date']}: doppelte Position im Gemini-Output"
            )
            continue

        # Stammdaten aus CSV: Gemini-Ausgabe wird nicht als Quelle akzeptiert.
        # Nur der Wert wird ersetzt, das Label/Format bleibt erhalten.
        src_entry = source["entry"]
        src_date = source["date"]

        em = re.search(
            r"(?im)^([ \t]*Einstieg(?:skurs)?\s*:\s*)[^\n]+$",
            pos_block,
        )
        if em:
            # Wenn Gemini das Datum in Klammern an die Einstiegszeile
            # geschrieben hat, bleibt diese Darstellung erhalten; nur der
            # Einstiegskurs wird durch den CSV-Masterwert ersetzt.
            date_suffix = f" ({src_date})" if inline_entry_date else ""
            pos_block = (
                pos_block[:em.start(0)]
                + em.group(1)
                + src_entry
                + date_suffix
                + pos_block[em.end(0):]
            )
        else:
            errors.append(
                f"{source['name']} ({source['ticker']}): "
                "Einstiegszeile konnte nicht kanonisiert werden"
            )
            continue

        dm = re.search(
            r"(?im)^([ \t]*Einstiegsdatum\s*:\s*)[^\n]+$",
            pos_block,
        )
        if dm:
            pos_block = (
                pos_block[:dm.start(0)]
                + dm.group(1)
                + src_date
                + pos_block[dm.end(0):]
            )

        technical = source["technical"]

        for field, value in technical.items():
            if value is None:
                continue

            pattern = technical_labels.get(field)
            if pattern is None:
                continue

            # Zielzone: vorhandenen Gemini-Wert vollständig verwerfen und
            # den CSV-String 1:1 einsetzen. Keine Berechnung/Normalisierung.
            if field == "Technische_Zielzone":
                replacement = f"Technische Zielzone: {value}"
                pos_block, count = pattern.subn(replacement, pos_block, count=1)
                if count == 0:
                    # Fehlende Zielzone ist erlaubt: sie wird deterministisch
                    # unmittelbar vor Ueberdehnung eingefügt.
                    anchor = re.search(
                        r"(?im)^\s*(?:Ueberdehnung|Überdehnung)\s*:",
                        pos_block,
                    )
                    if anchor:
                        pos_block = (
                            pos_block[:anchor.start()]
                            + replacement + "\n"
                            + pos_block[anchor.start():]
                        )
                        count = 1
                if count == 0:
                    # Falls auch kein Ueberdehnung/Überdehnung-Anker vorhanden
                    # ist, wird die verbindliche CSV-Zielzone am Ende des
                    # Positionsblocks eingesetzt. Der CSV-Wert bleibt 1:1.
                    pos_block = pos_block.rstrip() + "\n" + replacement + "\n"
                    count = 1
                continue

            # Alle anderen technischen Check-Felder werden ebenfalls aus der
            # CSV übernommen, sofern Gemini die entsprechende Zeile ausgegeben
            # hat. Fehlende technische Zeilen werden nicht erfunden.
            pos_block, _ = pattern.subn(
                lambda m, v=value: m.group(1) + v,
                pos_block,
                count=1,
            )

        replacements.append((start, end, pos_block))

    # Jede offene CSV-Position muss genau einmal im Gemini-Block auftauchen.
    for source_key in expected:
        if seen.get(source_key, 0) == 0:
            source = expected[source_key]
            errors.append(
                f"{source['name']} ({source['ticker']}) | Einstieg: {source['entry']} | "
                f"Einstiegsdatum: {source['date']}: fehlt im Gemini-Output"
            )

    if errors:
        raise RuntimeError(
            "Offene Positionen konnten nicht verbindlich gegen "
            "Offene Positionen+Check.csv abgeglichen werden: "
            + " | ".join(errors)
        )

    # Ersetzungen rückwärts anwenden, damit Positionen ihre Original-Indizes behalten.
    for start, end, pos_block in sorted(replacements, reverse=True):
        block = block[:start] + pos_block + block[end:]

    text = text[:match.start()] + block + text[match.end():]
    return text


def _lese_makro_datenqualitaet(makro_text):
    """Liest die autoritative Gesamt-Datenqualitaet aus dem Makro-Datenpaket."""
    if not makro_text:
        return None
    for pattern in (
        r"MAKRO-DATENQUALITAET\s*[:=]\s*(VOLLSTAENDIG|EINGESCHRAENKT|UNZUREICHEND)",
        r"DATENQUALITAET\s*[:=]\s*(VOLLSTAENDIG|EINGESCHRAENKT|UNZUREICHEND)",
    ):
        m = re.search(pattern, makro_text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def _normalisiere_makro_datenqualitaet(text, makro_datenqualitaet):
    """Belasst den autoritativen Makro-Status ausschliesslich in Abschnitt 4.

    Die drei Statuszeilen werden bereits von _kanonisiere_ausgabestruktur()
    1:1 aus Makro_Briefing uebernommen. Diese Funktion greift daher nicht
    mehr in andere Abschnitte ein und kann insbesondere keine Positionsfelder
    oder Glossartexte versehentlich veraendern.
    """
    return text

def speichere_ergebnis(text):
    heute = datetime.date.today().isoformat()
    ausgabe_datei = f"Auswertung({heute}).txt"
    text = _kanonisiere_ausgabestruktur(text)
    text = normalisiere_ausgabe(
        text,
        zielzonen=_technische_zielzonen_quelle("Offene Positionen+Check.csv"),
    )
    with open(ausgabe_datei, "w", encoding="utf-8-sig") as f:
        f.write(text)
    print(f"\nGespeichert: {ausgabe_datei}")
    return ausgabe_datei


if __name__ == "__main__":
    print("Gemini-Auswertung gestartet...")
    ergebnis_text = gemini_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(ergebnis_text)
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
