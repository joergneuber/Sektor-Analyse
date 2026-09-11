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
from pathlib import Path
import mimetypes

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
    # Letzter erfolgreicher HEBELTRADER-Einzelcheck. Optional: Am ersten Lauf
    # kann die Datei noch fehlen. Wenn vorhanden, wird sie als normale
    # strukturierte Datenquelle an Gemini übergeben.
    "HEBELTRADER-Einzelcheck": ["hebeltrader_einzel_check.json"],
    "Einzel-Check-Technikhistorie": ["einzel_check_historie.jsonl"],
    "Edelmetalle_Setups(...).csv": ["Edelmetalle_Setups(*).csv"],
    "Edelmetalle_Briefing(...).txt": ["Edelmetalle_Briefing(*).txt"],
    # Woechentliche Langfrist-Ebene: beide Dateien sind optional, muessen aber
    # aus Drive nachgeladen werden, wenn der Montags-Lauf bereits existiert.
    "Langfrist_Bewertung(...).csv": ["Langfrist_Bewertung(*).csv"],
    "Langfrist_Briefing(...).txt": ["Langfrist_Briefing(*).txt"],
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

def get_drive_service(strict=False):
    """Baut den Drive-Service auf (lesender Zugriff).

    Im Standardmodus bleibt der Zugriff fuer optionale Short-Dateien tolerant
    und liefert bei fehlendem/ungueltigem Token None. Fuer autoritative Daten
    wie Punkt 7.4 wird strict=True verwendet: Technische Auth-/Drive-Fehler
    duerfen dort niemals als "keine Historie" erscheinen.
    """
    token_str = os.environ.get("GDRIVE_TOKEN")
    if not token_str:
        msg = "GDRIVE_TOKEN nicht gesetzt"
        if strict:
            raise RuntimeError(f"7.4: Autoritativer Google-Drive-Zugriff nicht moeglich: {msg}")
        print(f"INFO: {msg} - Short-Dateien werden nicht nachgeladen.")
        return None

    try:
        token_data = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_data)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                msg = "GDRIVE_TOKEN ungueltig, kein Refresh moeglich"
                if strict:
                    raise RuntimeError(f"7.4: Autoritativer Google-Drive-Zugriff nicht moeglich: {msg}")
                print(f"WARNUNG: {msg} - Short-Dateien werden uebersprungen.")
                return None
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        if strict:
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"7.4: Autoritativer Google-Drive-Zugriff fehlgeschlagen: {e}") from e
        print(f"WARNUNG: Drive-Verbindung fuer Short-Dateien fehlgeschlagen ({e}) - wird uebersprungen.")
        return None



def lade_offenen_positionen_check_tab2():
    """Liest ausschließlich Tab 2 des Master-Sheets für Punkt 7.4.

    Tab 2 „Geschlossene Positionen“ von „Offene Positionen+Check“ ist die
    autoritative Faktenbasis. Es werden nur Datensätze mit Ausstiegsdatum
    innerhalb der letzten drei Kalendertage relativ zum Auswertungstag geliefert.
    Die Funktion verändert keine bestehende Positions-, Retry- oder
    Gemini-Validierungslogik.
    """
    service = get_drive_service(strict=True)

    try:
        # Der bestehende Drive-Service enthält die bereits authentifizierten
        # Credentials. Damit wird keine neue Authentifizierungslogik eingeführt.
        creds = getattr(getattr(service, "_http", None), "credentials", None)
        if creds is None:
            raise RuntimeError("7.4: Google-Credentials für autoritativen Tab-2-Zugriff nicht verfügbar.")

        sheets = build("sheets", "v4", credentials=creds)

        result = service.files().list(
            q=f"name='Offene Positionen+Check' and mimeType='application/vnd.google-apps.spreadsheet' and '{DRIVE_FOLDER_ID}' in parents and trashed=false",
            spaces="drive",
            fields="files(id,name,modifiedTime,parents)",
            orderBy="modifiedTime desc",
            pageSize=10,
        ).execute()
        files = result.get("files", [])
        if not files:
            raise RuntimeError("7.4: Master-Sheet 'Offene Positionen+Check' im konfigurierten Projektordner nicht gefunden.")
        if len(files) > 1:
            raise RuntimeError(
                "7.4: Mehrere Master-Sheets 'Offene Positionen+Check' im konfigurierten Projektordner gefunden: "
                + ", ".join(f"{f.get('id')} (modified={f.get('modifiedTime')})" for f in files)
            )

        master = files[0]
        spreadsheet_id = master["id"]
        print(
            f"7.4 MASTER: Offene Positionen+Check | id={spreadsheet_id} | "
            f"modified={master.get('modifiedTime')} | folder={DRIVE_FOLDER_ID}"
        )

        metadata = sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(title,index,sheetId)"
        ).execute()
        sheet_props = [s.get("properties", {}) for s in metadata.get("sheets", [])]
        titles = [str(p.get("title", "")).strip() for p in sheet_props]
        print(f"7.4 MASTER-TABS: {titles}")
        if "Geschlossene Positionen" not in titles:
            raise RuntimeError("7.4: Tab 'Geschlossene Positionen' im Master-Sheet nicht vorhanden.")

        values = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="'Geschlossene Positionen'!A:AA",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [])

        if len(values) < 2:
            raise RuntimeError("7.4: Tab 'Geschlossene Positionen' enthält keine Headerzeile.")

        headers = [str(x).strip() for x in values[1]]
        required_headers = {"Ticker", "Ausstiegsdatum", "Status"}
        missing_headers = sorted(required_headers - set(headers))
        if missing_headers:
            raise RuntimeError(
                "7.4: Pflichtspalten in Tab 'Geschlossene Positionen' fehlen: "
                + ", ".join(missing_headers)
            )
        rows = [dict(zip(headers, row + [""] * max(0, len(headers) - len(row)))) for row in values[2:]]

        def parse_date(value):
            # UNFORMATTED_VALUE liefert Google-Sheets-Datumszellen als
            # Seriennummer. Die bisherigen Textformate bleiben gültig.
            if isinstance(value, datetime.datetime):
                return value.date()
            if isinstance(value, datetime.date):
                return value

            raw = str(value or "").strip()
            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    return datetime.datetime.strptime(raw, fmt).date()
                except ValueError:
                    pass

            # Google-Sheets-Datumssystem: Seriennummer 25569 = 1970-01-01.
            # Nur ein plausibler Kalenderbereich wird als Datum interpretiert,
            # damit normale numerische Werte wie 270.58 niemals als Datum gelten.
            if re.fullmatch(r"\d+(?:\.\d+)?", raw):
                try:
                    serial = float(raw)
                    if 30000 <= serial <= 60000:
                        return (datetime.datetime(1899, 12, 30) +
                                datetime.timedelta(days=serial)).date()
                except (OverflowError, ValueError):
                    pass
            return None

        today = datetime.date.today()
        start = today - datetime.timedelta(days=2)
        selected = []
        parseable_dates = 0
        for row in rows:
            exit_date = parse_date(row.get("Ausstiegsdatum"))
            if exit_date is not None:
                parseable_dates += 1
            if exit_date is not None and start <= exit_date <= today:
                selected.append(row)

        all_exit_dates = [parse_date(row.get("Ausstiegsdatum")) for row in rows]
        all_exit_dates = [d for d in all_exit_dates if d is not None]
        newest_exit = max(all_exit_dates).isoformat() if all_exit_dates else "keine parsebaren Ausstiegsdaten"
        print(
            f"7.4 HISTORIE-PRUEFUNG: daten={len(rows)} | parsebar={parseable_dates} | "
            f"neuestes_ausstiegsdatum={newest_exit} | fenster={start.isoformat()}..{today.isoformat()} | "
            f"treffer={len(selected)}"
        )

        if not selected:
            print("HISTORIE 7.4: Keine geschlossene Position innerhalb der letzten 3 Kalendertage.")
            return ""

        # Nur Faktenfelder aus Tab 2; keine technische Neubewertung.
        fields = [
            "Ticker", "Name", "Einstiegsdatum", "Einstieg",
            "Ausstiegsdatum", "Ausstiegskurs", "Performance_Seit_Einstieg%",
            "Status", "Richtung", "Produkt_Typ",
            "OS_WKN", "OS_Einstiegskurs", "OS_Aktueller_Kurs", "OS_Performance%",
            "OS_Quelle", "OS_Kurszeit",
        ]
        out = []
        for row in selected:
            formatted_fields = []
            for field in fields:
                raw_value = row.get(field, "")
                if not str(raw_value).strip():
                    continue
                # Bei UNFORMATTED_VALUE ist Ausstiegsdatum ggf. eine
                # Google-Sheets-Seriennummer. Für Gemini wieder als Datum
                # ausgeben, damit die Faktenbasis lesbar und stabil bleibt.
                if field == "Ausstiegsdatum":
                    parsed_exit = parse_date(raw_value)
                    value = parsed_exit.strftime("%d.%m.%Y") if parsed_exit is not None else str(raw_value)
                else:
                    value = raw_value
                formatted_fields.append(f"{field}: {value}")
            out.append(" | ".join(formatted_fields))
        selected_tickers = [str(row.get("Ticker", "")).strip() for row in selected]
        print(
            f"HISTORIE 7.4: {len(selected)} geschlossene Position(en) aus Tab 2 innerhalb des 3-Tage-Fensters | "
            f"Ticker={selected_tickers}"
        )
        return "\n".join(out)

    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"7.4: Tab 'Geschlossene Positionen' konnte nicht autoritativ gelesen/verifiziert werden: {exc}") from exc


def lade_langfrist_dateien_von_drive():
    """Laedt die woechentlichen Langfrist-Dateien aus Drive nach.

    langfrist_scan_catchup.py prueft bewusst nur, ob der Wochenlauf bereits
    erledigt wurde. Fuer den taeglichen Hauptlauf muessen die vorhandenen
    Dateien trotzdem lokal verfuegbar sein, damit Gemini sie als Eingabe
    erhaelt. An sechs von sieben Tagen ist die Quelle nicht vorhanden; das
    bleibt ein normaler, optionaler Zustand.
    """
    service = get_drive_service()
    if service is None:
        return {}

    heute = datetime.date.today().isoformat()
    gefunden = {}
    for prefix, key, pattern in [
        ("Langfrist_Bewertung", "Langfrist_Bewertung(...).csv", f"Langfrist_Bewertung({heute}).csv"),
        ("Langfrist_Briefing", "Langfrist_Briefing(...).txt", f"Langfrist_Briefing({heute}).txt"),
    ]:
        try:
            query = (
                f"name contains '{prefix}' and '{DRIVE_FOLDER_ID}' in parents "
                "and trashed = false"
            )
            ergebnis = service.files().list(
                q=query, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc", pageSize=20
            ).execute()
            treffer = ergebnis.get("files", [])
            if not treffer:
                print(f"INFO: Keine {prefix}-Datei in Drive gefunden - Langfrist-Ebene bleibt heute optional.")
                continue
            datei = treffer[0]
            lokaler_name = datei.get("name") or pattern
            request = service.files().get_media(fileId=datei["id"])
            with io.FileIO(lokaler_name, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                fertig = False
                while not fertig:
                    _, fertig = downloader.next_chunk()
            gefunden[key] = lokaler_name
            print(f"INFO: {lokaler_name} von Drive nachgeladen -> {lokaler_name}")
        except Exception as e:
            print(f"WARNUNG: Nachladen von {prefix} fehlgeschlagen ({e}) - wird uebersprungen.")
    return gefunden


def lade_hebeltrader_datei_von_drive(lokaler_pfad):
    """Synchronisiert die aktuellste HEBELTRADER-Datei aus Drive.

    Die Drive-Datei ist die autoritative Quelle fuer den separaten
    HEBELTRADER-Workflow. Ein lokales Download-Mtime darf NICHT gegen
    ``modifiedTime`` ausgespielt werden: ein vorheriger Download kann lokal
    juenger aussehen, obwohl Drive inzwischen z.B. 166/26 enthaelt.
    Deshalb wird die neueste Drive-Datei bei erfolgreichem Zugriff immer
    heruntergeladen und anschliessend anhand des Inhalts (issue_label) geloggt.
    Bei einem echten Drive-Fehler bleibt die vorhandene lokale Datei erhalten.
    """
    service = get_drive_service()
    if service is None:
        return lokaler_pfad
    try:
        query = (
            f"name = 'hebeltrader_einzel_check.json' and '{DRIVE_FOLDER_ID}' in parents "
            "and trashed = false"
        )
        ergebnis = service.files().list(
            q=query,
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=5,
        ).execute()
        treffer = ergebnis.get("files", [])
        if not treffer:
            print("INFO: Keine hebeltrader_einzel_check.json in Drive gefunden - lokale Version bleibt erhalten.")
            return lokaler_pfad

        datei = treffer[0]
        request = service.files().get_media(fileId=datei["id"])
        ziel = "hebeltrader_einzel_check.json"
        with io.FileIO(ziel, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            fertig = False
            while not fertig:
                _, fertig = downloader.next_chunk()

        try:
            payload = json.loads(Path(ziel).read_text(encoding="utf-8"))
        except Exception as exc:
            # Eine unvollstaendige/ungueltige Drive-Datei darf die lokale
            # funktionierende Version nicht ersetzen.
            print(
                f"WARNUNG: HEBELTRADER-Drive-Datei ungueltiges JSON "
                f"({type(exc).__name__}: {exc}) - lokale Version bleibt erhalten."
            )
            if lokaler_pfad and os.path.exists(lokaler_pfad) and lokaler_pfad != ziel:
                return lokaler_pfad
            return lokaler_pfad

        label = payload.get("issue_label") or payload.get("issue_number") or "unbekannt"
        if label == "unbekannt":
            print(
                "WARNUNG: HEBELTRADER-Drive-Datei enthaelt keine issue_label/issue_number "
                "- Datei wird nicht als verifizierte Ausgabe akzeptiert."
            )
            return lokaler_pfad

        print(
            f"INFO: HEBELTRADER-Einzelcheck aus Drive synchronisiert: "
            f"Ausgabe={label} | modified={datei.get('modifiedTime')} | Drive-ID={datei.get('id')}"
        )
        return ziel
    except Exception as exc:
        print(
            f"WARNUNG: HEBELTRADER-Synchronisierung aus Drive fehlgeschlagen "
            f"({type(exc).__name__}: {exc}) - lokale Version bleibt erhalten."
        )
        return lokaler_pfad

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


def _csv_value(row, aliases):
    """Liest den ersten vorhandenen CSV-Wert aus einer Aliasliste."""
    for alias in aliases:
        if alias in row:
            value = row.get(alias)
            if value is not None and str(value).strip():
                return str(value).strip()
    normalized = {re.sub(r"[^a-z0-9]+", "", str(k).casefold()): v for k, v in row.items()}
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]+", "", str(alias).casefold())
        value = normalized.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _offene_positionen_rows(csv_pfad):
    """Liest Offene Positionen+Check.csv ausschliesslich als Faktenquelle."""
    if not csv_pfad or not os.path.exists(csv_pfad):
        raise RuntimeError("Offene Positionen+Check.csv fehlt fuer den autoritativen Punkt-7-Aufbau.")
    with open(csv_pfad, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ","
        reader = csv.DictReader(f, dialect=dialect)
        rows = []
        for row in reader:
            if any(str(v or "").strip() for v in row.values()):
                rows.append({str(k).strip(): v for k, v in row.items() if k is not None})
    return rows


def _format_position_value(value):
    return str(value).strip() if value is not None else ""


def _erstelle_punkt7_fakten(csv_pfad, geschlossene_7_4=""):
    """Erzeugt 7.1/7.3/7.4 deterministisch aus autoritativen Fakten.

    Gemini liefert nur 7.2 Handlungsbedarf/Interpretation. Firmenname, Ticker,
    Einstieg, Einstiegsdatum und technische Check-Felder werden hier niemals
    von Gemini erzeugt oder veraendert.
    """
    rows = _offene_positionen_rows(csv_pfad)

    name_aliases = ["Firmenname", "Name", "Unternehmen", "Company"]
    ticker_aliases = ["Ticker", "Yahoo-Ticker", "Yahoo Ticker"]
    market_aliases = ["Markt", "Market", "Boerse", "Börse"]
    core_fields = [
        ("Firmenname", name_aliases),
        ("Ticker", ticker_aliases),
        ("Einstiegskurs", ["Einstiegskurs", "Einstieg", "Entry"]),
        ("Einstiegsdatum", ["Einstiegsdatum", "Einstieg Datum", "Entry Date"]),
        ("Richtung", ["Richtung"]),
        ("Produkt_Typ", ["Produkt_Typ", "Produkt Typ", "Produkt"]),
        ("Status", ["Status"]),
        ("Aktueller Kurs", ["Aktueller Kurs", "Kurs", "Current Price"]),
    ]
    technical_fields = [
        ("Technischer Zustand", ["Technischer_Zustand", "Technischer Zustand"]),
        ("Trendrichtung", ["Trendrichtung"]),
        ("Support/Widerstand", ["Support/Widerstand", "Support_Widerstand"]),
        ("Breakout Status", ["Breakout_Status", "Breakout Status"]),
        ("A-B-C Status", ["A-B-C_Status", "A-B-C Status"]),
        ("Fibonacci Status/Ziele", ["Fibonacci_Status/Ziele", "Fibonacci Status/Ziele"]),
        ("Trendkanal", ["Trendkanal"]),
        ("Measured Move", ["Measured Move"]),
        ("Formation", ["Formation"]),
        ("Round Number", ["Round Number"]),
        ("Major Resistance", ["Major Resistance"]),
        ("Ueberdehnung", ["Ueberdehnung", "Überdehnung"]),
        ("Relative Staerke_Sektor", ["Relative Staerke_Sektor", "Relative Staerke Sektor"]),
        ("Konfluenz", ["Konfluenz"]),
        ("Retest_Support", ["Retest_Support", "Retest Support"]),
        ("Technische Zielzone", ["Technische_Zielzone", "Technische Zielzone"]),
        ("Datenqualitaet", ["Datenqualitaet", "Datenqualität"]),
        ("Analysehinweis", ["Analysehinweis"]),
    ]

    out = ["7. OFFENE POSITIONEN", "", "7.1 Portfolio-Übersicht"]
    if not rows:
        out.append("Keine offenen Positionen laut Offene Positionen+Check.csv.")
    else:
        out.append(f"Anzahl offene Positionen: {len(rows)}")
        for row in rows:
            name = _csv_value(row, name_aliases) or "(Name nicht vorhanden)"
            ticker = _csv_value(row, ticker_aliases) or "(Ticker nicht vorhanden)"
            entry = _csv_value(row, ["Einstiegskurs", "Einstieg", "Entry"])
            entry_date = _csv_value(row, ["Einstiegsdatum", "Einstieg Datum", "Entry Date"])
            status = _csv_value(row, ["Status"])
            direction = _csv_value(row, ["Richtung"])
            current = _csv_value(row, ["Aktueller Kurs", "Kurs", "Current Price"])
            summary = f"- {name} ({ticker})"
            facts = []
            for label, value in [
                ("Einstieg", entry), ("Einstiegsdatum", entry_date),
                ("Richtung", direction), ("Status", status), ("Aktueller Kurs", current)
            ]:
                if value:
                    facts.append(f"{label}: {value}")
            if facts:
                summary += " | " + " | ".join(facts)
            out.append(summary)

    out.extend(["", "7.2 Handlungsbedarf"])
    out.append(
        "[GEMINI-INTERPRETATION] Dieser Unterpunkt wird ausschliesslich aus der "
        "Gemini-Auswertung uebernommen. Die Fakten in 7.1 und 7.3 stammen "
        "deterministisch aus Offene Positionen+Check.csv."
    )
    out.extend(["", "7.3 Einzelpositionen"])

    if not rows:
        out.append("Keine offenen Positionen laut Offene Positionen+Check.csv.")
    else:
        for row in rows:
            name = _csv_value(row, name_aliases) or "(Name nicht vorhanden)"
            ticker = _csv_value(row, ticker_aliases) or "(Ticker nicht vorhanden)"
            market = _csv_value(row, market_aliases) or "-"
            out.append("")
            out.append(f"{name} ({ticker}) | Markt: {market}")
            for label, aliases in core_fields:
                value = _csv_value(row, aliases)
                if value:
                    out.append(f"{label}: {value}")
            for label, aliases in technical_fields:
                value = _csv_value(row, aliases)
                if value:
                    out.append(f"{label}: {value}")
            out.append("KI-Positionsfazit: [GEMINI-INTERPRETATION]")

    out.extend([
        "",
        "7.4 GESCHLOSSENE POSITIONEN – LETZTE 3 TAGE",
        geschlossene_7_4 or "Keine geschlossene Position innerhalb der letzten 3 Kalendertage."
    ])
    return "\n".join(out).strip() + "\n"


def _ersetze_punkt7_durch_python_fakten(text, python_punkt7):
    """Ersetzt den gesamten Gemini-Punkt 7 durch den Python-Faktenblock.

    7.2 wird aus der Gemini-Antwort extrahiert und in den Python-Block eingesetzt.
    7.1/7.3/7.4 bleiben dadurch vollständig autoritativ.
    """
    if not python_punkt7:
        raise RuntimeError("Python-Punkt-7-Faktenblock ist leer.")

    gemini_7_2 = ""
    m = re.search(
        r"(?ims)^\s*7\.2\s+Handlungsbedarf\s*$.*?(?=^\s*7\.3\b|^\s*7\.4\b|^\s*8\.\s+|\Z)",
        text or "",
    )
    if m:
        gemini_7_2 = m.group(0).strip()
    if not gemini_7_2:
        gemini_7_2 = (
            "7.2 Handlungsbedarf\n"
            "Keine Gemini-Interpretation fuer den Handlungsbedarf verfuegbar."
        )

    python_punkt7 = re.sub(
        r"(?ims)^\s*7\.2\s+Handlungsbedarf\s*$.*?(?=^\s*7\.3\b)",
        gemini_7_2 + "\n\n",
        python_punkt7,
        count=1,
    )

    old = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*8\.\s+|\Z)",
        text or "",
    )
    if old:
        return (text[:old.start()] + python_punkt7.rstrip() + "\n\n" + text[old.end():]).strip() + "\n"
    # Falls Gemini Punkt 7 komplett ausgelassen hat: vor Punkt 8 einsetzen.
    next8 = re.search(r"(?im)^\s*8\.\s+", text or "")
    if next8:
        return (text[:next8.start()] + python_punkt7.rstrip() + "\n\n" + text[next8.start():]).strip() + "\n"
    return (text.rstrip() + "\n\n" + python_punkt7.rstrip() + "\n").strip() + "\n"


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


def _lade_6_5_statusverlauf(historie_pfad):
    """Liest den letzten bekannten Status fuer die reine 6.5-Darstellung.

    Die Beobachtungsliste bleibt allein autoritativ fuer die AKTUELLE
    Kategorie. Historie wird hier ausschliesslich fuer die Anzeige
    ``letzter Status -> aktueller Status`` verwendet und kann keine
    Kategoriezuordnung veraendern. Wenn ein heutiger Snapshot vorhanden ist,
    ist dessen ``Vorheriger_Status`` der Status des vorherigen Laufs.
    """
    if not historie_pfad or not os.path.isfile(historie_pfad):
        return {}
    heute = datetime.date.today().isoformat()
    latest = {}
    try:
        with open(historie_pfad, "r", encoding="utf-8-sig") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("Ticker", "")).strip().upper()
                datum = str(row.get("Datum", "")).strip()
                if not ticker or not datum:
                    continue
                if datum <= heute and (
                    ticker not in latest or datum >= str(latest[ticker].get("Datum", ""))
                ):
                    latest[ticker] = row
        result = {}
        for ticker, row in latest.items():
            datum = str(row.get("Datum", "")).strip()
            if datum == heute:
                previous = str(row.get("Vorheriger_Status") or "").strip().upper()
            else:
                previous = str(row.get("Status", "")).strip().upper()
            result[ticker] = previous or "NICHT BEKANNT"
        return result
    except Exception as exc:
        print(f"WARNUNG: 6.5-Statushistorie konnte nicht gelesen werden: {exc}")
        return {}


def _kurzstatus(status):
    """Normalisiert die langen Beobachtungsstatus auf A/B/C/Kein Kandidat."""
    mapping = {
        "KAUFKANDIDAT A": "A",
        "KAUFKANDIDAT B": "B",
        "KAUFKANDIDAT C": "C",
        "KEIN KANDIDAT": "Kein Kandidat",
    }
    return mapping.get(str(status or "").strip().upper(), str(status or "NICHT BEKANNT").strip())


def _lade_6_5_namen(eingabedateien=None, historie_pfad=None):
    """Ermittelt autoritative Anzeigenamen fuer 6.5.

    Prioritaet: aktueller Einzel-Check-Historieneintrag, danach strukturierte
    CSV/JSON-Quellen des aktuellen Laufs. Der Name dient nur der Darstellung;
    die aktuelle Kategorie bleibt ausschliesslich aus der Beobachtungsliste.
    """
    namen = {}

    def add(ticker, name):
        ticker = _normalisiere_ticker(ticker)
        name = str(name or "").strip()
        if ticker and name and name.upper() != ticker.upper():
            namen.setdefault(ticker, name)

    if historie_pfad and os.path.isfile(historie_pfad):
        try:
            with open(historie_pfad, "r", encoding="utf-8-sig") as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        add(row.get("Ticker"), row.get("Name"))
        except Exception as exc:
            print(f"WARNUNG: 6.5-Namenshistorie konnte nicht gelesen werden: {exc}")

    for pfad in (eingabedateien or {}).values():
        if not pfad or not os.path.isfile(pfad):
            continue
        suffix = Path(pfad).suffix.lower()
        try:
            if suffix == ".csv":
                with open(pfad, "r", encoding="utf-8-sig", newline="") as f:
                    sample = f.read(4096)
                    f.seek(0)
                    try:
                        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t") if sample.strip() else None
                    except csv.Error:
                        dialect = None
                    reader = csv.DictReader(f, delimiter=dialect.delimiter if dialect else ";")
                    fields = reader.fieldnames or []
                    lower = {str(x).strip().lower(): x for x in fields}
                    ticker_k = next((lower[k] for k in ("ticker", "yahoo-ticker", "yahoo ticker") if k in lower), None)
                    name_k = next((lower[k] for k in ("name", "firmenname", "unternehmen", "company") if k in lower), None)
                    if ticker_k and name_k:
                        for row in reader:
                            add(row.get(ticker_k), row.get(name_k))
            elif suffix == ".json":
                with open(pfad, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for ticker, entry in data.items():
                        if isinstance(entry, dict):
                            add(ticker, entry.get("Name") or entry.get("name") or entry.get("Firmenname") or entry.get("firmenname"))
        except Exception:
            continue
    return namen


def erstelle_6_5_autoritative_liste(beobachtungsliste_pfad, historie_pfad=None, eingabedateien=None):
    """Erzeugt die verbindliche 6.5.1-/6.5.2-Zuordnung aus dem aktuellen
    Einzel-Check-Status. Historie wird nur fuer die Darstellung des
    Statusverlaufs verwendet, niemals fuer die aktuelle Kategoriezuordnung.
    """
    if not beobachtungsliste_pfad or not os.path.exists(beobachtungsliste_pfad):
        raise RuntimeError(
            "6.5: Autoritative einzel_check_beobachtung.json fehlt. "
            "Die 6.5-Zuordnung darf nicht aus historischen Daten rekonstruiert werden."
        )

    try:
        with open(beobachtungsliste_pfad, "r", encoding="utf-8-sig") as f:
            daten = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"6.5: Beobachtungsliste konnte nicht gelesen werden: {exc}") from exc

    if not isinstance(daten, dict):
        raise RuntimeError("6.5: einzel_check_beobachtung.json ist kein JSON-Objekt.")

    aktuelle_a = []
    aktuelle_nicht_a = []
    zulaessige_nicht_a_status = {"KAUFKANDIDAT B", "KAUFKANDIDAT C", "KEIN KANDIDAT"}
    vorherige_status = _lade_6_5_statusverlauf(historie_pfad)
    namen = _lade_6_5_namen(eingabedateien, historie_pfad)

    for ticker, eintrag in daten.items():
        if not isinstance(eintrag, dict):
            raise RuntimeError(f"6.5: Ungueltiger Beobachtungslisteneintrag fuer {ticker!r}.")
        status = str(eintrag.get("status", "")).strip()
        quelle = str(eintrag.get("quelle", "-")).strip() or "-"
        if status == "KAUFKANDIDAT A":
            aktuelle_a.append((str(ticker).strip(), quelle))
        elif status in zulaessige_nicht_a_status:
            aktuelle_nicht_a.append((str(ticker).strip(), status, quelle))
        else:
            raise RuntimeError(
                f"6.5: Unerwarteter aktueller Status fuer {ticker!r}: {status!r}. "
                "Die Liste wird nicht aus historischen Daten repariert."
            )

    aktuelle_a.sort(key=lambda x: x[0].upper())
    aktuelle_nicht_a.sort(key=lambda x: x[0].upper())

    zeilen = [
        "AUTORITATIVE 6.5-ZUORDNUNG AUS einzel_check_beobachtung.json",
        "Diese Zuordnung ist verbindlich und wurde von Python aus dem AKTUELLEN Status erzeugt.",
        "Gemini darf die Kategoriezuordnung NICHT selbst rekonstruieren, veraendern oder aus anderen Dateien ableiten.",
        "Historische Status aus einzel_check_historie.jsonl und der zuletzt erfolgreichen HEBELTRADER-Datei sind fuer die 6.5-Kategoriezuordnung unzulaessig.",
        "",
        f"6.5.1 AKTUELLE KAUFKANDIDATEN A ({len(aktuelle_a)} Titel):",
    ]
    for ticker, quelle in aktuelle_a:
        vorher = vorherige_status.get(ticker, "NICHT BEKANNT")
        name = namen.get(_normalisiere_ticker(ticker), "Name nicht verfügbar")
        zeilen.append(
            f"- {name} ({ticker}) | {_kurzstatus(vorher)} -> A | aktueller Status: KAUFKANDIDAT A | Quelle: {quelle}"
        )

    # 6.5.2 zeigt ausschliesslich aktuell aktive Nicht-A-Kandidaten (B/C).
    # KEIN KANDIDAT bleibt intern Bestandteil der autoritativen Beobachtungsliste,
    # wird aber bewusst nicht dargestellt. Sobald derselbe Titel wieder B/C/A wird,
    # erscheint er automatisch wieder. Es gibt weiterhin KEINE Mengenbegrenzung.
    aktive_nicht_a = [row for row in aktuelle_nicht_a if row[1] in {"KAUFKANDIDAT B", "KAUFKANDIDAT C"}]
    zeilen.extend([
        "",
        f"6.5.2 AKTUELLE NICHT-A-KANDIDATEN ({len(aktive_nicht_a)} Titel):",
        "Darstellung: Name (Ticker) | Letzter Status -> aktueller Status | Quelle",
    ])
    gruppen = {"KAUFKANDIDAT B": [], "KAUFKANDIDAT C": []}
    for ticker, status, quelle in aktive_nicht_a:
        gruppen[status].append((ticker, status, quelle))
    for status in ("KAUFKANDIDAT B", "KAUFKANDIDAT C"):
        zeilen.append("")
        zeilen.append(f"{_kurzstatus(status)}:")
        for ticker, current_status, quelle in gruppen[status]:
            vorher = vorherige_status.get(ticker, "NICHT BEKANNT")
            name = namen.get(_normalisiere_ticker(ticker), "Name nicht verfügbar")
            zeilen.append(
                f"- {name} ({ticker}) | {_kurzstatus(vorher)} -> {_kurzstatus(current_status)} | Quelle: {quelle}"
            )

    zeilen.extend([
        "",
        f"KONTROLLSUMME: {len(aktuelle_a)} A-Kandidaten + {len(aktive_nicht_a)} aktive B/C-Kandidaten = {len(aktuelle_a) + len(aktive_nicht_a)} dargestellte Titel; weitere {len(aktuelle_nicht_a) - len(aktive_nicht_a)} Titel mit Status KEIN KANDIDAT werden bewusst nicht dargestellt.",
        "Die Quelle ist unabhaengig vom Status: Quelle HEBELTRADER oder Quelle '-' aendert die Kategorie nicht.",
        "Gemini darf fuer 6.5 nur die hier vorgegebene Mitgliedschaft verwenden; technische Inhalte duerfen weiterhin nur aus den bereitgestellten Quelldaten uebernommen werden.",
    ])
    print(
        f"6.5-Autoritaetsliste: {len(aktuelle_a)} A-Kandidaten | "
        f"{len(aktuelle_nicht_a)} Nicht-A-Kandidaten | {len(daten)} beobachtete Titel"
    )
    return "\n".join(zeilen)


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

    # HEBELTRADER: Der separate Scanner kann kurz vor dem Hauptlauf eine neue
    # Ausgabe in Drive geschrieben haben. Eine neuere Drive-Version hat Vorrang
    # vor einer eventuell alten lokalen JSON-Datei. Damit wird z.B. 166/26 im
    # unmittelbar folgenden Hauptlauf automatisch mitverarbeitet.
    if gefunden.get("HEBELTRADER-Einzelcheck"):
        gefunden["HEBELTRADER-Einzelcheck"] = lade_hebeltrader_datei_von_drive(
            gefunden["HEBELTRADER-Einzelcheck"]
        )

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

    # Langfrist: Wenn der Wochenlauf bereits in Drive vorhanden ist, muss die
    # Quelle fuer Gemini trotzdem lokal synchronisiert werden. An Tagen ohne
    # Wochenlauf bleibt die Ebene bewusst optional.
    if (gefunden.get("Langfrist_Bewertung(...).csv") is None or
            gefunden.get("Langfrist_Briefing(...).txt") is None):
        for key, pfad in lade_langfrist_dateien_von_drive().items():
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
            market_k = key("Markt")
            direction_k = key("Richtung")
            source_k = key("Quelle")
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
                    "market": str(row.get(market_k, "") or "").strip() if market_k else "",
                    "direction": str(row.get(direction_k, "") or "").strip() if direction_k else "",
                    "source": str(row.get(source_k, "") or "").strip() if source_k else "",
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

    # 3. Sicherheits-Fallback: Name + Einstieg + Einstiegsdatum.
    #
    # Dieser Fallback darf nur greifen, wenn die Kombination in der
    # Master-Datei exakt EINMAL vorkommt. Damit kann ein fehlender/falsch
    # ausgegebener Ticker (z. B. EUNL statt EUNL.DE) repariert werden, ohne
    # bei mehreren gleichnamigen Positionen zu raten. Der Master bleibt
    # autoritativ: Die gefundene Position liefert anschließend den kanonischen
    # Ticker, Einstieg und das Datum.
    kandidaten = [
        pos for key, pos in quellpositionen.items()
        if key[0] == name and key[2] == entry and key[3] == date
    ]
    if len(kandidaten) == 1:
        return kandidaten[0]
    if len(kandidaten) > 1:
        raise RuntimeError(
            "Position nicht eindeutig zuordenbar: gleiche Kombination aus "
            "Name + Einstieg + Einstiegsdatum mehrfach vorhanden: "
            f"{name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}"
        )

    # 4. Falls der Firmenname durch Gemini leicht abweicht, ist ein eindeutiger
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
    """Liefert eine präzise Diagnose für das Punkt-7-API-Retry-Gate."""
    diagnose = {
        "ueberschrift_vorhanden": _enthaelt_abschnitt_7(text),
        "positionsbereich_vorhanden": False,
        "positionskoepfe": 0,
    }
    if not diagnose["ueberschrift_vorhanden"]:
        return diagnose
    match = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*7\.4\b|^\s*8\.\s+|\Z)",
        text or "",
    )
    if not match:
        return diagnose
    diagnose["positionsbereich_vorhanden"] = True
    # Dieselbe tolerante Header-Grundstruktur wie in der eigentlichen
    # Master-Zuordnung verwenden. Die Interpretation von Name/Ticker erfolgt
    # erst danach master-gestützt; eine Klammer ist daher hier kein Pflicht-
    # bestandteil. So bleiben Diagnose-Gate und finale Normalisierung konsistent.
    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\|\s*Markt:\s*[^\n]+$"
    )
    diagnose["positionskoepfe"] = len(header_re.findall(match.group(0)))
    return diagnose


def _abschnitt_7_strukturell_gueltig(text, csv_pfad):
    """Prueft nur die strukturelle Mindestvoraussetzung fuer Punkt 7.

    Punkt 7.1/7.3/7.4 wird inzwischen deterministisch von Python aus den
    autoritativen Quellen erzeugt. Gemini muss nur 7.2 interpretieren.
    Diese Funktion bleibt als strukturelle Endpruefung fuer den bereits
    injizierten Python-Faktenblock erhalten.
    """
    if not _enthaelt_abschnitt_7(text):
        return False

    match = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*7\.4\b|^\s*8\.\s+|\Z)",
        text or "",
    )
    if not match:
        return False

    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\|\s*Markt:\s*[^\n]+$"
    )
    return bool(header_re.search(match.group(0)))


def _fuege_abschnitt_7_ein(original_text, abschnitt_7):
    """Fügt einen ausschließlich für Punkt 7 angeforderten Gemini-Block ein.

    Der Reparatur-Call darf nur Punkt 7 liefern. Der Block wird deshalb nicht
    als komplette neue Auswertung verwendet, sondern deterministisch in die
    bestehende Antwort vor den nächsten nummerierten Hauptabschnitt eingesetzt.
    """
    if not _enthaelt_abschnitt_7(abschnitt_7):
        raise RuntimeError(
            "Gezielter Reparaturversuch lieferte ebenfalls keinen Abschnitt "
            "'7. OFFENE POSITIONEN'."
        )

    block_match = re.search(
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*7\.4\b|^\s*8\.\s+|\Z)",
        abschnitt_7,
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
        r"(?ims)^\s*7\. OFFENE POSITIONEN\s*$.*?(?=^\s*7\.4\b|^\s*8\.\s+|\Z)",
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


def ermittle_upload_mime_type(pfad):
    """Ermittelt den MIME-Type fuer einen Gemini-Dateiupload."""
    pfad = str(pfad or "").strip()
    if not pfad:
        raise ValueError("Leerer Dateipfad fuer Gemini-Upload")

    endung = os.path.splitext(pfad)[1].lower()
    projekt_mime_types = {
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
        # .jsonl wird von Python/mimetypes nicht in allen Laufzeitumgebungen
        # erkannt; Gemini benoetigt hier deshalb einen expliziten MIME-Type.
        ".jsonl": "application/json",
    }

    if endung in projekt_mime_types:
        return projekt_mime_types[endung]

    mime_type, _ = mimetypes.guess_type(pfad)
    if mime_type:
        return mime_type

    raise ValueError(
        f"Kein MIME-Type fuer Gemini-Upload ableitbar: {pfad!r} "
        f"(Dateiendung: {endung or '<keine>'})"
    )


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

    makro_datenqualitaet = _lese_makro_datenqualitaet(makro_text if makro_pfad else "")
    if makro_datenqualitaet:
        print(f"Makro-Datenqualitaet: {makro_datenqualitaet} | Quelle: Makro-Datenpaket")

    # Punkt 6.5 wird nicht mehr allein per Prompt interpretiert: Python erzeugt
    # die aktuelle A-/Nicht-A-Mitgliedschaft verbindlich aus der Beobachtungsliste
    # und uebergibt diese beiden Mengen explizit an Gemini. Damit koennen alte
    # HEBELTRADER- oder Historienstatus die aktuelle Kategorie nicht mehr verfälschen.
    beobachtung_pfad = eingabedateien.get("Einzel-Check-Beobachtungsliste")
    sechs_fuenf_autoritaet = erstelle_6_5_autoritative_liste(
        beobachtung_pfad, eingabedateien.get("Einzel-Check-Technikhistorie"), eingabedateien
    )

    # Autoritative Punkt-7-Fakten werden genau einmal pro Lauf gelesen.
    # Sie sind von Gemini-Retries unabhaengig und duerfen nicht bei jedem
    # API-Versuch erneut aus Drive/CSV beschafft werden.
    offene_quelle = _offene_positionen_quellblock(eingabedateien.get("Offene Positionen+Check.csv"))
    geschlossene_7_4 = lade_offenen_positionen_check_tab2()

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
                hochgeladene_teile = []
                for pfad in eingabedateien.values():
                    if not pfad:
                        continue
                    mime_type = ermittle_upload_mime_type(pfad)
                    print(f"  Gemini-Upload: {os.path.basename(pfad)} | MIME: {mime_type}")
                    hochgeladene_teile.append(
                        client.files.upload(
                            file=pfad,
                            config=types.UploadFileConfig(mime_type=mime_type),
                        )
                    )

            antwort = client.models.generate_content(
                model=aktuelles_modell,
                contents=hochgeladene_teile + [
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. Die Dateien Bitcoin_Trading_DE_Briefing.txt, Gold_Trading_DE_Briefing.txt und Silber_Trading_DE_Briefing.txt sind ausschließlich qualitative externe YouTube-Quellen. Nutze sie nur als Kontext/Abgleich; sie dürfen niemals objektive Kursdaten, technische Check-Felder, CRV, Setup-Scores, Filter, Setup-Qualität oder Handelsentscheidungen verändern. Wenn eine solche Datei fehlt, ist das kein Fehler und es darf nichts daraus erfunden werden. "
                    "ERSTELLE in der fertigen Auswertung zusätzlich eine feste Sektion mit exakt der Überschrift 'EXTERNE MARKTQUELLEN'. Gliedere sie getrennt nach 'Bitcoin', 'Gold' und 'Silber'. Für jeden Markt nenne die Anzahl der tatsächlich in der jeweiligen bereitgestellten Briefing-Datei enthaltenen relevanten Videos. WICHTIG: Zähle und verarbeite jedes vorhandene Video einzeln anhand jedes einzelnen 'Titel:'-Blocks bzw. Video-Blocks. Wenn die Briefing-Datei beispielsweise 3 relevante Videos enthält, müssen in der fertigen Auswertung genau diese 3 Videos einzeln erscheinen. Kein Video darf wegen Kürze, Ähnlichkeit, Redundanz oder eigener Auswahl des Modells weggelassen, zusammengefasst oder durch ein anderes ersetzt werden. Führe für JEDES vorhandene relevante Video separat Titel und eine kurze Kernaussage auf und ordne JEDE einzelne Aussage ausschließlich im Verhältnis zur bestehenden Systemanalyse als 'BESTÄTIGT', 'WIDERSPRICHT' oder 'NEUTRAL' ein. Die Anzahl muss mit der Zahl der tatsächlich einzeln aufgeführten Videos übereinstimmen. Ergänze bei jedem Markt ausdrücklich 'Technische Auswirkung: KEINE'. Wenn für einen Markt keine relevanten Videos in der bereitgestellten Briefing-Datei vorhanden sind oder die Datei fehlt, schreibe ausdrücklich 'Keine neuen relevanten Videos verarbeitet'. Verwende für Titel und Kernaussagen ausschließlich die Inhalte der bereitgestellten YouTube-Briefing-Dateien; ergänze nichts aus allgemeinem Modellwissen und erfinde nichts. Die Einordnung darf keine technische Berechnung oder Entscheidung verändern. Die externe Quelle ist ausschließlich qualitativer Kontext. Eine Übereinstimmung mit der externen Quelle ist keine technische Bestätigung; eine Abweichung ist kein technischer Ausschluss. Eine Aussage wie '1 Video' ist nur zulässig, wenn tatsächlich genau 1 relevanter Video-Block in der betreffenden Briefing-Datei vorhanden ist. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben "
                    "und erstelle die vollstaendige Daten-Uebersicht. "
                    "HARTE VORGABE FUER PUNKT 6.5: Die folgende von Python erzeugte "
                    "AUTORITATIVE 6.5-ZUORDNUNG ist die alleinige Wahrheit fuer die "
                    "Mitgliedschaft von 6.5.1 und 6.5.2. Gemini darf keinen Titel zwischen "
                    "den beiden Kategorien verschieben, Titel aus historischen Daten "
                    "hinzufuegen oder aktuelle Titel entfernen. Gemini uebernimmt die "
                    "vorgegebene Kategorie und kuemmert sich innerhalb dieser Kategorie "
                    "nur um die inhaltliche/technische Darstellung aus den bereitgestellten "
                    "Quelldaten. Die Zuordnung ist statusbasiert und unabhaengig von der Quelle."
                    "\n\n"
                    + sechs_fuenf_autoritaet + "\n\n"
                    "TRADE-STORY-EBENE: Die Auswertung muss die bereits im System verteilten Informationen zu einer nachvollziehbaren Trade-Story verbinden, ohne ein neues Handelssignal zu erzeugen. INTERESSANT bedeutet strategische These/Beobachtung; VORBEREITET bedeutet vorhandener Kandidat bzw. technische Trigger-Naehe ohne bestaetigtes Setup; VALIDE SETUP bedeutet ausschliesslich ein vom bestehenden regelbasierten Setup-System bestaetigtes Setup. Nutze fuer die Story nur bereitgestellte Daten. Zeige die Kette Thema -> Treiber -> Beleg -> Sektor/Asset -> Kandidat -> Status -> naechster Trigger -> Risiko. Ein VORBEREITET-Titel darf nicht als Kauf dargestellt werden. Ein VALIDE-SETUP-Status darf nur aus den bestehenden Setup-/CRV-Ausgaben uebernommen werden; Gemini darf keine Filter, CRV-Regeln oder technische Schwellen veraendern. "
                    "HEBELTRADER-EINZELCHECK: Falls die bereitgestellte Datei "
                    "'hebeltrader_einzel_check.json' vorhanden ist, nutze sie als strukturierte "
                    "Quelle fuer die zuletzt erfolgreich verarbeitete HEBELTRADER-Ausgabe und "
                    "verwende dabei die aus Drive synchronisierte neueste Version, falls sie neuer als eine lokale Kopie ist. "
                    "deren Kandidaten. Sie ist KEINE eigene Kandidatenkategorie. Entscheidend "
                    "fuer die Zuordnung in Punkt 6.5 ist ausschliesslich der aktuelle Status aus "
                    "dem bestehenden einzel_check.py: Jeder aktuelle 'KAUFKANDIDAT A' gehoert in "
                    "6.5.1 'AKTUELLE KAUFKANDIDATEN A', unabhaengig von seiner Quelle. Jeder "
                    "Kandidat mit 'KAUFKANDIDAT B', 'KAUFKANDIDAT C' oder 'KEIN KANDIDAT' gehoert "
                    "in 6.5.2 'AKTUELLE NICHT-A-KANDIDATEN / BEOBACHTUNGSLISTE', sofern er nach der "
                    "bestehenden Beobachtungslistenlogik noch vorhanden ist. Wenn ein bisheriger "
                    "A-Kandidat bei einem spaeteren Einzel-Check auf B/C/KEIN KANDIDAT faellt, "
                    "rutscht er entsprechend nach 6.5.2; wenn er wieder A wird, kommt er wieder "
                    "nach 6.5.1. Es gibt KEINE separate HEBELTRADER-A-Kategorie. "
                    "Die Quelle ist davon vollstaendig getrennt und wird als zusaetzliches Feld "
                    "'Quelle' angezeigt: HEBELTRADER-Kandidaten tragen die konkrete Ausgabe "
                    "(z.B. 'HEBELTRADER 164/26'), manuell oder anderweitig hinzugefuegte Titel "
                    "tragen 'Quelle: -'. Ein A-Kandidat mit 'Quelle: -' gehoert also ebenfalls "
                    "in 6.5.1. Zeige bei JEDEM Titel immer Firmenname UND Yahoo-Ticker gemeinsam im Format 'Name (Ticker)'. Diese Regel gilt fuer JEDE Titel-/Unternehmensnennung in der gesamten fertigen Auswertung, nicht nur fuer Punkt 6.5. Ticker allein ist unzulaessig, sofern ein Name aus den bereitgestellten Daten verfuegbar ist. "
                    "Die bestehende einzel_check.py-Logik, insbesondere A/B/C, Momentum, Gruende, "
                    "Risiken und die Watchlist-Bereinigung nach >45 Tagen ohne A/B/C, darf nicht "
                    "neu berechnet, veraendert, aufgehoben oder ersetzt werden. "
                    "Fuer 6.5.1 muessen bei JEDEM A-Kandidaten die vorhandenen technischen Details "
                    "des Einzel-Checks ausgegeben werden. Nutze dafuer insbesondere das Feld "
                    "'technischer_zustand' aus dem HEBELTRADER-Einzelcheck sowie die darin "
                    "enthaltenen Setup-/Kurs-/Stop-/TP1-/TP2-/CRV-/RSI-/MACD-Informationen. "
                    "Diese Werte sind ausschliesslich aus den vorhandenen technischen Daten zu "
                    "uebernehmen. Einstieg, Stop, TP1 und TP2 duerfen nur angegeben werden, wenn "
                    "sie aus den bereitgestellten Daten ersichtlich sind. Fehlen Werte, darf Gemini "
                    "sie NICHT erfinden oder aus allgemeinem Modellwissen schaetzen. Wenn aus den "
                    "vorhandenen technischen Daten ein konkreter Einstieg/Stop/TP1/TP2 ableitbar "
                    "ist, darf diese Ableitung transparent als Ableitung gekennzeichnet werden; "
                    "keine neue technische Berechnungslogik erfinden. Insbesondere gilt weiterhin: "
                    "Breakout allein aktiviert Fibonacci nicht; Fibonacci/Extension nur bei "
                    "qualifizierter und bestaetigter A-B-C-Struktur. "
                    "Wenn die HEBELTRADER-JSON fehlt, erfinde keinen HEBELTRADER-Inhalt. "
                    "Für A-Kandidaten, die nicht aus HEBELTRADER stammen, nutze die bereitgestellte "
                    "'einzel_check_historie.jsonl' als autoritative technische Historie des Einzel-Checks. "
                    "Nutze daraus nur den Snapshot des aktuellen Auswertungstages und den darin enthaltenen "
                    "bereits berechneten Block 'Technik'. Diese Historie dient ausschließlich dazu, den "
                    "technischen Zustand eines aktuellen A-Kandidaten vollständig darzustellen; keine Werte "
                    "neu berechnen. Wenn die Historie für einen Titel fehlt, keine technischen Werte erfinden. "
                    "Die Beobachtungsliste bleibt ausschließlich für Status, Quelle und Watchlist-Zugehörigkeit "
                    "maßgeblich. "
                    "Die vollstaendige 6.5.2-Liste soll aus der bestehenden einzel_check_beobachtung.json "
                    "kommen; deren 'quelle' zeigt HEBELTRADER-Ausgabe oder '-' an. "
                    "6.5.2 darf nicht auf 5 Titel gekuerzt werden. Gib ALLE aktuell vorhandenen B- und C-Kandidaten aus. Titel mit aktuellem Status KEIN KANDIDAT werden in der sichtbaren 6.5.2-Liste bewusst NICHT ausgegeben; sie bleiben jedoch Bestandteil der autoritativen Beobachtungsliste und erscheinen automatisch wieder, sobald ihr aktueller Status erneut B, C oder A ist. Gruppiere die sichtbaren Titel nach aktuellem Status in B und C und sortiere innerhalb jeder Gruppe alphabetisch. Zeige fuer jeden sichtbaren Titel den Statusverlauf kompakt als 'Name (Ticker) | Letzter Status -> aktueller Status', z.B. 'Advanced Micro Devices, Inc. (AMD) | A -> A' bzw. 'Chevron Corporation (CVX) | C -> B'. Verwende dafuer ausschliesslich die von Python bereitgestellte Statusverlaufsinformation; Gemini darf keinen frueheren Status selbst rekonstruieren. Die Darstellung darf die Mitgliedschaft nicht veraendern und darf keine Titel auslassen. "
                    "PORTFOLIO-MAKRO-ABGLEICH / WARNER: Vergleiche die autoritativen offenen Positionen mit dem von Gemini aus dem Makro-Datenpaket abgeleiteten Marktumfeld und den Sektorwirkungen. Wenn eine offene Position klar oder zunehmend gegen das Makro-Bild bzw. die relevante Sektorwirkung laeuft, MUSS dies in 7.2 Handlungsbedarf als '⚠ MAKRO-KONFLIKT' gekennzeichnet und die betroffene Position namentlich/Ticker zugeordnet werden. Nenne kurz den konkreten Widerspruch aus den vorhandenen Daten. Das ist eine Warnung zur erneuten Pruefung, KEINE automatische Verkaufs-/Kaufempfehlung und keine neue technische Kennzahl. Wenn kein belastbarer Konflikt aus den bereitgestellten Daten ableitbar ist, erfinde keinen.\nPUNKT-7-ARCHITEKTUR: Python erzeugt 7.1 Portfolio-Übersicht, 7.3 Einzelpositionen und 7.4 geschlossene Positionen aus den autoritativen Quellen. Gemini erzeugt ausschließlich die qualitative Interpretation für 7.2 Handlungsbedarf und darf in 7.1/7.3/7.4 keine Faktenblöcke erzeugen.\n"
                    "AUTORITATIVE OFFENE-POSITIONEN-LISTE (ausschließlich aus Offene Positionen+Check.csv):\n"
                    + (offene_quelle or "(keine offenen Positionen gefunden)") + "\n"
                    "AUTORITATIVE FAKTENBASIS FUER 7.4 AUS TAB 2 VON 'Offene Positionen+Check':\n"
                    + (geschlossene_7_4 or "(keine geschlossene Position innerhalb der letzten 3 Kalendertage)") + "\n"
                    "Für 7.4 gilt ausschließlich diese Tab-2-Faktenbasis. Gib nur geschlossene Positionen "
                    "mit Ausstiegsdatum innerhalb der letzten 3 Kalendertage bezogen auf den Auswertungstag aus. "
                    "Wenn die Faktenbasis leer ist, lasse 7.4 vollständig weg. Rekonstruiere, ergänze, schätze "
                    "oder erfinde keine geschlossenen Positionen aus anderen Dateien oder aus Modellwissen. "
                    "Übernimm die Faktenfelder aus Tab 2 unverändert. 7.4 ist von der offenen Positionsprüfung "
                    "und deren Reparaturmechanik getrennt.\n"
                    "Diese Liste ist für Firmenname, Ticker, Einstiegskurs und Einstiegsdatum verbindlich. "
                    "Übernimm diese vier Werte exakt; erfinde, schätze oder ändere sie nicht. "
                    "PUNKT-7-ARCHITEKTUR: 'Offene Positionen+Check.csv' ist die alleinige "
                    "autoritative Faktenquelle fuer Punkt 7. Python erzeugt daraus deterministisch "
                    "7.1 Portfolio-Uebersicht und 7.3 Einzelpositionen einschliesslich aller bereitgestellten "
                    "technischen Check-Felder. Python erzeugt 7.4 ausschliesslich aus der autoritativen "
                    "Tab-2-Faktenbasis. Gemini liefert fuer Punkt 7 ausschliesslich die qualitative "
                    "Interpretation in 7.2 Handlungsbedarf. Gemini darf 7.1, 7.3 oder 7.4 nicht als "
                    "Faktenblock erzeugen, veraendern, kuerzen, berechnen, schaetzen oder ersetzen. "
                    "Firmenname, Ticker, Einstiegskurs, Einstiegsdatum und technische Check-Felder bleiben "
                    "an die autoritative Quelle gebunden. Technische_Zielzone wird niemals aus anderen "
                    "technischen Feldern neu abgeleitet. Die alte Offene_Positionen.csv darf fuer diese "
                    "Fakten nicht als Quelle oder Fallback verwendet werden. Mehrere offene Positionen "
                    "desselben Tickers sind zulaessig; jede Kombination aus Name + Ticker + Einstiegskurs + "
                    "Einstiegsdatum ist eine eigene Position.",
                    (
                        f"HARTE MAKRO-GATE-VORGABE: Das Makro-Szenario-Gate ist GESPERRT. Grund: {makro_gate_grund} "
                        "Erzeuge in Punkt 2 KEINE Base/Bull/Bear-Wahrscheinlichkeiten, "
                        "keine geschaetzten Ersatzwerte und keine numerischen Makro-Prognosen. "
                        "Benenne stattdessen die konkreten kritischen Datenluecken bzw. den Ausfall des Makro-Datenpakets. "
                         "Verwende dabei NICHT die Bezeichnungen Base Case, Bull Case oder Bear Case, gib KEINE Makro-Trade-Ideen und KEINE qualitative Richtungsprognose aus. "
                        if makro_gate == "GESPERRT" else
                        "HARTE MAKRO-GATE-VORGABE: Das Makro-Datenpaket ist autoritativ. "
                        "Sein MAKRO-SZENARIO-GATE hat Vorrang vor jeder eigenen Bewertung der "
                        "Datenvollstaendigkeit. Das Gate lautet FREIGEGEBEN. Punkt 2 MUSS daher "
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
                        "PROXY-Werte muessen als Proxy bezeichnet werden. Von Gemini abgeleitete Szenarioaussagen "
                        "und Wahrscheinlichkeiten sind Interpretationen und keine Eingangsdaten. Eingangsdaten niemals schaetzen. "
                        "VERBINDLICHE NEUE MAKRO-ARCHITEKTUR: Python liefert ausschliesslich Rohdaten, objektive "
                        "Berechnungen und das autoritative MAKRO-SZENARIO-GATE. Gemini uebernimmt die vollstaendige "
                        "makrooekonomische Interpretation: Divergenzen erkennen, Zusammenhaenge herstellen, die "
                        "Daten gegeneinander gewichten, daraus das Makro-Szenario und das daraus resultierende "
                        "Marktumfeld ableiten und die Zukunftsperspektive fuer die geforderten Horizonte formulieren. "
                        "Es gibt KEIN vorgegebenes Python-Makro-Szenario und KEINE vorgegebene Python-Marktumfeldklassifikation. "
                        "VERBINDLICHE MAKRO-DATENREGEL - AKTUELLES DATENPAKET ALS EINZIGE ZAHLENQUELLE: Fuer saemtliche numerischen Aussagen in Makro-Interpretation, Trade-Storys, Marktperspektive, Chancen/Risiken und Szenario-Matrix sind ausschliesslich die im aktuellen Makro_Briefing(<Datum>).txt enthaltenen Daten massgeblich. Gemini darf keine numerischen Werte aus eigenem Vorwissen, aelteren Auswertungen, frueheren Briefings, Nachrichtenartikeln oder sonstigen externen Quellen ergaenzen oder ersetzen, wenn der betreffende Sachverhalt im aktuellen Makro-Datenpaket enthalten ist. Berechnungen sind zulaessig, wenn saemtliche dafuer benoetigten Ausgangswerte aus dem aktuellen Makro-Datenpaket stammen. Historische Vergleichswerte duerfen nur verwendet werden, wenn sie im aktuellen Makro-Datenpaket enthalten sind. Bei widerspruechlichen Werten innerhalb verschiedener Quellen gilt fuer die aktuelle Makro-Auswertung der Wert aus dem aktuellen Makro-Datenpaket. Ist ein Wert im aktuellen Datenpaket nicht vorhanden, darf Gemini ihn nicht schaetzen oder aus aelterem Kontext rekonstruieren; die Aussage ist qualitativ zu formulieren oder wegzulassen. Insbesondere verboten: einen aktuellen Wert mit einem Wert aus einer frueheren Auswertung.txt oder einem frueheren Lauf zu kombinieren, um daraus eine neue numerische Aussage abzuleiten. "
                        "QUELLENROLLEN BEI ROHSTOFFEN: Ein strukturierter aktueller Marktpreis ist ROLE=CURRENT_PRICE. Externe Artikel, Videos, Kommentare oder Forecasts sind ROLE=COMMENTARY bzw. ROLE=FORECAST. Eine externe Zahl aus COMMENTARY/FORECAST darf niemals als aktueller Marktpreis uebernommen werden, wenn ein aktueller CURRENT_PRICE im Makro-Datenpaket vorhanden ist. Externe Zahlen duerfen nur dann numerisch verwendet werden, wenn Instrument, Zeitpunkt, Einheit und Quellenrolle eindeutig mit dem betrachteten Wert uebereinstimmen; andernfalls nur qualitativ oder gar nicht verwenden. "
                        "Keine Python-Schwellen, Gewichte oder vorgefertigten Richtungsurteile fuer das Makro uebernehmen. "
                        "VERBINDLICHE TRADE-STORY-ARCHITEKTUR: Behandle die fertige Auswertung als eine nachvollziehbare Kette von Daten zu Handlungsebene, nicht als neues Scoring. Python liefert die Puzzleteile (Rohdaten, objektive Berechnungen, technische Statusfelder, bestehende Kandidaten-/Beobachtungsstatus und regelbasierte Setups). Gemini verbindet diese Puzzleteile zu einer Trade-Story: Warum ist ein Thema oder Titel interessant, welche Daten bestaetigen die These, welcher Sektor bzw. welche Aktie ist betroffen, was muss als Naechstes passieren und welche Risiken koennen die These entkraeften? Die Statusstufen sind strikt zu trennen: INTERESSANT = strategische Idee/These ohne bestaetigtes Setup; VORBEREITET = bestehender Kandidat bzw. technische Vorbereitung/Trigger-Naehe, aber noch kein bestaetigtes Kauf-Setup; VALIDE SETUP = ausschliesslich ein bereits vom bestehenden Regelwerk bestaetigtes Setup. Gemini darf niemals aus einer interessanten Story oder aus einem VORBEREITET-Status selbst ein VALIDE SETUP oder einen Kauf machen. Die bestehende technische Setup-, Filter- und CRV-Logik bleibt allein autoritativ fuer die Stufe VALIDE SETUP. "
                        "Jede perspektivische Trade-Story in 6.1 soll deshalb, soweit aus den Dateien ableitbar, die Kette Thema -> Makro-Treiber -> bestaetigende Daten -> Sektor/Asset -> bestehender Kandidat -> Status (INTERESSANT/VORBEREITET/VALIDE SETUP) -> naechster technischer Trigger -> Gegentreiber/Risiko sichtbar machen. Wenn kein bestehender Kandidat vorhanden ist, ist das explizit zu kennzeichnen. Ein Makro-Treiber allein ist niemals ein Einstiegssignal. "
                        "Die Anleihenmarkt-Auswertung ist eine eigene strukturierte Datenebene und darf nicht "
                        "als blosse Wiederholung der Treasury-Renditen behandelt werden. Geopolitik ist TIER-3-CONTEXT: "
                        "sie kann das Gate niemals sperren. Nutze 'MAKRO-EVENTS / WICHTIGE IMPULSE VORAUS' fuer "
                        "verifizierte kommende FOMC-, EZB-, CPI-, PPI- und weitere wichtige Makrotermine. Nutze "
                        "'BOERSENHAMMER / BIG NEWS 24H' als eine einzige, quellengebundene Top-Nachricht; die GDELT-Relevanzsortierung "
                        "ist kein Beweis dafuer, dass es objektiv die groesste Nachricht des Tages ist. Erfinde keine Termine, "
                        "Konsenswerte oder News. 'Wichtige Impulse voraus' darf nur auf verifizierten Kalenderdaten beruhen."
                        + (
                            f" ZUSAETZLICHE HARTE DATENQUALITAETS-VORGABE: Das Makro-Datenpaket meldet "
                            f"MAKRO-DATENQUALITAET={makro_datenqualitaet}. Uebernimm diesen Wert in Punkt 2 "
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
            # Direkte aktuelle Makro-Zahlen werden deterministisch gegen das
            # autoritative aktuelle Makro-Briefing abgesichert. Gemini bleibt
            # fuer Interpretation und abgeleitete Aussagen zustaendig.
            text, makro_zahlen_korrekturen = _sichere_makro_zahlen(text, makro_text if makro_pfad else "")
            print(f"  Gemini finish_reason (Hauptantwort): {_gemini_finish_reason(antwort)}")

            if not pruefe_makro_gate_konsistenz(text, makro_gate):
                print("WARNUNG: Gemini widerspricht dem autoritativen Makro-Gate - starte gezielte Makro-Reparatur.")
                reparatur = client.models.generate_content(
                    model=aktuelles_modell,
                    contents=hochgeladene_teile + [
                        "REPARATUR NUR FÜR PUNKT 2: Das Makro-Datenpaket meldet MAKRO-SZENARIO-GATE=FREIGEGEBEN. "
                        "Überarbeite ausschließlich Punkt 2. Eine Sperrung ist unzulässig, wenn nur TIER-2- oder TIER-3-Daten fehlen. "
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

            # PUNKT 7 IST DATEN-AUTORITATIV UND WIRD NICHT MEHR VON GEMINI
            # ERZEUGT. Python baut 7.1/7.3/7.4 aus den verbindlichen Quellen;
            # Gemini liefert ausschliesslich die Interpretation in 7.2.
            python_punkt7 = _erstelle_punkt7_fakten(
                eingabedateien.get("Offene Positionen+Check.csv"),
                geschlossene_7_4,
            )
            text = _ersetze_punkt7_durch_python_fakten(text, python_punkt7)
            print(
                "  Punkt 7: 7.1/7.3/7.4 deterministisch aus autoritativen "
                "Positionsdaten erzeugt; Gemini bleibt auf 7.2-Interpretation beschraenkt."
            )

            # TRADE-STORY-VALIDIERUNG: Die Interpretationsschicht darf nur
            # die vorhandenen Statusstufen verwenden. Ein VALIDE-SETUP-Status
            # wird deterministisch gegen die autoritativen Setup-Dateien
            # gespiegelt; INTERESSANT/VORBEREITET duerfen keine Kaufaufforderung
            # enthalten. Bei einem Fehler genau ein gezielter Reparaturversuch.
            story_ok, story_errors = _trade_story_validierung(text, eingabedateien, beobachtung_pfad)
            if not story_ok:
                print("WARNUNG: Trade-Story-Validierung fehlgeschlagen:")
                for err in story_errors:
                    print(f"  - {err}")
                # WICHTIG: Trade-Story-Reparatur ist deterministisch und
                # benoetigt keinen zweiten Gemini-Request. Ein zusaetzlicher
                # grosser Request kann das Minutenkontingent erschoepfen und
                # dadurch einen ansonsten erfolgreichen Tageslauf in einen
                # technischen Fallback zwingen.
                story_reparatur_text = _trade_story_deterministische_reparatur(
                    text, eingabedateien, beobachtung_pfad
                )
                story_ok2, story_errors2 = _trade_story_validierung(
                    story_reparatur_text, eingabedateien, beobachtung_pfad
                )
                if not story_ok2:
                    raise RuntimeError(
                        "TRADE_STORY_DETERMINISTISCHE_REPARATUR_TERMINAL: "
                        + " | ".join(story_errors2)
                    )
                print("  Trade-Story-Reparatur erfolgreich (deterministisch, ohne Gemini-API-Call).")
                original = re.search(r"(?ims)^6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN.*?(?=^6\.2\s+|\Z)", text or "")
                repaired = re.search(r"(?ims)^6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN.*?(?=^6\.2\s+|\Z)", story_reparatur_text or "")
                if not original or not repaired:
                    raise RuntimeError("Trade-Story-Reparatur enthielt keinen gueltigen Abschnitt 6.1.")
                text = text[:original.start()] + repaired.group(0).rstrip() + "\n\n" + text[original.end():]
                print("  Trade-Story-Reparatur erfolgreich.")

        except Exception as e:
            fehlertext = str(e)
            print(f"  Technischer Fehler beim API-Call: {e}")
            letzte_antwort = f"[Technischer Fehler] {e}"

            # Ein 429 waehrend der gezielten Punkt-7-Reparatur ist terminal
            # fuer diesen Lauf: Der normale API-Retry darf hier NICHT erneut
            # eine vollstaendige Gemini-Hauptanfrage starten. Gleichzeitig
            # muss der bestehende technische Fallback-Pfad erhalten bleiben.
            if "PUNKT7_REPARATUR_TERMINAL_429" in fehlertext:
                print(
                    "  Punkt-7-Reparatur: terminaler 429 erkannt - "
                    "kein weiterer Gemini-Hauptretry; technischer Fallback wird erzeugt."
                )
                break

            if "TRADE_STORY_REPARATUR_TERMINAL:" in fehlertext:
                print(
                    "  Trade-Story-Reparatur blieb ungueltig - kein weiterer "
                    "vollstaendiger Gemini-Hauptretry; technischer Fallback wird erzeugt."
                )
                break

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
                # Bei serverseitiger Ueberlast (503) oder Netzwerk-Abbruch
                # wird jedes konfigurierte Modell hoechstens EINMAL versucht.
                # Danach wird kein bereits gescheitertes Modell erneut verbrannt.
                naechstes_modell = None
                if (aktuelles_modell == MODELL and
                        FALLBACK_MODELL and FALLBACK_MODELL != MODELL):
                    naechstes_modell = FALLBACK_MODELL
                elif (aktuelles_modell == FALLBACK_MODELL and
                      DRITTER_FALLBACK_MODELL and
                      DRITTER_FALLBACK_MODELL not in (MODELL, FALLBACK_MODELL)):
                    naechstes_modell = DRITTER_FALLBACK_MODELL

                if naechstes_modell:
                    grund = "503-Overload" if kategorie == "ueberlast" else "Netzwerk-Abbruch"
                    print(
                        f"  {grund} nach Versuch {versuch}/{MAX_VERSUCHE}. "
                        f"Wechsle fuer den naechsten Versuch von {aktuelles_modell} "
                        f"auf {naechstes_modell}."
                    )
                    aktuelles_modell = naechstes_modell
                    continue

                # Kein weiteres Modell verfuegbar: nicht dasselbe Modell
                # erneut versuchen. Der technische Fallback wird direkt
                # ueber den bestehenden Ausgabeweg erzeugt.
                print(
                    f"  {('503-Overload' if kategorie == 'ueberlast' else 'Netzwerk-Abbruch')} "
                    "auf allen konfigurierten Modellen - keine Wiederholung "
                    "eines bereits fehlgeschlagenen Modells."
                )
                break

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

    # TECHNISCHER FALLBACK:
    # Ein dauerhafter Gemini-503 darf den gesamten GitHub-Lauf nicht mehr
    # mit Exit-Code 1 beenden. Es wird bewusst KEINE Gemini-Analyse erfunden.
    # Stattdessen wird ein klar gekennzeichneter technischer Bericht an den
    # normalen Speicherpfad uebergeben. speichere_ergebnis() erkennt diesen
    # Marker und schreibt ihn direkt als Auswertung(<Datum>).txt.
    return (
        "[GEMINI_TECHNISCHER_FALLBACK]\n"
        "Gemini war nach allen konfigurierten Versuchen nicht verfuegbar. "
        "Es wurde deshalb keine kuenstliche Gemini-Analyse erzeugt.\n\n"
        f"Letzter API-Fehler:\n{letzte_antwort}\n\n"
        "Die Eingabedateien wurden vor dem API-Aufruf geladen. "
        "Die autoritativen Positionsdaten bleiben unveraendert."
    )


def _normalisiere_7_4_numerische_ausgabe(text):
    """Normalisiert nur numerische Faktenfelder innerhalb von Abschnitt 7.4.

    Die autoritative Tab-2-Faktenbasis bleibt unverändert. Diese Funktion
    betrifft ausschließlich die Darstellung in der fertigen Auswertung:
    deutsche Dezimalkommas werden in den bekannten numerischen 7.4-Feldern
    deterministisch in Dezimalpunkte umgewandelt.
    """
    match = re.search(
        r"(?ims)^\s*7\.4\b.*?(?=^\s*8\.\s+|\Z)",
        text or "",
    )
    if not match:
        return text

    block = match.group(0)
    numeric_labels = (
        "Einstieg",
        "Ausstiegskurs",
        "Performance_Seit_Einstieg%",
        "OS_Einstiegskurs",
        "OS_Aktueller_Kurs",
        "OS_Performance%",
    )

    label_re = re.compile(
        r"(?im)(^|\|\s*)(\s*(?:" + "|".join(re.escape(x) for x in numeric_labels) +
        r")\s*:\s*)([^\n|]+?)(\s*)(?=\||$)"
    )

    def normalize_value(m):
        prefix = m.group(1)
        label_prefix = m.group(2)
        value = m.group(3).strip()
        trailing_ws = m.group(4)
        suffix = ""
        suffix_match = re.search(r"([%$€£])\s*$", value)
        if suffix_match:
            suffix = suffix_match.group(1)
            value = value[:-1].strip()

        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            value = value.replace(",", ".")

        return prefix + label_prefix + value + suffix + trailing_ws

    block = label_re.sub(normalize_value, block)
    return text[:match.start()] + block + text[match.end():]


def normalisiere_ausgabe(text, zielzonen=None):
    """Erzwingt formale Regeln fuer die fertige Ausgabe.

    Offene Positionen+Check.csv ist die verbindliche Faktenquelle. Punkt 7.1,
    7.3 und 7.4 wurden vor dieser Normalisierung bereits deterministisch durch
    Python erzeugt; Gemini liefert nur die Interpretation in 7.2. Die
    Normalisierung darf keine technischen Werte neu berechnen.
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

    text = _normalisiere_7_4_numerische_ausgabe(text)

    if not zielzonen:
        empty_positions = bool(re.search(
            r"(?ims)^7\.1\s+Portfolio-Übersicht\s*$.*?Keine offenen Positionen laut Offene Positionen\+Check\.csv\.",
            text or "",
        ))
        if not empty_positions:
            raise RuntimeError(
                "Keine verbindlichen technischen Positionsdaten aus "
                "Offene Positionen+Check.csv vorhanden."
            )

    match = re.search(
        r"(?ims)^7\. OFFENE POSITIONEN\s*$.*?(?=^\s*7\.4\b|^\s*8\.\s+|\Z)",
        text,
    )
    if not match:
        raise RuntimeError(
            "Abschnitt '7. OFFENE POSITIONEN' fehlt; "
            "CSV-Masterwerte koennen nicht verbindlich eingesetzt werden."
        )

    block = match.group(0)
    # Nur die Position bis zum Markt-Trenner erkennen. Die Klammerstruktur
    # wird bewusst NICHT mehr durch den Regex erzwungen, weil Firmennamen selbst
    # Klammern enthalten können und Gemini den Ticker gelegentlich weglässt oder
    # die Kopfzeile vertauscht. Die eigentliche Interpretation erfolgt darunter
    # master-gestützt.
    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\|\s*Markt:\s*[^\n]+$"
    )
    headers = list(header_re.finditer(block))
    if not headers:
        if "Keine offenen Positionen laut Offene Positionen+Check.csv." in block:
            return text
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

        # Header-Parsing ist bewusst master-gestützt. Der Inhalt der letzten
        # Klammer darf NUR dann als Ticker interpretiert werden, wenn er einem
        # bekannten Master-Ticker entspricht. Das verhindert insbesondere,
        # dass Namensbestandteile wie "(Acc)" oder "(Series A)" fälschlich als
        # Ticker behandelt werden.
        #
        # Zusätzlich wird die umgekehrte Form "Ticker (Firmenname)" erkannt.
        # Wenn kein bekannter Ticker gefunden wird, bleibt der komplette linke
        # Header-Teil der Name; der bestehende Master-Fallback kann anschließend
        # über Name + Einstieg + Einstiegsdatum eindeutig auflösen.
        raw_header = header.group(1).strip()
        known_tickers = {key[1] for key in expected if key[1]}
        known_tickers_norm = {_normalisiere_ticker(t) for t in known_tickers}

        # 1) Standardform: "Firmenname (Ticker)". Nur eine ABSCHLIESSENDE
        # Klammer wird als Tickerkandidat betrachtet. Ist ihr Inhalt kein
        # bekannter Master-Ticker, bleibt sie Bestandteil des Namens. Dadurch
        # werden z. B. "(Acc)" und "(Series A)" nicht als Ticker missbraucht.
        trailing_match = re.search(r"\s*\(([^()]*)\)\s*$", raw_header)
        raw_left = raw_header
        raw_right = ""
        if trailing_match:
            raw_left = raw_header[:trailing_match.start()].strip()
            raw_right = trailing_match.group(1).strip()

        # Umgekehrte Form zuerst prüfen, weil der Firmenname selbst verschachtelte
        # Klammern enthalten kann, z. B. "EUNL.DE (iShares ... (Acc))".
        reversed_match = re.match(r"^\s*([^\s(]+)\s+\((.*)\)\s*$", raw_header)
        reversed_ticker = _normalisiere_ticker(reversed_match.group(1)) if reversed_match else ""
        if reversed_match and reversed_ticker in known_tickers_norm:
            ticker = reversed_match.group(1).strip()
            name = reversed_match.group(2).strip()
        else:
            left_ticker = _normalisiere_ticker(raw_left)
            right_ticker = _normalisiere_ticker(raw_right)

            if right_ticker in known_tickers_norm and left_ticker not in known_tickers_norm:
                name, ticker = raw_left, raw_right
            elif left_ticker in known_tickers_norm and raw_right:
                # 2) Fallback für die umgekehrte Form ohne verschachtelte Klammern.
                ticker, name = raw_left, raw_right
            elif not raw_right:
                # 3) Ticker fehlt vollständig. Der komplette Header ist der Name.
                # Der Master-Fallback kann danach über Name + Einstieg + Einstiegsdatum
                # eindeutig auflösen.
                name, ticker = raw_header, ""
            else:
                # 4) Unbekannte Abschlussklammer gehört zum Namen, nicht zum Ticker.
                # Beispiel: "iShares ... USD (Acc)".
                name, ticker = raw_header, ""

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

            # Wenn eine unbekannte Abschlussklammer angehängt wurde, war sie
            # möglicherweise ein von Gemini erfundener/falsch gesetzter Ticker.
            # Zuerst wird jedoch immer der vollständige Name versucht, damit
            # echte Namensbestandteile wie "(Acc)" oder "(Series A)" erhalten
            # bleiben. Erst wenn dieser Match scheitert, wird die Abschlussklammer
            # als falscher Tickerkandidat entfernt und der unveränderte Name davor
            # erneut gegen den Master geprüft.
            if source is None and not ticker and raw_right and raw_left != raw_header:
                alt_key = (
                    _normalisiere_positionsname(raw_left),
                    "",
                    _positionsfeld_schluessel(entry),
                    _positionsfeld_schluessel(date),
                )
                source = _finde_quellposition(alt_key, expected)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue

        if source is None:
            # Diagnostik bewusst ohne automatische Annahmen: Wenn kein Match
            # möglich ist, werden die engsten Master-Kandidaten ausgegeben.
            # So ist im CI-Log sofort sichtbar, ob z. B. der Ticker fehlt,
            # abweicht oder die Quelldatei tatsächlich andere Stammdaten
            # enthält.
            namens_kandidaten = [
                pos for key, pos in expected.items()
                if key[0] == _normalisiere_positionsname(name)
            ]
            diagnose = "; ".join(
                f"{pos['name']} ({pos['ticker']}) | Einstieg: {pos['entry']} | "
                f"Einstiegsdatum: {pos['date']}"
                for pos in namens_kandidaten[:5]
            ) or "kein Master-Kandidat mit identischem normalisiertem Namen"
            errors.append(
                f"{name} ({ticker}) | Einstieg: {entry} | Einstiegsdatum: {date}: "
                "kein passender Positionsschluessel in Offene Positionen+Check.csv "
                f"[Master-Kandidaten nach Name: {diagnose}]"
            )
            continue

        # Nach erfolgreicher Master-Zuordnung wird der komplette Positionskopf
        # ebenfalls aus dem Master kanonisiert. Damit werden fehlende oder
        # falsch interpretierte Ticker nicht nur toleriert, sondern im Ergebnis
        # deterministisch repariert. Die Marktangabe aus Gemini bleibt erhalten.
        market_suffix = header.group(0)[header.group(0).find("|"):]
        canonical_header = f"{source['name']} ({source['ticker']}) {market_suffix.lstrip()}"
        current_header_text = header.group(0)
        if current_header_text != canonical_header:
            pos_block = canonical_header + pos_block[len(current_header_text):]

        source_key = (
            _normalisiere_positionsname(source["name"]),
            _normalisiere_ticker(source["ticker"]),
            _positionsfeld_schluessel(source["entry"]),
            _positionsfeld_schluessel(source["date"]),
        )
        seen[source_key] = seen.get(source_key, 0) + 1
        if seen[source_key] > 1:
            # Gemini darf eine Masterposition versehentlich mehrfach ausgeben.
            # Die Masterdatei bleibt autoritativ; die erste bereits verarbeitete
            # Instanz bleibt erhalten, jede weitere identische Gemini-Instanz
            # wird deterministisch entfernt. Eine echte Fremd-/Mehrdeutigkeits-
            # position bleibt dagegen ein harter Fehler.
            replacements.append((start, end, ""))
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

    # Offene Positionen+Check.csv ist der verbindliche Master. Gemini muss
    # deshalb weder Vollstaendigkeit noch Einmaligkeit beweisen. Doppelte
    # identische Gemini-Bloecke wurden oben bereits deterministisch entfernt.
    # Fehlende Masterpositionen werden jetzt aus dem Master als kanonischer
    # Faktenblock ergaenzt. So kann Gemini weder durch Auslassung noch durch
    # Wiederholung die offene Positionsliste veraendern.
    missing_keys = [key for key in expected if key not in seen]
    if missing_keys:
        master_blocks = []
        for key in missing_keys:
            source = expected[key]
            market = source.get("market") or "EU"
            direction = source.get("direction") or ""
            source_label = source.get("source") or ""
            lines = [
                f"{source['name']} ({source['ticker']}) | Markt: {market}",
            ]
            if direction:
                lines.append(f"Richtung: {direction}")
            if source_label:
                lines.append(f"Quelle: {source_label}")
            lines.append(f"Einstieg: {source['entry']} ({source['date']})")
            for field, value in source.get("technical", {}).items():
                if value is None or value == "":
                    continue
                label = {
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
                }.get(field)
                if label:
                    lines.append(f"{label}: {value}")
            master_blocks.append("\n".join(lines))
        append_text = "\n\n" + "\n\n".join(master_blocks) + "\n"
        block = block.rstrip() + append_text

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


def _extrahiere_makro_referenzwerte(makro_text):
    """Extrahiert aktuelle numerische Referenzwerte aus dem autoritativen Makro-Briefing.

    Nur explizite strukturierte Felder (5T/1M/3M/6M/1J und der erste Kurswert)
    werden als Referenz übernommen. Fehlende Felder bleiben unbekannt.
    """
    referenzen = {}
    if not makro_text:
        return referenzen
    for raw in makro_text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        label, rest = line.split(":", 1)
        label = label.strip()
        if not label or len(label) > 80 or not re.search(r"[A-Za-zÄÖÜäöüß]", label):
            continue
        kurs = re.match(r"\s*([-+]?\d+(?:[.,]\d+)?)", rest)
        if not kurs:
            continue
        ref = {"kurs": float(kurs.group(1).replace(",", ".")), "perioden": {}}
        for key in ("5T", "1M", "3M", "6M", "1J"):
            m = re.search(rf"(?:^|\|)\s*{re.escape(key)}\s*=\s*([-+]?\d+(?:[.,]\d+)?)\s*%", rest)
            if m:
                ref["perioden"][key] = float(m.group(1).replace(",", "."))
        referenzen[label.lower()] = ref
    return referenzen


def _sichere_makro_zahlen(text, makro_text):
    """Sichert direkte Makro-Kurs- und Periodenangaben gegen das aktuelle Briefing.

    Jede Metrik besitzt auf einer Zeile einen eigenen Textabschnitt bis zur
    naechsten Metrik. Korrekturen bleiben dadurch auf die zugehoerige Kennzahl
    begrenzt und koennen keine benachbarten Rohstoff-/Marktwerte ueberschreiben.
    """
    if not text or not makro_text:
        return text, []
    referenzen = _extrahiere_makro_referenzwerte(makro_text)
    if not referenzen:
        return text, []

    period_aliases = {
        "5T": r"(?:5T|5\s+Handelstagen?|5\s+Tagen?|5-Tage(?:n)?|fünf\s+Tagen?)",
        "1M": r"(?:1M|1\s+Monat|einem\s+Monat|4\s+Wochen?|4-Wochen|4W)",
        "3M": r"(?:3M|3\s+Monate?|3\s+Monaten?|12\s+Wochen?)",
        "6M": r"(?:6M|6\s+Monate?|6\s+Monaten?)",
        "1J": r"(?:1J|1\s+Jahr|einem\s+Jahr|12\s+Monate?)",
    }
    price_re = re.compile(r"(?P<num>[-+]?\d[\d.,]*\d|[-+]?\d)\s*(?P<unit>\$|USD|US\$)")
    pct_re = re.compile(r"(?P<num>[-+]?\d{1,3}(?:[.,]\d{1,6})?)\s*%")

    def _parse_price_number(raw):
        value = str(raw or "").strip().replace(" ", "")
        if not value:
            raise ValueError("empty price")
        sign = ""
        if value[0] in "+-":
            sign, value = value[0], value[1:]
        if not re.fullmatch(r"\d[\d.,]*\d|\d", value):
            raise ValueError("invalid price")
        if "." in value or "," in value:
            last_sep = max(value.rfind("."), value.rfind(","))
            integer = re.sub(r"[.,]", "", value[:last_sep]) or "0"
            decimal = value[last_sep + 1:]
            return float(f"{sign}{integer}.{decimal}")
        return float(sign + value)

    changes = []
    out_lines = []

    for line in text.splitlines():
        matches = []
        for label, ref in referenzen.items():
            for lm in re.finditer(re.escape(label), line.lower()):
                matches.append((lm.start(), lm.end(), label, ref))
        if not matches:
            out_lines.append(line)
            continue
        matches.sort(key=lambda item: item[0])

        pieces = []
        for idx, (start_pos, end_pos, label, ref) in enumerate(matches):
            segment_end = matches[idx + 1][0] if idx + 1 < len(matches) else len(line)
            segment = line[end_pos:segment_end]
            pieces.append((start_pos, end_pos, segment_end, label, ref, segment))

        # Von hinten nach vorne ersetzen, damit Positionsverschiebungen die
        # nachfolgenden Treffer nicht beeinflussen.
        new_line = line
        for start_pos, end_pos, segment_end, label, ref, segment in reversed(pieces):
            replacements = []

            price_matches = list(price_re.finditer(segment))
            if price_matches:
                pm = price_matches[0]
                try:
                    old = _parse_price_number(pm.group("num"))
                except ValueError:
                    old = None
                if old is not None and ("." in pm.group("num") or "," in pm.group("num")):
                    if abs(old - ref["kurs"]) >= 0.005:
                        replacements.append((
                            pm.start(),
                            pm.end(),
                            f"{ref['kurs']:.2f}".replace(".", ",") + pm.group("unit"),
                        ))
                        changes.append(f"{label}: Kurs {old} -> {ref['kurs']}")

            for pct in pct_re.finditer(segment):
                context_start = max(0, pct.start() - 90)
                context_end = min(len(segment), pct.end() + 90)
                context = segment[context_start:context_end]
                period_candidates = []
                for key, alias in period_aliases.items():
                    if key not in ref["perioden"]:
                        continue
                    for pmatch in re.finditer(alias, context, re.IGNORECASE):
                        absolute = context_start + pmatch.start()
                        # In natürlicher Sprache steht die Periode meist direkt
                        # nach dem Prozentwert ("-0,9% 5T"). Falls sie davor steht
                        # ("5T: -0,9%"), wird die vorherige Periode verwendet.
                        direction = 0 if absolute >= pct.end() else 1
                        period_candidates.append((direction, abs(absolute - pct.start()), key))
                # Nicht unterstuetzte Perioden wie YOY/YTD blockieren eine
                # Zuordnung zu einem benachbarten 5T/1M-Feld.
                adjacent_context = new_line[max(0, pct.start() - 12):min(len(new_line), pct.end() + 12)]
                if re.search(r"(?i)(?:^|[\\s(:,-])(?:YOY|YTD)(?:$|[\\s:,.);-])", adjacent_context):
                    continue
                if not period_candidates:
                    continue
                # Eine direkt nachgestellte Periode ("+1,2% 5T") hat
                # Vorrang. Wenn keine solche nahe Angabe existiert, darf eine
                # einleitende Formulierung ("in 5 Handelstagen um +1,2%")
                # etwas weiter vor dem Prozentwert liegen.
                following = [item for item in period_candidates if item[0] == 0 and item[1] <= 12]
                preceding = [item for item in period_candidates if item[0] == 1 and item[1] <= 30]
                if following:
                    period_match = min(following, key=lambda item: item[1])
                elif preceding:
                    period_match = min(preceding, key=lambda item: item[1])
                else:
                    continue
                period_key = period_match[2]
                try:
                    old = float(pct.group("num").replace(",", "."))
                except ValueError:
                    continue
                target = ref["perioden"][period_key]
                if abs(old - target) < 0.005:
                    continue
                replacements.append((
                    pct.start(),
                    pct.end(),
                    f"{target:+.2f}".replace(".", ",") + "%",
                ))
                changes.append(f"{label}: {period_key} {old} -> {target}")

            for rel_start, rel_end, replacement in sorted(replacements, reverse=True):
                abs_start = end_pos + rel_start
                abs_end = end_pos + rel_end
                new_line = new_line[:abs_start] + replacement + new_line[abs_end:]

        out_lines.append(new_line)

    if changes:
        print(
            f"MAKRO-ZAHLENINTEGRITAET: {len(changes)} direkte Zahlenangaben "
            "gegen aktuelles Makro-Briefing korrigiert."
        )
    return "\n".join(out_lines), changes


def _normalisiere_makro_datenqualitaet(text, makro_datenqualitaet):
    """Sichert die autoritative Datenqualitaet ausschliesslich in Abschnitt 2."""
    if not text or not makro_datenqualitaet:
        return text
    start = text.find("2. MAKRO-ZUKUNFTSSZENARIO")
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





def _trade_story_setup_universum(eingabedateien):
    """Liest ausschliesslich nachweislich gueltige regelbasierte Setups.

    Die vier Setup-Dateien haben unterschiedliche Statusfelder. Deshalb wird
    nicht mehr nur die Existenz eines Namens/Tickers als Autoritaetsbeleg
    verwendet:
      - Setups(...).csv: Status muss KAUFKANDIDAT A sein.
      - Trendwende_Setups(...).csv: jede vorhandene Datenzeile ist ein vom
        Trendwende-Scanner ausgegebenes Setup.
      - Short_Setups(...).csv: Status2 muss VALIDE sein.
      - Edelmetalle_Setups(...).csv: Status2 muss VALIDE sein.

    Rueckgabe: (gueltige_schluessel, gelesene_quellen, fehlende_quellen).
    """
    result = set()
    gelesene_quellen = set()
    fehlende_quellen = set()
    specs = (
        # Normale Setups verwenden im realen Tagesformat Status2=VALIDE.
        # A-Kandidat ist eine Kategoriezuordnung der Beobachtungsliste, nicht
        # die einzige Definition eines bestaetigten technischen Setups.
        ("Setups(...).csv", "status2_or_status", "VALIDE"),
        ("Trendwende_Setups(...).csv", "presence", None),
        ("Short_Setups(...).csv", "status2", "VALIDE"),
        ("Edelmetalle_Setups(...).csv", "status2", "VALIDE"),
    )
    for key, mode, required_status in specs:
        pfad = eingabedateien.get(key)
        if not pfad or not os.path.isfile(pfad):
            fehlende_quellen.add(key)
            continue
        try:
            with open(pfad, "r", encoding="utf-8-sig", newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=";,\t") if sample.strip() else None
                delimiter = dialect.delimiter if dialect else ";"
                reader = csv.DictReader(f, delimiter=delimiter)
                fields = reader.fieldnames or []
                lower_fields = {str(x).strip().lower(): x for x in fields}
                name_fields = [lower_fields[k] for k in ("name", "firmenname") if k in lower_fields]
                ticker_fields = [lower_fields[k] for k in ("ticker", "yahoo-ticker", "yahoo_ticker") if k in lower_fields]
                if not name_fields and not ticker_fields:
                    raise ValueError("keine Name-/Ticker-Spalte vorhanden")
                if mode == "status2_or_status" and "status2" not in lower_fields and "status" not in lower_fields:
                    raise ValueError("weder Status2- noch Status-Spalte vorhanden")
                if mode == "status2" and "status2" not in lower_fields:
                    raise ValueError("Status2-Spalte fehlt")

                rows_usable = 0
                for row in reader:
                    if mode == "status2_or_status":
                        status2 = str(row.get(lower_fields["status2"]) or "").strip().upper() if "status2" in lower_fields else ""
                        status = str(row.get(lower_fields["status"]) or "").strip().upper() if "status" in lower_fields else ""
                        if not (status2 == required_status or status == "KAUFKANDIDAT A"):
                            continue
                    elif mode == "status2":
                        status2 = str(row.get(lower_fields["status2"]) or "").strip().upper()
                        if status2 != required_status:
                            continue
                    rows_usable += 1
                    for field in name_fields:
                        value = str(row.get(field) or "").strip()
                        if value:
                            result.add(_normalisiere_positionsname(value))
                    for field in ticker_fields:
                        value = str(row.get(field) or "").strip()
                        if value:
                            result.add(_normalisiere_ticker(value))
                gelesene_quellen.add(key)
                print(f"INFO: Trade-Story-Setupquelle {key}: {rows_usable} gueltige Setup-Zeilen.")
        except Exception as exc:
            fehlende_quellen.add(key)
            print(f"WARNUNG: Trade-Story-Setupquelle konnte nicht autoritativ gelesen werden: {pfad}: {exc}")
    return result, gelesene_quellen, fehlende_quellen


def _trade_story_beobachtung_universum(beobachtungsliste_pfad):
    """Liest das aktuelle Beobachtungsuniversum fuer INTERESSANT/VORBEREITET."""
    result = set()
    if not beobachtungsliste_pfad or not os.path.isfile(beobachtungsliste_pfad):
        return result, False
    try:
        with open(beobachtungsliste_pfad, "r", encoding="utf-8-sig") as f:
            daten = json.load(f)
        if not isinstance(daten, dict):
            return result, False
        for ticker, eintrag in daten.items():
            if not isinstance(eintrag, dict):
                continue
            status = str(eintrag.get("status", "")).strip().upper()
            if status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C", "KEIN KANDIDAT"}:
                result.add(_normalisiere_ticker(ticker))
                for key in ("name", "firmenname"):
                    if eintrag.get(key):
                        result.add(_normalisiere_positionsname(eintrag[key]))
        return result, True
    except Exception as exc:
        print(f"WARNUNG: Trade-Story-Beobachtungsquelle konnte nicht gelesen werden: {beobachtungsliste_pfad}: {exc}")
        return result, False


def _trade_story_bloecke(text):
    """Extrahiert die einzelnen Eintraege aus Abschnitt 6.1."""
    m = re.search(r"(?ims)^6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN.*?(?=^6\.2\s+|\Z)", text or "")
    if not m:
        return []
    block = m.group(0)
    starts = list(re.finditer(r"(?m)^(?!6\.1\s+)(?!\s*$)([^\n]+)\n(?=Zeithorizont:)", block))
    return [block[a.start(): (starts[i+1].start() if i+1 < len(starts) else len(block))].strip() for i,a in enumerate(starts)]


def _trade_story_kandidaten_schluessel(candidate):
    """Erzeugt mehrere Vergleichsschluessel aus einer Kandidatenzeile.

    Gemini darf mehrere Titel in einer Story nennen. Deshalb wird nicht mehr
    die komplette Textzeile als ein einziger Firmenname verglichen. Ticker in
    Klammern sind primaer; zusaetzlich werden bekannte Namen als Teilstring
    gegen das jeweilige autoritative Universum geprueft.
    """
    text = str(candidate or "").strip()
    keys = set()
    for ticker in re.findall(r"\(([A-Za-z0-9._=-]{1,30})\)", text):
        keys.add(_normalisiere_ticker(ticker))
    clean = re.sub(r"\s*\([^)]*\)", " ", text)
    clean = re.sub(r"\[[^]]*\]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean:
        keys.add(_normalisiere_positionsname(clean))
    return {k for k in keys if k}


def _trade_story_keys_treffen(candidate, universe):
    keys = _trade_story_kandidaten_schluessel(candidate)
    if keys & universe:
        return True
    normalized_candidate = _normalisiere_positionsname(candidate)
    if not normalized_candidate:
        return False
    # Namen koennen in einer Story mit mehreren Titeln zusammenstehen. Ein
    # einzelner autoritativer Name innerhalb des Kandidaten reicht dann als
    # Verankerung; der Vergleich bleibt auf dem bereits gelieferten Universum.
    for key in universe:
        if len(key) >= 4 and (key in normalized_candidate or normalized_candidate in key):
            return True
    return False


def _trade_story_validierung(text, eingabedateien, beobachtungsliste_pfad=None):
    """Validiert Status, Kaufgrenze und autoritative Kandidatenherkunft."""
    stories = _trade_story_bloecke(text)
    if not stories:
        return False, ["Abschnitt 6.1 mit perspektivischen Trade-Story-Eintraegen fehlt oder ist nicht parsebar."]
    errors = []
    valid_keys, gelesene_quellen, fehlende_quellen = _trade_story_setup_universum(eingabedateien)
    beobachtungs_keys, beobachtung_verfuegbar = _trade_story_beobachtung_universum(beobachtungsliste_pfad)

    for idx, story in enumerate(stories, 1):
        status = re.search(r"(?im)^Status\s*:\s*(INTERESSANT|VORBEREITET|VALIDE SETUP)\s*$", story)
        if not status:
            errors.append(f"Trade-Story {idx}: Status fehlt; erlaubt sind INTERESSANT, VORBEREITET, VALIDE SETUP.")
            continue
        st = status.group(1)
        name_m = re.search(r"(?im)^(?:Bestehender Kandidat / Bezug|Kandidat|Name)\s*:\s*(.+)$", story)
        candidate = name_m.group(1).strip() if name_m else ""
        if re.search(r"(?i)\bkein(?:e|en)?\s+(?:bestehender\s+)?kandidat(?:en)?\b|\bkein bestehender kandidat vorhanden\b", candidate):
            candidate = ""
        if st == "VALIDE SETUP":
            if not gelesene_quellen:
                errors.append("Trade-Story %d: VALIDE SETUP nicht verifizierbar, weil keine autoritative Setup-Datei erfolgreich gelesen wurde." % idx)
            elif not _trade_story_keys_treffen(candidate, valid_keys):
                errors.append(f"Trade-Story {idx}: VALIDE SETUP fuer '{candidate or 'unbekannter Kandidat'}' nicht in gueltigen autoritativen Setup-Zeilen gefunden.")
        else:
            if re.search(r"(?i)\b(?:kaufen|direkt(?:er|en)?\s+einstieg|jetzt\s+einsteigen|entry|buy)\b", story):
                errors.append(f"Trade-Story {idx}: {st} darf keine Kauf-/Entry-Formulierung enthalten.")
            if st == "VORBEREITET" and not candidate:
                errors.append(f"Trade-Story {idx}: VORBEREITET benoetigt einen bestehenden Kandidaten.")
            # INTERESSANT ist bewusst eine strategische Ebene und darf auch
            # Titel/Assets ausserhalb der technischen Beobachtungsliste nennen.
            # Nur VORBEREITET braucht einen bestehenden Kandidaten.
            if st == "VORBEREITET" and candidate:
                if not beobachtung_verfuegbar:
                    errors.append(f"Trade-Story {idx}: VORBEREITET nicht verifizierbar, weil die aktuelle Beobachtungsliste fehlt oder unlesbar ist.")
                elif not _trade_story_keys_treffen(candidate, beobachtungs_keys):
                    errors.append(f"Trade-Story {idx}: VORBEREITET fuer '{candidate}' ist nicht in der aktuellen Beobachtungsliste verankert.")
                elif _trade_story_keys_treffen(candidate, valid_keys):
                    errors.append(f"Trade-Story {idx}: VORBEREITET fuer '{candidate}' verweist bereits auf ein autoritatives VALIDE SETUP; verwende Status: VALIDE SETUP.")

    if fehlende_quellen and not gelesene_quellen:
        errors.append("Trade-Story: Keine der autoritativen Setup-Quellen konnte erfolgreich gelesen werden: " + ", ".join(sorted(fehlende_quellen)))
    return not errors, errors


def _trade_story_deterministische_reparatur(text, eingabedateien, beobachtungsliste_pfad=None):
    """Repariert 6.1 ohne weiteren Gemini-API-Aufruf.

    Hintergrund: Eine kleine Trade-Story-Reparatur darf einen bereits
    erfolgreichen Hauptlauf nicht durch einen zusaetzlichen, grossen Gemini-
    Request gefaehrden. Insbesondere ein 429/RESOURCE_EXHAUSTED im Repair-Call
    darf nicht zum Verlust der kompletten Tagesauswertung fuehren.

    Die Reparatur ist bewusst konservativ: fehlende/ungueltige Statusangaben
    werden auf INTERESSANT zurueckgestuft; VORBEREITET wird nur beibehalten,
    wenn der Kandidat in der aktuellen Beobachtungsliste verankert ist; ein
    bereits gueltiges Setup wird VALIDE SETUP. Kauf-/Entry-Formulierungen auf
    nicht-validen Ebenen werden neutralisiert. Es werden keine technischen
    Werte, Kandidaten oder Setups erfunden.
    """
    stories = _trade_story_bloecke(text)
    if not stories:
        return (
            "6.1 PERSPEKTIVISCHE TRADE-IDEEN\n"
            "Makro-/Sektorbeobachtung\n"
            "Zeithorizont: offen\n"
            "Bestehender Kandidat / Bezug: kein bestehender Kandidat vorhanden\n"
            "Status: INTERESSANT\n"
            "Naechster technischer Trigger: aus der bestehenden Systemanalyse abwarten\n"
            "Risiko: These ist nicht als bestaetigtes Setup zu verstehen.\n"
        )

    valid_keys, gelesene_quellen, _ = _trade_story_setup_universum(eingabedateien)
    beobachtungs_keys, beobachtung_verfuegbar = _trade_story_beobachtung_universum(beobachtungsliste_pfad)

    repaired = []
    for story in stories:
        block = story.strip()
        status_m = re.search(r"(?im)^Status\s*:\s*(INTERESSANT|VORBEREITET|VALIDE SETUP)\s*$", block)
        status = status_m.group(1) if status_m else None
        name_m = re.search(r"(?im)^(?:Bestehender Kandidat / Bezug|Kandidat|Name)\s*:\s*(.+)$", block)
        candidate = name_m.group(1).strip() if name_m else ""
        if re.search(r"(?i)\bkein(?:e|en)?\s+(?:bestehender\s+)?kandidat(?:en)?\b|\bkein bestehender kandidat vorhanden\b", candidate):
            candidate = ""

        if status == "VALIDE SETUP":
            if not gelesene_quellen or not _trade_story_keys_treffen(candidate, valid_keys):
                status = "INTERESSANT"
        elif status == "VORBEREITET":
            if (not candidate or not beobachtung_verfuegbar or
                    not _trade_story_keys_treffen(candidate, beobachtungs_keys)):
                status = "INTERESSANT"
            elif _trade_story_keys_treffen(candidate, valid_keys):
                status = "VALIDE SETUP"
        elif status != "INTERESSANT":
            status = "INTERESSANT"

        if status_m:
            block = block[:status_m.start()] + f"Status: {status}" + block[status_m.end():]
        else:
            block = block.rstrip() + f"\nStatus: {status}\n"

        # Auf nicht-validen Ebenen keine Kauf-/Entry-Sprache stehen lassen.
        if status != "VALIDE SETUP":
            replacements = [
                (r"(?i)jetzt\s+einsteigen", "weiter beobachten"),
                (r"(?i)direkter\s+einstieg", "weitere technische Bestaetigung"),
                (r"(?i)direkten\s+einstieg", "weitere technische Bestaetigung"),
                (r"(?i)kauf(?:en|signal)?", "Beobachtung"),
                (r"(?i)entry", "Trigger"),
                (r"(?i)\bbuy\b", "Beobachtung"),
            ]
            for pattern, repl in replacements:
                block = re.sub(pattern, repl, block)

        repaired.append(block.strip())

    # Validator erwartet 6.1 als gemeinsamen Abschnitt.
    return "6.1 PERSPEKTIVISCHE TRADE-IDEEN\n" + "\n\n".join(repaired) + "\n"


def speichere_ergebnis(text):
    heute = datetime.date.today().isoformat()
    ausgabe_datei = f"Auswertung({heute}).txt"

    # Technischer Gemini-Fallback darf nicht durch die normale
    # Positions-/Punkt-7-Validierung laufen: Es gibt in diesem Fall bewusst
    # keine Gemini-Auswertung, die validiert werden koennte.
    if str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        final_text = str(text)
    else:
        final_text = normalisiere_ausgabe(
            text,
            zielzonen=_technische_zielzonen_quelle("Offene Positionen+Check.csv"),
        )

    with open(ausgabe_datei, "w", encoding="utf-8-sig") as f:
        f.write(final_text)
    print(f"\nGespeichert: {ausgabe_datei}")
    return ausgabe_datei


if __name__ == "__main__":
    print("Gemini-Auswertung gestartet...")
    ergebnis_text = gemini_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(ergebnis_text)
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
