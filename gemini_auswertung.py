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
from trade_story_universum import write_trade_story_universe
import time
import random
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

MODELL = "gemini-3.5-flash"  # Primaer-Modell (bereits im Projekt erfolgreich erprobt)
FALLBACK_MODELL = "gemini-3.8-flash"  # Erster Fallback
DRITTER_FALLBACK_MODELL = "gemini-3.7-flash"  # Zweiter Fallback
VIERTER_FALLBACK_MODELL = "gemini-3.6-flash"  # Dritter Fallback
FUENFTER_FALLBACK_MODELL = "gemini-3.5-flash-lite"  # Vierter Fallback

# Alle fuer diesen Lauf konfigurierten Modelle werden hoechstens einmal
# versucht. So wird ein einzelnes Free-Tier-Modell bei 503/Netzwerkproblemen
# nicht mehrfach in derselben Nachfragespitze verbrannt.
GEMINI_MODELLREIHENFOLGE = tuple(
    modell for modell in (
        MODELL,
        FALLBACK_MODELL,
        DRITTER_FALLBACK_MODELL,
        VIERTER_FALLBACK_MODELL,
        FUENFTER_FALLBACK_MODELL,
    )
    if modell
)
MAX_VERSUCHE = len(GEMINI_MODELLREIHENFOLGE)
WARTEZEIT_SEKUNDEN = 10  # Grundwartezeit fuer Sicherheitsfilter-Retries (steigt leicht an)

# Fuer SERVERSEITIGE UEBERLAST (HTTP 503) und Netzwerk-Abbrueche gilt eine
# exponentiell ansteigende Backoff-Staffel. Zusaetzlicher Jitter verhindert,
# dass mehrere parallele Laeufe exakt gleichzeitig erneut anfragen.
UEBERLAST_WARTEZEITEN = [15, 30, 60, 120]  # Sekunden; Backoff vor dem Modellwechsel

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
    "Trade_Story_Setup_Rohuniversum(...).csv": ["Trade_Story_Setup_Rohuniversum(*).csv"],
    "Trade_Story_Bitcoin(...).json": ["Trade_Story_Bitcoin(*).json"],
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
    "Einzel_Check_A_Meldungen(...).txt": ["Einzel_Check_A_Meldungen(*).txt"],
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
    # Struktur-Trends (C): eigenständige strukturelle Datenquelle. Optional,
    # weil A+B+D bei einem C-Ausfall weiterhin ausgewertet werden sollen.
    "Struktur_Trend_Briefing(...).txt": ["Struktur_Trend_Briefing(*).txt"],
    # Qualitative externe YouTube-Marktquellen; niemals technische/CRV-Werte ersetzen.
    "Bitcoin_Trading_DE_Briefing.txt": ["Bitcoin_Trading_DE_Briefing.txt"],
    "Gold_Trading_DE_Briefing.txt": ["Gold_Trading_DE_Briefing.txt"],
    "Silber_Trading_DE_Briefing.txt": ["Silber_Trading_DE_Briefing.txt"],
    # NEU: Live-Benchmark gegen MSCI World; wird als verbindlicher
    # Datenblock an Gemini uebergeben.
    "Benchmark_Live.txt": ["Benchmark_Live.txt"],
    "Trade_Story_Universum(...).json": ["Trade_Story_Universum(*).json"],
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
    # WICHTIG: Wenn ein vorheriger Schritt dieses Jobs die Liste bereits lokal
    # aktualisiert hat (z.B. einzel_check.py --beobachtungsliste), MUSS diese
    # lokale Version Vorrang haben. Sonst würde Gemini sie im selben Lauf aus
    # Drive zurück auf den alten Stand überschreiben. Nur auf einem frischen
    # Runner ohne lokale Datei wird der letzte persistierte Drive-Stand geladen.
    if os.path.isfile(BEOBACHTUNGSLISTE_DATEI):
        gefunden["Einzel-Check-Beobachtungsliste"] = BEOBACHTUNGSLISTE_DATEI
    elif gefunden.get("Einzel-Check-Beobachtungsliste") is None:
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

    # ZENTRALES TRADE-STORY-UNIVERSUM:
    # täglich neu aus den bereits erzeugten Scanner-Ausgaben aufbauen.
    # Es ist die deterministische Kandidaten-Handoff-Schicht zwischen
    # Python-Scannern und Gemini. Ein Fehler beim optionalen Aggregator
    # darf den Hauptlauf nicht blockieren; in diesem Fall bleibt die
    # bisherige Validator-Logik als Sicherheitsnetz aktiv.
    try:
        heute = datetime.date.today().isoformat()
        universe_path = f"Trade_Story_Universum({heute}).json"
        write_trade_story_universe(
            gefunden,
            universe_path,
            gefunden.get("Einzel-Check-Beobachtungsliste"),
        )
        gefunden["Trade_Story_Universum(...).json"] = universe_path
        print(f"TRADE-STORY-UNIVERSUM: {universe_path} erzeugt.")
    except Exception as exc:
        print(f"WARNUNG: Zentrales Trade-Story-Universum konnte nicht erzeugt werden: {exc}")

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
    modell_index = 0
    aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]

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
                    "VERBINDLICHE STRUKTUR-TREND-DATENREGEL (C): Wenn die Datei Struktur_Trend_Briefing(<Datum>).txt vorhanden ist, ist sie die maßgebliche Quelle für den strukturellen Datenblock C. C ist eine eigenständige Datenebene und darf nicht mit B (Makro) oder D (Geopolitik) vermischt werden. Die Gesamtbewertung entsteht erst durch die gemeinsame Einordnung von A+B+C+D. Struktur-Trend-Werte sind Strukturindikatoren und keine unmittelbaren Kauf-, Verkaufs-, Breakout- oder Zielzonensignale. Verwende für das Alter einer Beobachtung die Beobachtungsperiode, nicht das Cache- oder Abrufdatum. PA bedeutet Prozent pro Jahr (% p.a.); XDC_H bedeutet XDC je Arbeitsstunde. C darf aktuelle A-, B- oder D-Signale niemals überschreiben oder ersetzen. Wenn die Struktur-Trend-Datei fehlt, fahre mit A+B+D fort und erfinde keine C-Werte. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. Die Dateien Bitcoin_Trading_DE_Briefing.txt, Gold_Trading_DE_Briefing.txt und Silber_Trading_DE_Briefing.txt sind ausschließlich qualitative externe YouTube-Quellen. Nutze sie nur als Kontext/Abgleich; sie dürfen niemals objektive Kursdaten, technische Check-Felder, CRV, Setup-Scores, Filter, Setup-Qualität oder Handelsentscheidungen verändern. Wenn eine solche Datei fehlt, ist das kein Fehler und es darf nichts daraus erfunden werden. "
                    "ERSTELLE in der fertigen Auswertung zusätzlich eine feste Sektion mit exakt der Überschrift 'EXTERNE MARKTQUELLEN'. Gliedere sie getrennt nach 'Bitcoin', 'Gold' und 'Silber'. Für jeden Markt nenne die Anzahl der tatsächlich in der jeweiligen bereitgestellten Briefing-Datei enthaltenen relevanten Videos. WICHTIG: Zähle und verarbeite jedes vorhandene Video einzeln anhand jedes einzelnen 'Titel:'-Blocks bzw. Video-Blocks. Wenn die Briefing-Datei beispielsweise 3 relevante Videos enthält, müssen in der fertigen Auswertung genau diese 3 Videos einzeln erscheinen. Kein Video darf wegen Kürze, Ähnlichkeit, Redundanz oder eigener Auswahl des Modells weggelassen, zusammengefasst oder durch ein anderes ersetzt werden. Führe für JEDES vorhandene relevante Video separat Titel und eine kurze Kernaussage auf und ordne JEDE einzelne Aussage ausschließlich im Verhältnis zur bestehenden Systemanalyse als 'BESTÄTIGT', 'WIDERSPRICHT' oder 'NEUTRAL' ein. Die Anzahl muss mit der Zahl der tatsächlich einzeln aufgeführten Videos übereinstimmen. Ergänze bei jedem Markt ausdrücklich 'Technische Auswirkung: KEINE'. Wenn für einen Markt keine relevanten Videos in der bereitgestellten Briefing-Datei vorhanden sind oder die Datei fehlt, schreibe ausdrücklich 'Keine neuen relevanten Videos verarbeitet'. Verwende für Titel und Kernaussagen ausschließlich die Inhalte der bereitgestellten YouTube-Briefing-Dateien; ergänze nichts aus allgemeinem Modellwissen und erfinde nichts. Die Einordnung darf keine technische Berechnung oder Entscheidung verändern. Die externe Quelle ist ausschließlich qualitativer Kontext. Eine Übereinstimmung mit der externen Quelle ist keine technische Bestätigung; eine Abweichung ist kein technischer Ausschluss. Eine Aussage wie '1 Video' ist nur zulässig, wenn tatsächlich genau 1 relevanter Video-Block in der betreffenden Briefing-Datei vorhanden ist. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. "
                    "PRIORITAET FRUEHE ENTDECKUNG: Arbeite zwingend in drei getrennten Schritten: (1) zuerst ein kandidatenunabhaengiger Gesamtscan ueber den gesamten bereitgestellten Research A+B+C+D, insbesondere Makro, Geopolitik, Oel/Rohstoffe, Inflation, Zentralbanken, Zinsen, Liquiditaet, Waehrungen, Sektoren und Marktstruktur; (2) erst danach fuer jede relevante These Veraenderung -> Treiber -> Belege -> Kausalzusammenhang -> moeglicher Kapitalfluss -> naechster bestaetigter Kalenderkatalysator pruefen; (3) erst danach vorhandene Setups, Watchlists und offene Positionen gegen die These abgleichen. Ein grosses Discovery-Thema darf ausdruecklich ohne bestehenden Kandidaten ausgegeben werden. Oel/Rohstoffe sind dabei ausdruecklich als Bruecke zwischen Geopolitik, Inflation, Zentralbanken, Zinsen, Transport, Chemie, Industrie und Energieaktien zu pruefen. Ein vorhandener Kandidat darf die Discovery-These weder erzeugen noch in ein Setup umwandeln. Der bestehende Sektor-Rotations-Score darf als objektiver Beleg aus den bereitgestellten Daten verwendet werden; Gemini darf daraus keinen eigenen Discovery-Score erzeugen und darf ihn niemals als alleinigen Grund fuer eine These oder ein Setup verwenden. "
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
                    "MARKTUMFELD-AUSGABEREGEL: In allen Abschnitten mit Marktumfeld/Marktumfeld-Fazit sowie in der globalen Risikolage sind Scores, Score-Werte, Score-Modelle, Punktwerte und Formulierungen wie \"Score 0,0\" VERBOTEN. Beschreibe ausschließlich den qualitativen Zustand (z.B. bullish, neutral, bearish) und die zugrunde liegenden beobachtbaren Marktmerkmale. Setup-/CRV-Scores außerhalb des Marktumfeld-Blocks sind davon nicht betroffen. "
                    "NUMERISCHE MAKRO-BINDUNG: Alle numerischen Markt-/Makroangaben muessen exakt aus dem bereitgestellten Makro_Briefing uebernommen werden. Nicht neu rechnen, schaetzen, runden oder aus einer anderen Quelle ersetzen. Wenn ein Wert nicht eindeutig im Makro_Briefing vorhanden ist, nur qualitativ beschreiben oder weglassen. Instrument, Einheit und Datenstand muessen zusammengehoeren.\n                     FRUEHE-ENTDECKUNGS-UND-TRADE-STORY-EBENE: Die Discovery-Ebene und die technische Ebene sind zwingend getrennt auszugeben. Verwende in jedem 6.1-Block exakt zwei getrennte Statusfelder: Discovery-Status: ENTDECKT oder BEOBACHTUNG; Technischer Status: NICHT VORHANDEN, NUR TEILW. VOLLSTAENDIG oder VALIDER SETUP. Discovery-Status beschreibt nur den Erkenntnisstand der These. Technischer Status beschreibt ausschliesslich den Stand der bestehenden technischen Systempruefung. Wenn kein bestehender Kandidat im autoritativen Datenbestand vorhanden ist, muss Technischer Status = NICHT VORHANDEN sein. Wenn ein vorhandener Kandidat vorhanden ist, aber kein vollstaendig bestaetigtes Setup besitzt, muss Technischer Status = NUR TEILW. VOLLSTAENDIG sein. VALIDER SETUP darf ausschliesslich aus dem bestehenden regelbasierten Setup-/CRV-System uebernommen werden. Eine Discovery bleibt auch dann eine Discovery, wenn bereits ein VALIDE-SETUP-Kandidat existiert. Die Existenz eines Kandidaten darf niemals die Discovery erzeugen. Gemini darf aus Discovery, ENTDECKT, BEOBACHTUNG, NICHT VORHANDEN oder NUR TEILW. VOLLSTAENDIG niemals selbst einen VALIDEN SETUP, einen Kauf oder einen Entry machen. Zeige die Kette Thema -> Veraenderung -> Treiber -> Beleg -> Kausalzusammenhang -> moeglicher Kapitalfluss -> betroffene Assetklasse/Sektor -> bestehender Kandidat (falls vorhanden) -> naechster bestaetigter Kalenderkatalysator -> Discovery-Status -> Technischer Status -> widerlegender Trigger -> Risiko. Nutze nur bereitgestellte Daten. Der bestehende Sektor-Rotations-Score darf als objektiver Beleg genannt werden, ist aber kein Gemini-Score und niemals alleiniger Grund fuer eine Discovery oder ein Setup. "
                     "VERBINDLICHES TRADE-STORY-UNIVERSUM: Wenn 'Trade_Story_Universum(<Datum>).json' vorhanden ist, ist dieses taeglich neu erzeugte JSON die autoritative Kandidaten-Handoff-Schicht fuer 6.1. VALIDE SETUP darf nur aus candidates mit trade_story_status='VALIDE SETUP' stammen; VORBEREITET nur aus candidates mit trade_story_status='VORBEREITET'. C/KEIN KANDIDAT/Langfrist sind keine konkreten Kandidatenquellen. Eine offene Position ist nur Kontext und kein Ausschluss. Ein STATUSKONFLIKT (z.B. gleichzeitig Long und Short) darf nicht als eindeutiges Setup dargestellt werden. Das Universum darf durch Top-Sektor-Zugehoerigkeit nicht nachtraeglich verengt werden. "
                     "BITCOIN-REGEL IM TRADE-STORY-UNIVERSUM: Pi-Cycle-Bottom DOWN-Cross (150-EMA von oben nach unten durch 0.745*471SMA) ist LONG/AKKUMULATION und kann VALIDE SETUP sein. Pi-Cycle UP-Cross beendet die Akkumulationsphase und ist kein generisches SELL. 50W-SMA UP-Cross ist LONG/BUY; 50W-SMA DOWN-Cross ist EXIT/SELL und daher kein Long-Kandidat. Verwende ausschliesslich die strukturierten Bitcoin-Felder im Tagesuniversum. "
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
                        "VERBINDLICHE TRADE-STORY-ARCHITEKTUR: Behandle die fertige Auswertung als eine nachvollziehbare Kette von Daten zu Handlungsebene, nicht als neues Scoring. Python liefert die Puzzleteile (Rohdaten, objektive Berechnungen, technische Statusfelder, bestehende Kandidaten-/Beobachtungsstatus und regelbasierte Setups). Gemini verbindet diese Puzzleteile zu einer Trade-Story: Warum ist ein Thema oder Titel interessant, welche Daten bestaetigen die These, welcher Sektor bzw. welche Aktie ist betroffen, was muss als Naechstes passieren und welche Risiken koennen die These entkraeften? Die technischen Statusstufen sind strikt zu trennen: NICHT VORHANDEN = kein bestehender Kandidat; NUR TEILW. VOLLSTAENDIG = bestehender Kandidat vorhanden, aber das vollstaendige Setup-Regelwerk ist noch nicht bestaetigt; VALIDER SETUP = ausschliesslich ein bereits vom bestehenden Regelwerk bestaetigtes Setup. Gemini darf niemals aus einer Discovery oder aus dem Status NUR TEILW. VOLLSTAENDIG selbst einen VALIDEN SETUP oder einen Kauf machen. Die bestehende technische Setup-, Filter- und CRV-Logik bleibt allein autoritativ fuer die Stufe VALIDE SETUP. "
                        "Jede perspektivische Trade-Story in 6.1 soll deshalb, soweit aus den Dateien ableitbar, die Kette Thema -> Makro-Treiber -> bestaetigende Daten -> Sektor/Asset -> bestehender Kandidat -> Discovery-/Technischer Status -> naechster technischer Trigger -> Gegentreiber/Risiko sichtbar machen. Wenn kein bestehender Kandidat vorhanden ist, ist das explizit zu kennzeichnen. Ein Makro-Treiber allein ist niemals ein Einstiegssignal. "
                        "Die Anleihenmarkt-Auswertung ist eine eigene strukturierte Datenebene und darf nicht "
                        "als blosse Wiederholung der Treasury-Renditen behandelt werden. Geopolitik ist TIER-3-CONTEXT: "
                        "sie kann das Gate niemals sperren. Nutze 'MAKRO-EVENTS / WICHTIGE IMPULSE VORAUS' fuer "
                        "verifizierte kommende FOMC-, EZB-, CPI-, PPI- und weitere wichtige Makrotermine. Nutze "
                        "'BOERSENHAMMER / BIG NEWS 24H' als eine einzige, quellengebundene Top-Nachricht; die GDELT-Relevanzsortierung "
                        "ist kein Beweis dafuer, dass es objektiv die groesste Nachricht des Tages ist. Erfinde keine Termine, "
                        "Konsenswerte oder News. 'Wichtige Impulse voraus' darf nur auf verifizierten Kalenderdaten beruhen. "
                        "VERBINDLICHE GDELT-NEWS-REGEL: Das aktuelle Makro_Briefing kann im Abschnitt GEOPOLITIK konkrete "
                        "GDELT-DOC-Artikel mit Titel und URL enthalten. Wenn solche Artikel vorhanden sind, sind sie die einzige "
                        "zulaessige Quelle fuer konkrete GDELT-Newsinhalte. Lies diese Artikelzeilen aktiv als Nachrichtenkontext "
                        "und beziehe relevante, im Artikel-Titel erkennbare Entwicklungen qualitativ in Makro-/Marktumfeld-, "
                        "Risiko- und Trade-Story-Interpretationen ein, sofern sie fuer die jeweilige Aussage relevant sind. "
                        "Nenne bei einer konkreten GDELT-Newsreferenz den vorhandenen Titel und die vorhandene Quelle/URL, soweit "
                        "dies fuer die Nachvollziehbarkeit sinnvoll ist. Verwende niemals Modellwissen, um Inhalt, Ereignis, Quelle "
                        "oder Bedeutung eines GDELT-Artikels zu ergaenzen. Titel und URL sind Metadaten und kein Beweis fuer den "
                        "vollstaendigen Artikelinhalt. Wenn nur THEMEN_TREFFER_24H_SAMPLE bzw. GKG/Bulk-Samples vorhanden sind, "
                        "behandle diese ausschliesslich als aggregierten geopolitischen Kontext und erfinde daraus keine konkreten "
                        "Nachrichten oder Einzelereignisse. Wenn GDELT-DOC wegen HTTP 429 deaktiviert ist oder keine konkreten "
                        "Artikel vorliegen, darf Gemini keine konkrete GDELT-News behaupten. GDELT bleibt TIER-3-CONTEXT und darf "
                        "das MAKRO-SZENARIO-GATE niemals veraendern oder sperren."
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
            text, makro_kompakt_korrekturen = _sichere_makro_kritische_kompaktangaben(text, makro_text if makro_pfad else "")
            makro_zahlen_korrekturen.extend(makro_kompakt_korrekturen)
            text, bitcoin_marken_korrigiert = _korrigiere_bitcoin_identische_marke(text, makro_text if makro_pfad else "")
            if bitcoin_marken_korrigiert:
                print("  BITCOIN-MARKENKORREKTUR: aktueller Bitcoin-Kurs wurde nicht als identische Schwellenmarke dargestellt.")
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
            # Legacy-Signatur bleibt als Regressionserkennung dokumentiert:
            # _trade_story_validierung(text, eingabedateien, beobachtung_pfad)
            story_ok, story_errors = _trade_story_validierung(text, eingabedateien, beobachtung_pfad, True)
            if not story_ok:
                # Gemini darf die Story weiterhin frei formulieren, aber die
                # Statusbindung wird VOR der finalen Ausgabe deterministisch
                # korrigiert. Ein modellgenerierter VORBEREITET-Status wie AMD
                # bei aktuellem KAUFKANDIDAT C darf deshalb nicht als WARNUNG
                # bis zum Endprodukt durchgereicht werden.
                print(
                    "INFO: Trade-Story-Vorpruefung korrigiert "
                    f"{len(story_errors)} nicht autoritative Status-/Kandidatenangabe(n)."
                )
                # WICHTIG: Trade-Story-Reparatur ist deterministisch und
                # benoetigt keinen zweiten Gemini-Request. Ein zusaetzlicher
                # grosser Request kann das Minutenkontingent erschoepfen und
                # dadurch einen ansonsten erfolgreichen Tageslauf in einen
                # technischen Fallback zwingen.
                story_reparatur_text = _trade_story_deterministische_reparatur(
                    text, eingabedateien, beobachtung_pfad
                )
                story_ok2, story_errors2 = _trade_story_validierung(
                    story_reparatur_text, eingabedateien, beobachtung_pfad, True
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

            # FINALER MAKRO-ZAHLEN-GATE: Eine eventuelle Trade-Story-Reparatur
            # kann den Text nach der ersten Zahlenabsicherung erneut erzeugen.
            # Deshalb werden die semantisch kritischen Angaben unmittelbar vor
            # der deterministischen Punkt-7-Erzeugung nochmals gegen das
            # aktuelle Makro-Briefing gebunden. So gilt auch nach jeder
            # Reparatur: Label + Instrument + Einheit + Zeitraum bleiben gekoppelt.
            if makro_pfad:
                text, final_makro_korrekturen = _sichere_makro_kritische_kompaktangaben(
                    text, makro_text
                )
                if final_makro_korrekturen:
                    print(
                        f"  FINAL-MAKRO-ZAHLEN-GATE: {len(final_makro_korrekturen)} "
                        "semantisch gebundene Angaben korrigiert."
                    )

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
                # Das RPD-Free-Tier-Limit ist modellbezogen. Bei PerDay wird
                # deshalb der naechste noch nicht versuchte Eintrag der festen
                # Modellreihenfolge verwendet. Ist die Reihe ausgeschoepft,
                # wird nicht versucht, ein bereits erschoepftes Modell erneut
                # zu verwenden.
                if modell_index + 1 < len(GEMINI_MODELLREIHENFOLGE):
                    vorheriges_modell = aktuelles_modell
                    modell_index += 1
                    aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]
                    print(
                        f"  Tages-Kontingent von {vorheriges_modell} erschoepft "
                        "(429 RESOURCE_EXHAUSTED, PerDay). "
                        f"Wechsle fuer diesen Lauf auf Fallback-Modell {aktuelles_modell}."
                    )
                    continue

                print(
                    f"  Tages-Kontingent des Gemini-Free-Tiers fuer {aktuelles_modell} ist erschoepft "
                    "(429 RESOURCE_EXHAUSTED, quotaId enthaelt 'PerDay'). "
                    f"Alle {len(GEMINI_MODELLREIHENFOLGE)} konfigurierten Modelle wurden fuer diesen Lauf ausgeschöpft; "
                    "breche ab statt ein bereits erschoepftes Modell erneut zu verwenden. "
                    "Naechster sinnvoller Versuch nach dem taeglichen Reset oder mit erweitertem Tier."
                )
                break

            if kategorie in ("ueberlast", "netzwerk"):
                # Bei serverseitiger Ueberlast (503) oder Netzwerk-Abbruch
                # wird jedes konfigurierte Modell hoechstens EINMAL versucht.
                # Vor dem Wechsel wartet der Lauf mit exponentiellem Backoff
                # und Jitter. Ein vom Server geliefertes retryDelay gewinnt,
                # wenn es laenger als die lokale Backoff-Stufe ist.
                if modell_index + 1 < len(GEMINI_MODELLREIHENFOLGE):
                    grund = "503-Overload" if kategorie == "ueberlast" else "Netzwerk-Abbruch"
                    backoff_index = min(modell_index, len(UEBERLAST_WARTEZEITEN) - 1)
                    basis_wartezeit = UEBERLAST_WARTEZEITEN[backoff_index]
                    server_wartezeit = (empfohlene_wartezeit
                                        if empfohlene_wartezeit is not None else 0)
                    wartezeit = max(float(basis_wartezeit), float(server_wartezeit))
                    jitter = random.uniform(0.0, wartezeit * 0.20)
                    wartezeit += jitter
                    naechstes_modell = GEMINI_MODELLREIHENFOLGE[modell_index + 1]
                    print(
                        f"  {grund} nach Versuch {versuch}/{MAX_VERSUCHE}. "
                        f"Warte {wartezeit:.1f}s (Backoff {basis_wartezeit}s + Jitter) und "
                        f"wechsle danach von {aktuelles_modell} auf {naechstes_modell}."
                    )
                    time.sleep(wartezeit)
                    modell_index += 1
                    aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]
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


def _entferne_marktumfeld_scores(text):
    """Entfernt verbotene Score-/Punktwertdarstellungen ausschließlich im Marktumfeld.

    Technische Setup-/CRV-Scores an anderer Stelle bleiben unverändert.
    """
    if not text:
        return text
    lines = text.splitlines()
    out = []
    in_market = False
    for line in lines:
        stripped = line.strip()
        # Market-environment headings used by the generated Auswertung.
        if re.search(r"(?i)\bMARKTUMFELD\b", stripped) or re.search(r"(?i)KOMPAKTE STICHPOINT-LISTE ZUM MARKTUMFELD", stripped):
            in_market = True
        # Numeric top-level section headings terminate the market block.
        if in_market and re.match(r"^\s*\d+(?:\.\d+)?\.?\s+\S+", stripped) and not re.search(r"(?i)\bMARKTUMFELD\b", stripped):
            in_market = False

        if in_market:
            # "... nach dem Score-Modell auf Stufe Bärisch (Score 0,00)"
            line = re.sub(
                r"(?i)\bnach dem\s+Score-Modell\s+auf\s+Stufe\s+([^(\n]+?)\s*\(\s*Score\s*[-+]?\d+(?:[.,]\d+)?\s*\)",
                r"auf Stufe \1",
                line,
            )
            # "... (Score 0,0)" / "Score 0.0"
            line = re.sub(r"(?i)\s*\(\s*Score\s*[-+]?\d+(?:[.,]\d+)?\s*\)", "", line)
            # Residual standalone score wording in the market block.
            line = re.sub(r"(?i)\bScore[- ]Modell\b", "Marktumfeld-Modell", line)
            line = re.sub(r"(?i)\bScore-Wert\b", "Bewertung", line)
            line = re.sub(r"(?i)\bScore\s*[:=]\s*[-+]?\d+(?:[.,]\d+)?", "", line)
        out.append(line)
    return "\n".join(out)

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
    text = _entferne_marktumfeld_scores(text)

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
        dm = re.search(r"(?:Datenstand|Datenstand:?)\s*=\s*(\d{4}-\d{2}-\d{2})", rest, re.I)
        if dm:
            ref["datenstand"] = dm.group(1)
        sm = re.search(r"(?:Letzter_Schluss|Letzter\s+Schluss)\s*=\s*(?!\d{4}-\d{2}-\d{2})([-+]?\d+(?:[.,]\d+)?)", rest, re.I)
        if sm:
            ref["schluss"] = float(sm.group(1).replace(",", "."))
        for key in ("5T", "1M", "3M", "6M", "1J"):
            m = re.search(rf"(?:^|\|)\s*{re.escape(key)}\s*=\s*([-+]?\d+(?:[.,]\d+)?)\s*%", rest)
            if m:
                ref["perioden"][key] = float(m.group(1).replace(",", "."))
        referenzen[label.lower()] = ref
        normalized_label = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
        treasury_aliases = {
            "2y": ("us 2y treasury", "2y us treasury", "us 2 year treasury", "2 year us treasury", "2j us treasury", "us 2j treasury", "2j", "2y"),
            "5y": ("us 5y treasury", "5y us treasury", "us 5 year treasury", "5 year us treasury", "5j us treasury", "us 5j treasury", "5j", "5y"),
            "10y": ("us 10y treasury", "10y us treasury", "us 10 year treasury", "10 year us treasury", "10j us treasury", "us 10j treasury", "10j treasury", "10j", "10y"),
            "30y": ("us 30y treasury", "30y us treasury", "us 30 year treasury", "30 year us treasury", "30j us treasury", "us 30j treasury", "30j treasury", "30j", "30y"),
            "real10y": ("realzins 10y tips", "10y tips real yield", "10y real yield", "real 10y tips", "10j tips realzins", "realzins 10j tips", "10j realzins", "10y realzins"),
        }
        for aliases in treasury_aliases.values():
            if normalized_label in aliases:
                for alias in aliases:
                    referenzen[alias] = ref
                break
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

            # Treasury-/TIPS-Renditen werden von Gemini typischerweise als
            # Prozentwerte geschrieben (z.B. "10J bei 4,97%"). Sie sind
            # trotzdem Kurs-/Referenzwerte der jeweiligen Metrik und muessen
            # gegen den autoritativen Briefing-Wert geprueft werden.
            normalized_label = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
            treasury_metric = bool(re.fullmatch(r"(?:2|5|10|30)[jy]", normalized_label)) or any(
                token in normalized_label for token in ("treasury", "tips", "realzins", "real yield")
            )
            if treasury_metric:
                yield_matches = list(pct_re.finditer(segment))
                if yield_matches:
                    ym = yield_matches[0]
                    try:
                        old = float(ym.group("num").replace(",", "."))
                    except ValueError:
                        old = None
                    if old is not None and abs(old - ref["kurs"]) >= 0.005:
                        replacements.append((
                            ym.start(), ym.end(),
                            f"{ref['kurs']:.2f}".replace(".", ",") + "%",
                        ))
                        changes.append(f"{label}: Kurs {old} -> {ref['kurs']}")

            price_matches = [] if treasury_metric else list(price_re.finditer(segment))
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


def _sichere_makro_kritische_kompaktangaben(text, makro_text):
    """Bindet kompakte Makroangaben strikt an Label, Instrument und Zeitraum.

    Wichtig: Eine Zeile kann mehrere Metriken enthalten (z.B. Gold, Silber,
    Platin und Palladium). Deshalb wird jede Metrik zuerst auf ihren eigenen
    Textabschnitt bis zur naechsten Metrik begrenzt. So kann ein Wert nie aus
    Versehen auf die benachbarte Metrik uebertragen werden.
    """
    if not text or not makro_text:
        return text, []
    refs = _extrahiere_makro_referenzwerte(makro_text)
    changes = []

    def ref_for(*aliases):
        for alias in aliases:
            ref = refs.get(alias.lower())
            if ref:
                return ref
        normalized_refs = {re.sub(r"[^a-z0-9]+", "", key.casefold()): ref for key, ref in refs.items()}
        for alias in aliases:
            ref = normalized_refs.get(re.sub(r"[^a-z0-9]+", "", alias.casefold()))
            if ref:
                return ref
        return None

    treasury = {
        "2j": ref_for("2j", "2y", "us 2y treasury"),
        "5j": ref_for("5j", "5y", "us 5y treasury"),
        "10j": ref_for("10j", "10y", "us 10y treasury"),
        "30j": ref_for("30j", "30y", "us 30y treasury"),
    }
    tips = ref_for("realzins 10y tips", "realzins 10j tips", "10y tips real yield")

    spread = None
    m = re.search(r"(?im)^2Y-10Y Spread:\s*([-+]?\d+(?:[.,]\d+)?)", makro_text)
    if m:
        spread = float(m.group(1).replace(",", "."))

    pct = r"[-+]?\d{1,3}(?:[.,]\d{1,6})?"
    num = r"[-+]?\d[\d.,]*"
    metals = {name: ref_for(name) for name in ("gold", "silber", "platin", "palladium")}
    indices = {
        r"DAX": ref_for("dax"),
        r"EuroStoxx\s*50": ref_for("eurostoxx 50"),
        r"Nikkei\s*225": ref_for("nikkei 225"),
        r"VIX(?:\s*\([^)]*\))?": ref_for("vix"),
    }
    lme_copper = ref_for("lme kupfer")

    def _parse_number(raw):
        value = str(raw or "").strip().replace(" ", "")
        if not value:
            raise ValueError("empty number")
        sign = ""
        if value[0] in "+-":
            sign, value = value[0], value[1:]
        if "," in value and "." in value:
            # German form: 25.402,28
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            value = value.replace(",", ".")
        elif value.count(".") > 1:
            value = value.replace(".", "")
        return float(sign + value)

    def _fmt(value):
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    out = []
    for line in text.splitlines():
        # --- Treasury / TIPS: each label owns only the text until the next
        # bond metric label. TIPS is handled before generic 10Y matching so
        # "10Y TIPS" can never be mistaken for the nominal 10Y Treasury.
        bond_labels = [
            (r"10Y\s*TIPS\s*Realzins", tips, "10Y TIPS"),
            (r"Realzins\s*(?:\(\s*)?10Y\s*TIPS(?:\s*\))?", tips, "10Y TIPS"),
            (r"TIPS[- ]Realrendite\s*10Y", tips, "10Y TIPS"),
            (r"US\s*2Y\s*Treasury", treasury["2j"], "2Y"),
            (r"US\s*5Y\s*Treasury", treasury["5j"], "5Y"),
            (r"US\s*10Y\s*Treasury", treasury["10j"], "10Y"),
            (r"US\s*30Y\s*Treasury", treasury["30j"], "30Y"),
            (r"\b2(?:J|Y)\b", treasury["2j"], "2Y"),
            (r"\b5(?:J|Y)\b", treasury["5j"], "5Y"),
            (r"\b10(?:J|Y)\b", treasury["10j"], "10Y"),
            (r"\b30(?:J|Y)\b", treasury["30j"], "30Y"),
        ]
        matches = []
        for pattern, ref, label in bond_labels:
            if not ref:
                continue
            for match in re.finditer(pattern, line, re.I):
                # Generic 10Y/2Y aliases must not hit inside an explicit TIPS label.
                if label == "10Y" and re.match(r"\s*TIPS", line[match.end():], re.I):
                    continue
                matches.append((match.start(), match.end(), ref, label))
        # Keep one match per position and prefer the longest explicit label.
        unique = {}
        for item in matches:
            key = (item[0], item[1])
            if key not in unique or (item[1] - item[0]) > (unique[key][1] - unique[key][0]):
                unique[key] = item
        matches = sorted(unique.values(), key=lambda x: x[0])

        # Compact form often places the tenor after the value: "4,43% (2J)".
        # Bind that percentage directly to the parenthesized tenor instead of
        # treating the text after the tenor as its value.
        compact_done = []
        for pattern, ref, label in bond_labels:
            if not ref:
                continue
            compact_pattern = re.compile(rf"(?P<num>{pct})\s*%\s*\(\s*{pattern}\s*\)", re.I)
            for cm in compact_pattern.finditer(line):
                try:
                    old = _parse_number(cm.group("num"))
                    target = ref["kurs"]
                except (ValueError, TypeError):
                    continue
                if abs(old - target) < 0.005:
                    continue
                replacement = _fmt(target)
                line = line[:cm.start("num")] + replacement + line[cm.end("num"):]
                changes.append(f"{label}: Kurs {old} -> {target}")
                compact_done.append((cm.start(), cm.end()))

        if compact_done:
            matches = []
        for idx, (start_pos, end_pos, ref, label) in enumerate(matches):
            segment_end = matches[idx + 1][0] if idx + 1 < len(matches) else len(line)
            segment = line[end_pos:segment_end]
            # Treasury/TIPS value is the percentage immediately following the
            # label, not a later period performance percentage.
            pm = re.search(rf"(?P<num>{pct})\s*%", segment)
            if pm:
                try:
                    old = _parse_number(pm.group("num"))
                    target = ref["kurs"]
                except (ValueError, TypeError):
                    old = target = None
                if old is not None and target is not None and abs(old - target) >= 0.005:
                    replacement = _fmt(target) + "%"
                    line = line[:end_pos + pm.start()] + replacement + line[end_pos + pm.end():]
                    shift = len(replacement) - (pm.end() - pm.start())
                    if shift:
                        # Recompute later labels from the modified line; only one
                        # correction is needed per bond segment.
                        pass
                    changes.append(f"{label}: Kurs {old} -> {target}")

        # Explicit TIPS pass: TIPS is semantically distinct from nominal 10Y.
        # Run this after generic tenor handling so a phrase such as
        # "Realzins 10Y TIPS ... 4,83%" can never inherit the nominal 10Y value.
        if tips:
            tips_pattern = re.compile(
                rf"(?P<label>(?:Realzins\s*(?:\(\s*)?10Y\s*TIPS(?:\s*\))?|10Y\s*TIPS\s*Realzins|TIPS[- ]Realrendite\s*10Y))"
                rf"(?P<middle>[^\n%]{{0,45}}?)(?P<num>{pct})\s*%",
                re.I,
            )
            def _tips_replace(tm):
                try:
                    old = _parse_number(tm.group("num"))
                except ValueError:
                    return tm.group(0)
                target = tips["kurs"]
                if abs(old - target) < 0.005:
                    return tm.group(0)
                changes.append(f"10Y TIPS: Kurs {old} -> {target}")
                return tm.group("label") + tm.group("middle") + _fmt(target) + "%"
            line = tips_pattern.sub(_tips_replace, line)

        # --- Explicit compact Treasury group: bind every parenthesized tenor
        # to its own authoritative reference. This treats the generated group
        # as one semantic structure instead of relying on label proximity.
        compact_tenor_refs = {
            "2j": treasury.get("2j"), "5j": treasury.get("5j"),
            "10j": treasury.get("10j"), "30j": treasury.get("30j"),
        }
        compact_tenor_pattern = re.compile(
            rf"(?P<num>{pct})\s*%\s*\(\s*(?P<tenor>2|5|10|30)(?:J|Y)\s*\)", re.I
        )
        replacements = []
        for tm in compact_tenor_pattern.finditer(line):
            ref = compact_tenor_refs.get(tm.group("tenor").lower() + "j")
            if not ref:
                continue
            try:
                old = _parse_number(tm.group("num")); target = ref["kurs"]
            except (ValueError, TypeError):
                continue
            if abs(old - target) >= 0.005:
                replacements.append((tm.start("num"), tm.end("num"), _fmt(target)))
                changes.append(f"{tm.group('tenor')}Y: Kurs {old} -> {target}")
        for rs, re_, replacement in sorted(replacements, reverse=True):
            line = line[:rs] + replacement + line[re_:]

        # --- 2Y-10Y spread: deterministic value from the calculated field.
        if spread is not None:
            # Explicit compact 2Y/10Y label.
            if re.search(r"2Y[- ]10Y[- ]Spread", line, re.I):
                line = re.sub(
                    rf"(2Y[- ]10Y[- ]Spread\s*(?:bei|von|ist|=|:)\s*){pct}\s*(?:Prozentpunkte?|%)?",
                    lambda mm: mm.group(1) + _fmt(spread) + " Prozentpunkte",
                    line,
                    flags=re.I,
                )
            # Narrative form: "Zinskurve (2J: ... | 10J: ...) ... Spread von X".
            elif re.search(r"(?i)Zinskurve.*\b2J\s*:", line) and re.search(r"(?i)\b10J\s*:", line):
                line = re.sub(
                    rf"(\bSpread\s*(?:bei|von|ist|=|:)\s*){pct}\s*(?:Prozentpunkte?|%)?",
                    lambda mm: mm.group(1) + _fmt(spread) + " Prozentpunkte",
                    line,
                    flags=re.I,
                )

        # --- Precious metals: isolate each metal segment before replacing
        # period values. This is the critical protection against cross-metal
        # propagation on a single line.
        metal_matches = []
        for name, ref in metals.items():
            if not ref:
                continue
            for mm in re.finditer(rf"\b{re.escape(name)}\b", line, re.I):
                metal_matches.append((mm.start(), mm.end(), name, ref))
        metal_matches.sort(key=lambda x: x[0])
        for idx, (start_pos, end_pos, name, ref) in enumerate(metal_matches):
            segment_end = metal_matches[idx + 1][0] if idx + 1 < len(metal_matches) else len(line)
            segment = line[end_pos:segment_end]
            period_aliases = {
                "5T": r"(?:5\s+Tagen?|5\s+Handelstagen?|5T)",
                "1M": r"(?:4\s+Wochen?|4-Wochen|1\s+Monat|1M)",
            }
            # Work from right to left so replacements do not invalidate matches.
            local_replacements = []
            for key, aliases in period_aliases.items():
                target = ref["perioden"].get(key)
                if target is None:
                    continue
                pm = re.search(rf"(?P<num>{pct})\s*%\s*(?P<period>{aliases})", segment, re.I)
                if not pm:
                    continue
                try:
                    old = _parse_number(pm.group("num"))
                except ValueError:
                    continue
                if abs(old - target) < 0.005:
                    continue
                local_replacements.append((pm.start("num"), pm.end("num"), f"{target:+.2f}".replace(".", ",")))
                changes.append(f"{name}: {key} {old} -> {target}")
            for rs, re_, replacement in sorted(local_replacements, reverse=True):
                segment = segment[:rs] + replacement + segment[re_:]
            line = line[:end_pos] + segment + line[segment_end:]

        # --- Market indices: points are not percentages/currency. Bind the
        # parenthesized closing value to the exact index label.
        for label_pattern, ref in indices.items():
            if not ref:
                continue
            index_name = re.sub(r"\\s+", " ", label_pattern.replace("\\s*", "")).strip()
            lm = re.search(label_pattern, line, re.I)
            if not lm:
                continue
            tail = line[lm.end():lm.end() + 120]
            # The closing value is the parenthesized number immediately after
            # the daily percentage (e.g. "-0,15% (25.402,28)"). Do not grab
            # EMA values or other parenthesized numbers later in the line.
            pm = re.search(rf"[-+]?\d+(?:[.,]\d+)?%\s*\(\s*(?P<num>{num})\s*(?:Punkte?)?\s*\)", tail, re.I)
            if not pm:
                continue
            try:
                old = _parse_number(pm.group("num"))
                target = ref["kurs"]
            except (ValueError, TypeError):
                continue
            if abs(old - target) < 0.005:
                continue
            replacement = _fmt(target)
            abs_start = lm.end() + pm.start("num")
            abs_end = lm.end() + pm.end("num")
            line = line[:abs_start] + replacement + line[abs_end:]
            changes.append(f"{index_name}: Kurs {old} -> {target}")

        # --- Market-data semantics: bind Nikkei's close/date and VIX's
        # current-vs-last-close semantics to the same authoritative macro row.
        nikkei_ref = indices.get(r"Nikkei\s*225")
        if nikkei_ref and re.search(r"(?i)\bNikkei\s*225\b", line):
            if nikkei_ref.get("datenstand"):
                try:
                    nikkei_date = datetime.date.fromisoformat(nikkei_ref["datenstand"])
                    display_date = nikkei_date.strftime("%d.%m.%Y")
                except ValueError:
                    display_date = nikkei_ref["datenstand"]
                line = re.sub(
                    r"(?i)(Datenstand\s*(?::|=)?\s*)(?:\d{1,2}[.]\d{1,2}[.]\d{4}|\d{4}-\d{2}-\d{2})",
                    lambda mm: mm.group(1) + display_date,
                    line,
                )
        # Explicitly disambiguate the common compact VIX pair:
        # current daily value / last completed close.
        line = re.sub(
            r"(?i)\bVIX\s*\([^)]*\)\s+notiert\s+erhöht\s+bei\s*"
            r"([-+]?\d+(?:[.,]\d+)?)\s*/\s*([-+]?\d+(?:[.,]\d+)?)\s*Punkten",
            r"VIX (aktueller Tageswert) \1; letzter Schlusskurs \2 Punkte",
            line,
        )
        # Explicitly label VIX semantics from the printed data date.
        if re.search(r"(?i)\bVIX\s*\([^)]*\)", line):
            dm = re.search(r"(?i)Datenstand\s*[:=]\s*(\d{4}-\d{2}-\d{2})", line)
            if dm:
                try:
                    vix_date = datetime.date.fromisoformat(dm.group(1))
                    today = datetime.date.today()
                    label = "VIX (letzter Schlusskurs)" if vix_date < today else "VIX (aktueller Tageswert)"
                    line = re.sub(r"(?i)\bVIX\s*\([^)]*\)", label, line, count=1)
                except ValueError:
                    pass

        # --- LME copper cash: keep the LME cash settlement distinct from the
        # HG=F copper future. Unit binding (/t) is part of the semantic key.
        if lme_copper:
            lm = re.search(r"LME\s+Kupfer", line, re.I)
            if lm:
                tail = line[lm.end():lm.end() + 80]
                cm = re.search(rf"(?:bei|von|ist|=|:)\s*(?P<num>{num})\s*(?P<unit>\$\s*/\s*t|USD\s*/\s*t|\$/t)", tail, re.I)
                if cm:
                    try:
                        old = _parse_number(cm.group("num"))
                        target = lme_copper["kurs"]
                    except (ValueError, TypeError):
                        old = target = None
                    if old is not None and target is not None and abs(old - target) >= 0.005:
                        replacement = _fmt(target)
                        abs_start = lm.end() + cm.start("num")
                        abs_end = lm.end() + cm.end("num")
                        line = line[:abs_start] + replacement + line[abs_end:]
                        changes.append(f"LME Kupfer Cash: Kurs {old} -> {target}")

        out.append(line)
    return "\n".join(out), changes

def _korrigiere_bitcoin_identische_marke(text, makro_text):
    """Verhindert eine irrefuehrende Bitcoin-Formulierung mit dem aktuellen Kurs als Marke.

    Wenn Gemini den aktuellen Bitcoin-Kurs selbst als „ueber X-Marke“ beschreibt,
    wird nur diese redundante Formulierung deterministisch auf „bei X USD“
    korrigiert. Unabhaengige Referenzwerte wie 50W-SMA/EMA20 bleiben unberuehrt.
    Kein zusaetzlicher Gemini-API-Call.
    """
    if not text or not makro_text:
        return text, False
    m = re.search(r"(?im)^Bitcoin:\s*([-+]?\d[\d.,\s]*)", makro_text)
    if not m:
        return text, False
    raw_value = m.group(1).strip()
    try:
        compact = re.sub(r"\s+", "", raw_value)
        if "," in compact and "." in compact:
            # Last separator is the decimal separator; the other one is thousands.
            if compact.rfind(",") > compact.rfind("."):
                normalized = compact.replace(".", "").replace(",", ".")
            else:
                normalized = compact.replace(",", "")
        elif compact.count(",") == 1:
            left, right = compact.split(",")
            normalized = f"{left}.{right}" if len(right) <= 2 else compact.replace(",", "")
        elif compact.count(".") == 1:
            left, right = compact.split(".")
            normalized = compact if len(right) <= 2 else compact.replace(".", "")
        else:
            normalized = compact.replace(",", "").replace(".", "")
        value = float(normalized)
    except ValueError:
        return text, False
    if value <= 0:
        return text, False
    value_de = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    value_us = f"{value:,.2f}"
    value_plain = f"{value:.2f}"
    escaped_variants = [re.escape(v) for v in {value_de, value_us, value_plain, raw_value}]
    number_pattern = "(?:" + "|".join(sorted(set(escaped_variants), key=len, reverse=True)) + ")"
    patterns = [
        rf"(?i)(?:ueber|über|oberhalb)\s+(?:der\s+|die\s+)?{number_pattern}\s*\$?\s*-?\s*Marke",
        rf"(?i)(?:ueber|über|oberhalb)\s+(?:der\s+|die\s+)?{number_pattern}\s*USD\s*-?\s*Marke",
    ]
    new_text = text
    for pattern in patterns:
        new_text = re.sub(pattern, f"bei {value_de} USD", new_text)
    return new_text, new_text != text


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
    """Liest das zentrale, taeglich neu erzeugte Trade-Story-Universum.

    Fallback: Wenn das zentrale JSON fehlt (z.B. Altbestand/Test), wird die
    bisherige direkte CSV-Validierung verwendet. So bleibt die Validierung
    rueckwaertskompatibel, ohne die neue Architektur zu umgehen.
    """
    central = eingabedateien.get("Trade_Story_Universum(...).json")
    if central and os.path.isfile(central):
        try:
            with open(central, "r", encoding="utf-8") as f:
                data = json.load(f)
            result = set()
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
            for item in candidates:
                if not isinstance(item, dict) or item.get("trade_story_status") != "VALIDE SETUP":
                    continue
                for value in (item.get("ticker"), item.get("name")):
                    if value:
                        result.add(_normalisiere_ticker(value))
                        result.add(_normalisiere_positionsname(value))
            return result, {"Trade_Story_Universum(...).json"}, set()
        except Exception as exc:
            print(f"WARNUNG: Zentrales Trade-Story-Universum konnte nicht gelesen werden: {central}: {exc}")

    result = set()
    gelesene_quellen = set()
    fehlende_quellen = set()
    specs = (
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
                        if str(row.get(lower_fields["status2"]) or "").strip().upper() != required_status:
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
    """Liest das aktuelle Beobachtungsuniversum fuer vorbereitete A/B-Kandidaten.
    C und KEIN KANDIDAT sind keine Trade-Story-Kandidaten.
    """
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
            if status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B"}:
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


def _trade_story_kandidaten_teile(candidate):
    """Zerlegt eine Kandidatenangabe nur an eindeutigen Story-Trennern.

    Besonders wichtig sind mehrere Titel mit Tickerangaben wie
    ``A (AAA) | B (BBB)``. Jeder Teil wird spaeter separat gegen das
    autoritative Universum geprueft. Ohne eindeutigen Trenner bleibt die
    komplette Angabe bewusst ein Kandidat, damit Firmennamen nicht
    versehentlich an Kommas/Bindestrichen zerlegt werden.
    """
    text = str(candidate or "").strip()
    if not text:
        return []
    if len(re.findall(r"\(([A-Za-z0-9._=-]{1,30})\)", text)) >= 2:
        parts = [p.strip() for p in re.split(r"\s*(?:\||;|\n)\s*", text) if p.strip()]
        if len(parts) >= 2:
            return parts
    parts = [p.strip() for p in re.split(r"\s*(?:\||;)\s*", text) if p.strip()]
    return parts or [text]


def _trade_story_keys_treffen(candidate, universe):
    keys = _trade_story_kandidaten_schluessel(candidate)
    if keys & universe:
        return True
    normalized_candidate = _normalisiere_positionsname(candidate)
    if not normalized_candidate:
        return False
    for key in universe:
        if len(key) >= 4 and (key in normalized_candidate or normalized_candidate in key):
            return True
    return False


def _trade_story_alle_kandidaten_treffen(candidate, universe):
    """Prueft jeden explizit getrennten Titel einer Story einzeln."""
    teile = _trade_story_kandidaten_teile(candidate)
    if not teile:
        return False
    return all(_trade_story_keys_treffen(teil, universe) for teil in teile)


def _trade_story_zentrales_universum(eingabedateien):
    """Liest valid/prepared Kandidaten aus dem zentralen Tages-Snapshot."""
    path = eingabedateien.get("Trade_Story_Universum(...).json")
    if not path or not os.path.isfile(path):
        return set(), set(), False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid, prepared = set(), set()
        for item in data.get("candidates", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            item_status = item.get("trade_story_status")
            # A directional conflict is neither valid nor prepared. It must
            # never be silently downgraded to VORBEREITET.
            if item_status == "STATUSKONFLIKT":
                continue
            if item_status not in {"VALIDE SETUP", "VORBEREITET"}:
                continue
            target = valid if item_status == "VALIDE SETUP" else prepared
            for value in (item.get("ticker"), item.get("name")):
                if value:
                    text = str(value).strip()
                    target.add(_normalisiere_ticker(text))
                    target.add(_normalisiere_positionsname(text))
        return valid, prepared, True
    except Exception as exc:
        print(f"WARNUNG: Trade-Story-Universum fuer Validator unlesbar: {path}: {exc}")
        return set(), set(), False


def _trade_story_validierung(text, eingabedateien, beobachtungsliste_pfad=None, strikt_statusfelder=False):
    """Validiert Discovery-/Technikstatus, Kaufgrenze und autoritative Kandidatenherkunft.

    Im Produktionslauf sind die beiden Statusfelder zwingend getrennt. Der
    Legacy-Modus bleibt fuer bestehende Regressionstests und Altbestandsdaten
    rueckwaertskompatibel; die deterministische Reparatur erzeugt immer das
    neue Zweifeldformat.
    """
    stories = _trade_story_bloecke(text)
    if not stories:
        return False, ["Abschnitt 6.1 mit perspektivischen Trade-Story-Eintraegen fehlt oder ist nicht parsebar."]
    errors = []
    valid_keys, gelesene_quellen, fehlende_quellen = _trade_story_setup_universum(eingabedateien)
    zentrale_valid_keys, zentrale_prepared_keys, zentrale_verfuegbar = _trade_story_zentrales_universum(eingabedateien)
    if zentrale_verfuegbar:
        valid_keys = zentrale_valid_keys
    beobachtungs_keys, beobachtung_verfuegbar = _trade_story_beobachtung_universum(beobachtungsliste_pfad)

    for idx, story in enumerate(stories, 1):
        discovery_m = re.search(r"(?im)^Discovery-Status\s*:\s*(ENTDECKT|BEOBACHTUNG)\s*$", story)
        technical_m = re.search(r"(?im)^Technischer Status\s*:\s*(NICHT VORHANDEN|NUR TEILW\. VOLLSTAENDIG|VALIDER SETUP)\s*$", story)
        legacy_status_m = re.search(r"(?im)^Status\s*:\s*(INTERESSANT|VORBEREITET|VALIDE SETUP|NUR TEILW\. VOLLSTAENDIG|VALIDER SETUP)\s*$", story)
        if strikt_statusfelder:
            if not discovery_m:
                errors.append(f"Trade-Story {idx}: Discovery-Status fehlt; erlaubt sind ENTDECKT oder BEOBACHTUNG.")
            if not technical_m:
                errors.append(f"Trade-Story {idx}: Technischer Status fehlt; erlaubt sind NICHT VORHANDEN, NUR TEILW. VOLLSTAENDIG, VALIDER SETUP.")
            if not discovery_m or not technical_m:
                continue
            ds = discovery_m.group(1)
            st = technical_m.group(1)
        else:
            if not legacy_status_m:
                errors.append(f"Trade-Story {idx}: Status fehlt; erlaubt sind NUR TEILW. VOLLSTAENDIG, VALIDER SETUP.")
                continue
            ds = "BEOBACHTUNG"
            st = legacy_status_m.group(1)
            st = {"INTERESSANT": "NUR TEILW. VOLLSTAENDIG", "VORBEREITET": "NUR TEILW. VOLLSTAENDIG", "VALIDE SETUP": "VALIDER SETUP"}.get(st, st)
        name_m = re.search(r"(?im)^(?:Bestehender Kandidat / Bezug|Kandidat|Name)\s*:\s*(.+)$", story)
        candidate = name_m.group(1).strip() if name_m else ""
        if re.search(r"(?i)\bkein(?:e|en)?\s+(?:bestehender\s+)?kandidat(?:en)?\b|\bkein bestehender kandidat vorhanden\b", candidate):
            candidate = ""
        if strikt_statusfelder:
            if not candidate and st != "NICHT VORHANDEN":
                errors.append(f"Trade-Story {idx}: Ohne bestehenden Kandidaten muss Technischer Status = NICHT VORHANDEN sein.")
            if candidate and st == "NICHT VORHANDEN":
                errors.append(f"Trade-Story {idx}: Vorhandener Kandidat darf nicht als NICHT VORHANDEN gekennzeichnet werden.")
        if st == "VALIDER SETUP":
            if not gelesene_quellen:
                errors.append("Trade-Story %d: VALIDER SETUP nicht verifizierbar, weil keine autoritative Setup-Datei erfolgreich gelesen wurde." % idx)
            elif not _trade_story_alle_kandidaten_treffen(candidate, valid_keys):
                errors.append(f"Trade-Story {idx}: VALIDER SETUP fuer '{candidate or 'unbekannter Kandidat'}' nicht in gueltigen autoritativen Setup-Zeilen gefunden.")
        else:
            if re.search(r"(?i)\b(?:kaufen|direkt(?:er|en)?\s+einstieg|jetzt\s+einsteigen|entry|buy)\b", story):
                errors.append(f"Trade-Story {idx}: {st} darf keine Kauf-/Entry-Formulierung enthalten.")
            if st == "NUR TEILW. VOLLSTAENDIG" and not candidate:
                errors.append(f"Trade-Story {idx}: NUR TEILW. VOLLSTAENDIG benoetigt einen bestehenden Kandidaten.")
            if st == "NUR TEILW. VOLLSTAENDIG" and candidate:
                prepared_keys = zentrale_prepared_keys if zentrale_verfuegbar else beobachtungs_keys
                prepared_available = zentrale_verfuegbar or beobachtung_verfuegbar
                if not prepared_available:
                    errors.append(f"Trade-Story {idx}: NUR TEILW. VOLLSTAENDIG nicht verifizierbar, weil das zentrale Trade-Story-Universum bzw. die aktuelle Beobachtungsliste fehlt oder unlesbar ist.")
                elif not _trade_story_alle_kandidaten_treffen(candidate, prepared_keys):
                    errors.append(f"Trade-Story {idx}: NUR TEILW. VOLLSTAENDIG fuer '{candidate}' ist nicht im autoritativen Kandidaten-/Beobachtungsuniversum verankert.")
                elif _trade_story_alle_kandidaten_treffen(candidate, valid_keys):
                    errors.append(f"Trade-Story {idx}: NUR TEILW. VOLLSTAENDIG fuer '{candidate}' verweist bereits auf ein autoritatives VALIDE SETUP; verwende Technischer Status: VALIDER SETUP.")

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
    werden auf NUR TEILW. VOLLSTAENDIG bzw. NICHT VORHANDEN zurueckgestuft; ein
    bereits gueltiges Setup wird als VALIDER SETUP ausgegeben. Kauf-/Entry-Formulierungen auf
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
            "Discovery-Status: ENTDECKT\n"
            "Technischer Status: NICHT VORHANDEN\n"
            "Naechster technischer Trigger: aus der bestehenden Systemanalyse abwarten\n"
            "Risiko: These ist nicht als bestaetigtes Setup zu verstehen.\n"
        )

    valid_keys, gelesene_quellen, _ = _trade_story_setup_universum(eingabedateien)
    zentrale_valid_keys, zentrale_prepared_keys, zentrale_verfuegbar = _trade_story_zentrales_universum(eingabedateien)
    if zentrale_verfuegbar:
        valid_keys = zentrale_valid_keys
    beobachtungs_keys, beobachtung_verfuegbar = _trade_story_beobachtung_universum(beobachtungsliste_pfad)

    repaired = []
    for story in stories:
        block = story.strip()
        discovery_m = re.search(r"(?im)^Discovery-Status\s*:\s*(ENTDECKT|BEOBACHTUNG)\s*$", block)
        technical_m = re.search(r"(?im)^Technischer Status\s*:\s*(NICHT VORHANDEN|NUR TEILW\. VOLLSTAENDIG|VALIDER SETUP)\s*$", block)
        legacy_status_m = re.search(r"(?im)^Status\s*:\s*(INTERESSANT|VORBEREITET|VALIDE SETUP|NUR TEILW\. VOLLSTAENDIG|VALIDER SETUP)\s*$", block)
        discovery_status = discovery_m.group(1) if discovery_m else ("BEOBACHTUNG" if legacy_status_m else "ENTDECKT")
        status = technical_m.group(1) if technical_m else (legacy_status_m.group(1) if legacy_status_m else None)
        status = {"INTERESSANT": "NUR TEILW. VOLLSTAENDIG", "VORBEREITET": "NUR TEILW. VOLLSTAENDIG", "VALIDE SETUP": "VALIDER SETUP"}.get(status, status)
        name_m = re.search(r"(?im)^(?:Bestehender Kandidat / Bezug|Kandidat|Name)\s*:\s*(.+)$", block)
        candidate = name_m.group(1).strip() if name_m else ""
        if re.search(r"(?i)\bkein(?:e|en)?\s+(?:bestehender\s+)?kandidat(?:en)?\b|\bkein bestehender kandidat vorhanden\b", candidate):
            candidate = ""
        # Nicht autoritative C/KEIN-KANDIDAT-Markierungen duerfen nach einer
        # Herabstufung auf INTERESSANT nicht als Kandidatenstatus stehen bleiben.
        candidate = re.sub(r"\s*\[(?:Kaufkandidat\s*C|KEIN\s+KANDIDAT)[^\]]*\]", "", candidate, flags=re.I).strip()

        # Technischer Status wird ausschliesslich aus Kandidatenexistenz und
        # autoritativ bestaetigtem Setup abgeleitet. Die interne Quelle darf
        # weiterhin VALIDE SETUP/VORBEREITET fuehren; nach aussen gibt es nur
        # noch drei technische Ausgabestufen.
        prepared_keys = zentrale_prepared_keys if zentrale_verfuegbar else beobachtungs_keys
        prepared_available = zentrale_verfuegbar or beobachtung_verfuegbar
        if candidate and gelesene_quellen and _trade_story_alle_kandidaten_treffen(candidate, valid_keys):
            status = "VALIDER SETUP"
        elif candidate and prepared_available and _trade_story_alle_kandidaten_treffen(candidate, prepared_keys):
            status = "NUR TEILW. VOLLSTAENDIG"
        else:
            candidate = ""
            status = "NICHT VORHANDEN"

        # Discovery-Status ist von der technischen Ebene getrennt.
        # Ohne Kandidat = neue Entdeckung; mit bestehendem Bezug = Beobachtung.
        discovery_status = "BEOBACHTUNG" if candidate else "ENTDECKT"
        status_line = f"Discovery-Status: {discovery_status}\nTechnischer Status: {status}"
        block = re.sub(r"(?im)^Discovery-Status\s*:\s*(ENTDECKT|BEOBACHTUNG)\s*$", "", block)
        block = re.sub(r"(?im)^Technischer Status\s*:\s*(NICHT VORHANDEN|NUR TEILW\. VOLLSTAENDIG|VALIDER SETUP)\s*$", "", block)
        block = re.sub(r"(?im)^Status\s*:\s*(INTERESSANT|VORBEREITET|VALIDE SETUP|NUR TEILW\. VOLLSTAENDIG|VALIDER SETUP)\s*$", "", block)
        block = block.rstrip() + "\n" + status_line + "\n"
        if name_m:
            block = block[:name_m.start(1)] + (candidate or "kein bestehender autoritativer Kandidat vorhanden") + block[name_m.end(1):]

        # Auf nicht-validen Ebenen keine Kauf-/Entry-Sprache stehen lassen.
        if status != "VALIDER SETUP":
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
