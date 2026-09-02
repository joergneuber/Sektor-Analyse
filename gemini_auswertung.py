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
from pathlib import Path
import sys
import glob
import re
import csv
import time
import json
import datetime
import zipfile
import xml.etree.ElementTree as ET

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
DRITTER_FALLBACK_MODELL = "gemini-3.6-flash"  # Zweiter Fallback bei 503-Ueberlast
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


def _excel_seriennummer_datum(value):
    """Wandelt Excel-Seriendaten (1900-System) ohne externe Abhaengigkeit um."""
    try:
        seriennummer = float(str(value).strip())
    except (TypeError, ValueError):
        return str(value or "").strip()
    basis = datetime.datetime(1899, 12, 30)
    return (basis + datetime.timedelta(days=seriennummer)).date().isoformat()


def _lese_geschlossene_positionen_tab2(xlsx_pfad, referenzdatum=None):
    """Liest ausschliesslich Tab 2 'Geschlossene Positionen' aus der XLSX."""
    if not xlsx_pfad or not os.path.isfile(xlsx_pfad):
        return []
    if referenzdatum is None:
        referenzdatum = datetime.date.today()
    elif isinstance(referenzdatum, str):
        referenzdatum = datetime.date.fromisoformat(referenzdatum)
    grenze = referenzdatum - datetime.timedelta(days=2)
    ns = {"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    try:
        with zipfile.ZipFile(xlsx_pfad, "r") as z:
            try:
                ssroot=ET.fromstring(z.read("xl/sharedStrings.xml"))
                strings=["".join(t.text or "" for t in si.iter("{%s}t"%ns["m"])) for si in ssroot.findall("m:si",ns)]
            except KeyError:
                strings=[]
            wb=ET.fromstring(z.read("xl/workbook.xml"))
            rels=ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            relmap={rel.get("Id"):rel.get("Target") for rel in rels}
            target=None
            for sh in wb.findall("m:sheets/m:sheet",ns):
                if sh.get("name","").strip().lower()=="geschlossene positionen":
                    target=relmap.get(sh.get("{%s}id"%ns["r"]))
                    break
            if not target: return []
            target=target.lstrip("/")
            if not target.startswith("xl/"): target="xl/"+target
            root=ET.fromstring(z.read(target))
            rows=[]
            for row in root.findall(".//m:row",ns):
                vals={}
                for cell in row.findall("m:c",ns):
                    ref=cell.get("r",""); m=re.match(r"[A-Z]+",ref)
                    if not m: continue
                    v=cell.find("m:v",ns)
                    if cell.get("t")=="inlineStr": value="".join(t.text or "" for t in cell.iter("{%s}t"%ns["m"]))
                    elif v is None: value=""
                    else:
                        value=v.text or ""
                        if cell.get("t")=="s":
                            try: value=strings[int(value)]
                            except (ValueError,IndexError): value=""
                    vals[m.group(0)]=value
                rows.append(vals)
            if len(rows)<3: return []
            headers=rows[1]; hm={str(v).strip():k for k,v in headers.items()}
            required=["Ticker","Name","Status","Ausstiegsdatum","Ausstiegskurs","Performance_Seit_Einstieg%"]
            if any(x not in hm for x in required): return []
            out=[]
            for row in rows[2:]:
                status=str(row.get(hm["Status"],"")).strip()
                if status.lower() not in {"gestoppt","geschlossen","verkauft","manuell verkauft"}: continue
                d=_excel_seriennummer_datum(row.get(hm["Ausstiegsdatum"],""))
                try: ed=datetime.date.fromisoformat(d)
                except ValueError: continue
                if not (grenze <= ed <= referenzdatum): continue
                def val(k): return str(row.get(hm[k],"") or "").strip()
                out.append({"ticker":val("Ticker"),"name":val("Name"),"status":status,
                            "ausstiegsdatum":d,"ausstiegskurs":val("Ausstiegskurs"),
                            "performance":val("Performance_Seit_Einstieg%")})
            return out
    except (OSError,zipfile.BadZipFile,ET.ParseError):
        return []


def _geschlossene_positionen_7_4_block(xlsx_pfad, referenzdatum=None):
    positionen=_lese_geschlossene_positionen_tab2(xlsx_pfad,referenzdatum)
    if not positionen: return ""
    lines=["7.4 GESCHLOSSENE POSITIONEN – LETZTE 3 TAGE",""]
    for p in positionen:
        detail=f"Status: {p['status']} | Ausstiegsdatum: {p['ausstiegsdatum']}"
        if p["ausstiegskurs"]: detail+=f" | Ausstiegskurs: {p['ausstiegskurs']}"
        if p["performance"]: detail+=f" | Performance seit Einstieg: {p['performance']}%"
        lines.append(f"{p['name']} ({p['ticker']}) | {detail}"); lines.append("")
    return "\n".join(lines).rstrip()


def _normalisiere_geschlossene_positionen_7_4(text,xlsx_pfad,referenzdatum=None):
    if not text: return text
    text=re.sub(r"(?ims)^7\.4\s+GESCHLOSSENE POSITIONEN\s*[–-]\s*LETZTE 3 TAGE\s*$.*?(?=^8\.\s+|\Z)","",text)
    block=_geschlossene_positionen_7_4_block(xlsx_pfad,referenzdatum)
    if not block: return text
    anchor=re.search(r"(?im)^8\.\s+",text)
    if not anchor: return text.rstrip()+"\n\n"+block+"\n"
    return text[:anchor.start()].rstrip()+"\n\n"+block+"\n\n"+text[anchor.start():].lstrip()


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

                # Zusaetzliche Stammdaten werden nur zur Darstellung verwendet.
                # Sie veraendern keine Analyse-/Tradinglogik.
                meta_fields = [
                    "Sektor", "Markt", "Waehrung", "Richtung", "Ideen_Quelle",
                    "Instrumentart", "Zeithorizont", "Steuerungsart", "Aktueller_Kurs",
                    "Performance_Seit_Einstieg%", "Stop_Aktuell", "TP1_Original",
                    "TP2_Original", "Abstand_Stop_%", "Gesicherter_Gewinn_%",
                    "Potenzial_TP1_%", "Potenzial_TP2_%", "Potenzial_Analyst_%",
                    "Analysten_Ziel", "Analysten_Ziel_Stand", "WKN",
                    "Zertifikat_Einstieg", "Zertifikat_Ausstieg", "TP_Hinweis",
                    "Alert_Hinweis", "Ereignis_Kontext",
                ]
                meta = {
                    field: (str(row.get(key(field), "") or "").strip() if key(field) else "")
                    for field in meta_fields
                }

                result[pos_key] = {
                    "name": name,
                    "ticker": ticker,
                    "entry": entry,
                    "date": date,
                    "technical": technical_values,
                    "meta": meta,
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

def _enthaelt_abschnitt_7(text):
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


def _abschnitt_7_pruefdiagnose(text, csv_pfad):
    """Prueft Punkt 7 wie bisher und liefert bei FAIL die konkreten Gruende.

    Reine Diagnoseebene: Die Zuordnungslogik und der Positionsschluessel
    Name + Ticker + Einstieg + Einstiegsdatum bleiben unveraendert.
    """
    errors = []
    if not _enthaelt_abschnitt_7(text):
        return False, ["Abschnitt '7. OFFENE POSITIONEN' fehlt"]

    expected = _technische_zielzonen_quelle(csv_pfad)
    match = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*\d+\.\s+|\Z)",
        text,
    )
    if not match:
        return False, ["Abschnitt '7. OFFENE POSITIONEN' konnte nicht abgegrenzt werden"]

    block = match.group(0)
    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\(([^()]+)\)\s*\|\s*Markt:\s*[^\n]+$"
    )
    headers = list(header_re.finditer(block))
    if not headers:
        return False, ["Keine gueltigen Positionskoepfe in Punkt 7"]

    seen = set()
    matched_keys = set()
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
            errors.append(f"Einstieg fehlt: {name} ({ticker})")
            continue

        entry = _positionsfeld_schluessel(entry_match.group(1).strip())
        if entry_match.group(2):
            date = _normalisiere_datum(entry_match.group(2).strip())
        else:
            date_match = re.search(
                r"(?im)^\s*Einstiegsdatum\s*:\s*([^\n]+)\s*$",
                pos_block,
            )
            if not date_match:
                errors.append(f"Einstiegsdatum fehlt: {name} ({ticker}) | Einstieg: {entry}")
                continue
            date = _normalisiere_datum(date_match.group(1).strip())

        key = (
            _normalisiere_positionsname(name),
            _normalisiere_ticker(ticker),
            entry,
            date,
        )
        try:
            source = _finde_quellposition(key, expected)
        except Exception as exc:
            errors.append(f"Nicht eindeutig zuordenbar: {name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date} | {exc}")
            continue
        if source is None:
            errors.append(f"Keine passende CSV-Position: {name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}")
            continue

        source_key = (
            _normalisiere_positionsname(source["name"]),
            _normalisiere_ticker(source["ticker"]),
            _positionsfeld_schluessel(source["entry"]),
            _normalisiere_datum(source["date"]),
        )
        if source_key in seen:
            errors.append(f"CSV-Position mehrfach ausgegeben: {source['name']} ({source['ticker']}) | Einstieg: {source['entry']} | Einstiegsdatum: {source['date']}")
            continue
        seen.add(source_key)
        matched_keys.add(source_key)

    missing = set(expected) - matched_keys
    for key in sorted(missing):
        source = expected[key]
        errors.append(f"CSV-Position fehlt in Gemini-Block: {source['name']} ({source['ticker']}) | Einstieg: {source['entry']} | Einstiegsdatum: {source['date']}")

    if len(matched_keys) != len(expected):
        errors.append(f"Positionsanzahl nicht vollstaendig: erkannt {len(matched_keys)}, erwartet {len(expected)}")

    return not errors, errors


def _abschnitt_7_vollstaendig(text, csv_pfad):
    """Bestehender boolescher Validator; Diagnose bleibt separat."""
    ok, _ = _abschnitt_7_pruefdiagnose(text, csv_pfad)
    return ok


def _fuege_abschnitt_7_ein(original_text, abschnitt_8):
    """Fügt einen ausschließlich für Punkt 7 angeforderten Gemini-Block ein.

    Der Reparatur-Call darf nur Punkt 7 liefern. Der Block wird deshalb nicht
    als komplette neue Auswertung verwendet, sondern deterministisch in die
    bestehende Antwort vor den nächsten nummerierten Hauptabschnitt eingesetzt.
    """
    if not _enthaelt_abschnitt_7(abschnitt_8):
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
    # Ersetze den bereits vorhandenen Punkt-8-Block vollständig durch
    # den erfolgreich reparierten Punkt-8-Block.
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
            geschlossene_xlsx = next((p for p in (glob.glob("Offene Positionen+Check.xlsx") + glob.glob("Offene Positionen+Check(*).xlsx")) if os.path.isfile(p)), None)
            geschlossene_7_4 = _geschlossene_positionen_7_4_block(geschlossene_xlsx)
            antwort = client.models.generate_content(
                model=aktuelles_modell,
                contents=hochgeladene_teile + [
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. Die Dateien Bitcoin_Trading_DE_Briefing.txt, Gold_Trading_DE_Briefing.txt und Silber_Trading_DE_Briefing.txt sind ausschließlich qualitative externe YouTube-Quellen. Nutze sie nur als Kontext/Abgleich; sie dürfen niemals objektive Kursdaten, technische Check-Felder, CRV, Setup-Scores, Filter, Setup-Qualität oder Handelsentscheidungen verändern. Wenn eine solche Datei fehlt, ist das kein Fehler und es darf nichts daraus erfunden werden. "
                    "ERSTELLE in der fertigen Auswertung zusätzlich eine feste Sektion mit exakt der Überschrift 'EXTERNE MARKTQUELLEN'. Gliedere sie getrennt nach 'Bitcoin', 'Gold' und 'Silber'. Für jeden Markt nenne die Anzahl der tatsächlich in der jeweiligen bereitgestellten Briefing-Datei enthaltenen relevanten Videos. WICHTIG: Zähle und verarbeite jedes vorhandene Video einzeln anhand jedes einzelnen 'Titel:'-Blocks bzw. Video-Blocks. Wenn die Briefing-Datei beispielsweise 3 relevante Videos enthält, müssen in der fertigen Auswertung genau diese 3 Videos einzeln erscheinen. Kein Video darf wegen Kürze, Ähnlichkeit, Redundanz oder eigener Auswahl des Modells weggelassen, zusammengefasst oder durch ein anderes ersetzt werden. Führe für JEDES vorhandene relevante Video separat Titel und eine kurze Kernaussage auf und ordne JEDE einzelne Aussage ausschließlich im Verhältnis zur bestehenden Systemanalyse als 'BESTÄTIGT', 'WIDERSPRICHT' oder 'NEUTRAL' ein. Die Anzahl muss mit der Zahl der tatsächlich einzeln aufgeführten Videos übereinstimmen. Ergänze bei jedem Markt ausdrücklich 'Technische Auswirkung: KEINE'. Wenn für einen Markt keine relevanten Videos in der bereitgestellten Briefing-Datei vorhanden sind oder die Datei fehlt, schreibe ausdrücklich 'Keine neuen relevanten Videos verarbeitet'. Verwende für Titel und Kernaussagen ausschließlich die Inhalte der bereitgestellten YouTube-Briefing-Dateien; ergänze nichts aus allgemeinem Modellwissen und erfinde nichts. Die Einordnung darf keine technische Berechnung oder Entscheidung verändern. Die externe Quelle ist ausschließlich qualitativer Kontext. Eine Übereinstimmung mit der externen Quelle ist keine technische Bestätigung; eine Abweichung ist kein technischer Ausschluss. Eine Aussage wie '1 Video' ist nur zulässig, wenn tatsächlich genau 1 relevanter Video-Block in der betreffenden Briefing-Datei vorhanden ist. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben "
                    "und erstelle die vollstaendige Daten-Uebersicht. "
                    "AUTORITATIVE OFFENE-POSITIONEN-LISTE (ausschließlich aus Offene Positionen+Check.csv):\n"
                    + (offene_quelle or "(keine offenen Positionen gefunden)") + "\n"
                    "AUTORITATIVE FAKTENBASIS FUER 7.4 AUS TAB 2 VON Offene Positionen+Check.xlsx:\n"
                    + (geschlossene_7_4 or "(keine geschlossene Position innerhalb der letzten 3 Kalendertage)") + "\n"
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
                    "Keine alternativen Kopfzeilenformate und keine Positionsköpfe ohne Ticker. "
                    "WICHTIG FUER DIE ARCHITEKTUR: Gemini bestimmt NICHT die Vollstaendigkeit von 7.3. "
                    "Python baut 7.3 nach der Gemini-Antwort deterministisch aus ALLEN offenen Masterpositionen neu auf. "
                    "Eine im Master vorhandene Position muss deshalb auch dann in 7.3 erscheinen, wenn Gemini sie nicht analysiert. "
                    "Fuer eine nicht analysierte Masterposition darf keine qualitative Analyse erfunden werden. "
                    "Eine Position, die nicht im Master vorhanden ist, darf niemals in 7.3 aufgenommen werden.",
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

            # Punkt 7 ist KEIN Gemini-Vollstaendigkeits-Gate mehr.
            # Der Master-Neuaufbau erfolgt deterministisch in normalisiere_ausgabe()
            # und erzeugt dort aus jeder offenen Masterposition genau einen 7.3-Block.

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


def _entferne_unerwuenschte_watchlists(text):
    """Entfernt unerwuenschte Watchlist-/Beinahe-Kandidaten-Bloecke.
    Die HEBELTRADER-Watchlist unter 6.5.2 bleibt vollstaendig erhalten.
    Keine Scanner-, Filter- oder Berechnungslogik wird veraendert.
    """
    if not text:
        return text

    def _bereinige_abschnitt(text, start_heading, end_heading):
        start = re.search(r"(?ims)^\ufeff?\s*" + re.escape(start_heading) + r"\s*$", text)
        if not start:
            return text
        end = re.search(r"(?ims)^\s*" + re.escape(end_heading) + r"\s*$", text[start.end():])
        end_pos = start.end() + end.start() if end else len(text)
        block = text[start.start():end_pos]

        # Einen erkannten unerwuenschten Block bis zur naechsten
        # Abschnittsgrenze entfernen. Dadurch bleiben auch Eintragszeilen
        # erhalten, die nach der Watchlist-Ueberschrift ohne eigene
        # Watchlist-Markierung folgen.
        marker = re.search(
            r"(?i)(?:WATCHLIST|RISIKO-WATCH|BEINAHE[- ]KANDIDAT|DIVERGENZ-WATCHLIST)",
            block,
        )
        if marker:
            line_start = block.rfind("\n", 0, marker.start()) + 1
            prefix = block[:line_start].rstrip()
            block = prefix + "\n"
        return text[:start.start()] + block + text[end_pos:]

    text = _bereinige_abschnitt(text, "1. DAS WICHTIGSTE AUF EINEN BLICK", "2. MAKRO & MARKT")
    text = _bereinige_abschnitt(text, "6.2 TRENDFOLGE", "6.3 TRENDWENDE")
    text = _bereinige_abschnitt(text, "6.3 TRENDWENDE", "6.4 LANGFRIST")
    text = _bereinige_abschnitt(text, "6.6 SHORT", "6.7 EDELMETALLE")
    return re.sub(r"\n{3,}", "\n\n", text)



def _legacy_sections(text):
    """Zerlegt Gemini ausschließlich anhand der bekannten Legacy-Quellüberschriften."""
    if not text:
        return {}
    rx = re.compile(
        r"(?im)^(?:\ufeff)?\s*("
        r"1\.\s*MARKTUMFELD & GLOBALE RISIKOLAGE|"
        r"2\.\s*MAKRO-ZUKUNFTSSZENARIO|"
        r"3\.\s*TRENDFOLGE-SETUPS|"
        r"4\.\s*TRENDWENDE-SETUPS[^\n]*|"
        r"5\.\s*HEBELTRADER-SETUPS|"
        r"6\.\s*SHORT-SETUPS[^\n]*|"
        r"7\.\s*EDELMETALLE-SETUPS|"
        r"8\.\s*OFFENE POSITIONEN[^\n]*|"
        r"9\.\s*GESCHLOSSENE POSITIONEN[^\n]*|"
        r"METHODIK & LESEHILFE|EXTERNE MARKTQUELLEN|"
        r"PERSPEKTIVISCHE TRADE-IDEEN|LIVE-PERFORMANCE vs\. MSCI WORLD|"
        r"KURZ-ZUSAMMENFASSUNG|RISIKO-WATCH|WOCHENAUSBLICK|SYSTEM-STATISTIK"
        r")\s*$"
    )
    ms = list(rx.finditer(text))
    out = {}
    for i,m in enumerate(ms):
        key = re.sub(r"\s+", " ", m.group(1).strip()).upper()
        end = ms[i+1].start() if i+1 < len(ms) else len(text)
        out[key] = text[m.start():end].strip()
    return out


def _strip_watchlists(block):
    """Entfernt komplette unerwünschte Watchlist-/Beinahe-Blöcke."""
    if not block:
        return ""
    m = re.search(
        r"(?im)^\s*(?:WATCHLIST(?:\s*\([^\n]*\))?|RISIKO-WATCH|"
        r"DIVERGENZ-WATCHLIST|BEINAHE-KANDIDATEN?)\s*$",
        block,
    )
    if m:
        block = block[:m.start()].rstrip()
    block = re.sub(r"(?im)^\s*Engstelle des Filters:[^\n]*\n?", "", block)
    return re.sub(r"\n{3,}", "\n\n", block).strip()


def _extract_summary_without_watchlist(text):
    raw = _legacy_sections(text).get("KURZ-ZUSAMMENFASSUNG", "")
    raw = re.sub(r"(?im)^KURZ-ZUSAMMENFASSUNG\s*$", "", raw, count=1).strip()
    return re.split(r"(?im)^\s*(?:WATCHLIST|RISIKO-WATCH)\s*$", raw, maxsplit=1)[0].strip()


def _legacy_source(sec, *names):
    for name in names:
        if name in sec:
            return sec[name]
    return ""


def _langfrist_ausgabe_block():
    """Übernimmt vorhandene wöchentliche Langfrist-Dateien ohne Neuberechnung."""
    files = sorted(glob.glob("Langfrist_Bewertung(*).csv"))
    briefs = sorted(glob.glob("Langfrist_Briefing(*).txt"))
    parts = []
    if files:
        try:
            raw = Path(files[-1]).read_text(encoding="utf-8-sig").strip()
            if raw:
                parts.append(f"Quelle: {Path(files[-1]).name}\n{raw}")
        except OSError:
            pass
    if briefs:
        try:
            raw = Path(briefs[-1]).read_text(encoding="utf-8-sig").strip()
            if raw:
                parts.append(f"Quelle: {Path(briefs[-1]).name}\n{raw}")
        except OSError:
            pass
    return "\n\n".join(parts).strip()


def _a_meldungen_ausgabe_block():
    files = sorted(glob.glob("Einzel_Check_A_Meldungen(*).txt"))
    if not files:
        return ""
    try:
        raw = Path(files[-1]).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return raw


def _hebeltrader_watchlist_ausgabe_block():
    """Vollständige JSON-Watchlist; keine Auswahl/Filterung."""
    pfad = finde_datei([BEOBACHTUNGSLISTE_DATEI, "einzel_check_beobachtung*.json"])
    if not pfad:
        return ""
    try:
        daten = json.loads(Path(pfad).read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    if not isinstance(daten, dict):
        return ""
    lines = ["6.5.2 HEBELTRADER-Watchlist / Beobachtungsliste", ""]
    for ticker, info in daten.items():
        info = info if isinstance(info, dict) else {}
        name = str(info.get("name") or info.get("unternehmen") or "").strip()
        status = str(info.get("status") or "").strip()
        check = str(info.get("letzter_check") or "").strip()
        label = f"{name} ({ticker})" if name else str(ticker)
        lines.append(f"- {label}")
        if status:
            lines.append(f"  Status: {status}")
        if check:
            lines.append(f"  Letzter Check: {check}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _kanonisiere_ausgabestruktur(text):
    """Legacy-Architektur: Rohsektionen -> p1...p9 -> endgültige Zielstruktur."""
    if not text:
        return text

    sec = _legacy_sections(text)

    summary = _extract_summary_without_watchlist(text)
    system = sec.get("SYSTEM-STATISTIK", "")
    # Punkt 1 bekommt bewusst KEINEN RISIKO-WATCH-/WATCHLIST-Block.
    p1_body = "\n\n".join(x for x in (summary, system) if x.strip()).strip()

    market = _legacy_source(sec, "1. MARKTUMFELD & GLOBALE RISIKOLAGE")
    market = re.sub(r"(?im)^1\.\s*MARKTUMFELD & GLOBALE RISIKOLAGE\s*$", "", market, count=1).strip()

    live = _legacy_source(sec, "LIVE-PERFORMANCE VS. MSCI WORLD")
    live = re.sub(r"(?im)^LIVE-PERFORMANCE vs\. MSCI WORLD\s*$", "", live, count=1).strip()

    macro = _legacy_source(sec, "2. MAKRO-ZUKUNFTSSZENARIO")
    macro = re.sub(r"(?im)^2\.\s*MAKRO-ZUKUNFTSSZENARIO\s*$", "", macro, count=1).strip()
    macro = re.sub(r"(?m)^2\.1\s+", "5.1 ", macro)
    macro = re.sub(r"(?m)^2\.2\s+", "5.2 ", macro)
    macro = re.sub(r"(?m)^2\.[34]\s+", "5.3 ", macro)

    perspective = sec.get("PERSPEKTIVISCHE TRADE-IDEEN", "")
    trend = sec.get("3. TRENDFOLGE-SETUPS", "")
    reversal = next((v for k,v in sec.items() if k.startswith("4. TRENDWENDE-SETUPS")), "")
    leverage = sec.get("5. HEBELTRADER-SETUPS", "")
    short = next((v for k,v in sec.items() if k.startswith("6. SHORT-SETUPS")), "")
    metals = sec.get("7. EDELMETALLE-SETUPS", "")
    external = sec.get("EXTERNE MARKTQUELLEN", "")

    def setup(title, body):
        body = re.sub(
            r"(?im)^\s*\d+\.\s*(?:TRENDFOLGE|TRENDWENDE|HEBELTRADER|SHORT|EDELMETALLE)-SETUPS[^\n]*\s*$",
            "", body or ""
        )
        body = _strip_watchlists(body)
        return f"{title}\n\n{body.strip()}".strip() if body.strip() else ""

    p6_parts = [
        setup("6.1 PERSPEKTIVISCHE TRADE-IDEEN", re.sub(r"(?im)^PERSPEKTIVISCHE TRADE-IDEEN\s*$", "", perspective, count=1)),
        setup("6.2 TRENDFOLGE", trend),
        setup("6.3 TRENDWENDE", reversal),
        ("6.4 LANGFRIST\n\n" + _langfrist_ausgabe_block()).strip()
        if _langfrist_ausgabe_block() else "6.4 LANGFRIST",
    ]

    ht = ["6.5 HEBELTRADER"]
    valid_ht = setup("6.5.1 VALIDE HEBELTRADER-SETUPS", leverage)
    if valid_ht:
        ht.append(valid_ht)
    watch = _hebeltrader_watchlist_ausgabe_block()
    if watch:
        ht.append(watch)
    a = _a_meldungen_ausgabe_block()
    if a:
        ht.append("6.5.3 A-KANDIDATEN / EINZEL-CHECK-MELDUNGEN\n\n" + a)
    p6_parts.append("\n\n".join(ht))
    p6_parts.append(setup("6.6 SHORT", short))

    metals_setup_body = re.sub(r"(?im)^\s*7\.\s*EDELMETALLE-SETUPS\s*$", "", metals or "", count=1).strip()
    metal_body = "\n\n".join(x for x in (_metals_information_block(), _strip_watchlists(metals_setup_body)) if x)
    p6_parts.append("6.7 EDELMETALLE" + (f"\n\n{metal_body}" if metal_body else ""))
    if external:
        p6_parts.append(setup("6.8 EXTERNE QUELLEN / WEITERE ANSÄTZE", external))
    p6_body = "\n\n".join(x for x in p6_parts if x).strip()

    # OFFENE POSITIONEN: Gemini darf die Analyse liefern, aber die
    # vollständige Positionsidentität kommt deterministisch aus der
    # Masterdatei. Dadurch können insbesondere Mehrfachpositionen desselben
    # Tickers nicht durch einen unvollständigen Gemini-Block verloren gehen.
    openpos = _legacy_source(
        sec,
        "8. OFFENE POSITIONEN (MANUELL BESTÄTIGT)",
        "8. OFFENE POSITIONEN",
        "7. OFFENE POSITIONEN (MANUELL BESTÄTIGT)",
        "7. OFFENE POSITIONEN",
    )
    openpos = re.sub(
        r"(?im)^\s*(?:7|8)\.\s*OFFENE POSITIONEN[^\n]*\s*$", "", openpos, count=1
    ).strip()
    p7 = (
        "7. OFFENE POSITIONEN\n\n"
        "7.1 PORTFOLIO-ÜBERSICHT\n\n"
        "7.2 HANDLUNGSBEDARF\n\n"
        "7.3 EINZELPOSITIONEN\n\n"
        + (openpos or "")
    )

    # 7.4 wird ausschließlich durch _normalisiere_geschlossene_positionen_7_4
    # aus Tab 2 ergänzt. Niemals eine alte 10-Tage-Ausgabe übernehmen.
    outlook = re.sub(r"(?im)^WOCHENAUSBLICK\s*$", "", sec.get("WOCHENAUSBLICK", ""), count=1).strip()
    method = re.sub(r"(?im)^METHODIK & LESEHILFE\s*$", "", sec.get("METHODIK & LESEHILFE", ""), count=1).strip()

    p4 = _macro_status_block().strip()
    parts = [
        "1. DAS WICHTIGSTE AUF EINEN BLICK" + (f"\n\n{p1_body}" if p1_body else ""),
        "2. MAKRO & MARKT" + (f"\n\n{market}" if market else ""),
        "3. SYSTEMPERFORMANCE & BENCHMARK" + (f"\n\n{live}" if live else ""),
        "4. DATEN- & SZENARIOSTATUS" + (f"\n\n{p4}" if p4 else ""),
        "5. MARKTPERSPEKTIVE" + (f"\n\n{macro}" if macro else ""),
        "6. TRADING-IDEEN & SETUPS" + (f"\n\n{p6_body}" if p6_body else ""),
        p7,
        "8. AUSBLICK & KEY EVENTS" + (f"\n\n{outlook}" if outlook else ""),
        "9. METHODIK & DATENHINWEISE" + (f"\n\n{method}" if method else ""),
    ]
    return "\n\n".join(x.strip() for x in parts if x.strip()).strip() + "\n"

def _master_position_key(source):
    return (
        _normalisiere_positionsname(source["name"]),
        _normalisiere_ticker(source["ticker"]),
        _positionsfeld_schluessel(source["entry"]),
        _positionsfeld_schluessel(source["date"]),
    )


def _gemini_positionsbloecke(block):
    """Zerlegt 7.3 in Positionsbloecke, ohne deren Vollstaendigkeit vorauszusetzen."""
    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\(([^()]+)\)\s*\|\s*Markt:\s*[^\n]+$"
    )
    headers = list(header_re.finditer(block))
    result = []
    for idx, header in enumerate(headers):
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(block)
        raw = block[header.start():end].strip()
        em = re.search(
            r"(?im)^\s*Einstieg(?:skurs)?\s*:\s*([^\n(]+?)(?:\s*\(([^)]+)\))?\s*$",
            raw,
        )
        entry = em.group(1).strip() if em else ""
        date = em.group(2).strip() if em and em.group(2) else ""
        if not date:
            dm = re.search(r"(?im)^\s*Einstiegsdatum\s*:\s*([^\n|]+?)\s*$", raw)
            date = dm.group(1).strip() if dm else ""
        result.append({
            "name": header.group(1).strip(),
            "ticker": header.group(2).strip(),
            "entry": entry,
            "date": date,
            "raw": raw,
        })
    return result


def _finde_gemini_block_fuer_master(source, gemini_blocks, used):
    """Ordnet hoechstens einen Gemini-Block einer Masterposition zu.

    Bei Mehrfachpositionen desselben Tickers ist ausschliesslich der vollstaendige
    Positionsschluessel zulaessig. Ohne Einstieg+Datum wird niemals geraten.
    """
    target = _master_position_key(source)

    exact = []
    for i, gb in enumerate(gemini_blocks):
        if i in used or not gb["entry"] or not gb["date"]:
            continue
        key = (
            _normalisiere_positionsname(gb["name"]),
            _normalisiere_ticker(gb["ticker"]),
            _positionsfeld_schluessel(gb["entry"]),
            _positionsfeld_schluessel(gb["date"]),
        )
        if key == target:
            exact.append(i)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(
            f"Mehrfacher Gemini-Block fuer Masterposition: {source['name']} "
            f"({source['ticker']}) | Einstieg: {source['entry']} | Einstiegsdatum: {source['date']}"
        )

    # Eindeutiger Name+Ticker darf fehlende/abweichende Einstieg-/Datumsdarstellung
    # korrigieren, weil die Masterdatei anschliessend die Werte verbindlich setzt.
    candidates = []
    for i, gb in enumerate(gemini_blocks):
        if i in used:
            continue
        if (_normalisiere_positionsname(gb["name"]) == target[0]
                and _normalisiere_ticker(gb["ticker"]) == target[1]):
            candidates.append(i)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RuntimeError(
            f"Nicht eindeutige Gemini-Zuordnung fuer {source['name']} ({source['ticker']}): "
            "mehrere Bloecke mit gleichem Name+Ticker. Einstieg und Einstiegsdatum muessen "
            "zur eindeutigen Zuordnung vorhanden sein."
        )

    # Ein eindeutiger Ticker darf nur dann als Fallback dienen, wenn er im Master
    # ebenfalls genau einmal vorkommt. Bei Mehrfach-Tickern bleibt der Block unzugeordnet.
    same_ticker_master = [
        k for k in []  # bewusst leer; Mastereindeutigkeit wird beim Aufrufer geprueft
    ]
    del same_ticker_master
    return None


def _master_positionsblock(source_positions, gemini_blocks):
    """Erzeugt 7.3 deterministisch aus ALLEN Masterpositionen.

    Gemini darf qualitative Zusatzinformationen beisteuern. Existiert fuer eine
    Masterposition kein Gemini-Block, wird trotzdem ein vollstaendiger
    Stammdaten-/Technikblock erzeugt. Eine Position ausserhalb des Masters wird
    nie in 7.3 aufgenommen.
    """
    master_by_ticker = {}
    for source in source_positions:
        master_by_ticker.setdefault(_normalisiere_ticker(source["ticker"]), []).append(source)

    used = set()
    blocks = []
    unmatched_gemini = []

    # Technische Ausgabeetiketten bleiben identisch zur bisherigen Legacy-Ausgabe.
    tech_labels = {
        "Technischer_Zustand": "Technischer Zustand",
        "Trendrichtung": "Trendrichtung",
        "Support/Widerstand": "Support/Widerstand",
        "Breakout_Status": "Breakout Status",
        "A-B-C_Status": "A-B-C Status",
        "Fibonacci_Status/Ziele": "Fibonacci Status/Ziele",
        "Trendkanal": "Trendkanal",
        "Measured Move": "Measured Move",
        "Formation": "Formation",
        "Round Number": "Round Number",
        "Major Resistance": "Major Resistance",
        "Ueberdehnung": "Ueberdehnung",
        "Relative Staerke_Sektor": "Relative Staerke_Sektor",
        "Konfluenz": "Konfluenz",
        "Retest_Support": "Retest_Support",
        "Technische_Zielzone": "Technische Zielzone",
        "Datenqualitaet": "Datenqualitaet",
        "Analysehinweis": "Analysehinweis",
    }

    master_keys = {_master_position_key(s) for s in source_positions}

    for source in source_positions:
        # Erst vollstaendiger Schluessel, danach nur eindeutiger Name+Ticker.
        idx = _finde_gemini_block_fuer_master(source, gemini_blocks, used)
        if idx is None:
            # Ein eindeutiger Ticker darf ebenfalls zugeordnet werden, aber niemals
            # bei Mehrfachpositionen desselben Tickers.
            ticker = _normalisiere_ticker(source["ticker"])
            if len(master_by_ticker.get(ticker, [])) == 1:
                candidates = [
                    i for i, gb in enumerate(gemini_blocks)
                    if i not in used and _normalisiere_ticker(gb["ticker"]) == ticker
                ]
                if len(candidates) == 1:
                    idx = candidates[0]
                elif len(candidates) > 1:
                    raise RuntimeError(
                        f"Mehrere Gemini-Bloecke fuer eindeutigen Master-Ticker {source['ticker']}; "
                        "keine Zuordnung geraten."
                    )

        gb = gemini_blocks[idx] if idx is not None else None
        if idx is not None:
            used.add(idx)

        meta = source.get("meta", {})
        lines = [
            f"{source['name']} ({source['ticker']}) | Markt: {meta.get('Markt', '') or 'nicht aus Quelle vorhanden'}",
        ]
        for label, field in [
            ("Sektor", "Sektor"), ("Richtung", "Richtung"), ("Quelle", "Ideen_Quelle"),
        ]:
            value = meta.get(field, "")
            if value:
                lines.append(f"{label}: {value}")
        lines.append(f"Einstieg: {source['entry']} ({source['date']})")
        for label, field, suffix in [
            ("Aktuell", "Aktueller_Kurs", ""),
            ("Performance", "Performance_Seit_Einstieg%", "%"),
            ("Stop", "Stop_Aktuell", ""),
            ("TP1", "TP1_Original", ""),
            ("TP2", "TP2_Original", ""),
        ]:
            value = meta.get(field, "")
            if value:
                lines.append(f"{label}: {value}{suffix}")

        technical = source.get("technical", {})
        for field, label in tech_labels.items():
            value = technical.get(field)
            if value not in (None, ""):
                lines.append(f"{label}: {value}")

        # Qualitative Gemini-Zusatzdaten: Stammdaten und technische Masterfelder
        # werden aus dem Gemini-Block entfernt, damit sie nicht doppelt erscheinen.
        if gb:
            extra = gb["raw"]
            extra = re.sub(r"(?m)^[^\n]*\|\s*Markt:\s*[^\n]*$\n?", "", extra, count=1)
            extra = re.sub(r"(?im)^\s*(?:Einstieg(?:skurs)?|Einstiegsdatum)\s*:\s*[^\n]+$\n?", "", extra)
            duplicate_labels = [
                "Sektor", "Richtung", "Quelle", "Aktuell", "Performance", "Stop", "TP1", "TP2",
                "Technischer Zustand", "Trendrichtung", "Support/Widerstand", "Breakout", "Breakout Status",
                "A-B-C", "A-B-C Status", "Fibonacci", "Fibonacci Status/Ziele", "Trendkanal", "Measured Move",
                "Formation", "Round Number", "Major Resistance", "Ueberdehnung", "Überdehnung",
                "Relative Staerke Sektor", "Relative Staerke_Sektor", "Konfluenz", "Retest_Support",
                "Technische Zielzone", "Datenqualitaet", "Analysehinweis",
            ]
            for label in duplicate_labels:
                extra = re.sub(r"(?im)^\s*" + re.escape(label) + r"\s*:\s*[^\n]*\n?", "", extra)
            extra = extra.strip()
            if extra:
                lines.append(extra)

        blocks.append("\n".join(lines).strip())

    # Gemini darf keine fremde Position einschleusen. Das ist bewusst nur eine
    # Diagnose, kein Anlass fuer einen weiteren API-Call.
    for i, gb in enumerate(gemini_blocks):
        if i in used:
            continue
        if gb["entry"] and gb["date"]:
            gkey = (
                _normalisiere_positionsname(gb["name"]),
                _normalisiere_ticker(gb["ticker"]),
                _positionsfeld_schluessel(gb["entry"]),
                _positionsfeld_schluessel(gb["date"]),
            )
            if gkey not in master_keys:
                unmatched_gemini.append(
                    f"{gb['name']} ({gb['ticker']}) | Einstieg: {gb['entry']} | Einstiegsdatum: {gb['date']}"
                )

    if unmatched_gemini:
        print("INFO: Gemini-Positionen ausserhalb des Masterbestands werden aus 7.3 entfernt:")
        for item in unmatched_gemini:
            print(f"  - {item}")

    return "\n\n".join(blocks)


def _baue_7_aus_master(text, zielzonen):
    """Ersetzt 7.3 durch einen vollstaendigen Master-Neuaufbau."""
    if not zielzonen:
        raise RuntimeError("Keine offenen Masterpositionen vorhanden; 7.3 kann nicht aufgebaut werden.")
    match = re.search(r"(?ims)^7\. OFFENE POSITIONEN\s*$.*?(?=^8\.\s+|\Z)", text)
    if not match:
        raise RuntimeError("Abschnitt '7. OFFENE POSITIONEN' fehlt; Master-Neuaufbau nicht moeglich.")
    block = match.group(0)
    m73 = re.search(r"(?ims)^7\.3\s+EINZELPOSITIONEN\s*$", block)
    if not m73:
        raise RuntimeError("Unterabschnitt 7.3 fehlt; Master-Neuaufbau nicht moeglich.")

    prefix = block[:m73.end()]
    old73 = block[m73.end():]
    # Alles nach 7.3 bis Abschnitt 8 sind Kandidaten fuer Gemini-Positionsbloecke.
    gemini_blocks = _gemini_positionsbloecke(old73)
    source_positions = list(zielzonen.values())
    new73 = _master_positionsblock(source_positions, gemini_blocks)
    rebuilt = prefix.rstrip() + "\n\n" + new73.strip() + "\n"
    return text[:match.start()] + rebuilt + text[match.end():]


def _validiere_master_7_3(text, zielzonen):
    """Harte Vollstaendigkeitspruefung: exakt alle Masterpositionen, keine Fremdposition."""
    match = re.search(r"(?ims)^7\.3\s+EINZELPOSITIONEN\s*$.*?(?=^8\.\s+|\Z)", text)
    if not match:
        raise RuntimeError("7.3 fehlt nach dem Master-Neuaufbau.")
    blocks = _gemini_positionsbloecke(match.group(0))
    seen = []
    for gb in blocks:
        if not gb["entry"] or not gb["date"]:
            raise RuntimeError(
                f"7.3 enthaelt eine Position ohne vollstaendige Identitaet: {gb['name']} ({gb['ticker']})"
            )
        seen.append((
            _normalisiere_positionsname(gb["name"]),
            _normalisiere_ticker(gb["ticker"]),
            _positionsfeld_schluessel(gb["entry"]),
            _positionsfeld_schluessel(gb["date"]),
        ))
    expected = list(zielzonen.keys())
    if len(seen) != len(expected):
        raise RuntimeError(f"Master-Vollstaendigkeit verletzt: 7.3 enthaelt {len(seen)} Positionen, erwartet {len(expected)}.")
    if len(set(seen)) != len(seen):
        raise RuntimeError("Master-Vollstaendigkeit verletzt: doppelte Position in 7.3.")
    missing = [k for k in expected if k not in set(seen)]
    extra = [k for k in seen if k not in set(expected)]
    if missing or extra:
        msg=[]
        if missing: msg.append("fehlend=" + "; ".join(map(str, missing)))
        if extra: msg.append("fremd=" + "; ".join(map(str, extra)))
        raise RuntimeError("Master-Vollstaendigkeit verletzt: " + " | ".join(msg))

def normalisiere_ausgabe(text, zielzonen=None):
    """Erzwingt formale Regeln und macht die Check-Datei zum Master.

    Gemini liefert die Analyse, aber offene Positions-Stammdaten und
    technische Check-Felder werden deterministisch aus
    Offene Positionen+Check.csv übernommen. Keine technische Berechnung
    findet hier statt.
    """
    if not text:
        return text

    text = _kanonisiere_ausgabestruktur(text)
    # ENTSCHEIDEND: 7.3 wird jetzt vollstaendig aus dem Master neu aufgebaut.
    # Gemini liefert nur qualitative Zusatzinformationen; Gemini bestimmt weder
    # Anzahl noch Existenz der offenen Positionen.
    text = _baue_7_aus_master(text, zielzonen)
    text = _begrenze_ki_positionsfazits(text)
    _validiere_master_7_3(text, zielzonen)

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
    """Sichert die autoritative Datenqualitaet ausschliesslich in Abschnitt 4."""
    if not text or not makro_datenqualitaet:
        return text
    start = text.find("4. DATEN- & SZENARIOSTATUS")
    if start < 0:
        return text
    end = text.find("\n3.", start)
    if end < 0:
        end = len(text)
    section = text[start:end]
    section = re.sub(
        r"(?im)^[^\n]*(?:MAKRO-DATENQUALITAET|Datenqualitaet)\s*[:=][^\n]*\n?",
        "",
        section,
    )
    section = section.rstrip() + f"\nDatenqualitaet: {makro_datenqualitaet}\n"
    return text[:start] + section + text[end:]


def _validiere_finale_ausgabestruktur(text, beobachtungsliste=None):
    """Harte Endpruefung der Darstellung gegen die vereinbarte 1-9-Struktur.
    Diese Funktion prueft nur Ausgabeform, keine Trading-/Analyseberechnung.
    """
    errors = []
    if not text:
        return ["Leere Auswertung"]

    # Hauptabschnitte exakt 1..9 und in Reihenfolge.
    text = text.lstrip("\ufeff")
    heads_all = re.findall(r"(?im)^\ufeff?[ \t]*([1-9])\.[ \t]+([^\n]+)$", text)
    # Hauptstruktur ausschließlich über die neun kanonischen Überschriften
    # der aktuellen Master-Prompt-Zielarchitektur bestimmen.
    _haupttitel = {
        "DAS WICHTIGSTE AUF EINEN BLICK",
        "MAKRO & MARKT",
        "SYSTEMPERFORMANCE & BENCHMARK",
        "DATEN- & SZENARIOSTATUS",
        "MARKTPERSPEKTIVE",
        "TRADING-IDEEN & SETUPS",
        "OFFENE POSITIONEN",
        "AUSBLICK & KEY EVENTS",
        "METHODIK & DATENHINWEISE",
    }
    heads = [
        (n, title) for n, title in heads_all
        if title.strip().upper() in _haupttitel
    ]
    nums = [int(n) for n, _ in heads]
    if nums != list(range(1, 10)):
        errors.append(f"Hauptstruktur ungueltig: {nums}")

    # Verbotene Watchlists ausserhalb HEBELTRADER.
    p1 = re.search(r"(?ims)^1\. DAS WICHTIGSTE AUF EINEN BLICK\s*$.*?(?=^2\.\s+)", text)
    if p1 and re.search(r"(?i)WATCHLIST|RISIKO-WATCH", p1.group(0)):
        errors.append("Punkt 1 enthaelt Watchlist/Risiko-Watch")
    for a,b in [("6.2 TRENDFOLGE","6.3 TRENDWENDE"),("6.3 TRENDWENDE","6.4 LANGFRIST"),("6.6 SHORT","6.7 EDELMETALLE")]:
        m=re.search(r"(?ims)^"+re.escape(a)+r"\s*$.*?(?=^"+re.escape(b)+r"\s*$)",text)
        if m and re.search(r"(?i)WATCHLIST|BEINAHE-KANDIDAT|DIVERGENZ-WATCHLIST",m.group(0)):
            errors.append(f"{a} enthaelt unzulaessige Watchlist/Beinahe-Kandidaten")

    # HEBELTRADER-Unterstruktur: 6.5.1 und 6.5.2 sind Pflicht; 6.5.3 nur bei
    # vorhandener nicht-leerer A-Meldungsdatei (die eigentliche Datei wird separat gesucht).
    m65=re.search(r"(?ims)^6\.5\s+HEBELTRADER\s*$.*?(?=^6\.6\s+SHORT\s*$)",text)
    if not m65:
        errors.append("6.5 fehlt")
    else:
        b=m65.group(0)
        if not re.search(r"(?im)^6\.5\.1\s+",b): errors.append("6.5.1 fehlt")
        if not re.search(r"(?im)^6\.5\.2\s+HEBELTRADER-Watchlist",b): errors.append("6.5.2 fehlt")
        if re.search(r"(?im)^6\.5\.3\s+",b) and not re.search(r"(?im)^6\.5\.2\s+HEBELTRADER-Watchlist",b): errors.append("6.5.3 falsch eingeordnet")

    # 6.5.3 darf ausschließlich bei real vorhandener, nicht leerer A-Meldungsdatei erscheinen.
    a_files = sorted(glob.glob("Einzel_Check_A_Meldungen(*).txt"))
    a_has_content = False
    if a_files:
        try:
            a_has_content = bool(Path(a_files[-1]).read_text(encoding="utf-8-sig").strip())
        except OSError:
            a_has_content = False
    m65_check = re.search(r"(?ims)^6\.5\s+HEBELTRADER\s*$.*?(?=^6\.6\s+SHORT\s*$)", text)
    if m65_check:
        b65 = m65_check.group(0)
        if a_has_content and not re.search(r"(?im)^6\.5\.3\s+", b65):
            errors.append("6.5.3 fehlt trotz vorhandener A-Meldungsdatei")
        if (not a_has_content) and re.search(r"(?im)^6\.5\.3\s+", b65):
            errors.append("6.5.3 vorhanden trotz leerer/fehlender A-Meldungsdatei")

    # 6.7 muss bei vorhandener Edelmetallquelle die vier Metalle enthalten.
    p67 = re.search(r"(?ims)^6\.7\s+EDELMETALLE\s*$.*?(?=^7\.\s+)", text)
    if p67 and (glob.glob("Edelmetalle_Briefing(*).txt") or _metals_information_block()):
        for metal in ("Gold", "Silber", "Platin", "Palladium"):
            if not re.search(r"(?im)^\s*" + metal + r"\s*:", p67.group(0)):
                errors.append(f"6.7 {metal} fehlt")

    # Offene Positionen: 7.1-7.3 Pflicht. 7.4 ist konditional und darf bei
    # fehlenden 3-Tage-Abgaengen nicht als "keine Position" erscheinen.
    p7=re.search(r"(?ims)^7\. OFFENE POSITIONEN\s*$.*?(?=^8\.\s+)",text)
    if not p7:
        errors.append("7. OFFENE POSITIONEN fehlt")
    else:
        b=p7.group(0)
        for sub in ("7.1", "7.2", "7.3"):
            if not re.search(r"(?im)^"+re.escape(sub)+r"\s+",b): errors.append(f"{sub} fehlt")
        if re.search(r"(?im)^7\.4\s+",b) and re.search(r"(?i)Keine Position in den letzten 3 (?:Kalender)?tagen geschlossen",b):
            errors.append("7.4 darf bei 0 Abschluessen nicht als Leerhinweis erscheinen")

    # KI-Positionsfazit unmittelbar unter jeder Position; maximal 2 Saetze.
    if p7:
        b=p7.group(0)
        parts=list(re.finditer(r"(?m)^([^\n|]+?)\s*\(([^()]+)\)\s*\|\s*Markt:",b))
        for i,h in enumerate(parts):
            end=parts[i+1].start() if i+1<len(parts) else len(b)
            pb=b[h.start():end]
            fm=re.search(r"(?im)^KI-Positionsfazit\s*:\s*(.+)$",pb)
            if not fm:
                # Eine Masterposition bleibt auch ohne Gemini-Analyse gueltig.
                # Es wird bewusst kein KI-Fazit erfunden.
                continue
            else:
                # Satzende ist ein Punkt/!/?, gefolgt von Whitespace oder Zeilenende.
                # Dadurch werden auch kurze Sätze wie "Satz eins." sicher erkannt.
                sentence_count = len(re.findall(
                    r"[^.!?]*[.!?](?=\s|$)",
                    fm.group(1).strip(),
                ))
                if sentence_count > 2:
                    errors.append(
                        f"KI-Positionsfazit >2 Saetze bei "
                        f"{h.group(1).strip()} ({h.group(2).strip()})"
                    )

    return errors


def speichere_ergebnis(text):
    heute = datetime.date.today().isoformat()
    ausgabe_datei = f"Auswertung({heute}).txt"
    text = normalisiere_ausgabe(
        text,
        zielzonen=_technische_zielzonen_quelle("Offene Positionen+Check.csv"),
    )
    xlsx_kandidaten = glob.glob("Offene Positionen+Check.xlsx") + glob.glob("Offene Positionen+Check(*).xlsx")
    xlsx_pfad = sorted(xlsx_kandidaten)[-1] if xlsx_kandidaten else None
    text = _normalisiere_geschlossene_positionen_7_4(text, xlsx_pfad)
    strukturfehler = _validiere_finale_ausgabestruktur(text)
    if strukturfehler:
        raise RuntimeError("Finale Ausgabestruktur ungueltig: " + " | ".join(strukturfehler))
    with open(ausgabe_datei, "w", encoding="utf-8-sig") as f:
        f.write(text)
    print(f"\nGespeichert: {ausgabe_datei}")
    return ausgabe_datei


if __name__ == "__main__":
    print("Gemini-Auswertung gestartet...")
    ergebnis_text = gemini_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(ergebnis_text)
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
