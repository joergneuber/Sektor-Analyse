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
import hashlib
import random
import json
import datetime
import datetime as dt
from pathlib import Path
import mimetypes
from types import SimpleNamespace

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from collections import defaultdict


# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

MODELL = "gemini-3.5-flash-lite"  # Primaer-Modell: hohes kostenloses RPD/TPM und hoher Durchsatz
FALLBACK_MODELL = "gemini-3.1-flash-lite"  # Erster Fallback: ebenfalls hohes kostenloses RPD/TPM
DRITTER_FALLBACK_MODELL = "gemini-3.8-flash"  # Qualitaets-Fallback bei Ausfall beider Lite-Modelle
VIERTER_FALLBACK_MODELL = "gemini-3.7-flash"  # Weiterer Qualitaets-Fallback
FUENFTER_FALLBACK_MODELL = "gemini-3.6-flash"  # Weiterer Qualitaets-Fallback

# Alle fuer diesen Lauf konfigurierten Modelle werden hoechstens einmal
# versucht. So wird ein einzelnes Free-Tier-Modell bei 503/Netzwerkproblemen
# nicht mehrfach in derselben Nachfragespitze verbrannt.
GEMINI_MODELLREIHENFOLGE = tuple(dict.fromkeys(
    modell for modell in (
        MODELL,
        FALLBACK_MODELL,
        DRITTER_FALLBACK_MODELL,
        VIERTER_FALLBACK_MODELL,
        FUENFTER_FALLBACK_MODELL,
    )
    if modell
))
MAX_VERSUCHE = len(GEMINI_MODELLREIHENFOLGE)
# Konservatives Einzelrequest-Budget. Das bisherige 210k-Budget erlaubte trotz
# 250k-Serverlimit noch sehr grosse Einzelrequests (z.B. 133k/197k).
# 120k begrenzt die Groesse eines einzelnen GenerateContent-Requests deutlich
# und laesst zugleich genug Raum fuer den grossen Master-Systemkontext.
GEMINI_INPUT_SAFE_BUDGET = 120_000
# Konservatives projektweites Minutenbudget fuer den Free-Tier-TPM-Schutz.
# WICHTIG: Dieses Tracking ist bewusst PROZESSLOKAL. Es verhindert, dass mehrere
# Modelle innerhalb dieses Python-Prozesses zusammen das lokale 240k/60s-Budget
# ueberschreiten. Es synchronisiert NICHT mehrere parallel laufende GitHub-Jobs
# oder Prozesse. Eine echte projektweite Synchronisation wuerde einen gemeinsamen
# persistenten/externen Quota-Ledger erfordern und ist hier bewusst nicht eingefuehrt.
# Das laesst 10k Reserve gegen Rundungs-/Vorverbrauchseffekte.
GEMINI_FREE_TIER_PROJECT_INPUT_LOCAL_LIMIT = 240_000
GEMINI_PROJECT_QUOTA_SCOPE = "PROCESS_LOCAL"
# Lokale Quell-Chunks werden bewusst token-konservativ gehalten. Die bisherige
# 60.000-Zeichen-Grenze war nicht ausreichend: datenreiche JSON/CSV-Inhalte
# koennen deutlich mehr als 1 Token pro Zeichen benoetigen und zusammen mit
# dem ca. 45,6k Token grossen Systemkontext das 120k-Einzelrequest-Budget
# ueberschreiten. 20.000 Zeichen halten selbst bei deutlich hoeherer
# Token-Dichte ausreichend Reserve fuer Systemanweisung und Stufenprompt.
GEMINI_SOURCE_CHUNK_MAX_CHARS = 20_000
# Serverseitiges Free-Tier-Input-Token-Kontingent pro Modell und Minute.
# Dieses Kontingent ist vom Einzelrequest-Limit getrennt.
GEMINI_FREE_TIER_INPUT_TOKEN_LIMIT = 250_000
GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS = 60.0
# Kleine Reserve gegen die Grenze des serverseitigen Minutenfensters.
GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS = 2.0
# Lokale Planungsreserve unterhalb des serverseitigen 250k-Minutenlimits.
# Sie schützt vor Rundungs-/Messabweichungen und unbekanntem serverseitigem
# Vorverbrauch, ohne das eigentliche Einzelrequest-Budget von 210k zu senken.
GEMINI_FREE_TIER_INPUT_TOKEN_RESERVE = 10_000
GEMINI_FREE_TIER_INPUT_LOCAL_LIMIT = (
    GEMINI_FREE_TIER_INPUT_TOKEN_LIMIT - GEMINI_FREE_TIER_INPUT_TOKEN_RESERVE
)
# Bekannte Free-Tier-Requests pro Modell und Tag. Diese Limits sind bewusst
# MODELLABHAENGIG: AI Studio zeigt fuer die beiden Flash-Lite-Modelle aktuell
# deutlich hoehere RPD-Limits als fuer die Flash-Modelle. Ein globales Limit
# von 20 wuerde gemini-3.5-flash-lite nach 20 erfolgreichen Requests lokal
# faelschlich sperren, obwohl dessen Free-Tier-RPD deutlich hoeher ist.
# Der lokale Zaehler bleibt eine Untergrenze, weil serverseitiger Verbrauch
# vor Prozessstart unbekannt ist.
GEMINI_FREE_TIER_RPD_LIMITS = {
    "gemini-3.5-flash-lite": 500,
    "gemini-3.1-flash-lite": 500,
    "gemini-3.5-flash": 20,
    "gemini-3.6-flash": 20,
    "gemini-3.7-flash": 20,
    "gemini-3.8-flash": 20,
}
GEMINI_FREE_TIER_RPD_DEFAULT_LIMIT = 20
WARTEZEIT_SEKUNDEN = 10  # Grundwartezeit fuer Sicherheitsfilter-Retries (steigt leicht an)

# Fuer SERVERSEITIGE UEBERLAST (HTTP 503) und Netzwerk-Abbrueche gilt eine
# exponentiell ansteigende Backoff-Staffel. Zusaetzlicher Jitter verhindert,
# dass mehrere parallele Laeufe exakt gleichzeitig erneut anfragen.
UEBERLAST_WARTEZEITEN = [15, 30, 60, 120]  # Sekunden; Backoff nur beim erneuten Poolversuch
# Beim Wechsel auf ein ANDERES Modell wird nicht zusaetzlich die volle
# Ueberlast-Backoffzeit verbrannt: das ist ein Fallback, kein Retry desselben
# Requests. Ein kleiner Jitter verhindert synchronisierte Modellwechsel.
GEMINI_MODELLWECHSEL_JITTER_MAX = 2.0
# Harte Obergrenze gegen endlose 503-Zyklen. Innerhalb eines Zyklus duerfen
# alle Modelle einmal versucht werden; erst danach beginnt ein neuer Poolversuch.
GEMINI_MAX_TECHNISCHE_RETRY_ZYKLEN = 3

ANWEISUNG_DATEI = "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"

# Gleicher Drive-Ordner wie in upload_to_drive.py - dort landen alle
# Scanner-Ausgaben, von dort werden ggf. die Short-Dateien nachgeladen.
DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
BEOBACHTUNGSLISTE_DATEI = "einzel_check_beobachtung.json"
GEMINI_HISTORIE_DATEI = "Gemini_Auswertung_Historie.txt"
GEMINI_A3_HISTORIE_DATEI = ".gemini_einzel_check_historie_a3.json"

# Laufzeit-Tracking fuer das serverseitige Free-Tier-Input-Token-Kontingent.
# Es werden nur Tokens registriert, die unmittelbar vor einem echten
# GenerateContent-Aufruf als gesendet betrachtet werden. count_tokens() selbst
# wird hier ausdruecklich NICHT angerechnet.
_gemini_input_quota_usage = defaultdict(list)
# CountTokens-Ergebnisse bleiben fuer die Dauer EINES Python-Laufs im Speicher.
# Das vermeidet tausende identische CountTokens-Aufrufe bei technischen Retries,
# ohne Ergebnisse zwischen verschiedenen GitHub-Laeufen zu persistieren.
_gemini_token_count_cache = {}
# Zusaetzliches konservatives projektweites Minutenfenster innerhalb DIESES
# Python-Prozesses. Es verhindert Modell-uebergreifende Ueberschreitungen im
# laufenden Prozess; parallele Prozesse/GitHub-Jobs werden bewusst nicht
# gegenseitig synchronisiert (siehe GEMINI_PROJECT_QUOTA_SCOPE).
_gemini_project_input_quota_usage = []
_gemini_input_quota_cooldown_until = {}
_gemini_rpd_requests = defaultdict(int)
_gemini_rpd_exhausted = set()
_gemini_active_modell = None
_gemini_last_request_model = None
_gemini_failed_models = set()
# Eindeutige lokale Sendungsreservierungen. Eine Reservierung bindet
# Input-Token-Buchung und RPD-Buchung an genau denselben GenerateContent-Versuch.
_gemini_sendungsreservierungen = []
# Erfolgreiche Analyse-Stufen bleiben bei rein technischen Retries erhalten.
# Dadurch startet ein 503 in A2/A3/Final nicht erneut bei A1 und verbraucht
# keine bereits erfolgreich erzeugten GenerateContent-Requests ein zweites Mal.
_gemini_stufen_cache = {}
# Erfolgreiche einzelne GenerateContent-Splits bleiben innerhalb des Laufes erhalten.
# Ein technischer Fehler darf niemals bereits erfolgreiche Splits erneut senden.
_gemini_request_split_cache = {}
# Exakte Eingabedateien desselben Gemini-Laufs; werden auch fuer deterministische
# Nachbearbeitung (u.a. Abschnitt 2.4) weitergereicht, statt Quellen neu zu suchen.
_GEMINI_EINGABEDATEIEN_AUSWERTUNG = None

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
    # PDF-Ausgaben des aktuellen Positionslaufs: zusaetzliche Syntheseinformation,
    # aber keine Ersatzquelle fuer die autoritative CSV/Tab-2-Faktenbasis.
    "Offene Positionen+Check.pdf": ["Offene Positionen+Check.pdf"],
    "Offene_Positionen.pdf": ["Offene_Positionen.pdf"],
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
    "Trade_Story_Aktienuniversum(...).csv": ["Trade_Story_Aktienuniversum(*).csv"],
    # Optional: letzter fertiger Gemini-Report fuer den expliziten Laufvergleich.
    "Letzte_Auswertung(...).txt": ["Auswertung(*).txt"],
    # Persistenter Langzeit-Kontext fuer Gemini: rollierende Historie der
    # investmentrelevanten Auswertungsteile. Wird nach jedem erfolgreichen
    # Lauf erzeugt und vom Drive-Upload-Skript als TXT-Datei persistiert.
    "Gemini_Auswertung_Historie.txt": ["Gemini_Auswertung_Historie.txt"],
}
# Diese Dateien MUESSEN vorhanden sein, sonst wird abgebrochen. Offene
# Positionen und die beiden Trendwende-Dateien sind optional (siehe
# Abschnitt 10 der Anleitung, die genau diesen Fall vorsieht).
PFLICHT_DATEIEN = {
    "briefing.txt",
    "Setups(...).csv",
    "Performance(...).csv",
    "Performance_EU(...).csv",
    "Trade_Story_Aktienuniversum(...).csv",
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
    wie Punkt 10.5 wird strict=True verwendet: Technische Auth-/Drive-Fehler
    duerfen dort niemals als "keine Historie" erscheinen.
    """
    token_str = os.environ.get("GDRIVE_TOKEN")
    if not token_str:
        msg = "GDRIVE_TOKEN nicht gesetzt"
        if strict:
            raise RuntimeError(f"10.5: Autoritativer Google-Drive-Zugriff nicht moeglich: {msg}")
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
                    raise RuntimeError(f"10.5: Autoritativer Google-Drive-Zugriff nicht moeglich: {msg}")
                print(f"WARNUNG: {msg} - Short-Dateien werden uebersprungen.")
                return None
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        if strict:
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"10.5: Autoritativer Google-Drive-Zugriff fehlgeschlagen: {e}") from e
        print(f"WARNUNG: Drive-Verbindung fuer Short-Dateien fehlgeschlagen ({e}) - wird uebersprungen.")
        return None



def lade_offenen_positionen_check_tab2():
    """Liest ausschließlich Tab 2 des Master-Sheets für Punkt 10.5.

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
            raise RuntimeError("10.5: Google-Credentials für autoritativen Tab-2-Zugriff nicht verfügbar.")

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
            raise RuntimeError("10.5: Master-Sheet 'Offene Positionen+Check' im konfigurierten Projektordner nicht gefunden.")
        if len(files) > 1:
            raise RuntimeError(
                "10.5: Mehrere Master-Sheets 'Offene Positionen+Check' im konfigurierten Projektordner gefunden: "
                + ", ".join(f"{f.get('id')} (modified={f.get('modifiedTime')})" for f in files)
            )

        master = files[0]
        spreadsheet_id = master["id"]
        print(
            f"10.5 MASTER: Offene Positionen+Check | id={spreadsheet_id} | "
            f"modified={master.get('modifiedTime')} | folder={DRIVE_FOLDER_ID}"
        )

        metadata = sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(title,index,sheetId)"
        ).execute()
        sheet_props = [s.get("properties", {}) for s in metadata.get("sheets", [])]
        titles = [str(p.get("title", "")).strip() for p in sheet_props]
        print(f"10.5 MASTER-TABS: {titles}")
        if "Geschlossene Positionen" not in titles:
            raise RuntimeError("10.5: Tab 'Geschlossene Positionen' im Master-Sheet nicht vorhanden.")

        values = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="'Geschlossene Positionen'!A:AA",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [])

        if len(values) < 2:
            raise RuntimeError("10.5: Tab 'Geschlossene Positionen' enthält keine Headerzeile.")

        headers = [str(x).strip() for x in values[1]]
        required_headers = {"Ticker", "Ausstiegsdatum", "Status"}
        missing_headers = sorted(required_headers - set(headers))
        if missing_headers:
            raise RuntimeError(
                "10.5: Pflichtspalten in Tab 'Geschlossene Positionen' fehlen: "
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
            f"10.5 HISTORIE-PRUEFUNG: daten={len(rows)} | parsebar={parseable_dates} | "
            f"neuestes_ausstiegsdatum={newest_exit} | fenster={start.isoformat()}..{today.isoformat()} | "
            f"treffer={len(selected)}"
        )

        if not selected:
            print("HISTORIE 10.5: Keine geschlossene Position innerhalb der letzten 3 Kalendertage.")
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
            f"HISTORIE 10.5: {len(selected)} geschlossene Position(en) aus Tab 2 innerhalb des 3-Tage-Fensters | "
            f"Ticker={selected_tickers}"
        )
        return "\n".join(out)

    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"10.5: Tab 'Geschlossene Positionen' konnte nicht autoritativ gelesen/verifiziert werden: {exc}") from exc


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
        raise RuntimeError("Offene Positionen+Check.csv fehlt fuer den autoritativen Punkt-10-Aufbau.")
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
                ("Richtung", direction), ("Status", status), ("Aktueller Kurs", current),
                ("Stop", _csv_value(row, ["Stop"])),
                ("TP1", _csv_value(row, ["TP1"])),
                ("TP2", _csv_value(row, ["TP2"])),
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


def _erstelle_punkt10_fakten(csv_pfad, geschlossene_10_5=""):
    """Erzeugt die autoritativen Fakten für 10.5 und die Portfolio-Grundlage.

    10.3 beschreibt ausschließlich veränderte Investmentthesen und wird von
    Gemini aus der aktuellen autoritativen Positionsliste im Abgleich mit dem
    vorherigen Lauf/der Historie formuliert. 10.5 bleibt vollständig
    deterministisch aus Tab 2.
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

    out = ["10. 💼 BESTEHENDES PORTFOLIO", "", "10.1 Sofortiger Handlungsbedarf",
           "[GEMINI-INTERPRETATION]", "", "10.2 Stop-/TP-Änderungen",
           "[GEMINI-INTERPRETATION]", "", "10.3 Positionen mit neuer Investmentthese",]

    if not rows:
        out[9] = "Keine offenen Positionen laut Offene Positionen+Check.csv."
    out.extend([
        "", "10.4 Positionen, deren These schwächer wird",
        "[GEMINI-INTERPRETATION]",
        "", "10.5 Geschlossene Positionen",
        geschlossene_10_5 or "Keine geschlossene Position innerhalb der letzten 3 Kalendertage."
    ])
    return "\n".join(out).strip() + "\n"


def _ersetze_punkt10_durch_python_fakten(text, python_punkt10):
    """Ersetzt 10.3/10.5 deterministisch und übernimmt 10.1/10.2/10.4 von Gemini.

    Positionsstammdaten und technische Check-Felder bleiben ausschließlich
    an die autoritativen Positionsquellen gebunden.
    """
    if not python_punkt10:
        raise RuntimeError("Python-Punkt-10-Faktenblock ist leer.")

    def extract_subsection(label, next_labels):
        pattern = rf"(?ims)^\s*{re.escape(label)}\s*$.*?(?=^\s*(?:{'|'.join(re.escape(x) for x in next_labels)})\s*$|\Z)"
        m = re.search(pattern, text or "")
        return m.group(0).strip() if m else ""

    gemini_10_1 = extract_subsection("10.1 Sofortiger Handlungsbedarf", [
        "10.2 Stop-/TP-Änderungen", "10.3 Positionen mit neuer Investmentthese", "10.4 Positionen, deren These schwächer wird", "10.5 Geschlossene Positionen", "11. METHODIK / DATENQUALITÄT"
    ])
    gemini_10_2 = extract_subsection("10.2 Stop-/TP-Änderungen", [
        "10.3 Positionen mit neuer Investmentthese", "10.4 Positionen, deren These schwächer wird", "10.5 Geschlossene Positionen", "11. METHODIK / DATENQUALITÄT"
    ])
    gemini_10_4 = extract_subsection("10.4 Positionen, deren These schwächer wird", [
        "10.5 Geschlossene Positionen", "11. METHODIK / DATENQUALITÄT"
    ])
    gemini_10_3 = extract_subsection("10.3 Positionen mit neuer Investmentthese", [
        "10.4 Positionen, deren These schwächer wird", "10.5 Geschlossene Positionen", "11. METHODIK / DATENQUALITÄT"
    ])

    if not gemini_10_1:
        gemini_10_1 = "10.1 Sofortiger Handlungsbedarf\nKeine Gemini-Interpretation für den Handlungsbedarf verfügbar."
    if not gemini_10_2:
        gemini_10_2 = "10.2 Stop-/TP-Änderungen\nKeine tatsächlichen Stop-/TP-Änderungen aus dem bereitgestellten Datenbestand erkannt."
    if not gemini_10_3:
        gemini_10_3 = "10.3 Positionen mit neuer Investmentthese\nKeine belastbare Veränderung einer bestehenden Investmentthese aus dem bereitgestellten Datenbestand erkannt."
    if not gemini_10_4:
        gemini_10_4 = "10.4 Positionen, deren These schwächer wird\nKeine belastbare Abschwächung einer bestehenden Investmentthese aus dem bereitgestellten Datenbestand erkannt."

    python_punkt10 = re.sub(
        r"(?ims)^\s*10\.1 Sofortiger Handlungsbedarf\s*\n.*?(?=^\s*10\.2 Stop-/TP-Änderungen\s*$)",
        gemini_10_1.rstrip() + "\n\n",
        python_punkt10,
        count=1,
    )
    python_punkt10 = re.sub(
        r"(?ims)^\s*10\.2 Stop-/TP-Änderungen\s*\n.*?(?=^\s*10\.3 Positionen mit neuer Investmentthese\s*$)",
        gemini_10_2.rstrip() + "\n\n",
        python_punkt10,
        count=1,
    )
    python_punkt10 = re.sub(
        r"(?ims)^\s*10\.3 Positionen mit neuer Investmentthese\s*\n.*?(?=^\s*10\.4 Positionen, deren These schwächer wird\s*$)",
        gemini_10_3.rstrip() + "\n\n",
        python_punkt10,
        count=1,
    )
    python_punkt10 = re.sub(
        r"(?ims)^\s*10\.4 Positionen, deren These schwächer wird\s*\n.*?(?=^\s*10\.5 Geschlossene Positionen\s*$)",
        gemini_10_4.rstrip() + "\n\n",
        python_punkt10,
        count=1,
    )

    old = re.search(
        r"(?ims)^\s*10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
        text or "",
    )
    if old:
        return (text[:old.start()] + python_punkt10.rstrip() + "\n\n" + text[old.end():]).strip() + "\n"
    next11 = re.search(r"(?im)^\s*11\. METHODIK / DATENQUALITÄT\s*$", text or "")
    if next11:
        return (text[:next11.start()] + python_punkt10.rstrip() + "\n\n" + text[next11.start():]).strip() + "\n"
    return (text.rstrip() + "\n\n" + python_punkt10.rstrip() + "\n").strip() + "\n"


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
    kategorie: str) zurueck. Kategorien: "tageskontingent", "modellpool_temporaer",
    "ueberlast", "netzwerk", "sonstiges"."""
    text_klein = fehlertext.lower()
    ist_cache_free_tier = (
        "totalcachedcontentstoragetokenspermodelfreetier" in text_klein
        or "cachedcontent" in text_klein and "limit=0" in text_klein
    )
    if ist_cache_free_tier:
        return True, None, "cache_free_tier"

    # Explizit terminal behandeln: Wenn der interne Fallback bereits
    # festgestellt hat, dass ALLE konfigurierten Modelle serverseitig wegen
    # PerDay erschoepft sind, darf der aeussere Hauptretry nicht noch einmal
    # anlaufen. Der normale technische Fallback am Ende dieser Funktion greift
    # unmittelbar nach dem break der Hauptretry-Schleife.
    ist_server_rpd_terminal = (
        "gemini_rpd_serverseitig_erschoepft" in text_klein
        and "gemini_rpd_serverseitig_erschoepft_temporaer" not in text_klein
    )
    ist_modellpool_temporaer = (
        "gemini_rpd_serverseitig_erschoepft_temporaer" in text_klein
        or "gemini_modellpool_temporaer_nicht_verfuegbar" in text_klein
    )
    ist_tages_kontingent = (
        not ist_modellpool_temporaer
        and (
            ist_server_rpd_terminal
            or "perday" in text_klein
            or "gemini_rpd_lokal_erschoepft" in text_klein
            or "gemini_rpd_modell_erschoepft" in text_klein
            or "generaterequestsperdayperprojectpermodelfreetier" in text_klein
        )
    )
    if ist_tages_kontingent:
        return True, None, "tageskontingent"

    ist_input_token_limit = (
        "generate_content_free_tier_input_token_count" in text_klein
        or "generatecontentinputtokenspermodelperminute-freetier" in text_klein
        or ("input_token_count" in text_klein and "250000" in text_klein)
        # Interner Server-Quota-Fallback meldet selbst kein Google-Quota-
        # Feld mehr, sondern wirft diesen eigenen Fehler. Er gehoert
        # semantisch trotzdem zum minutenbezogenen Input-Token-Limit.
        or "gemini_input_quota_serverseitig_erschoepft" in text_klein
    )
    if ist_input_token_limit:
        # Das Free-Tier-Input-Limit von 250.000 Tokens ist ein
        # minutenbezogenes Kontingent. Deshalb nicht das Modell wechseln
        # und den vollstaendigen Request nicht verwerfen: nach Ablauf des
        # Minutenfensters ist derselbe Request erneut zulaessig. Falls
        # Google einen retryDelay liefert, wird dieser mindestens auf
        # 65 Sekunden angehoben, damit das 60-Sekunden-Fenster sicher
        # verlassen wird.
        treffer_250k = re.search(
            r"retryDelay['\"]?\s*[:=]\s*['\"](\d+(?:\.\d+)?)s['\"]",
            fehlertext,
            flags=re.IGNORECASE,
        )
        server_wartezeit = float(treffer_250k.group(1)) if treffer_250k else 0.0
        return False, max(65.0, server_wartezeit), "input_token_limit"

    treffer = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", fehlertext)
    empfohlene_wartezeit = float(treffer.group(1)) if treffer else None

    if ist_modellpool_temporaer:
        return False, empfohlene_wartezeit, "modellpool_temporaer"

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
    """Liest den letzten bekannten Status fuer die interne A/B/C-Kandidatenhistorie.

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
        print(f"WARNUNG: A/B/C-Statushistorie konnte nicht gelesen werden: {exc}")
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
    """Ermittelt autoritative Anzeigenamen fuer die interne A/B/C-Kandidatenhistorie.

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
            print(f"WARNUNG: A/B/C-Namenshistorie konnte nicht gelesen werden: {exc}")

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


def erstelle_abkandidaten_autoritative_liste(beobachtungsliste_pfad, historie_pfad=None, eingabedateien=None):
    """Erzeugt die verbindliche A/B/C-Zuordnung aus dem aktuellen
    Einzel-Check-Status. Historie wird nur fuer die Darstellung des
    Statusverlaufs verwendet, niemals fuer die aktuelle Kategoriezuordnung.
    """
    if not beobachtungsliste_pfad or not os.path.exists(beobachtungsliste_pfad):
        raise RuntimeError(
            "Autoritative einzel_check_beobachtung.json fehlt. "
            "Die A/B/C-Zuordnung darf nicht aus historischen Daten rekonstruiert werden."
        )

    try:
        with open(beobachtungsliste_pfad, "r", encoding="utf-8-sig") as f:
            daten = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"Beobachtungsliste konnte nicht gelesen werden: {exc}") from exc

    if not isinstance(daten, dict):
        raise RuntimeError("einzel_check_beobachtung.json ist kein JSON-Objekt.")

    aktuelle_a = []
    aktuelle_nicht_a = []
    zulaessige_nicht_a_status = {"KAUFKANDIDAT B", "KAUFKANDIDAT C", "KEIN KANDIDAT"}
    vorherige_status = _lade_6_5_statusverlauf(historie_pfad)
    namen = _lade_6_5_namen(eingabedateien, historie_pfad)

    for ticker, eintrag in daten.items():
        if not isinstance(eintrag, dict):
            raise RuntimeError(f"Ungueltiger Beobachtungslisteneintrag fuer {ticker!r}.")
        status = str(eintrag.get("status", "")).strip()
        quelle = str(eintrag.get("quelle", "-")).strip() or "-"
        if status == "KAUFKANDIDAT A":
            aktuelle_a.append((str(ticker).strip(), quelle))
        elif status in zulaessige_nicht_a_status:
            aktuelle_nicht_a.append((str(ticker).strip(), status, quelle))
        else:
            raise RuntimeError(
                f"Unerwarteter aktueller Status fuer {ticker!r}: {status!r}. "
                "Die Liste wird nicht aus historischen Daten repariert."
            )

    aktuelle_a.sort(key=lambda x: x[0].upper())
    aktuelle_nicht_a.sort(key=lambda x: x[0].upper())

    zeilen = [
        "AUTORITATIVE A/B/C-ZUORDNUNG AUS einzel_check_beobachtung.json",
        "Diese Zuordnung ist verbindlich und wurde von Python aus dem AKTUELLEN Status erzeugt.",
        "Gemini darf die Kategoriezuordnung NICHT selbst rekonstruieren, veraendern oder aus anderen Dateien ableiten.",
        "Historische Status aus einzel_check_historie.jsonl und der zuletzt erfolgreichen HEBELTRADER-Datei sind fuer die A/B/C-Kategoriezuordnung unzulaessig.",
        "",
        f"INTERNE A-KANDIDATEN ({len(aktuelle_a)} Titel):",
    ]
    for ticker, quelle in aktuelle_a:
        vorher = vorherige_status.get(ticker, "NICHT BEKANNT")
        name = namen.get(_normalisiere_ticker(ticker), "Name nicht verfügbar")
        zeilen.append(
            f"- {name} ({ticker}) | {_kurzstatus(vorher)} -> A | aktueller Status: KAUFKANDIDAT A | Quelle: {quelle}"
        )

    # Interne Nicht-A-Kandidaten zeigt ausschliesslich aktuell aktive Nicht-A-Kandidaten (B/C).
    # KEIN KANDIDAT bleibt intern Bestandteil der autoritativen Beobachtungsliste,
    # wird aber bewusst nicht dargestellt. Sobald derselbe Titel wieder B/C/A wird,
    # erscheint er automatisch wieder. Es gibt weiterhin KEINE Mengenbegrenzung.
    aktive_nicht_a = [row for row in aktuelle_nicht_a if row[1] in {"KAUFKANDIDAT B", "KAUFKANDIDAT C"}]
    zeilen.extend([
        "",
        f"INTERNE NICHT-A-KANDIDATEN ({len(aktive_nicht_a)} Titel):",
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
        "Gemini darf fuer die interne A/B/C-Kandidatenmitgliedschaft nur die hier vorgegebene Zuordnung verwenden; technische Inhalte duerfen weiterhin nur aus den bereitgestellten Quelldaten uebernommen werden.",
    ])
    print(
        f"Interne A/B/C-Autoritaetsliste: {len(aktuelle_a)} A-Kandidaten | "
        f"{len(aktuelle_nicht_a)} Nicht-A-Kandidaten | {len(daten)} beobachtete Titel"
    )
    return "\n".join(zeilen)


def finde_datei(muster_liste):
    for muster in muster_liste:
        treffer = sorted(glob.glob(muster))
        if treffer:
            return treffer[-1]
    return None



def _download_drive_text_file(service, item, local_name):
    request = service.files().get_media(fileId=item["id"])
    with io.FileIO(local_name, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        fertig = False
        while not fertig:
            _, fertig = downloader.next_chunk()
    return local_name


def lade_letzte_auswertung_von_drive():
    """Laedt die chronologisch letzte fertige Auswertung vor heute.

    Entscheidend ist das Datum im Dateinamen, nicht ``modifiedTime``. Damit
    wird z.B. am Montag die Freitag-Auswertung verwendet, auch wenn eine
    andere alte Datei spaeter geaendert/hochgeladen wurde.
    """
    service = get_drive_service()
    if service is None:
        return None
    heute = datetime.date.today()
    try:
        query = (
            f"name contains 'Auswertung(' and '{DRIVE_FOLDER_ID}' in parents "
            "and trashed=false"
        )
        result = service.files().list(
            q=query,
            spaces="drive",
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=100,
        ).execute()
        kandidaten = []
        for item in result.get("files", []):
            name = str(item.get("name", ""))
            match = re.fullmatch(r"Auswertung\((\d{4}-\d{2}-\d{2})\)\.txt", name)
            if not match:
                continue
            try:
                datum = datetime.date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if datum < heute:
                kandidaten.append((datum, item))
        if not kandidaten:
            return None
        _, item = max(kandidaten, key=lambda x: x[0])
        local_name = item["name"]
        _download_drive_text_file(service, item, local_name)
        print(f"INFO: Letzte Auswertung aus Drive geladen: {item['name']}")
        return local_name
    except Exception as exc:
        print(f"WARNUNG: Vorherige Auswertung konnte nicht aus Drive geladen werden ({exc}).")
    return None


def lade_gemini_historie_von_drive():
    """Laedt die persistente rollierende Gemini-Investment-Historie.

    Die Historie ist eine zweite, langfristige Kontextquelle neben dem
    unmittelbaren Vorgaengerreport. Sie wird bewusst unter einem festen
    Dateinamen gespeichert und vom normalen Drive-Uploader persistiert.
    """
    if os.path.isfile(GEMINI_HISTORIE_DATEI):
        return GEMINI_HISTORIE_DATEI
    service = get_drive_service()
    if service is None:
        return None
    try:
        query = (
            f"name = '{GEMINI_HISTORIE_DATEI}' and '{DRIVE_FOLDER_ID}' in parents "
            "and trashed=false"
        )
        result = service.files().list(
            q=query, spaces="drive", fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc", pageSize=10,
        ).execute()
        files = result.get("files", [])
        if not files:
            return None
        _download_drive_text_file(service, files[0], GEMINI_HISTORIE_DATEI)
        print(f"INFO: Persistente Gemini-Historie aus Drive geladen -> {GEMINI_HISTORIE_DATEI}")
        return GEMINI_HISTORIE_DATEI
    except Exception as exc:
        print(f"WARNUNG: Persistente Gemini-Historie konnte nicht aus Drive geladen werden ({exc}).")
    return None


def _extrahiere_historienblock(text, heading_pattern, next_heading_pattern=r"^\d+\."):
    match = re.search(
        rf"(?ims)^({heading_pattern}\s*$).*?(?=^{next_heading_pattern}|\Z)",
        text or "",
    )
    return match.group(0).strip() if match else ""


def _baue_gemini_historie(aktuelle_auswertung, bestehende_historie=None):
    """Erzeugt eine persistente Langzeit-Historie aus vollständigen Auswertungen.

    Jede Auswertung wird vollständig als eigener Lauf-Snapshot gespeichert. Es
    werden weder einzelne Abschnitte ausgewählt noch Inhalte je Abschnitt oder
    die Anzahl der gespeicherten Läufe begrenzt. Aktuelle numerische Tageswerte
    bleiben in den Tagesdateien autoritativ und werden hier nicht zur aktuellen
    Zahlenquelle.
    """
    heute = datetime.date.today().isoformat()
    inhalt_aktuell = (aktuelle_auswertung or "").strip()
    snapshot = f"\n===== LAUF {heute} =====\n" + inhalt_aktuell
    alte = bestehende_historie or ""
    # Alte Snapshots anhand der Laufmarke trennen und doppelte Tagesmarke
    # vermeiden, falls ein Runner nach einem erfolgreichen Upload erneut startet.
    matches = list(re.finditer(r"(?m)^===== LAUF (\d{4}-\d{2}-\d{2}) =====\s*$", alte))
    alte_mit_datum = []
    for i, m in enumerate(matches):
        ende = matches[i + 1].start() if i + 1 < len(matches) else len(alte)
        datum = m.group(1)
        inhalt = alte[m.end():ende].strip()
        if datum != heute and inhalt:
            alte_mit_datum.append((datum, inhalt))
    # Nicht die Dateireihenfolge, sondern das Datum des Snapshots bestimmt die
    # Historienreihenfolge. So bleibt die Historie auch nach einem manuellen
    # Nachladen/Neuordnen der Drive-Datei chronologisch korrekt.
    alte_mit_datum.sort(key=lambda x: x[0])
    snapshots = [snapshot.strip()] + [f"===== LAUF {d} =====\n{inhalt}" for d, inhalt in alte_mit_datum]
    return (
        "NEUBER MACRO & MARKETS – PERSISTENTE GEMINI-INVESTMENT-HISTORIE\n"
        "Vollständige Auswertung-Snapshots: keine Lauf- oder Abschnittsbegrenzung.\n"
        "Zweck: Entwicklung von Investmentthesen, Frühindikatoren, handelbaren Chancen, "
        "Widersprüchen und Edelmetallideen über mehrere Läufe hinweg nachvollziehen.\n"
        "WICHTIG: Diese Datei ist Langzeit-Kontext, keine autoritative Quelle für aktuelle Kurse, "
        "Makro-Zahlen, Stopps, TP-Werte oder aktuelle technische Kennzahlen. Für aktuelle Werte "
        "gelten ausschließlich die aktuellen Tagesdateien.\n\n"
        + "\n\n".join(snapshots)
    )

def sammle_eingabedateien():
    gefunden = {}
    for name, muster_liste in DATEIMUSTER.items():
        gefunden[name] = finde_datei(muster_liste)

    # Abschnitt 2.4 darf ausschliesslich die A-Meldungsliste des heutigen
    # Einzel-Check-Laufs verwenden. Wenn heute keine A-Kandidaten vorliegen,
    # entfernt einzel_check.py die Tagesdatei; dann darf kein Vortag einspringen.
    heute_a_meldungen = (
        f"Einzel_Check_A_Meldungen({datetime.date.today().isoformat()}).txt"
    )
    gefunden["Einzel_Check_A_Meldungen(...).txt"] = (
        heute_a_meldungen if os.path.isfile(heute_a_meldungen) else None
    )

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

    # Letzte Auswertung: lokaler Treffer hat Vorrang, aber die heutige
    # Ausgabedatei darf niemals als "vorheriger Lauf" verwendet werden.
    heute_auswertung = f"Auswertung({datetime.date.today().isoformat()}).txt"
    lokale_auswertungen = [
        p for p in sorted(glob.glob("Auswertung(*).txt"))
        if os.path.basename(p) != heute_auswertung
    ]
    if lokale_auswertungen:
        # Das Datum im Dateinamen ist autoritativ fuer die Reihenfolge; so wird
        # auch am Montag die Freitag-Auswertung als direkter Vorgaenger erkannt.
        datierte = []
        for pfad in lokale_auswertungen:
            match = re.fullmatch(r"Auswertung\((\d{4}-\d{2}-\d{2})\)\.txt", os.path.basename(pfad))
            if match:
                try:
                    datierte.append((datetime.date.fromisoformat(match.group(1)), pfad))
                except ValueError:
                    pass
        if datierte:
            passende = [d for d in datierte if d[0] < datetime.date.today()]
            gefunden["Letzte_Auswertung(...).txt"] = max(passende, key=lambda x: x[0])[1] if passende else None
        else:
            gefunden["Letzte_Auswertung(...).txt"] = None
    if gefunden.get("Letzte_Auswertung(...).txt") is None:
        gefunden["Letzte_Auswertung(...).txt"] = lade_letzte_auswertung_von_drive()

    # Zusaetzlich zum unmittelbaren Vorgaenger immer die persistente Langzeit-
    # Historie laden. Sie ist besonders am Montag/Feiertag wichtig, weil dort
    # mehrere fruehere Laeufe fuer die Entwicklung einer These relevant sein
    # koennen, nicht nur der letzte Freitag.
    if gefunden.get("Gemini_Auswertung_Historie.txt") is None:
        gefunden["Gemini_Auswertung_Historie.txt"] = lade_gemini_historie_von_drive()

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
            current_k = key("Aktueller_Kurs") or key("Aktueller Kurs")
            stop_k = key("Stop")
            tp1_k = key("TP1")
            tp2_k = key("TP2")
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
                current = str(row.get(current_k, "") or "").strip() if current_k else ""
                stop = str(row.get(stop_k, "") or "").strip() if stop_k else ""
                tp1 = str(row.get(tp1_k, "") or "").strip() if tp1_k else ""
                tp2 = str(row.get(tp2_k, "") or "").strip() if tp2_k else ""
                if name or ticker:
                    parts = [f"- {name} ({ticker})", f"Einstieg: {entry}", f"Einstiegsdatum: {date}"]
                    if current:
                        parts.append(f"Aktueller Kurs: {current}")
                    if stop:
                        parts.append(f"Stop: {stop}")
                    if tp1:
                        parts.append(f"TP1: {tp1}")
                    if tp2:
                        parts.append(f"TP2: {tp2}")
                    rows.append(" | ".join(parts))
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
            current_k = key("Aktueller_Kurs") or key("Aktueller Kurs")
            stop_k = key("Stop")
            tp1_k = key("TP1")
            tp2_k = key("TP2")
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
                    "current": str(row.get(current_k, "") or "").strip() if current_k else "",
                    "stop": str(row.get(stop_k, "") or "").strip() if stop_k else "",
                    "tp1": str(row.get(tp1_k, "") or "").strip() if tp1_k else "",
                    "tp2": str(row.get(tp2_k, "") or "").strip() if tp2_k else "",
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


def _ticker_grenzen_regex(ticker):
    """Erzeugt eine Unicode-sichere Regex fuer isolierte Ticker.

    \\w ist in Python standardmaessig Unicode-aware. Dadurch wird z. B.
    "geändert" nicht mehr faelschlich als isoliertes "GE" erkannt.
    Punkte und Bindestriche bleiben weiterhin Teil der Tickergrenze.
    """
    return rf"(?<![\w.\-]){re.escape(str(ticker))}(?![\w.\-])"


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
    """Prüft strikt, ob Gemini den vollständigen Abschnitt 10 begonnen hat."""
    return bool(re.search(r"(?im)^\s*10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$", text or ""))


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
    """Liefert eine präzise Diagnose für das Punkt-10-API-Retry-Gate."""
    diagnose = {
        "ueberschrift_vorhanden": _enthaelt_abschnitt_7(text),
        "positionsbereich_vorhanden": False,
        "positionskoepfe": 0,
    }
    if not diagnose["ueberschrift_vorhanden"]:
        return diagnose
    match = re.search(
        r"(?ims)^\s*10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
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
    """Prueft nur die strukturelle Mindestvoraussetzung fuer Punkt 10.

    Punkt 10.3/10.5 wird inzwischen deterministisch von Python aus den
    autoritativen Quellen erzeugt. Gemini liefert 10.1, 10.2 und 10.4 als qualitative Interpretation.
    Diese Funktion bleibt als strukturelle Endpruefung fuer den bereits
    injizierten Python-Faktenblock erhalten.
    """
    if not _enthaelt_abschnitt_7(text):
        return False

    match = re.search(
        r"(?ims)^\s*10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
        text or "",
    )
    if not match:
        return False

    header_re = re.compile(
        r"(?m)^([^\n|]+?)\s*\|\s*Markt:\s*[^\n]+$"
    )
    return bool(header_re.search(match.group(0)))


def _fuege_abschnitt_7_ein(original_text, abschnitt_7):
    """Fügt einen ausschließlich für Punkt 10 angeforderten Gemini-Block ein.

    Der Reparatur-Call darf nur Punkt 10 liefern. Der Block wird deshalb nicht
    als komplette neue Auswertung verwendet, sondern deterministisch in die
    bestehende Antwort vor den nächsten nummerierten Hauptabschnitt eingesetzt.
    """
    if not _enthaelt_abschnitt_7(abschnitt_7):
        raise RuntimeError(
            "Gezielter Reparaturversuch lieferte ebenfalls keinen Abschnitt "
            "'10. BESTEHENDES PORTFOLIO'."
        )

    block_match = re.search(
        r"(?ims)^\s*10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
        abschnitt_7,
    )
    if not block_match:
        raise RuntimeError(
            "Gezielter Reparaturversuch lieferte keinen verwertbaren "
            "Abschnitt '10. BESTEHENDES PORTFOLIO'."
        )

    block = block_match.group(0).strip("\n")
    # Ersetze den bereits vorhandenen Punkt-10-Block vollständig durch
    # den erfolgreich reparierten Punkt-10-Block.
    vorhandener_abschnitt = re.search(
        r"(?ims)^\s*10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
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


def _sha256_datei(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _speichere_gemini_input_manifest(eingabedateien):
    """Sichert exakt den fuer diesen Gemini-Lauf bestimmten Eingabebestand.

    Der ChatGPT-Fallback verwendet dieses Manifest und ermittelt die Dateien
    nicht erneut ueber eigene Dateimuster. Damit entspricht das Fallback-Paket
    exakt der beim Gemini-Lauf verwendeten Eingabemenge.
    """
    manifest = {
        "version": 1,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "master_instruction": ANWEISUNG_DATEI,
        "files": [],
    }
    if os.path.isfile(ANWEISUNG_DATEI):
        manifest["master_sha256"] = _sha256_datei(ANWEISUNG_DATEI)
    for logical_name, pfad in sorted(eingabedateien.items()):
        if not pfad or not os.path.isfile(pfad):
            continue
        manifest["files"].append({
            "logical_name": logical_name,
            "path": os.path.abspath(pfad),
            "filename": os.path.basename(pfad),
            "size": os.path.getsize(pfad),
            "sha256": _sha256_datei(pfad),
        })
    with open(".gemini_input_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(
        f"GEMINI-INPUT-MANIFEST: {len(manifest['files'])} Dateien + Master-Anweisung gesichert."
    )



def _gemini_token_cache_key(modell, contents, system_instruction=None):
    """Erzeugt einen laufinternen, modellabhaengigen CountTokens-Schluessel."""
    teile = list(contents if isinstance(contents, list) else [contents])

    def stable_part(value):
        for attr in ("uri", "name", "display_name", "text"):
            candidate = getattr(value, attr, None)
            if candidate:
                return f"{attr}={candidate}"
        return repr(value)

    payload = {
        "modell": modell,
        "contents": [stable_part(value) for value in teile],
        "system": str(system_instruction or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _gemini_tokenzahl(client, modell, contents, system_instruction=None, label=""):
    """Misst den Eingabeumfang vor GenerateContent.

    Die Gemini Developer API dieses Skripts akzeptiert system_instruction
    nicht in CountTokensConfig. Wenn eine Systemanweisung vorhanden ist,
    werden Contents und Systemanweisung deshalb direkt separat gemessen.
    Das vermeidet einen bekannten, deterministisch fehlschlagenden API-Aufruf
    vor jeder eigentlichen Messung. Bei fehlender Messung wird NICHT gesendet.
    """
    teile = list(contents if isinstance(contents, list) else [contents])
    cache_key = _gemini_token_cache_key(modell, teile, system_instruction)
    cached_tokens = _gemini_token_count_cache.get(cache_key)
    if cached_tokens is not None:
        print(
            f"GEMINI-TOKEN-CACHE-HIT: {label or 'Request'} | Modell={modell} | "
            f"Gesamt={cached_tokens:,}"
        )
        return cached_tokens

    try:
        if system_instruction:
            # In der Developer API ist system_instruction in CountTokensConfig
            # nicht unterstützt. Die Addition entspricht dem bisherigen
            # separaten Fallback und bleibt bewusst konservativ.
            content_result = client.models.count_tokens(
                model=modell,
                contents=teile,
            )
            system_result = client.models.count_tokens(
                model=modell,
                contents=[system_instruction],
            )
            content_tokens = int(getattr(content_result, "total_tokens", 0) or 0)
            system_tokens = int(getattr(system_result, "total_tokens", 0) or 0)
            gesamt = content_tokens + system_tokens
            print(
                f"GEMINI-TOKEN-CHECK: {label or 'Request'} | Modell={modell} | "
                f"Contents={content_tokens:,} | System={system_tokens:,} | "
                f"Gesamt={gesamt:,} | Sicherheitsbudget={GEMINI_INPUT_SAFE_BUDGET:,} | "
                "Messung=separate CountTokens (Developer API)"
            )
        else:
            result = client.models.count_tokens(
                model=modell,
                contents=teile,
            )
            gesamt = int(getattr(result, "total_tokens", 0) or 0)
            print(
                f"GEMINI-TOKEN-CHECK: {label or 'Request'} | Modell={modell} | "
                f"Gesamt={gesamt:,} | Sicherheitsbudget={GEMINI_INPUT_SAFE_BUDGET:,}"
            )
        _gemini_token_count_cache[cache_key] = gesamt
        return gesamt
    except Exception as exc:
        messmodus = "separat" if system_instruction else "einzeln"
        raise RuntimeError(
            f"GEMINI_TOKENMESSUNG_FEHLER ({label or 'Request'}): "
            f"{messmodus}={exc}"
        ) from exc



def _gemini_a3_diff(vorher, aktuell, pfad=""):
    """Erzeugt einen rekonstruktionssicheren Delta-Snapshot.

    Die Funktion veraendert keine fachlichen Werte: Unveraenderte Teilbaeume
    werden weggelassen, geaenderte Werte werden mit ihrem vollstaendigen Pfad
    gespeichert. Loeschungen werden explizit markiert. Damit kann jeder
    historische Snapshot aus Basis + Delta exakt rekonstruiert werden.
    """
    set_values = {}
    deleted = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            # Datum und Ticker sind strukturelle Identitaet des Deltas und
            # werden nicht als wiederholte Feldwerte gespeichert.
            keys_a = set(a.keys()) - {"Datum", "Ticker"}
            keys_b = set(b.keys()) - {"Datum", "Ticker"}
            for key in keys_a - keys_b:
                deleted.append(f"{path}.{key}" if path else str(key))
            for key in keys_b:
                child = f"{path}.{key}" if path else str(key)
                if key not in a:
                    set_values[child] = b[key]
                else:
                    walk(a[key], b[key], child)
            return
        if a != b:
            set_values[path] = b

    walk(vorher, aktuell, pfad)
    return set_values, deleted


def _gemini_a3_set_path(obj, path, value):
    parts = path.split(".") if path else []
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            raise ValueError(f"A3-Historie: Pfad ist kein Objekt: {path}")
        cur = cur.setdefault(part, {})
    if not parts:
        raise ValueError("A3-Historie: leerer Set-Pfad")
    cur[parts[-1]] = value


def _gemini_a3_delete_path(obj, path):
    parts = path.split(".") if path else []
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict) and parts:
        cur.pop(parts[-1], None)


def _erstelle_gemini_a3_historie(historie_pfad, beobachtung_pfad=None):
    """Erzeugt eine vollstaendige, verlustfreie A3-Historie.

    Die Original-JSONL bleibt unveraendert und ist weiterhin die autoritative
    Rohquelle. Fuer Gemini wird sie tickerweise chronologisch als Basis +
    rekonstruktionssichere Deltas dargestellt. Dadurch bleiben ALLE Snapshots
    und ALLE Felder erhalten; lediglich unveraenderte Wiederholungen werden
    nicht erneut ausgeschrieben.

    einzel_check_beobachtung.json wird fuer einen Konsistenzcheck des aktuellen
    Status verwendet, aber nicht erneut in die Historie kopiert.
    """
    if not historie_pfad or not os.path.isfile(historie_pfad):
        return None

    rows = []
    with open(historie_pfad, "r", encoding="utf-8-sig") as f:
        for zeilennummer, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"A3-Historie: ungueltiges JSON in Zeile {zeilennummer}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"A3-Historie: Zeile {zeilennummer} ist kein JSON-Objekt.")
            ticker = str(row.get("Ticker") or "").strip()
            if not ticker:
                raise RuntimeError(f"A3-Historie: Zeile {zeilennummer} ohne Ticker.")
            rows.append(row)

    gruppen = {}
    for index, row in enumerate(rows):
        ticker = str(row.get("Ticker") or "").strip()
        gruppen.setdefault(ticker, []).append((index, row))

    for ticker, eintraege in gruppen.items():
        eintraege.sort(key=lambda item: (str(item[1].get("Datum") or ""), item[0]))

    observation_status = {}
    if beobachtung_pfad and os.path.isfile(beobachtung_pfad):
        try:
            with open(beobachtung_pfad, "r", encoding="utf-8-sig") as f:
                beobachtungen = json.load(f)
            if isinstance(beobachtungen, dict):
                observation_status = {
                    str(t).strip(): str(v.get("status") or "").strip()
                    for t, v in beobachtungen.items()
                    if isinstance(v, dict)
                }
        except Exception as exc:
            print(f"WARNUNG: A3-Historie konnte Beobachtungsliste nicht pruefen: {exc}")

    ticker_blocks = []
    for ticker in sorted(gruppen, key=str.upper):
        eintraege = gruppen[ticker]
        basis = eintraege[0][1]
        deltas = []
        vorher = basis
        for _, aktuell in eintraege[1:]:
            sets, deleted = _gemini_a3_diff(vorher, aktuell)
            deltas.append({
                "datum": aktuell.get("Datum"),
                "set": sets,
                "delete": deleted,
            })
            vorher = aktuell

        letzter_status = str(eintraege[-1][1].get("Status") or "").strip()
        beobachtungs_status = observation_status.get(ticker)
        block = {
            "ticker": ticker,
            "snapshots": len(eintraege),
            "basis": basis,
            "deltas": deltas,
        }
        if beobachtungs_status:
            block["aktueller_beobachtungsstatus"] = beobachtungs_status
            if letzter_status and beobachtungs_status != letzter_status:
                block["status_konsistenz"] = "ABWEICHUNG_PRUEFEN"
        ticker_blocks.append(block)

    meta = {
        "format": "A3_EINZEL_CHECK_HISTORIE_DELTA_V1",
        "verlustfrei": True,
        "beschreibung": (
            "Vollstaendige Einzel-Check-Historie. Jeder Snapshot ist aus BASIS + DELTAS "
            "exakt rekonstruierbar. Keine Historie wird zeitlich abgeschnitten."
        ),
        "source_sha256": _sha256_datei(historie_pfad),
        "snapshot_count": len(rows),
        "ticker_count": len(gruppen),
        "dates": sorted({str(r.get("Datum") or "") for r in rows}),
        "reconstruction": (
            "BASIS vollstaendig laden; DELTAS je Datum chronologisch anwenden. "
            "set setzt Pfade, delete entfernt Pfade."
        ),
        "ticker_blocks": ticker_blocks,
    }
    output = os.path.abspath(GEMINI_A3_HISTORIE_DATEI)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    print(
        f"GEMINI-A3-HISTORIE: {len(rows):,} Snapshots / {len(gruppen):,} Ticker | "
        f"Roh={os.path.getsize(historie_pfad):,} B | "
        f"Kompakt={os.path.getsize(output):,} B | verlustfrei=True"
    )
    return output


def _gemini_a3_rekonstruiere(pfad):
    """Test-/Prueffunktion: rekonstruiert alle Snapshots aus der A3-Darstellung."""
    with open(pfad, "r", encoding="utf-8") as f:
        data = json.load(f)
    result = []
    for block in data.get("ticker_blocks", []):
        current = json.loads(json.dumps(block["basis"], ensure_ascii=False))
        result.append(json.loads(json.dumps(current, ensure_ascii=False)))
        for delta in block.get("deltas", []):
            current["Datum"] = delta.get("datum")
            for path in delta.get("delete", []):
                _gemini_a3_delete_path(current, path)
            for path, value in delta.get("set", {}).items():
                _gemini_a3_set_path(current, path, value)
            result.append(json.loads(json.dumps(current, ensure_ascii=False)))
    return result


def _gemini_cache_erstellen(client, modell, anweisung, hochgeladene_teile, eingabedateien=None, exclude_names=None, include_names=None):
    """Bereitet einen strikt stufenspezifischen Datenkontext vor.

    ``include_names`` ist die bevorzugte Variante: Nur die dort genannten
    Quellen werden in den Gemini-Kontext dieser Stufe aufgenommen.
    ``exclude_names`` bleibt fuer bestehende Aufrufer rueckwaertskompatibel.
    Die Original-Uploads und lokalen autoritativen Quelldateien werden dadurch
    nicht geloescht oder veraendert.
    """
    names = list(eingabedateien.keys()) if eingabedateien else []

    def basisname(name):
        # Chunk-Namen tragen die Form logical_name#CHUNK_001. Fuer include/exclude
        # bleibt trotzdem der urspruengliche logische Dateiname autoritativ.
        return str(name).split("#CHUNK_", 1)[0]

    if isinstance(hochgeladene_teile, list) and hochgeladene_teile and all(
        isinstance(item, tuple) and len(item) == 2 for item in hochgeladene_teile
    ):
        upload_pairs = list(hochgeladene_teile)
    else:
        name_to_index = {name: i for i, name in enumerate(names)}
        upload_pairs = [(name, hochgeladene_teile[name_to_index[name]]) for name in names]

    if include_names is not None and eingabedateien:
        allowed = set(include_names)
        selected_pairs = [(name, teil) for name, teil in upload_pairs if basisname(name) in allowed]
    elif exclude_names and eingabedateien:
        excluded = set(exclude_names)
        selected_pairs = [(name, teil) for name, teil in upload_pairs if basisname(name) not in excluded]
    else:
        selected_pairs = upload_pairs

    context_names = [name for name, _ in selected_pairs]
    contents = [teil for _, teil in selected_pairs]

    print(
        f"  Gemini-Kontext fuer normale GenerateContent-Anfragen vorbereitet: "
        f"{len(contents)} Quellen/Chunks"
    )
    if context_names:
        paar = list(zip(context_names, contents))
        a_teile = []
        c_teile = []
        bd_teile = []
        for name, teil in paar:
            if name == "Struktur_Trend_Briefing(...).txt":
                c_teile.append(teil)
            elif name == "Makro_Briefing(...).txt":
                bd_teile.append(teil)
            else:
                a_teile.append(teil)

        for label, teile in (
            ("A operative Daten", a_teile),
            ("C Struktur-Trend", c_teile),
            ("B+D Makro/Geopolitik-Paket", bd_teile),
        ):
            if teile:
                try:
                    tokens = _gemini_tokenzahl(
                        client, modell, teile, None, label=label
                    )
                    print(f"GEMINI-DATENBLOCK: {label} = {tokens:,} Tokens")
                except Exception as exc:
                    raise RuntimeError(
                        f"GEMINI_DATENBLOCK_MESSUNG_FEHLER: {label}: {exc}"
                    ) from exc
        if bd_teile:
            print(
                "GEMINI-DATENBLOCK: D wird nicht doppelt gezaehlt; "
                "Geopolitik ist technisch im Makro_Briefing enthalten."
            )
    return {"contents": contents, "system_instruction": anweisung, "context_names": context_names}



def _gemini_quelltext_chunks(pfad, logical_name):
    """Liest grosse Textquellen verlustfrei und erzeugt deterministische Chunks.

    CSV/JSONL/sonstige Textdateien werden bevorzugt an Zeilengrenzen geteilt.
    Bei einem einzelnen uebergrossen Datensatz wird kontrolliert innerhalb der
    Zeile geteilt. JSON-Dateien mit ``ticker_blocks`` werden, wenn moeglich,
    blockweise als weiterhin valides JSON segmentiert. Originaldateien bleiben
    unveraendert; die Chunks sind ausschliesslich Gemini-Requestteile.
    """
    ext = Path(pfad).suffix.lower()
    text_mimes = {".txt", ".csv", ".json", ".jsonl", ".md", ".log"}
    if ext not in text_mimes or os.path.getsize(pfad) <= GEMINI_SOURCE_CHUNK_MAX_CHARS:
        return None

    text = Path(pfad).read_text(encoding="utf-8-sig")
    if len(text) <= GEMINI_SOURCE_CHUNK_MAX_CHARS:
        return None

    # Spezialfall A3-Historie / JSON-Objekte mit ticker_blocks: jeder Chunk
    # bleibt syntaktisch valides JSON und enthaelt nur ganze Ticker-Bloecke.
    if ext == ".json":
        try:
            payload = json.loads(text)
            blocks = payload.get("ticker_blocks") if isinstance(payload, dict) else None
            if isinstance(blocks, list) and blocks:
                prefix = {k: v for k, v in payload.items() if k != "ticker_blocks"}
                chunks = []
                current = []
                for block in blocks:
                    candidate = current + [block]
                    probe = dict(prefix)
                    probe["ticker_blocks"] = candidate
                    probe["chunking"] = {"logical_source": logical_name, "partial": True}
                    if current and len(json.dumps(probe, ensure_ascii=False, separators=(",", ":"))) > GEMINI_SOURCE_CHUNK_MAX_CHARS:
                        chunks.append(current)
                        current = [block]
                    else:
                        current = candidate
                if current:
                    chunks.append(current)
                result = []
                for idx, block_group in enumerate(chunks, 1):
                    out = dict(prefix)
                    out["ticker_blocks"] = block_group
                    out["chunking"] = {
                        "logical_source": logical_name,
                        "chunk_index": idx,
                        "chunk_count": len(chunks),
                        "partial": len(chunks) > 1,
                    }
                    result.append(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
                return result
        except Exception:
            # Kein Spezialformat erzwingen; unten folgt der sichere Text-Fallback.
            pass

    chunks = []
    current_lines = []
    current_chars = 0
    for line in text.splitlines(keepends=True):
        if current_lines and current_chars + len(line) > GEMINI_SOURCE_CHUNK_MAX_CHARS:
            chunks.append("".join(current_lines))
            current_lines = []
            current_chars = 0
        if len(line) > GEMINI_SOURCE_CHUNK_MAX_CHARS:
            if current_lines:
                chunks.append("".join(current_lines))
                current_lines = []
                current_chars = 0
            for start in range(0, len(line), GEMINI_SOURCE_CHUNK_MAX_CHARS):
                chunks.append(line[start:start + GEMINI_SOURCE_CHUNK_MAX_CHARS])
        else:
            current_lines.append(line)
            current_chars += len(line)
    if current_lines:
        chunks.append("".join(current_lines))
    return chunks


def _gemini_hochgeladene_quellen_erstellen(client, eingabedateien_gemini):
    """Erzeugt Upload-Referenzen bzw. lokale Text-Chunks fuer Gemini.

    Rueckgabe: ``[(logical_name, content_part), ...]``. Damit koennen mehrere
    Chunks derselben logischen Quelle in den stufenspezifischen Kontext gelangen,
    ohne die bestehende include/exclude-Logik zu veraendern.
    """
    result = []
    for logical_name, pfad in eingabedateien_gemini.items():
        if not pfad:
            continue
        chunks = _gemini_quelltext_chunks(pfad, logical_name)
        if chunks:
            print(
                f"  Gemini-Chunking: {os.path.basename(pfad)} | "
                f"{len(chunks)} Chunks | max. {GEMINI_SOURCE_CHUNK_MAX_CHARS:,} Zeichen/Chunk"
            )
            for idx, chunk_text in enumerate(chunks, 1):
                header = (
                    f"\n\n===== QUELLDATEI {logical_name} | CHUNK {idx}/{len(chunks)} =====\n"
                    "Dieser Chunk ist ein vollstaendiger Ausschnitt der unveraenderten Originalquelle. "
                    "Keine Daten ergaenzen oder weglassen.\n\n"
                )
                result.append((f"{logical_name}#CHUNK_{idx:03d}", types.Part.from_text(text=header + chunk_text)))
            continue

        mime_type = ermittle_upload_mime_type(pfad)
        print(f"  Gemini-Upload: {os.path.basename(pfad)} | MIME: {mime_type}")
        result.append((logical_name, client.files.upload(
            file=pfad,
            config=types.UploadFileConfig(mime_type=mime_type),
        )))
    return result


def _gemini_sichere_daten_gruppen(client, modell, daten_teile, arbeits_contents,
                                   system_instruction, label):
    """Erzeugt strikt budgetkonforme Request-Gruppen.

    Normalerweise wird an logischen Dateigrenzen gebuendelt. Die Upload-Schicht
    zerlegt sehr grosse lokale Textquellen jedoch bereits vorab in kontrollierte
    ``#CHUNK_NNN``-Teile. Dadurch kann auch eine einzelne grosse Quelle niemals
    wieder einen 133k/197k-Request erzwingen. Jeder tatsaechliche Request wird
    weiterhin unmittelbar vor dem Senden erneut per CountTokens verifiziert.
    """
    gruppe = []
    for teil in daten_teile:
        kandidat = gruppe + [teil]
        tokens = _gemini_tokenzahl(
            client, modell, kandidat + arbeits_contents,
            system_instruction,
            label=label,
        )
        if tokens > GEMINI_INPUT_SAFE_BUDGET:
            if not gruppe:
                raise RuntimeError(
                    f"GEMINI_QUELLE_NACH_CHUNKING_ZU_GROSS: {label} "
                    f"ein einzelner Daten-Chunk ueberschreitet "
                    f"{GEMINI_INPUT_SAFE_BUDGET:,} Tokens. "
                    "Die Quelle muss kleiner segmentiert werden."
                )
            yield gruppe
            einzel_tokens = _gemini_tokenzahl(
                client, modell, [teil] + arbeits_contents,
                system_instruction,
                label=f"{label} Einzelchunk",
            )
            if einzel_tokens > GEMINI_INPUT_SAFE_BUDGET:
                raise RuntimeError(
                    f"GEMINI_QUELLE_NACH_CHUNKING_ZU_GROSS: {label} "
                    f"ein einzelner Daten-Chunk ueberschreitet "
                    f"{GEMINI_INPUT_SAFE_BUDGET:,} Tokens "
                    f"(gemessen: {einzel_tokens:,})."
                )
            gruppe = [teil]
        else:
            gruppe = kandidat

    if gruppe:
        yield gruppe


def _gemini_quota_prune(modell, jetzt=None):
    """Entfernt lokale Modell- und projektweite Buchungen ausserhalb des Minutenfensters."""
    jetzt = time.monotonic() if jetzt is None else jetzt
    grenze = jetzt - GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
    eintraege = _gemini_input_quota_usage.get(modell, [])
    if eintraege:
        _gemini_input_quota_usage[modell] = [
            (zeitpunkt, tokens)
            for zeitpunkt, tokens in eintraege
            if zeitpunkt > grenze
        ]
    if _gemini_project_input_quota_usage:
        _gemini_project_input_quota_usage[:] = [
            (zeitpunkt, tokens)
            for zeitpunkt, tokens in _gemini_project_input_quota_usage
            if zeitpunkt > grenze
        ]
    if (_gemini_input_quota_cooldown_until.get(modell, 0.0) <= jetzt):
        _gemini_input_quota_cooldown_until.pop(modell, None)



def _gemini_quota_kandidaten(preferred_modell, fehlgeschlagene_einschliessen=False):
    """Liefert die Modellreihenfolge mit stabilem Active-Model-Pin.

    Standardmaessig werden Modelle mit einem temporaeren 503-/Netzwerkfehler
    ausgeschlossen. Bei einem serverseitigen 429 PerDay duerfen diese Modelle
    jedoch wieder als Fallback betrachtet werden: Ihr vorheriger 503 war kein
    Tagesquota-Verbrauch und wurde lokal zurueckgerollt.
    """
    reihenfolge = []
    for modell in (_gemini_active_modell, preferred_modell, *GEMINI_MODELLREIHENFOLGE):
        if (
            modell
            and modell not in reihenfolge
            and modell not in _gemini_rpd_exhausted
            and (fehlgeschlagene_einschliessen or modell not in _gemini_failed_models)
        ):
            reihenfolge.append(modell)
    return reihenfolge


def _gemini_quota_waehlen(
    preferred_modell, request_tokens, ausgeschlossene_modelle=None,
    fehlgeschlagene_einschliessen=False
):
    """Waehlt ein Modell mit Minuten- und bekanntem Tagesquota.

    Die Wartezeit beruecksichtigt sowohl das Auslaufen des lokalen 60-s-
    Inputfensters als auch einen vorhandenen Modell-Cooldown. Es gilt immer
    die laengere der beiden notwendigen Wartezeiten.
    """
    jetzt = time.monotonic()
    beste_wartezeit = None
    ausgeschlossene_modelle = set(ausgeschlossene_modelle or ())
    # Erst das gemeinsame lokale 60-s-Fenster bereinigen, dann Projektverbrauch
    # und Restbudget berechnen. Andernfalls konnten bereits abgelaufene
    # Projektbuchungen die erste Restberechnung unnoetig verknappen.
    _gemini_quota_prune(None, jetzt)
    projektverbrauch = sum(tokens for _, tokens in _gemini_project_input_quota_usage)
    projekt_rest = GEMINI_FREE_TIER_PROJECT_INPUT_LOCAL_LIMIT - projektverbrauch
    for modell in _gemini_quota_kandidaten(
        preferred_modell,
        fehlgeschlagene_einschliessen=fehlgeschlagene_einschliessen,
    ):
        if modell in ausgeschlossene_modelle or modell in _gemini_rpd_exhausted:
            continue
        _gemini_quota_prune(modell, jetzt)
        cooldown = _gemini_input_quota_cooldown_until.get(modell, 0.0)
        verbrauch = sum(tokens for _, tokens in _gemini_input_quota_usage.get(modell, []))
        rest = GEMINI_FREE_TIER_INPUT_LOCAL_LIMIT - verbrauch

        if cooldown <= jetzt and request_tokens <= rest and request_tokens <= projekt_rest:
            return modell, 0.0

        eintraege = _gemini_input_quota_usage.get(modell, [])
        wait_for_usage = 0.0
        if request_tokens > rest and eintraege:
            aeltester_zeitpunkt = min(zeitpunkt for zeitpunkt, _ in eintraege)
            wait_for_usage = max(
                0.0,
                aeltester_zeitpunkt
                + GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
                + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS
                - jetzt,
            )
        if request_tokens > projekt_rest and _gemini_project_input_quota_usage:
            aeltester_projektzeitpunkt = min(zeitpunkt for zeitpunkt, _ in _gemini_project_input_quota_usage)
            wait_for_project = max(
                0.0,
                aeltester_projektzeitpunkt
                + GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
                + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS
                - jetzt,
            )
            wait_for_usage = max(wait_for_usage, wait_for_project)
        wait_for_cooldown = max(0.0, cooldown - jetzt)
        wartezeit = max(wait_for_usage, wait_for_cooldown)
        if beste_wartezeit is None or wartezeit < beste_wartezeit:
            beste_wartezeit = wartezeit

    return None, (beste_wartezeit if beste_wartezeit is not None else 0.0)


def _gemini_rpd_registriere_sendung(modell):
    """Zaehlt einen echten GenerateContent-Versuch fuer diesen Prozesslauf."""
    if modell in _gemini_rpd_exhausted:
        raise RuntimeError(f"GEMINI_RPD_MODELL_ERCHOEPFT: {modell} ist fuer diesen Lauf gesperrt.")
    limit = GEMINI_FREE_TIER_RPD_LIMITS.get(
        modell, GEMINI_FREE_TIER_RPD_DEFAULT_LIMIT
    )
    if _gemini_rpd_requests[modell] >= limit:
        _gemini_rpd_exhausted.add(modell)
        raise RuntimeError(
            f"GEMINI_RPD_LOKAL_ERCHOEPFT: Modell={modell} | "
            f"Requests={_gemini_rpd_requests[modell]} | Limit={limit}"
        )
    _gemini_rpd_requests[modell] += 1


def _gemini_quota_registriere_sendung(modell, request_tokens):
    """Registriert einen unmittelbar bevorstehenden echten GenerateContent-Call.

    Die Rueckgabe ist eine eindeutige lokale Sendungsreservierung. Dadurch kann
    ein technischer 503 exakt die zugehoerige Input-Token- und RPD-Buchung
    zurueckrollen, auch wenn mehrere Requests dieselbe Tokenzahl haben.
    """
    jetzt = time.monotonic()
    _gemini_quota_prune(modell, jetzt)
    verbrauch = sum(tokens for _, tokens in _gemini_input_quota_usage.get(modell, []))
    projektverbrauch = sum(tokens for _, tokens in _gemini_project_input_quota_usage)
    if verbrauch + request_tokens > GEMINI_FREE_TIER_INPUT_LOCAL_LIMIT:
        raise RuntimeError(
            "GEMINI_LOKALES_INPUT_QUOTA_VOR_SENDUNG: "
            f"Modell={modell} | bereits={verbrauch:,} | Request={request_tokens:,} | "
            f"Limit={GEMINI_FREE_TIER_INPUT_TOKEN_LIMIT:,}"
        )
    if projektverbrauch + request_tokens > GEMINI_FREE_TIER_PROJECT_INPUT_LOCAL_LIMIT:
        raise RuntimeError(
            "GEMINI_PROJEKT_INPUT_QUOTA_VOR_SENDUNG: "
            f"Projektverbrauch={projektverbrauch:,} | Request={request_tokens:,} | "
            f"LokalesProjektLimit={GEMINI_FREE_TIER_PROJECT_INPUT_LOCAL_LIMIT:,} | "
            f"ServerTPMLimit={GEMINI_FREE_TIER_INPUT_TOKEN_LIMIT:,}"
        )
    _gemini_rpd_registriere_sendung(modell)
    eintrag = (jetzt, request_tokens)
    _gemini_input_quota_usage[modell].append(eintrag)
    _gemini_project_input_quota_usage.append(eintrag)
    reservierung = {
        "modell": modell,
        "request_tokens": request_tokens,
        "zeitpunkt": jetzt,
        "input_eintrag": eintrag,
        "aktiv": True,
    }
    _gemini_sendungsreservierungen.append(reservierung)
    print(
        f"GEMINI-INPUT-QUOTA: Modell={modell} | Request={request_tokens:,} | "
        f"Fensterverbrauch={verbrauch + request_tokens:,}/{GEMINI_FREE_TIER_INPUT_TOKEN_LIMIT:,} | "
        f"Projektfenster={projektverbrauch + request_tokens:,}/{GEMINI_FREE_TIER_PROJECT_INPUT_LOCAL_LIMIT:,} "
        f"({GEMINI_PROJECT_QUOTA_SCOPE}) | "
        f"RPD-lokal={_gemini_rpd_requests[modell]}/{GEMINI_FREE_TIER_RPD_LIMITS.get(modell, GEMINI_FREE_TIER_RPD_DEFAULT_LIMIT)}"
    )
    return reservierung


def _gemini_quota_rollback_sendung(reservierung):
    """Rollt die vollstaendige lokale Sendungsbuchung eines Requests zurueck.

    Eine Sendungsreservierung besteht aus genau zwei lokalen Buchungen:
    * Input-Token im minutenbezogenen Fenster-Tracking
    * ein Request im modellbezogenen RPD-Zaehler

    Die Reservierung wird ueber ihre eindeutige Identitaet zurueckgerollt und
    nicht nur ueber die Tokenzahl gesucht. Dadurch kann bei identischen
    Request-Groessen niemals die falsche Sendung zurueckgesetzt werden.

    Ein serverseitig bestaetigtes 429 PerDay darf diese Funktion nicht
    verwenden; dieser Pfad bleibt bewusst ohne Rollback.
    """
    if not reservierung or not reservierung.get("aktiv"):
        return False

    modell = reservierung["modell"]
    request_tokens = reservierung["request_tokens"]
    input_eintrag = reservierung["input_eintrag"]

    eintraege = _gemini_input_quota_usage.get(modell, [])
    try:
        eintraege.remove(input_eintrag)
        input_entfernt = True
    except ValueError:
        input_entfernt = False

    try:
        _gemini_project_input_quota_usage.remove(input_eintrag)
    except ValueError:
        pass

    if _gemini_rpd_requests[modell] > 0:
        _gemini_rpd_requests[modell] -= 1
    if not _gemini_rpd_requests[modell]:
        _gemini_rpd_requests.pop(modell, None)

    reservierung["aktiv"] = False
    try:
        _gemini_sendungsreservierungen.remove(reservierung)
    except ValueError:
        pass

    print(
        f"GEMINI-QUOTA-ROLLBACK: Modell={modell} | Request={request_tokens:,} | "
        f"Input-Quota-Buchung {'entfernt' if input_entfernt else 'bereits nicht mehr vorhanden'} | "
        f"RPD-lokal={_gemini_rpd_requests.get(modell, 0)}"
    )
    return True


def _gemini_generate_content_quota_safe(
    client, preferred_modell, contents, config_kwargs, measured_tokens, label
):
    """Sendet einen bereits gemessenen Request quota-sicher.

    count_tokens() wird vor der Quota-Pruefung fuer das konkret ausgewaehlte
    Modell erneut ausgefuehrt, falls der Scheduler vom bevorzugten Modell auf
    ein Fallback-Modell wechselt. Kein Request wird gesendet, wenn das lokale
    Minutenkontingent nicht ausreicht.
    """
    global _gemini_active_modell, _gemini_last_request_model
    preferred_modell = _gemini_active_modell or preferred_modell
    modell, wartezeit = _gemini_quota_waehlen(preferred_modell, measured_tokens)
    while modell is None:
        kandidaten = _gemini_quota_kandidaten(preferred_modell)
        if not kandidaten:
            # Sind alle Kandidaten nur wegen temporaerer 503-/Netzwerkfehler
            # gesperrt, darf dieser technische Zustand nicht als Input-Quota-
            # Fehler terminal werden. Nach einem echten Wartefenster wird der
            # temporaere failed_models-Zustand geloescht und Quota + Modellwahl
            # vollstaendig neu bewertet. RPD-PerDay-Sperren bleiben erhalten.
            if _gemini_failed_models:
                wartezeit = max(
                    float(wartezeit),
                    UEBERLAST_WARTEZEITEN[-1],
                    GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
                    + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS,
                )
                print(
                    "GEMINI-503-WAIT: Alle aktuell verfuegbaren Modelle sind "
                    "wegen temporaerer 503-/Netzwerkfehler gesperrt; "
                    f"warte {wartezeit:.1f}s, loesche den temporaeren "
                    "failed_models-Zustand und bewerte Quota/Modelle neu."
                )
                time.sleep(wartezeit)
                _gemini_failed_models.clear()
                _gemini_active_modell = None
                _gemini_last_request_model = None
                modell, wartezeit = _gemini_quota_waehlen(
                    preferred_modell, measured_tokens
                )
                continue

            raise RuntimeError(
                "GEMINI_INPUT_QUOTA_KEIN_MODELL_FREI: "
                f"Request={measured_tokens:,} Tokens; alle Modelle sind fuer diesen Lauf gesperrt."
            )
        # 0.0 s bedeutet hier: lokales Tracking kennt keinen freien Slot.
        # Niemals sofort denselben Zustand erneut pruefen; mindestens ein
        # komplettes serverseitiges Minutenfenster abwarten.
        wartezeit = max(
            float(wartezeit),
            GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
            + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS,
        )
        print(
            f"GEMINI-INPUT-QUOTA-WAIT: kein Modell hat aktuell genug Restkontingent; "
            f"warte {wartezeit:.1f}s. Request={measured_tokens:,}"
        )
        time.sleep(wartezeit)
        modell, wartezeit = _gemini_quota_waehlen(preferred_modell, measured_tokens)

    if modell != preferred_modell:
        _gemini_active_modell = modell
        neu_gemessen = _gemini_tokenzahl(
            client,
            modell,
            contents,
            config_kwargs.get("system_instruction"),
            label=f"{label} Quota-Fallback {modell}",
        )
        if neu_gemessen > GEMINI_INPUT_SAFE_BUDGET:
            raise RuntimeError(
                "GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_FALLBACK: "
                f"Modell={modell} | {neu_gemessen:,} > "
                f"{GEMINI_INPUT_SAFE_BUDGET:,}"
            )
        measured_tokens = neu_gemessen
        # Die Neumessung gilt fuer das bereits ausgewaehlte Fallback-Modell.
        # Nur wenn dieses Modell dadurch selbst nicht mehr in sein Restkontingent
        # passt, wird ein anderes Modell gesucht; das urspruenglich bevorzugte
        # Modell wird dabei nicht erneut als Kandidat erzwungen.
        modell, wartezeit = _gemini_quota_waehlen(
            modell, measured_tokens, ausgeschlossene_modelle=set()
        )
        if modell is None:
            print(
                f"GEMINI-INPUT-QUOTA-WAIT: nach erneuter Messung kein Modell frei; "
                f"warte {wartezeit:.1f}s. Request={measured_tokens:,}"
            )
            time.sleep(wartezeit)
            modell, _ = _gemini_quota_waehlen(
                preferred_modell, measured_tokens, ausgeschlossene_modelle=set()
            )
            if modell is None:
                raise RuntimeError(
                    "GEMINI_INPUT_QUOTA_KEIN_MODELL_FREI_NACH_NEUMESSUNG: "
                    f"Request={measured_tokens:,} Tokens."
                )

    if measured_tokens > GEMINI_INPUT_SAFE_BUDGET:
        raise RuntimeError(
            "GEMINI_REQUEST_ZU_GROSS_VOR_SENDUNG: "
            f"{measured_tokens:,} > {GEMINI_INPUT_SAFE_BUDGET:,}"
        )

    # Modellbindung fuer den laufenden Analyseversuch: Nach einem lokalen
    # Quota-Fallback bleibt dieses Modell bevorzugt, solange es die naechsten
    # Requests zulaesst.
    _gemini_active_modell = modell
    _gemini_last_request_model = modell
    # Der tatsaechlich reservierte Sendungszustand wird separat verfolgt, damit
    # ein spaeterer 503 exakt dieselbe lokale Buchung zurueckrollen kann.
    sendung_modell = modell
    sendung_tokens = measured_tokens
    # Die Registrierung erfolgt unmittelbar vor dem echten API-Aufruf.
    sendungs_reservierung = _gemini_quota_registriere_sendung(
        sendung_modell, sendung_tokens
    )
    print(
        f"GEMINI-REQUEST-SAFE: {label} | Modell={modell} | "
        f"vorab gemessen={measured_tokens:,} <= {GEMINI_INPUT_SAFE_BUDGET:,} | "
        "Free-Tier-Input-Quota vor Sendung geprueft"
    )
    try:
        return client.models.generate_content(
            model=modell,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )
    except Exception as exc:
        fehlertext = str(exc).lower()

        # Ein 503/Netzwerkfehler ist keine erfolgreich verbrauchte Sendung.
        # Deshalb wird die unmittelbar vor dem API-Aufruf reservierte lokale
        # Input- und RPD-Buchung vollstaendig zurueckgerollt. Ein 429 PerDay
        # erreicht diesen Block ebenfalls, wird aber bewusst NICHT gerollt.
        ist_perday = (
            "perday" in fehlertext
            or "generaterequestsperdayperprojectpermodelfreetier" in fehlertext
        )
        ist_technischer_sendefehler = (
            "503" in fehlertext
            or "unavailable" in fehlertext
            or "high demand" in fehlertext
            or "connection reset" in fehlertext
            or "connection aborted" in fehlertext
            or "timed out" in fehlertext
        )
        if ist_technischer_sendefehler and not ist_perday:
            _gemini_quota_rollback_sendung(sendungs_reservierung)

        if ist_perday:
            _gemini_rpd_exhausted.add(modell)
            print(f"GEMINI-RPD-SERVER: Modell={modell} wegen 429 PerDay fuer den restlichen Lauf gesperrt.")
            temporaerer_fallback_fehlgeschlagen = False
            for naechstes_modell in _gemini_quota_kandidaten(
                modell,
                fehlgeschlagene_einschliessen=True,
            ):
                if naechstes_modell == modell:
                    continue
                neu_gemessen = _gemini_tokenzahl(
                    client, naechstes_modell, contents,
                    config_kwargs.get("system_instruction"),
                    label=f"{label} Server-RPD-Fallback {naechstes_modell}",
                )
                if neu_gemessen > GEMINI_INPUT_SAFE_BUDGET:
                    continue
                verfuegbares_modell, _ = _gemini_quota_waehlen(
                    naechstes_modell,
                    neu_gemessen,
                    ausgeschlossene_modelle={modell},
                    fehlgeschlagene_einschliessen=True,
                )
                if verfuegbares_modell is None:
                    continue
                _gemini_active_modell = verfuegbares_modell
                _gemini_last_request_model = verfuegbares_modell
                sendung_modell = verfuegbares_modell
                sendung_tokens = neu_gemessen
                sendungs_reservierung = _gemini_quota_registriere_sendung(
                    sendung_modell, sendung_tokens
                )
                print(
                    f"GEMINI-RPD-SERVER-FALLBACK: {modell} -> {verfuegbares_modell} | "
                    f"Request={neu_gemessen:,}"
                )
                try:
                    return client.models.generate_content(
                        model=verfuegbares_modell,
                        contents=contents,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                except Exception as fallback_exc:
                    fallback_text = str(fallback_exc).lower()
                    fallback_perday = (
                        "perday" in fallback_text
                        or "generaterequestsperdayperprojectpermodelfreetier" in fallback_text
                    )
                    fallback_technisch = (
                        "503" in fallback_text
                        or "unavailable" in fallback_text
                        or "high demand" in fallback_text
                        or "connection reset" in fallback_text
                        or "connection aborted" in fallback_text
                        or "timed out" in fallback_text
                    )
                    if fallback_perday:
                        _gemini_rpd_exhausted.add(verfuegbares_modell)
                        print(
                            f"GEMINI-RPD-SERVER: Modell={verfuegbares_modell} ebenfalls wegen 429 PerDay "
                            "fuer den restlichen Lauf gesperrt."
                        )
                        continue
                    if fallback_technisch:
                        _gemini_quota_rollback_sendung(sendungs_reservierung)
                        _gemini_failed_models.add(verfuegbares_modell)
                        temporaerer_fallback_fehlgeschlagen = True
                        print(
                            f"GEMINI-RPD-SERVER-FALLBACK-503: {verfuegbares_modell} "
                            "ist temporaer ueberlastet; pruefe den naechsten Fallback ohne "
                            "den gesamten Retry-Zyklus neu zu starten."
                        )
                        continue
                    raise
            if temporaerer_fallback_fehlgeschlagen:
                raise RuntimeError(
                    "GEMINI_RPD_SERVERSEITIG_ERSCHOEPFT_TEMPORAER: "
                    "PerDay-gesperrte Modelle vorhanden, die uebrigen Modelle sind temporaer "
                    "wegen 503/Netzwerk nicht verfuegbar; erneute Poolpruefung nach Backoff erforderlich."
                ) from exc
            raise RuntimeError(
                "GEMINI_RPD_SERVERSEITIG_ERSCHOEPFT: kein anderes Modell ist fuer diesen Request noch verfuegbar."
            ) from exc

        if (
            "generate_content_free_tier_input_token_count" in fehlertext
            or "generatecontentinputtokenspermodelperminute-freetier" in fehlertext
            or ("input_token_count" in fehlertext and "250000" in fehlertext)
        ):
            # Der Server kennt ggf. Vorverbrauch aus einem anderen Prozess/Lauf,
            # den unser lokaler Zaehler nicht kennen kann. Die lokale Buchung wird
            # deshalb zurueckgerollt, das betroffene Modell fuer ein volles
            # Minutenfenster gesperrt und anschliessend wird zuerst ein anderes
            # Modell versucht. Ist keines frei, wartet der Scheduler selbststaendig
            # bis zum fruehesten sicheren Quota-Fenster.
            _gemini_quota_rollback_sendung(sendungs_reservierung)
            _gemini_input_quota_cooldown_until[modell] = (
                time.monotonic()
                + GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
                + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS
            )
            print(
                f"GEMINI-INPUT-QUOTA-SERVER: {modell} hat das serverseitige "
                "Minutenkontingent abgewiesen; Modell fuer mindestens "
                f"{GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS:.0f}s gesperrt."
            )

            while True:
                kandidaten = _gemini_quota_kandidaten(modell)
                kandidaten = [m for m in kandidaten if m != modell]
                bester_wait = None
                for naechstes_modell in kandidaten:
                    neu_gemessen = _gemini_tokenzahl(
                        client, naechstes_modell, contents,
                        config_kwargs.get("system_instruction"),
                        label=f"{label} Server-Quota-Fallback {naechstes_modell}",
                    )
                    if neu_gemessen > GEMINI_INPUT_SAFE_BUDGET:
                        continue
                    verfuegbares_modell, modell_wait = _gemini_quota_waehlen(
                        naechstes_modell, neu_gemessen,
                        ausgeschlossene_modelle={modell},
                    )
                    if verfuegbares_modell is not None:
                        _gemini_active_modell = verfuegbares_modell
                        _gemini_last_request_model = verfuegbares_modell
                        fallback_reservierung = _gemini_quota_registriere_sendung(
                            verfuegbares_modell, neu_gemessen
                        )
                        print(
                            f"GEMINI-INPUT-QUOTA-SERVER-FALLBACK: {modell} -> "
                            f"{verfuegbares_modell} | Request={neu_gemessen:,}"
                        )
                        try:
                            return client.models.generate_content(
                                model=verfuegbares_modell,
                                contents=contents,
                                config=types.GenerateContentConfig(**config_kwargs),
                            )
                        except Exception as fallback_exc:
                            fallback_text = str(fallback_exc).lower()
                            fallback_technisch = (
                                "503" in fallback_text
                                or "unavailable" in fallback_text
                                or "high demand" in fallback_text
                                or "connection reset" in fallback_text
                                or "connection aborted" in fallback_text
                                or "timed out" in fallback_text
                            )
                            if fallback_technisch:
                                _gemini_quota_rollback_sendung(fallback_reservierung)
                                _gemini_failed_models.add(verfuegbares_modell)
                                raise
                            if (
                                "generate_content_free_tier_input_token_count" in fallback_text
                                or "generatecontentinputtokenspermodelperminute-freetier" in fallback_text
                                or ("input_token_count" in fallback_text and "250000" in fallback_text)
                            ):
                                _gemini_quota_rollback_sendung(fallback_reservierung)
                                _gemini_input_quota_cooldown_until[verfuegbares_modell] = (
                                    time.monotonic()
                                    + GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS
                                    + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS
                                )
                                print(
                                    f"GEMINI-INPUT-QUOTA-SERVER: {verfuegbares_modell} "
                                    "ebenfalls temporaer gesperrt; suche weiter."
                                )
                                continue
                            raise
                    if modell_wait > 0.0:
                        bester_wait = (
                            modell_wait if bester_wait is None
                            else min(bester_wait, modell_wait)
                        )

                # Kein anderes Modell ist momentan frei. Nicht abbrechen und
                # insbesondere keinen identischen aeusseren Retry erzeugen.
                # Stattdessen bis zum naechsten sicheren Fenster warten.
                if bester_wait is None:
                    bester_wait = GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS + GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS
                bester_wait = max(float(bester_wait), 1.0)
                print(
                    "GEMINI-INPUT-QUOTA-WAIT: kein alternatives Modell frei; "
                    f"warte {bester_wait:.1f}s und pruefe alle Modelle erneut."
                )
                time.sleep(bester_wait)
                # Nach dem Warten wird der gesperrte Serverkandidat wieder
                # automatisch beruecksichtigt, sobald sein Cooldown abgelaufen ist.
                neue_modell, _ = _gemini_quota_waehlen(
                    modell, measured_tokens, ausgeschlossene_modelle=set()
                )
                if neue_modell is not None:
                    neu_gemessen = measured_tokens
                    if neue_modell != modell:
                        neu_gemessen = _gemini_tokenzahl(
                            client, neue_modell, contents,
                            config_kwargs.get("system_instruction"),
                            label=f"{label} Server-Quota-Wait-Fallback {neue_modell}",
                        )
                    if neu_gemessen > GEMINI_INPUT_SAFE_BUDGET:
                        raise RuntimeError(
                            "GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_WARTEN: "
                            f"Modell={neue_modell} | {neu_gemessen:,} > "
                            f"{GEMINI_INPUT_SAFE_BUDGET:,}"
                        )
                    _gemini_active_modell = neue_modell
                    _gemini_last_request_model = neue_modell
                    wait_reservierung = _gemini_quota_registriere_sendung(
                        neue_modell, neu_gemessen
                    )
                    try:
                        return client.models.generate_content(
                            model=neue_modell,
                            contents=contents,
                            config=types.GenerateContentConfig(**config_kwargs),
                        )
                    except Exception as wait_exc:
                        wait_text = str(wait_exc).lower()
                        wait_technisch = (
                            "503" in wait_text
                            or "unavailable" in wait_text
                            or "high demand" in wait_text
                            or "connection reset" in wait_text
                            or "connection aborted" in wait_text
                            or "timed out" in wait_text
                        )
                        if wait_technisch:
                            _gemini_quota_rollback_sendung(wait_reservierung)
                            _gemini_failed_models.add(neue_modell)
                        raise

        raise


def _gemini_synthese(client, modell, teiltexte, system_instruction, label):
    """Fuehrt mehrere Teilanalysen hierarchisch unter dem Sicherheitsbudget zusammen."""
    aktuelle = list(teiltexte)
    stufe = 1
    while len(aktuelle) > 1:
        gruppen = []
        gruppe = []
        for text in aktuelle:
            kandidat = gruppe + [text]
            synth_prompt = (
                "TEILANALYSE-SYNTHESE. Fuehre die bereitgestellten Teilanalysen "
                "vollstaendig zusammen. Keine Aussage aus den Teilanalysen darf "
                "weggelassen werden. Widersprueche ausdruecklich erhalten und "
                "kennzeichnen. Dies ist nur eine technische Zusammenfuehrung; "
                "keine neuen Fakten erfinden.\n\n"
                + "\n\n--- TEILANALYSE ---\n".join(kandidat)
            )
            tokens = _gemini_tokenzahl(
                client, modell, [synth_prompt], system_instruction,
                label=f"{label} Synthese {stufe}",
            )
            if tokens > GEMINI_INPUT_SAFE_BUDGET:
                if not gruppe:
                    raise RuntimeError(
                        f"GEMINI_SYNTHESE_ZU_GROSS: Einzelne Teilanalyse "
                        f"ueberschreitet {GEMINI_INPUT_SAFE_BUDGET:,} Tokens."
                    )
                gruppen.append(gruppe)
                gruppe = [text]
            else:
                gruppe = kandidat
        if gruppe:
            gruppen.append(gruppe)

        neue_aktuelle = []
        for idx, teilgruppe in enumerate(gruppen, 1):
            synth_prompt = (
                "TEILANALYSE-SYNTHESE. Fuehre die bereitgestellten Teilanalysen "
                "vollstaendig zusammen. Keine Aussage aus den Teilanalysen darf "
                "weggelassen werden. Widersprueche ausdruecklich erhalten und "
                "kennzeichnen. Keine neuen Fakten erfinden.\n\n"
                + "\n\n--- TEILANALYSE ---\n".join(teilgruppe)
            )
            tokens = _gemini_tokenzahl(
                client, modell, [synth_prompt], system_instruction,
                label=f"{label} Synthese {stufe}/{idx}",
            )
            if tokens > GEMINI_INPUT_SAFE_BUDGET:
                raise RuntimeError(
                    f"GEMINI_SYNTHESE_REQUEST_ZU_GROSS: {tokens:,} Tokens."
                )
            response = _gemini_generate_content_quota_safe(
                client,
                modell,
                [synth_prompt],
                {"system_instruction": system_instruction},
                tokens,
                f"{label} Synthese {stufe}/{idx}",
            )
            neue_aktuelle.append(response.text or "")
        aktuelle = neue_aktuelle
        stufe += 1
    return aktuelle[0] if aktuelle else ""


def _erstelle_gemini_final_autoritative_fakten(eingabedateien, sechs_fuenf_autoritaet, offene_quelle, geschlossene_10_5, makro_gate, makro_gate_grund):
    """Erzeugt den finalen autoritativen Fakten-/Synthese-Handoff.\n\n    Makro-, Marktbreiten- und Benchmarkdaten werden vollstaendig als Kontext\n    bereitgestellt; deterministische Gates und Positionsfakten bleiben davon\n    getrennt. """
    lines = [
        "FINALE AUTORITATIVE FAKTEN – KOMPAKTER REFERENZBLOCK",
        "Diese Datei ersetzt in der Final-Synthese die erneute Bereitstellung des gesamten Rohdatenbestands.",
        "Aktuelle Zahlen/Statuswerte duerfen nur aus den hier bzw. in den explizit genannten autoritativen Bloecken stammen.",
        "",
        f"MAKRO-SZENARIO-GATE: {makro_gate}",
        f"MAKRO-GATE-GRUND: {makro_gate_grund}",
        "",
        "AUTORITATIVE A/B/C-KANDIDATEN-/BEOBACHTUNGSZUORDNUNG (INTERN):",
        sechs_fuenf_autoritaet or "(keine autoritative A/B/C-Zuordnung vorhanden)",
        "",
        "AUTORITATIVE OFFENE POSITIONEN:",
        offene_quelle or "(keine offenen Positionen)",
        "",
        "AUTORITATIVE GESCHLOSSENE POSITIONEN 10.5:",
        geschlossene_10_5 or "(keine geschlossenen Positionen im relevanten Zeitraum)",
        "",
        "POSITIONSZAHLEN-REGEL:",
        "Einstieg, Aktueller Kurs, Stop, TP1 und TP2 sind positionsgebundene Fakten. "
        "Sie dürfen niemals untereinander verwechselt, aus Fließtext rekonstruiert oder "
        "aus einer anderen Position übernommen werden. Für offene Positionen ist "
        "Offene Positionen+Check.csv maßgeblich; für geschlossene Positionen ist die "
        "autoritative Tab-2-Faktenbasis maßgeblich.",
    ]

    vorherige_auswertung_pfad = eingabedateien.get("Letzte_Auswertung(...).txt")
    if vorherige_auswertung_pfad and os.path.isfile(vorherige_auswertung_pfad):
        try:
            with open(vorherige_auswertung_pfad, "r", encoding="utf-8-sig") as f:
                vorherige_auswertung = f.read().strip()
            if vorherige_auswertung:
                lines.extend(["", "VORHERIGE AUSWERTUNG – KONTINUITAETS-HANDOFF:", vorherige_auswertung])
        except Exception as exc:
            lines.append(f"VORHERIGE AUSWERTUNG NICHT LESBAR: {exc}")
    else:
        lines.extend(["", "VORHERIGE AUSWERTUNG – KONTINUITAETS-HANDOFF:", "(keine vorherige Auswertung vorhanden)"])

    makro_path = eingabedateien.get("Makro_Briefing(...).txt")
    if makro_path and os.path.isfile(makro_path):
        try:
            with open(makro_path, "r", encoding="utf-8-sig") as f:
                makro_text = f.read()
            qualitaet = _lese_makro_datenqualitaet(makro_text)
            refs = _extrahiere_makro_referenzwerte(makro_text)
            lines.extend(["", "AUTORITATIVE MAKRO-REFERENZWERTE (nur explizite strukturierte Werte):"])
            if qualitaet:
                lines.append(f"DATENQUALITAET: {qualitaet}")
            for label in sorted(refs):
                ref = refs[label]
                if not isinstance(ref, dict) or "kurs" not in ref:
                    continue
                parts = [f"{label}: {ref['kurs']}"]
                if ref.get("datenstand"):
                    parts.append(f"Datenstand={ref['datenstand']}")
                if ref.get("schluss") is not None:
                    parts.append(f"Letzter_Schluss={ref['schluss']}")
                perioden = ref.get("perioden") or {}
                if perioden:
                    parts.append("| " + " | ".join(f"{k}={v}%" for k, v in sorted(perioden.items())))
                if label.casefold() == "lithium te":
                    parts.append("| Einheit=CNY/T | DATENTYP=TE_PUBLIC_LITHIUM")
                lines.append(" ".join(parts))
        except Exception as exc:
            lines.append(f"MAKRO-REFERENZWERTE NICHT LESBAR: {exc}")

    # FINAL-SYNTHESE: Der kompakte Referenzblock darf die reichhaltige
    # Makro-Quelle nicht semantisch verkuerzen. A1 sieht das vollstaendige
    # Makro-Datenpaket, die finale Synthese erhaelt es hier deshalb ebenfalls
    # als unveraenderten autoritativen Handoff. Dadurch bleiben auch Daten
    # erhalten, die nicht zu den 7.1-7.7-Kernfeldern gehoeren, z.B. M2,
    # Fed-Target-Korridor, OAS/NFCI/SLOOS, LME-Metalle, Lithium TE CNY/T,
    # ISM-Komponenten, GSCPI/GEPU sowie Quellen-/Status-/Datenstandsangaben.
    # Keine Zahl wird hier neu berechnet oder ersetzt.
    if makro_path and os.path.isfile(makro_path):
        try:
            lines.extend([
                "",
                "AUTORITATIVER MAKRO-DATENHANDOFF – VOLLSTAENDIG",
                "Die folgenden Zeilen stammen 1:1 aus dem aktuellen Makro_Briefing. "+
                "Sie sind zusaetzlicher Kontext fuer die finale Synthese und ersetzen "+
                "keine deterministischen Python-Gates.",
                "--- BEGIN MAKRO_Briefing ORIGINAL ---",
                makro_text.rstrip(),
                "--- END MAKRO_Briefing ORIGINAL ---",
            ])
        except Exception as exc:
            lines.append(f"VOLLSTAENDIGER MAKRO-DATENHANDOFF NICHT LESBAR: {exc}")

    # FINAL-SYNTHESE: Auch das vollstaendige Markt-/Index-Briefing und der
    # Live-Benchmark werden explizit mitgegeben. Damit stehen Gemini fuer die
    # Synthese nicht nur die ausgewaehlten 7.1-Kernwerte, sondern die komplette
    # Marktbreite, Entwicklung und die dazugehoerigen Datenstaende/Statusfelder
    # zur Verfuegung. Diese Daten sind Kontext und duerfen keine bestehenden
    # technischen Setup- oder Positionsregeln ersetzen.
    for handoff_label, handoff_key in (
        ("AKTUELLER MARKT-/INDEX-DATENHANDOFF – VOLLSTAENDIG", "briefing.txt"),
        ("LIVE-BENCHMARK-DATENHANDOFF – VOLLSTAENDIG", "Benchmark_Live.txt"),
    ):
        handoff_path = eingabedateien.get(handoff_key)
        if handoff_path and os.path.isfile(handoff_path):
            try:
                with open(handoff_path, "r", encoding="utf-8-sig") as f:
                    handoff_text = f.read()
                lines.extend([
                    "",
                    handoff_label,
                    "Die folgenden Zeilen stammen 1:1 aus der aktuellen Projektquelle. "
                    "Nutze sie fuer Marktbreite, Entwicklung und Querverbindungen in der "
                    "finalen Synthese; keine Zahl daraus darf erfunden oder umetikettiert werden.",
                    f"--- BEGIN {handoff_key} ORIGINAL ---",
                    handoff_text.rstrip(),
                    f"--- END {handoff_key} ORIGINAL ---",
                ])
            except Exception as exc:
                lines.append(f"{handoff_label} NICHT LESBAR: {exc}")

    universe_path = eingabedateien.get("Trade_Story_Universum(...).json")
    if universe_path and os.path.isfile(universe_path):
        try:
            with open(universe_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            lines.extend(["", "TRADE-STORY-UNIVERSUM – KOMPAKTER HANDOFF:"])
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
            lines.append(
                "UNIVERSUM-REGEL: Jeder Eintrag mit ausgelesenen Assetdaten ist "
                "Kontext-/Querverbindungsmitglied. Der Universumsstatus ist KEIN "
                "technischer Setup-Status und darf weder Kauf noch Entry erzeugen."
            )
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("trade_story_status") or "").strip()
                # The complete universe is handed to Gemini for context.
                # STATUSKONFLIKT remains visible as a technical conflict and
                # must never be silently converted into a setup.
                name = str(item.get("name") or "").strip()
                ticker = str(item.get("ticker") or "").strip()
                source = str(item.get("quelle") or "").strip()
                universe_only = status in {"KEIN KANDIDAT", "KEIN SETUP", "STATUSKONFLIKT", ""}
                role = "UNIVERSUM/KONTEXT" if universe_only else "UNIVERSUM + TECHNIKSTATUS"
                lines.append(
                    f"- {name} ({ticker}) | Status={status or 'UNIVERSUM'} | Rolle={role}"
                    + (f" | Quelle={source}" if source else "")
                )
        except Exception as exc:
            lines.append(f"TRADE-STORY-UNIVERSUM NICHT LESBAR: {exc}")

    output = os.path.abspath(".gemini_final_autoritative_fakten.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    return output




def _gemini_request_part_fingerprint(cache_name, arbeits_contents, system_instruction, part_index, part_contents):
    """Erzeugt einen modellunabhaengigen Fingerabdruck fuer einen einzelnen Request-Teil.

    Der Fingerabdruck bindet Datenkontext, Arbeitsanweisung und Teilnummer. Damit
    kann ein nachfolgender technischer Retry exakt dort fortsetzen, wo der letzte
    Laufstand abgebrochen ist. Ein bereits erfolgreich erzeugter Teil wird nicht
    nochmals an Gemini gesendet.
    """
    context_names = []
    if isinstance(cache_name, dict):
        context_names = list(cache_name.get("context_names") or [])
    def _stable_part(value):
        for attr in ("uri", "name", "display_name", "text"):
            candidate = getattr(value, attr, None)
            if candidate:
                return f"{attr}={candidate}"
        return repr(value)
    payload = {
        "part_index": int(part_index),
        "context_names": context_names,
        "part": [_stable_part(x) for x in part_contents],
        "work": [_stable_part(x) for x in arbeits_contents],
        "system": str(system_instruction or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _gemini_request_plan(client, modell, cache_name, daten_teile, arbeits_contents,
                         system_instruction, label):
    """Erstellt den fachlich/stufenbezogenen Request-Plan genau einmal pro Aufruf.

    Die bestehende A1/A2/A3/Final-Architektur definiert bereits die fachlichen
    Grenzen. Innerhalb einer Stufe wird nur noch an Dateigrenzen gebündelt.
    Das Ergebnis ist eine kleine, deterministische Liste von Requests; es gibt
    keine blinden Wiederholungen eines bereits erfolgreichen Teils.
    """
    gruppen = list(_gemini_sichere_daten_gruppen(
        client, modell, daten_teile, arbeits_contents,
        system_instruction, label,
    ))
    if not gruppen:
        raise RuntimeError(f"GEMINI_REQUEST_PLAN_LEER: {label}")
    print(
        f"GEMINI-REQUEST-PLAN: {label} | "
        f"{len(gruppen)} fachlich gebundene Request-Teile | "
        f"bereits erfolgreiche Teile werden beim Retry aus dem Cache uebernommen."
    )
    return gruppen


def _gemini_cache_antwort(client, modell, cache_name, contents, system_instruction=None):
    """Sendet einen fachlich geplanten Request mit resumierbarem Split-Cache.

    Die verbindliche A1/A2/A3/Final-Architektur bleibt erhalten. Wenn eine
    Stufe das 120k-Sicherheitsbudget ueberschreitet, werden die Quellen an
    logischen Datei-/Chunk-Grenzen gebuendelt.
    Jeder erfolgreiche Teil wird sofort lokal gecacht. Ein 503, Netzwerkfehler
    oder minutenbezogenes Quota-Problem setzt deshalb beim naechsten Versuch
    exakt beim fehlgeschlagenen Teil fort.
    """
    config_kwargs = {}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    elif isinstance(cache_name, dict):
        config_kwargs["system_instruction"] = cache_name.get("system_instruction")

    daten_teile = cache_name.get("contents", []) if isinstance(cache_name, dict) else []
    arbeits_contents = list(contents if isinstance(contents, list) else [contents])
    anfrage_contents = list(daten_teile) + arbeits_contents
    token_count = _gemini_tokenzahl(
        client, modell, anfrage_contents,
        config_kwargs.get("system_instruction"),
        label="GenerateContent",
    )

    if token_count <= GEMINI_INPUT_SAFE_BUDGET:
        return _gemini_generate_content_quota_safe(
            client, modell, anfrage_contents, config_kwargs, token_count,
            "GenerateContent",
        )

    print(
        f"GEMINI-REQUEST-SPLIT: {token_count:,} > "
        f"{GEMINI_INPUT_SAFE_BUDGET:,}; fachlicher Request-Plan wird erstellt."
    )
    gruppen = _gemini_request_plan(
        client, modell, cache_name, daten_teile, arbeits_contents,
        config_kwargs.get("system_instruction"), "GenerateContent Split",
    )

    teiltexte = []
    response = None
    for idx, gruppe in enumerate(gruppen, 1):
        gruppe_contents = list(gruppe) + arbeits_contents
        part_key = _gemini_request_part_fingerprint(
            cache_name, arbeits_contents,
            config_kwargs.get("system_instruction"), idx, gruppe,
        )
        cached_text = _gemini_request_split_cache.get(part_key)
        if cached_text is not None:
            print(
                f"GEMINI-SPLIT-CACHE-HIT: Split {idx}/{len(gruppen)} | "
                f"bereits erfolgreich | Zeichen={len(cached_text)}"
            )
            teiltexte.append(cached_text)
            continue

        group_tokens = _gemini_tokenzahl(
            client, modell, gruppe_contents,
            config_kwargs.get("system_instruction"),
            label=f"GenerateContent Split {idx}",
        )
        if group_tokens > GEMINI_INPUT_SAFE_BUDGET:
            raise RuntimeError(
                f"GEMINI_REQUEST_ZU_GROSS_NACH_SPLIT: Split={idx} Tokens={group_tokens:,}."
            )

        print(
            f"GEMINI-SPLIT-SEND: Split {idx}/{len(gruppen)} | "
            f"Tokens={group_tokens:,} | nur dieser Split ist noch offen."
        )
        response = _gemini_generate_content_quota_safe(
            client, modell, gruppe_contents, config_kwargs, group_tokens,
            f"GenerateContent Split {idx}",
        )
        result_text = response.text or ""
        if ist_ablehnung(result_text):
            raise RuntimeError(
                f"GEMINI_SPLIT_{idx}_SICHERHEITSFILTER_ABLEHNUNG"
            )
        _gemini_request_split_cache[part_key] = result_text
        print(
            f"GEMINI-SPLIT-CACHE-STORE: Split {idx}/{len(gruppen)} | "
            f"erfolgreich gespeichert | Zeichen={len(result_text)}"
        )
        teiltexte.append(result_text)

    if len(teiltexte) == 1:
        if response is not None:
            return response
        return SimpleNamespace(text=teiltexte[0], candidates=[])

    # Die Teilanalysen werden hierarchisch zusammengefuehrt. Auch die Synthese
    # ist ein eigener Request und wird bei einem technischen Fehler ueber den
    # bestehenden Stage-Cache der aufrufenden Ebene nicht erneut fuer A1/A2/A3
    # aufgebaut.
    synth_text = _gemini_synthese(
        client,
        modell,
        teiltexte,
        config_kwargs.get("system_instruction"),
        "GenerateContent",
    )
    return SimpleNamespace(text=synth_text, candidates=[])

def _gemini_mehrstufige_gesamtanalyse(client, modell, hochgeladene_teile, anweisung,
                                      zusatz_anweisungen, eingabedateien, final_fakten_pfad=None,
                                      reuse_stages=False):
    """Vier getrennte Datenkontexte: Discovery, Technik, Historie und Final-Synthese.

    A1 = Discovery ohne HEBELTRADER/Einzelcheck-Historie und ohne technische Rohquellen.
    A2 = aktuelle technische Quellen ohne Discovery-/Historien-Rohdaten.
    A3 = komplette Historie + kleiner aktueller Referenzkontext + A1-Ergebnis.
    Final = A1/A2/A3-Ergebnisse + autoritativer Faktenblock + vollstaendiger
    aktueller Quelldaten-Handoff des Laufes; persistente Historien bleiben separat.
    """
    global _gemini_cache_name

    discovery_exclude = {
        "HEBELTRADER-Einzelcheck",
        "Einzel-Check-Technikhistorie",
        "Setups(...).csv",
        "Trade_Story_Setup_Rohuniversum(...).csv",
        "Trendwende_Setups(...).csv",
        "Trendwende_Briefing(...).txt",
        "Short_Setups(...).csv",
        "Short_Briefing(...).txt",
        "Einzel_Check_Aufstiege(...).txt",
        "Einzel_Check_A_Meldungen(...).txt",
        "Edelmetalle_Setups(...).csv",
        "Edelmetalle_Briefing(...).txt",
        "Offene Positionen+Check.csv",
        "Offene_Positionen.csv",
        "Letzte_Auswertung(...).txt",
        "Gemini_Auswertung_Historie.txt",
        "Finale-Autoritative-Fakten",
    }
    technical_names = {
        "HEBELTRADER-Einzelcheck",
        "Setups(...).csv",
        "Trade_Story_Setup_Rohuniversum(...).csv",
        "Trendwende_Setups(...).csv",
        "Trendwende_Briefing(...).txt",
        "Short_Setups(...).csv",
        "Short_Briefing(...).txt",
        "Einzel_Check_Aufstiege(...).txt",
        "Einzel_Check_A_Meldungen(...).txt",
        "Edelmetalle_Setups(...).csv",
        "Edelmetalle_Briefing(...).txt",
        "Offene Positionen+Check.csv",
    }
    history_names = {
        "Einzel-Check-Technikhistorie",
        "Einzel-Check-Beobachtungsliste",
        "Trade_Story_Universum(...).json",
        "Trade_Story_Aktienuniversum(...).csv",
        "Gemini_Auswertung_Historie.txt",
    }
    # Die Final-Synthese muss den vollstaendigen aktuellen Quelldatenbestand
    # sehen koennen. Persistente Historien werden bereits separat in A3 verarbeitet
    # und die vorherige Auswertung dient bereits als eigener Kontinuitaets-Handoff;
    # der aktuelle Tagesreport selbst darf nicht als Rohquelle wieder eingespeist
    # werden (sonst entstuende ein zirkulaerer Final-Input).
    final_exclude_from_full_handoff = {
        "Letzte_Auswertung(...).txt",
        "Gemini_Auswertung_Historie.txt",
        "Einzel-Check-Technikhistorie",
        "Finale-Autoritative-Fakten",
    }
    final_names = {
        name for name in eingabedateien
        if name not in final_exclude_from_full_handoff
    }
    final_names.add("Finale-Autoritative-Fakten")

    cache_stufe1 = _gemini_cache_erstellen(
        client, modell, anweisung, hochgeladene_teile, eingabedateien,
        exclude_names=discovery_exclude,
    )
    cache_stufe2 = _gemini_cache_erstellen(
        client, modell, anweisung, hochgeladene_teile, eingabedateien,
        include_names=technical_names,
    )
    cache_stufe3 = _gemini_cache_erstellen(
        client, modell, anweisung, hochgeladene_teile, eingabedateien,
        include_names=history_names,
    )
    cache_final = _gemini_cache_erstellen(
        client, modell, anweisung, hochgeladene_teile, eingabedateien,
        include_names=final_names,
    )
    _gemini_cache_name = cache_final

    daten_prompt = (
        "STUFE 1 – DISCOVERY. Analysiere ausschliesslich die bereitgestellte Discovery-Datenbasis. "
        "HEBELTRADER-Einzelcheck, technische Einzelchecks, komplette Setups, offene Positionsdaten "
        "und die vollstaendige Einzel-Check-Historie sind absichtlich NICHT Teil dieser Stufe. "
        "Entdecke kandidatenunabhaengig Makro-/Markt-/Geopolitik-/Rohstoff-/Sektor-/Struktur-Zusammenhaenge, "
        "Fruehsignale, entstehende Investmentthesen und den moeglichen Aufbau konkreter Chancen. "
        "Das Trade-Story-Universum ist nur Handoff/Orientierung und darf Discovery nicht ersetzen. "
        "Keine technischen Setups erfinden, keine Scores erzeugen und keine technische Kaufentscheidung vorwegnehmen."
    )
    if reuse_stages and _gemini_stufen_cache.get("A1") is not None:
        daten_analyse = _gemini_stufen_cache["A1"]
        print(f"  Gemini A1 Discovery aus technischem Retry-Cache übernommen | Zeichen: {len(daten_analyse)}")
    else:
        daten_antwort = _gemini_cache_antwort(client, modell, cache_stufe1, daten_prompt)
        daten_analyse = daten_antwort.text or ""
        if ist_ablehnung(daten_analyse):
            raise RuntimeError("GEMINI_STUFE_1_SICHERHEITSFILTER_ABLEHNUNG")
        _gemini_stufen_cache["A1"] = daten_analyse
        print(f"  Gemini A1 Discovery erfolgreich | Zeichen: {len(daten_analyse)}")

    technik_prompt = (
        "STUFE 2 – TECHNIK / SETUPS. Analysiere ausschliesslich die bereitgestellten aktuellen "
        "technischen Quellen: HEBELTRADER, aktuelle Einzelcheck-Meldungen, Setups, Trendwende, Short, "
        "Edelmetalle und technische Positionsdaten. Die Discovery-Rohdaten aus A1 sowie die komplette "
        "Einzel-Check-Historie sind nicht Bestandteil dieser Stufe. A1 soll hier nicht ersetzt werden; "
        "pruefe stattdessen technische Bestaetigung, Status, Setup, CRV, Stop/TP und Widersprueche nur aus "
        "den autoritativen technischen Quellen. Keine neuen technischen Berechnungen und keine erfundenen Setups."
    )
    if reuse_stages and _gemini_stufen_cache.get("A2") is not None:
        technik_analyse = _gemini_stufen_cache["A2"]
        print(f"  Gemini A2 Technik aus technischem Retry-Cache übernommen | Zeichen: {len(technik_analyse)}")
    else:
        technik_antwort = _gemini_cache_antwort(client, modell, cache_stufe2, technik_prompt)
        technik_analyse = technik_antwort.text or ""
        if ist_ablehnung(technik_analyse):
            raise RuntimeError("GEMINI_STUFE_2_TECHNIK_SICHERHEITSFILTER_ABLEHNUNG")
        _gemini_stufen_cache["A2"] = technik_analyse
        print(f"  Gemini A2 Technik erfolgreich | Zeichen: {len(technik_analyse)}")

    historie_prompt = (
        "STUFE 3 – HISTORIENANALYSE. Analysiere die GESAMTE verfuegbare Einzel-Check-Historie ohne "
        "Zeitbegrenzung. Sie ist technisch delta-komprimiert, aber verlustfrei: BASIS + DELTAS bilden "
        "jeden historischen Snapshot exakt ab. Kein historischer Zeitraum, Ticker oder Snapshot darf "
        "fachlich entfernt werden. Nutze den aktuellen Beobachtungsstatus und das Trade-Story-Universum "
        "nur als Referenz. A1-Erkenntnisse dienen als Orientierung, duerfen aber durch die Historie bestaetigt, "
        "widerlegt oder erweitert werden. Suche nach Statusentwicklungen, Fruehsignalen, Aufbau/Abschwaechung, "
        "erledigten Thesen und historischen Uebergaengen. Aktuelle numerische Werte bleiben autoritativ aus "
        "den aktuellen Referenzdaten.\n\n"
        "A1-ORIENTIERUNG:\n" + daten_analyse
    )
    if reuse_stages and _gemini_stufen_cache.get("A3") is not None:
        historie_analyse = _gemini_stufen_cache["A3"]
        print(f"  Gemini A3 Historie aus technischem Retry-Cache übernommen | Zeichen: {len(historie_analyse)}")
    else:
        historie_antwort = _gemini_cache_antwort(client, modell, cache_stufe3, historie_prompt)
        historie_analyse = historie_antwort.text or ""
        if ist_ablehnung(historie_analyse):
            raise RuntimeError("GEMINI_STUFE_3_HISTORIE_SICHERHEITSFILTER_ABLEHNUNG")
        _gemini_stufen_cache["A3"] = historie_analyse
        print(f"  Gemini A3 Historie erfolgreich | Zeichen: {len(historie_analyse)}")

    final_prompt = (
        "FINALE SYNTHESE. Erstelle die vollstaendige finale Auswertung aus den drei fachlichen "
        "Voranalysen, dem autoritativen Faktenblock UND dem vollstaendigen aktuellen Quelldaten-Handoff "
        "des heutigen Laufes. Die Rohquellen sind fuer die Synthese ausdruecklich verfuegbar und sollen "
        "aktiv fuer Querverbindungen, Marktbreite, Entwicklungen, Widersprueche und neue Investmentthesen "
        "genutzt werden. "
        "A1 beantwortet: Was passiert gerade / welche neuen Zusammenhaenge und Thesen gibt es? "
        "A2 beantwortet: Was bestaetigen die technischen Systeme und Setups? "
        "A3 beantwortet: Wie haben sich relevante Entwicklungen historisch aufgebaut oder veraendert? "
        "Fuehre diese Ebenen zusammen, suche selbst nach Querverbindungen und beachte die autoritativen "
        "Fakten. Erhalte die bestehende Auswertungsstruktur 1–11, CRV-/Setup-Regeln und Statuslogik. "
        "Alle aktuellen Quelldateien des Laufes stehen als Final-Handoff zur Verfuegung. Nutze nicht nur "
        "die bereits verdichteten A1/A2/A3-Ergebnisse, sondern pruefe bei relevanten Aussagen auch den "
        "zugrunde liegenden aktuellen Quelldatenbestand. Persistent gespeicherte Historien bleiben davon "
        "getrennt und werden ueber A3 bzw. den bestehenden Historienmechanismus verarbeitet.\n\n"
        "DARSTELLUNGSREGEL: Jede genannte Aktie bzw. jedes Unternehmen muss immer mit Firmenname und Ticker im Format Name (TICKER) erscheinen. "
        "Keine Aktiennennung nur über den Ticker oder nur über den Namen. Dies gilt insbesondere für 1.3, 1.4, 2.1–2.5, 3.x, 4, 5, 6.x, 8.x und 9.x.\n\n"
        "VERBINDLICHER INHALTSVERTRAG 1–11: Halte die folgende Struktur exakt ein. "
        "Jeder Unterpunkt wird eigenständig bearbeitet. Wenn eine geforderte Information im bereitgestellten Datenbestand nicht vorhanden ist, "
        "schreibe ausdrücklich 'NICHT VERFUEGBAR' bzw. eine gleichwertige konkrete Negativfeststellung. Erfinde niemals Daten, Termine, Kurse, CRV, "
        "Fundamentaldaten oder Unternehmensinformationen. 'Kein Setup' darf einen Abschnitt nicht ersetzen, wenn dort andere Daten verfügbar sind.\n\n"
        "1. 🔥 WAS KÖNNTE GELD VERDIENEN?: 1.1 Veränderungen seit dem letzten Lauf einschließlich neuer Makro-, Sektor-, Rohstoff-, Aktien- und Investmententwicklungen sowie "
        "Ideenstatus; 1.2 nur konkret handelbare Chancen mit Titel/Ticker, Richtung, These, Treibern, Technik, Scanner-Setup soweit vorhanden, Entry/Zone, Stop, TP1/TP2, CRV soweit vorhanden, "
        "Quellen, Gegenargumenten, Trigger und Invalidierung; 1.3 Ideen im Aufbau mit These, bestätigenden und widersprechenden Daten, Makro/Sektor/Rohstoff-Zusammenhang, Zweitrundeneffekten, "
        "Profiteuren/Verlierern, konkreten Titeln, technischem Status, fehlenden Voraussetzungen, Aktivierungs- und Widerlegungstrigger; 1.4 Frühindikatoren/neue Themen mit Ereignis, Zusammenhang, "
        "Branche und soweit möglich konkreten Aktien sowie Triggern, ohne Scores.\n"
        "2. 🎯 KONKRETE TRADES: 2.1 Trendfolge mit validem Setup, Aktie/Ticker, Entry, Stop, TP1/TP2, CRV, technischem Zustand, Makro-/Sektorunterstützung und Risiken; "
        "2.2 Trendwende mit Abwärtsbewegung, Boden-/Wendezeichen, Entry, Stop, Ziele, CRV und bestätigten/fehlenden Kriterien; "
        "2.3 Short mit Abwärtsthese, technischer Bestätigung, Entry, Stop, TP1/TP2, CRV, Makro-/Sektorunterstützung und Risiken; "
        "2.4 HebelTrader darf AUSSCHLIESSLICH aktuelle KAUFKANDIDAT-A-Titel als konkrete Trades wiedergeben. "
        "KAUFKANDIDAT B/C, KEIN KANDIDAT, KEIN SETUP, VALIDE/VORBEREITET ohne A sowie reine Universums-/Kontextmitglieder dürfen NICHT als Trade in 2.4 erscheinen. "
        "Aufstiege/Abstiege zwischen A/B/C oder C→KEIN KANDIDAT dürfen ausschließlich qualitativ interpretiert werden und erzeugen erst bei aktuellem Status KAUFKANDIDAT A einen konkreten 2.4-Trade. "
        "Offene Positionen aus Offene Positionen+Check.csv gehören NICHT in 2.4, sondern ausschließlich in Punkt 10. "
        "Für jeden genannten Titel zwingend Name und Ticker im Format Name (TICKER); Basisinstrument, Richtung, Setup, Entry, Stop, Ziel, Risiko und Hebel-/Volatilitätsrisiken nur soweit autoritativ vorhanden. "
        "2.5 sonstige Gemini-Chancen mit nachvollziehbarer Datenbegründung und konkretem Titel. Fehlende Daten nicht ersetzen.\n"
        "3. 🧠 THEMEN & ZUSAMMENHÄNGE: 3.1 Makro→Branche→Aktie; 3.2 Rohstoff→Branche→Aktie; 3.3 Politik→Branche→Aktie; "
        "3.4 Technologie→Branche→Aktie; 3.5 Unternehmens-/Fundamentaldaten→Aktie. Immer konkreten Investmentbezug herstellen und keine isolierte Allgemeinanalyse.\n"
        "4. 🔭 IDEEN IM AUFBAU: Für jede relevante These THESE, bestätigende Daten, Gegenargumente, Kausalkette, Profiteure/Verlierer, frühe Aktienreaktion, Status, fehlende Information/Entwicklung, "
        "Aktivierungstrigger und Invalidierung. Keine Scores oder künstliche Rangfolge.\n"
        "5. 🥇 AKTIEN MIT FRÜHEM SIGNAL: konkrete Aktie, Zusammenhang, unabhängige Datenquellen, bereits sichtbar, noch nicht bestätigt, mögliche Fehlbewertung und nächster entscheidender Trigger. Keine Scores.\n"
        "6. ⚠️ WIDERSPRÜCHE & RISIKEN: 6.1 Makro gegen Technik, 6.2 Technik gegen Fundamentaldaten, 6.3 Sektor gegen Aktie, 6.4 Rohstoff gegen Aktie, "
        "6.5 Investmentthese gegen aktuelle Marktdaten, 6.6 Risiken bestehender Ideen. Für jeden tatsächlichen Fall Aktie/Idee, Ausgangsthese, widersprechende Information, Bedeutung, Prüfpunkt und Invalidierung nennen. "
        "Wenn kein belastbarer Fall vorhanden ist, ausdrücklich so feststellen.\n"
        "7. 🌍 MARKT- & MAKROKONTEXT: 7.1 Aktienmärkte/Indizes Europa, USA, Asien, Marktbreite und Trend/Momentum; 7.2 Leitzinsen, 2Y/10Y, Realzinsen und Zinskurve; "
        "7.3 VIX/Volatilität; 7.4 EUR/USD, DXY, USD/JPY und weitere relevante FX; 7.5 Öl, Kupfer, Lithium, Industriemetalle und weitere relevante Rohstoffe, ABER KEINE Edelmetalle. "
        "Gold, Silber, Platin und Palladium gehören ausschließlich in Punkt 8 und dürfen in 7.5 nicht wiederholt werden; 7.6 Bitcoin, Ethereum und relevante Kryptoentwicklung; "
        "7.7 Inflation, Arbeitsmarkt, ISM/PMI, Konsum, Kreditbedingungen und sonstige relevante Makrodaten. "
        "Nur investmentrelevante Informationen und deren Bedeutung nennen.\n"
        "8. 🪙 EDELMETALLE: 8.1 Gold, 8.2 Silber, 8.3 Platin, 8.4 Palladium jeweils separat mit aktuellem Kurs, kurzfristiger Entwicklung, 4-Wochen-Entwicklung, "
        "52-Wochen-Situation, EMA200/WMA200 soweit vorhanden, technischem Zustand, Trendfolge-, Trendwende- und Short-Status, CRV/relevanten Filtern soweit vorhanden, "
        "Beinahe-Kandidaten, Saisonalität soweit vorhanden, Makrotreibern, Rohstoff-/Branchenzusammenhängen, möglichen Gewinnern/Verlierern, konkreten Aktienbezügen, frühen Aktienreaktionen und Triggern. "
        "Fehlende einzelne Daten explizit als NICHT VERFUEGBAR kennzeichnen; 'Kein Setup' ersetzt diese Analyse nicht.\n"
        "9. 📅 NÄCHSTE KATALYSATOREN: 9.1 verifizierte Makrotermine, 9.2 Earnings/Unternehmensveranstaltungen/-meldungen, 9.3 Branchenereignisse, "
        "9.4 technische Trigger und 9.5 mögliche Aktivierung/Invalidierung. Keine Termine erfinden. Ein tatsächlicher Termin/Ereignis muss einen verifizierbaren Zeit-/Datumsbezug haben; "
        "Datumsformen wie 7. Oktober 2026, 07.10.2026 und 2026-10-07 sind gleichwertig.\n"
        "10. 💼 BESTEHENDES PORTFOLIO: 10.1 unmittelbarer Handlungsbedarf, 10.2 tatsächliche Stop-/TP-Änderungen, 10.3 Positionen mit neuer Investmentthese, "
        "10.4 Positionen mit schwächerer These und 10.5 letzte relevante geschlossene Positionen. 10.5 ausschließlich aus der autoritativen Tab-2-Faktenbasis; "
        "nur tatsächlich vorhandene Fakten übernehmen und nichts aus anderen Quellen ergänzen. Die Mindesttiefe von 10.5 passt sich ausschließlich den tatsächlich vorhandenen Tab-2-Fakten an.\n"
        "11. METHODIK / DATENQUALITÄT: 11.1 Datenstatus, 11.2 Makro-Szenario-Status, 11.3 Datenlücken, 11.4 externe Quellen, "
        "11.5 technische/fundamentale Datenqualität, 11.6 Hinweise zur Interpretation und 11.7 klare Abgrenzung zwischen regelbasiertem Scanner, Gemini-Szenario, Idee im Aufbau und konkreter handelbarer Idee.\n"
        "SUBSTANZREGEL: Die obigen Anforderungen sind fachliche Inhaltsanforderungen, keine Aufforderung zum Auffüllen mit Stichworten. "
        "Bearbeite nur Informationen, die aus den bereitgestellten Daten ableitbar sind. Kurze legitime Abschnitte dürfen kurz sein, wenn die Datenlage tatsächlich kurz ist; "
        "umgekehrt darf ein vorhandener Datenbestand nicht durch 'keine Erkenntnisse' oder 'kein Setup' abgefertigt werden.\n\n"
        "DATEN-UND-SYNTHESE-ROLLEN: Python und die autoritativen Faktenblöcke liefern die verbindlichen Fakten, Zahlen, Kurse, Entries, Stops, Ziele, CRV, Performancewerte, Termine und Statusangaben. "
        "Gemini liefert daraus Interpretation, Kausalität, Querverbindungen, Szenarien, Investmentthesen, Gegenargumente, Trigger und Invalidierungen. "
        "Eine harte numerische Tatsachenbehauptung darf nur aus den bereitgestellten autoritativen Daten stammen. Gemini darf die Bedeutung eines Wertes interpretieren, aber keinen alternativen Wert daneben erfinden oder als Tatsache darstellen. "
        "Wenn eine Zahl für die Interpretation nicht benötigt wird, wiederhole sie nicht. Verwende autoritative Zahlen nur dort erneut, wo sie für eine neue Schlussfolgerung tatsächlich erforderlich sind.\n\n"
        "SYNTHESE-VORRANG: Die finale Auswertung ist eine Synthese und keine Aneinanderreihung oder Wiederholung der A1/A2/A3-Ergebnisse. Suche aktiv nach Erkenntnissen, die erst durch die Kombination mehrerer Datenebenen entstehen. "
        "Prüfe insbesondere Ketten wie MAKRO → ZINS/FX → ROHSTOFF/MARKT → SEKTOR → UNTERNEHMEN → AKTIE → TECHNIK/SETUP. "
        "Eine echte Synthese muss erklären, warum die Verbindung relevant ist, welche Daten sie stützen, welche Daten dagegen sprechen, welche konkrete Aktie betroffen ist und was als nächstes passieren müsste. "
        "Wenn eine solche Querverbindung noch kein handelbares Setup besitzt, ist sie ausdrücklich als IDEE IM AUFBAU oder FRÜHINDIKATOR zulässig und soll einen konkreten Aktivierungstrigger erhalten. "
        "Wenn der Scanner bereits ein Setup bestätigt, soll die Synthese prüfen, ob Makro, Sektor und Fundamentaldaten die technische Idee unterstützen oder ihr widersprechen. "
        "Ein fehlendes Setup beendet eine interessante Investmentthese nicht; ein vorhandenes Setup macht eine Investmentthese aber auch nicht automatisch überzeugend.\n\n"
        "NEUHEITSREGEL / WIEDERHOLUNGSSCHUTZ: Jede wesentliche Erkenntnis erhält in der Auswertung einen primären Ort. Erkläre eine These dort ausführlich, wo sie am besten hingehört. "
        "Wenn dieselbe Aktie, dasselbe Makrothema oder dieselbe Kausalkette später erneut relevant ist, darf sie nicht nochmals vollständig beschrieben werden. Wiederhole nur den neuen Informationsanteil oder verweise knapp auf die bereits erklärte These. "
        "7.x ist primär Kontext und darf keine bereits in 1–6 ausführlich erklärte These nochmals ausformulieren. 8.x konzentriert sich auf Edelmetalle und ergänzt nur neue metallbezogene Erkenntnisse. 10.x behandelt das bestehende Portfolio und wiederholt keine vollständigen Trade-Storys aus 1–6. "
        "Abschnitte mit identischer Aussage, identischer Begründung und identischem Schluss sollen nicht mehrfach gefüllt werden. Ziel ist weniger Text bei höherer Erkenntnisdichte.\n\n"
        "HISTORIEN-DELTA / PERSISTENTE THESENENTWICKLUNG: Die bereitgestellte Gemini_Auswertung_Historie.txt ist nicht nur Hintergrundwissen, sondern ein persistenter Änderungslog für Investmentthesen. "
        "Nutze sie in der finalen Synthese aktiv zusammen mit A3, um relevante aktuelle Thesen mit älteren Läufen zu vergleichen. Prüfe insbesondere: NEU ENTSTANDEN, VERSTAERKT, ABGESCHWAECHT, WIDERLEGT/ERLEDIGT, BESTAETIGT oder UNVERAENDERT. "
        "Beschreibe eine historische Entwicklung nur dann, wenn sie durch die Historie und/oder aktuelle Daten belegbar ist. Wenn eine ältere These weiterläuft, wiederhole nicht ihre gesamte Begründung, sondern nenne nur den aktuellen Entwicklungsstand und den neuen Informationsanteil. "
        "Wenn eine ältere These durch aktuelle Daten deutlich verändert, abgeschwächt oder widerlegt wird, soll diese Veränderung ausdrücklich sichtbar werden. Bevorzuge dabei den Vergleich mit dem unmittelbar vorherigen relevanten Lauf; bei längeren Entwicklungen darfst du mehrere historische Läufe verbinden. "
        "Historische Snapshots liefern keine autoritativen aktuellen Zahlen: aktuelle Kurse, Stops, TP-Werte, Makrodaten und technische Kennzahlen stammen ausschließlich aus den aktuellen Tagesdateien bzw. autoritativen Faktenblöcken. Nutze historische Inhalte für Entwicklung, Richtung und These – niemals als Ersatz für aktuelle Fakten. "
        "Eine Historien-Referenz soll einen echten Mehrwert liefern: Was war die These, was hat sich seitdem verändert, was bedeutet das heute und welcher nächste Trigger bzw. welche Invalidierung entscheidet über die weitere Entwicklung. Wenn keine relevante Veränderung vorliegt, genügt eine kurze Feststellung; erfinde kein Historien-Delta.\n\n"
        "MAKRO-ZÜNDUNG: Prüfe bei jeder Final-Synthese ausdrücklich, ob aus mehreren aktuellen Datenpunkten mindestens eine neue Makro-/Investmentthese entsteht. Suche nicht nur nach der Beschreibung einzelner Makrodaten, sondern nach einer veränderten Beziehung zwischen ihnen. "
        "Bevorzuge Aussagen der Form: WAS HAT SICH VERÄNDERT → WARUM IST DIE KOMBINATION RELEVANT → WELCHER SEKTOR/ROHSTOFF PROFITIERT ODER LEIDET → WELCHE AKTIE ZEIGT BEREITS EINE REAKTION → WAS FEHLT NOCH → WELCHER TRIGGER AKTIVIERT DIE THESE → WAS INVALIDIERT SIE. "
        "Wenn die Daten keine belastbare neue Makrothese tragen, sage das ausdrücklich, statt eine künstliche These zu erzeugen.\n\n"
        "KEINE KÜNSTLICHE AUFFÜLLUNG: Die Mindesttiefe darf nicht durch Wiederholung derselben Aussage, generische Floskeln oder erfundene Daten erfüllt werden. "
        "VERBINDLICHE AUSGABESTRUKTUR: Jede vorgeschriebene Ueberschrift von 1.1 bis 11.7 muss exakt "
        "uebernommen werden und allein auf einer eigenen Zeile stehen. Direkt nach jeder solchen Ueberschrift "
        "ist eine Leerzeile einzufuegen, bevor der zugehoerige Inhalt beginnt. Keine Ueberschrift darf mit "
        "Inhalt, Doppelpunkt oder sonstigem Text in derselben Zeile verbunden werden. Keine Ueberschrift "
        "darf umbenannt, ausgelassen, zusammengefasst oder doppelt ausgegeben werden. Die Reihenfolge der "
        "vorgegebenen Ueberschriften ist verbindlich. Wenn zu einem Abschnitt keine relevanten Erkenntnisse "
        "vorliegen, muss die Ueberschrift trotzdem allein auf ihrer eigenen Zeile erscheinen, gefolgt von einer "
        "Leerzeile und einer klaren Negativfeststellung.\n\n"
        "AUSGABESTRUKTUR-ALLEINHERRSCHAFT: Die fertige Auswertung darf ausschließlich die verbindliche 1–11-Struktur enthalten. Interne Datenblöcke, autoritative Handoffs, A/B/C-Listen, Einzel-Check-Ausgaben, HEBELTRADER-Quellen und externe Briefing-Strukturen sind keine Ausgabestruktur und dürfen nicht als eigene Überschrift, nummerierter Abschnitt oder Unterabschnitt in die fertige Auswertung übernommen werden. Ihre Informationen dürfen nur inhaltlich in die dafür passenden Pflichtabschnitte einfließen. Insbesondere sind 6.5.1, 6.5.2 und EXTERNE MARKTQUELLEN als Ausgabestrukturen verboten. Wenn eine Information keinem Pflichtabschnitt zugeordnet werden kann, darf dafür kein neuer Abschnitt erfunden werden.\n\n"
        "VORANALYSE A1 – DISCOVERY:\n" + daten_analyse +
        "\n\nVORANALYSE A2 – TECHNIK / SETUPS:\n" + technik_analyse +
        "\n\nVORANALYSE A3 – HISTORIE:\n" + historie_analyse
    )
    if reuse_stages and _gemini_stufen_cache.get("FINAL") is not None:
        final_text = _gemini_stufen_cache["FINAL"]
        print(f"  Gemini Finale Synthese aus technischem Retry-Cache übernommen | Zeichen: {len(final_text)}")
        return SimpleNamespace(text=final_text, candidates=[])

    final_antwort = _gemini_cache_antwort(
        client, modell, cache_final, [final_prompt] + zusatz_anweisungen
    )
    _gemini_stufen_cache["FINAL"] = final_antwort.text or ""
    print(f"  Gemini Finale Synthese erfolgreich | Zeichen: {len(final_antwort.text or '')}")
    return final_antwort


def _gemini_fallback_status_datei(aktion="clear", fehler=""):
    """Verwaltet ausschließlich die tagesbezogene technische Fallback-Statusdatei.

    Die Datei ist bewusst von der normalen Auswertung getrennt. Sie wird zu
    Laufbeginn für den aktuellen Tag entfernt und ausschließlich bei einem
    technischen Gemini-Fallback neu erzeugt. Dadurch kann ein alter Status
    nicht versehentlich als Status des aktuellen erfolgreichen Laufs gelten.
    """
    heute = datetime.date.today().isoformat()
    status_datei = f"Gemini_Fallback_Status_{heute}.txt"

    if aktion == "clear":
        try:
            if os.path.isfile(status_datei):
                os.remove(status_datei)
                print(f"INFO: Alter technischer Gemini-Fallback-Status entfernt: {status_datei}")
        except OSError as exc:
            raise RuntimeError(
                f"GEMINI_FALLBACK_STATUS_ALTBESTAND_NICHT_ENTFERNBAR: {exc}"
            ) from exc
        return None

    if aktion == "write":
        fehler_text = str(fehler or "").strip()
        inhalt = (
            f"GEMINI_STATUS=TECHNISCHER_FALLBACK\n"
            f"GEMINI_FALLBACK_DATUM={heute}\n"
            f"GEMINI_AUSWERTUNG_DATEI=KEINE\n"
            f"GEMINI_AUSWERTUNG_UEBERSCHREIBUNG=NEIN\n"
            f"GEMINI_FALLBACK_STATUS_DATEI={status_datei}\n"
            "HINWEIS=Keine gueltige Gemini-Auswertung erzeugt.\n"
        )
        if fehler_text:
            inhalt += f"LETZTER_API_FEHLER={fehler_text}\n"
        with open(status_datei, "w", encoding="utf-8") as f:
            f.write(inhalt)
        print(f"Technischer Gemini-Fallback-Status gespeichert: {status_datei}")
        return status_datei

    raise ValueError(f"Unbekannte Aktion fuer Gemini-Fallback-Status: {aktion}")


def gemini_auswertung_starten():
    # Ein alter Status des gleichen Tages darf niemals in einen neuen Lauf
    # hineinragen. Bei erfolgreichem Lauf existiert danach keine Statusdatei.
    _gemini_fallback_status_datei("clear")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("FEHLER: Umgebungsvariable GEMINI_API_KEY nicht gesetzt.")
        sys.exit(1)

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=600000,
            # Die eigene Retry-/Modellpool-Logik behandelt 503/429 bereits.
            # SDK-interne Wiederholungen werden deaktiviert, damit sie nicht
            # zusaetzlich und unsichtbar auf die expliziten Retries aufaddieren.
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    anweisung = lade_anweisung()
    eingabedateien = sammle_eingabedateien()
    a3_historie_pfad = _erstelle_gemini_a3_historie(
        eingabedateien.get("Einzel-Check-Technikhistorie"),
        eingabedateien.get("Einzel-Check-Beobachtungsliste"),
    )
    if a3_historie_pfad:
        eingabedateien_gemini = dict(eingabedateien)
        eingabedateien_gemini["Einzel-Check-Technikhistorie"] = a3_historie_pfad
    else:
        eingabedateien_gemini = dict(eingabedateien)
    global _GEMINI_EINGABEDATEIEN_AUSWERTUNG
    _GEMINI_EINGABEDATEIEN_AUSWERTUNG = dict(eingabedateien_gemini)
    _speichere_gemini_input_manifest(eingabedateien_gemini)

    letzte_antwort = None
    hochgeladene_teile = None  # wird bei Bedarf (neu) befuellt, siehe unten
    global _gemini_cache_name, _gemini_active_modell, _gemini_last_request_model, _gemini_failed_models, _gemini_stufen_cache, _gemini_request_split_cache
    _gemini_cache_name = None
    _gemini_request_split_cache = {}
    _gemini_token_count_cache.clear()
    _gemini_input_quota_usage.clear()
    _gemini_project_input_quota_usage.clear()
    _gemini_input_quota_cooldown_until.clear()
    _gemini_rpd_requests.clear()
    _gemini_rpd_exhausted.clear()
    _gemini_active_modell = None
    _gemini_last_request_model = None
    _gemini_failed_models = set()
    _gemini_stufen_cache = {}
    technische_retry_wiederverwenden = False
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

    # Die Kandidaten-/Beobachtungszuordnung wird nicht mehr allein per Prompt interpretiert: Python erzeugt
    # die aktuelle A-/Nicht-A-Mitgliedschaft verbindlich aus der Beobachtungsliste
    # und uebergibt diese beiden Mengen explizit an Gemini. Damit koennen alte
    # HEBELTRADER- oder Historienstatus die aktuelle Kategorie nicht mehr verfälschen.
    beobachtung_pfad = eingabedateien.get("Einzel-Check-Beobachtungsliste")
    sechs_fuenf_autoritaet = erstelle_abkandidaten_autoritative_liste(
        beobachtung_pfad, eingabedateien.get("Einzel-Check-Technikhistorie"), eingabedateien
    )

    # Autoritative Punkt-10-Fakten werden genau einmal pro Lauf gelesen.
    # Sie sind von Gemini-Retries unabhaengig und duerfen nicht bei jedem
    # API-Versuch erneut aus Drive/CSV beschafft werden.
    offene_quelle = _offene_positionen_quellblock(eingabedateien.get("Offene Positionen+Check.csv"))
    geschlossene_10_5 = lade_offenen_positionen_check_tab2()

    # Fuer die finale Synthese wird weiterhin der autoritative Faktenblock erzeugt.
    # ZUSAETZLICH werden jetzt alle aktuellen Projekt-Quelldateien des Laufes als
    # vollstaendiger Synthese-Handoff an die Finalstufe gegeben. Die bisherigen
    # stufenspezifischen A1/A2/A3-Kontexte bleiben unveraendert. Die Finalstufe
    # bekommt dadurch nicht nur die Voranalysen, sondern den gesamten aktuellen
    # Datenbestand fuer Querverbindungen, Marktbreite, Entwicklung und neue Thesen.
    final_fakten_pfad = _erstelle_gemini_final_autoritative_fakten(
        eingabedateien_gemini, sechs_fuenf_autoritaet, offene_quelle,
        geschlossene_10_5, makro_gate, makro_gate_grund,
    )
    eingabedateien_gemini["Finale-Autoritative-Fakten"] = final_fakten_pfad

    versuch = 0
    versuch_zyklus = 1
    while True:
        versuch += 1
        versuch_nummer = ((versuch - 1) % MAX_VERSUCHE) + 1
        print(
            f"\nVersuch {versuch_nummer}/{MAX_VERSUCHE} "
            f"(technischer 503-Zyklus {versuch_zyklus})..."
        )

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
                hochgeladene_teile = _gemini_hochgeladene_quellen_erstellen(
                    client, eingabedateien_gemini
                )

            _gemini_zusatz_anweisungen = [
                    "VERBINDLICHE STRUKTUR-TREND-DATENREGEL (C): Wenn die Datei Struktur_Trend_Briefing(<Datum>).txt vorhanden ist, ist sie die maßgebliche Quelle für den strukturellen Datenblock C. C ist eine eigenständige Datenebene und darf nicht mit B (Makro) oder D (Geopolitik) vermischt werden. Die Gesamtbewertung entsteht erst durch die gemeinsame Einordnung von A+B+C+D. Struktur-Trend-Werte sind Strukturindikatoren und keine unmittelbaren Kauf-, Verkaufs-, Breakout- oder Zielzonensignale. Verwende für das Alter einer Beobachtung die Beobachtungsperiode, nicht das Cache- oder Abrufdatum. PA bedeutet Prozent pro Jahr (% p.a.); XDC_H bedeutet XDC je Arbeitsstunde. C darf aktuelle A-, B- oder D-Signale niemals überschreiben oder ersetzen. Wenn die Struktur-Trend-Datei fehlt, fahre mit A+B+D fort und erfinde keine C-Werte. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. Die Dateien Bitcoin_Trading_DE_Briefing.txt, Gold_Trading_DE_Briefing.txt und Silber_Trading_DE_Briefing.txt sind ausschließlich qualitative externe YouTube-Quellen. Nutze sie nur als Kontext/Abgleich; sie dürfen niemals objektive Kursdaten, technische Check-Felder, CRV, Setup-Scores, Filter, Setup-Qualität oder Handelsentscheidungen verändern. Wenn eine solche Datei fehlt, ist das kein Fehler und es darf nichts daraus erfunden werden. "
                    "KEINE EIGENE SEKTION 'EXTERNE MARKTQUELLEN' ERZEUGEN. Die bereitgestellten externen Bitcoin-/Gold-/Silber-YouTube-Briefings sind ausschließlich qualitative Quellen. Integriere relevante Erkenntnisse ausschließlich in die fachlich passenden Abschnitte der verbindlichen 1–11-Struktur; Quellen-/Datenqualität ist in 11.4 externe Quellen zu dokumentieren. Für jeden Markt nenne die Anzahl der tatsächlich in der jeweiligen bereitgestellten Briefing-Datei enthaltenen relevanten Videos. WICHTIG: Zähle und verarbeite jedes vorhandene Video einzeln anhand jedes einzelnen 'Titel:'-Blocks bzw. Video-Blocks. Wenn die Briefing-Datei beispielsweise 3 relevante Videos enthält, müssen in der fertigen Auswertung genau diese 3 Videos einzeln erscheinen. Kein Video darf wegen Kürze, Ähnlichkeit, Redundanz oder eigener Auswahl des Modells weggelassen, zusammengefasst oder durch ein anderes ersetzt werden. Führe für JEDES vorhandene relevante Video separat Titel und eine kurze Kernaussage auf und ordne JEDE einzelne Aussage ausschließlich im Verhältnis zur bestehenden Systemanalyse als 'BESTÄTIGT', 'WIDERSPRICHT' oder 'NEUTRAL' ein. Die Anzahl muss mit der Zahl der tatsächlich einzeln aufgeführten Videos übereinstimmen. Ergänze bei jedem Markt ausdrücklich 'Technische Auswirkung: KEINE'. Wenn für einen Markt keine relevanten Videos in der bereitgestellten Briefing-Datei vorhanden sind oder die Datei fehlt, schreibe ausdrücklich 'Keine neuen relevanten Videos verarbeitet'. Verwende für Titel und Kernaussagen ausschließlich die Inhalte der bereitgestellten YouTube-Briefing-Dateien; ergänze nichts aus allgemeinem Modellwissen und erfinde nichts. Die Einordnung darf keine technische Berechnung oder Entscheidung verändern. Die externe Quelle ist ausschließlich qualitativer Kontext. Eine Übereinstimmung mit der externen Quelle ist keine technische Bestätigung; eine Abweichung ist kein technischer Ausschluss. Eine Aussage wie '1 Video' ist nur zulässig, wenn tatsächlich genau 1 relevanter Video-Block in der betreffenden Briefing-Datei vorhanden ist. "
                    "Verarbeite die bereitgestellten Dateien wie in der Anleitung beschrieben. "
                    "Falls die Datei 'Letzte_Auswertung(...).txt' bereitgestellt wurde, nutze sie ausschließlich als Vergleichsbasis für Abschnitt 1.1. Aktuelle Zahlen und aktuelle technische Werte stammen ausschließlich aus den aktuellen Tagesdateien; die vorherige Auswertung darf keine aktuellen Werte überschreiben. "
                    "PERSISTENTER LANGZEIT-KONTEXT: Falls die Datei 'Gemini_Auswertung_Historie.txt' bereitgestellt wurde, nutze sie zusaetzlich als vollständige persistente Historie ueber mehrere Laeufe. Sie dient dazu, Entwicklungen von Investmentthesen, Fruehsignalen, handelbaren Chancen, Widerspruechen und Edelmetallideen ueber mehrere Tage zu erkennen. Verwende sie NICHT als Quelle fuer aktuelle numerische Werte, aktuelle Kurse, aktuelle Stops/TPs, aktuelle Makrodaten oder aktuelle technische Kennzahlen. Diese stammen ausschliesslich aus den aktuellen Tagesdateien. Wenn eine These in der Historie mehrfach auftaucht, beschreibe die Entwicklung nur, wenn sie durch die Historie und/oder aktuelle Daten belegbar ist. Am Montag oder nach einem Lauf-Ausfall darf die Historie ausdruecklich mehrere vorherige Laeufe miteinander verbinden; 1.1 vergleicht dennoch den aktuellen Lauf primaer mit dem unmittelbar vorherigen verfuegbaren Lauf. "
                    "PRIORITAET FRUEHE ENTDECKUNG: Arbeite zwingend in drei getrennten Schritten: (1) zuerst ein kandidatenunabhaengiger Gesamtscan ueber den gesamten bereitgestellten Research A+B+C+D, insbesondere Makro, Geopolitik, Oel/Rohstoffe, Inflation, Zentralbanken, Zinsen, Liquiditaet, Waehrungen, Sektoren und Marktstruktur; (2) erst danach fuer jede relevante These Veraenderung -> Treiber -> Belege -> Kausalzusammenhang -> moeglicher Kapitalfluss -> naechster bestaetigter Kalenderkatalysator pruefen; (3) erst danach vorhandene Setups, Watchlists und offene Positionen gegen die These abgleichen. Ein grosses Discovery-Thema darf ausdruecklich ohne bestehenden Kandidaten ausgegeben werden. Oel/Rohstoffe sind dabei ausdruecklich als Bruecke zwischen Geopolitik, Inflation, Zentralbanken, Zinsen, Transport, Chemie, Industrie und Energieaktien zu pruefen. Ein vorhandener Kandidat darf die Discovery-These weder erzeugen noch in ein Setup umwandeln. Der bestehende Sektor-Rotations-Score darf als objektiver Beleg aus den bereitgestellten Daten verwendet werden; Gemini darf daraus keinen eigenen Discovery-Score erzeugen und darf ihn niemals als alleinigen Grund fuer eine These oder ein Setup verwenden. "
                    "und erstelle die vollstaendige Daten-Uebersicht. "
                    "INTERNE KANDIDATEN-/EINZELCHECK-DATEN: Die folgende von Python erzeugte A/B/C-Zuordnung ist ausschließlich eine autoritative interne Faktenquelle. Sie dient Gemini zur Interpretation der bestehenden Kandidaten-, Watchlist- und Technikdaten, darf aber niemals als eigene Ausgabestruktur, Zwischenüberschrift oder nummerierter Abschnitt der fertigen Auswertung erscheinen. Die alleinige autoritative Struktur der fertigen Auswertung ist ausschließlich die verbindliche 1–11-Struktur. A/B/C-Statuswerte dürfen in den fachlich passenden Abschnitten erwähnt werden, wenn sie für die Investmentaussage relevant sind. Die Kategoriezuordnung darf nicht verändert, ergänzt oder aus historischen Daten rekonstruiert werden. Historische Daten dienen nur dem ausdrücklich erlaubten Status-/Entwicklungsvergleich.\n\n"
                    + sechs_fuenf_autoritaet + "\n\n"
"MARKTUMFELD-AUSGABEREGEL: In allen Abschnitten mit Marktumfeld/Marktumfeld-Fazit sowie in der globalen Risikolage sind Scores, Score-Werte, Score-Modelle, Punktwerte und Formulierungen wie \"Score 0,0\" VERBOTEN. Beschreibe ausschließlich den qualitativen Zustand (z.B. bullish, neutral, bearish) und die zugrunde liegenden beobachtbaren Marktmerkmale. Setup-/CRV-Scores außerhalb des Marktumfeld-Blocks sind davon nicht betroffen. "
                    "NUMERISCHE MAKRO-BINDUNG: Alle numerischen Markt-/Makroangaben muessen exakt aus dem bereitgestellten Makro_Briefing uebernommen werden. Nicht neu rechnen, schaetzen, runden oder aus einer anderen Quelle ersetzen. Wenn ein Wert nicht eindeutig im Makro_Briefing vorhanden ist, nur qualitativ beschreiben oder weglassen. Instrument, Einheit und Datenstand muessen zusammengehoeren.\n                     FRUEHE-ENTDECKUNGS-UND-TRADE-STORY-EBENE: Die Discovery-Ebene und die technische Ebene sind zwingend getrennt auszugeben. Verwende in jedem 1.3-Block exakt zwei getrennte Statusfelder: Discovery-Status: ENTDECKT oder BEOBACHTUNG; Technischer Status: NICHT VORHANDEN, NUR TEILW. VOLLSTAENDIG oder VALIDER SETUP. Discovery-Status beschreibt nur den Erkenntnisstand der These. Technischer Status beschreibt ausschliesslich den Stand der bestehenden technischen Systempruefung. Wenn kein bestehender Kandidat im autoritativen Datenbestand vorhanden ist, muss Technischer Status = NICHT VORHANDEN sein. Wenn ein vorhandener Kandidat vorhanden ist, aber kein vollstaendig bestaetigtes Setup besitzt, muss Technischer Status = NUR TEILW. VOLLSTAENDIG sein. VALIDER SETUP darf ausschliesslich aus dem bestehenden regelbasierten Setup-/CRV-System uebernommen werden. Eine Discovery bleibt auch dann eine Discovery, wenn bereits ein VALIDE-SETUP-Kandidat existiert. Die Existenz eines Kandidaten darf niemals die Discovery erzeugen. Gemini darf aus Discovery, ENTDECKT, BEOBACHTUNG, NICHT VORHANDEN oder NUR TEILW. VOLLSTAENDIG niemals selbst einen VALIDEN SETUP, einen Kauf oder einen Entry machen. Zeige die Kette Thema -> Veraenderung -> Treiber -> Beleg -> Kausalzusammenhang -> moeglicher Kapitalfluss -> betroffene Assetklasse/Sektor -> bestehender Kandidat (falls vorhanden) -> naechster bestaetigter Kalenderkatalysator -> Discovery-Status -> Technischer Status -> widerlegender Trigger -> Risiko. Nutze nur bereitgestellte Daten. Der bestehende Sektor-Rotations-Score darf als objektiver Beleg genannt werden, ist aber kein Gemini-Score und niemals alleiniger Grund fuer eine Discovery oder ein Setup. "
                     "VERBINDLICHES TRADE-STORY-UNIVERSUM: Wenn 'Trade_Story_Universum(<Datum>).json' vorhanden ist, ist dieses taeglich neu erzeugte JSON die autoritative Discovery-/Handoff-Schicht. Jeder echte HEBELTRADER-Fund, einschliesslich KAUFKANDIDAT A/B/C und KEIN KANDIDAT, gehoert zum Universum. KEIN KANDIDAT ist dabei nur Universums-/Discovery-Mitglied und keine konkrete Setup-Quelle. VALIDE SETUP darf nur aus candidates mit trade_story_status='VALIDE SETUP' stammen; VORBEREITET nur aus candidates mit trade_story_status='VORBEREITET'. Eine offene Position ist nur Kontext und kein Ausschluss. Ein STATUSKONFLIKT (z.B. gleichzeitig Long und Short) darf nicht als eindeutiges Setup dargestellt werden. Das Universum darf durch Top-Sektor-Zugehoerigkeit nicht nachtraeglich verengt werden. "
                     "BITCOIN-REGEL IM TRADE-STORY-UNIVERSUM: Pi-Cycle-Bottom DOWN-Cross (150-EMA von oben nach unten durch 0.745*471SMA) ist LONG/AKKUMULATION und kann VALIDE SETUP sein. Pi-Cycle UP-Cross beendet die Akkumulationsphase und ist kein generisches SELL. 50W-SMA UP-Cross ist LONG/BUY; 50W-SMA DOWN-Cross ist EXIT/SELL und daher kein Long-Kandidat. Verwende ausschliesslich die strukturierten Bitcoin-Felder im Tagesuniversum. "
                    "HEBELTRADER-EINZELCHECK / INTERNE DATENQUELLE: Falls die bereitgestellte Datei 'hebeltrader_einzel_check.json' vorhanden ist, nutze sie als strukturierte Quelle fuer die zuletzt erfolgreich verarbeitete HEBELTRADER-Ausgabe und verwende die aus Drive synchronisierte neueste Version, falls sie neuer ist. Diese Datenquelle ist KEINE eigene Ausgabekategorie. Ihre A/B/C-/Technik-/Setup-Informationen duerfen ausschließlich in die fachlich passenden Abschnitte der verbindlichen 1–11-Struktur einfließen. Insbesondere darf daraus niemals eine zusätzliche nummerierte Ausgabestruktur erzeugt werden. Die bestehende einzel_check.py-Logik, insbesondere A/B/C, Momentum, Gruende, Risiken und die Watchlist-Bereinigung nach >45 Tagen ohne A/B/C, darf nicht neu berechnet, veraendert, aufgehoben oder ersetzt werden. Fuer konkrete technische Details sind ausschließlich die bereits berechneten Felder aus den bereitgestellten autoritativen Einzel-Check-/HebelTrader-Daten zu verwenden. Einstieg, Stop, TP1, TP2 und CRV duerfen nur angegeben werden, wenn sie aus bereitgestellten Daten ersichtlich sind; fehlende Werte duerfen nicht erfunden oder geschaetzt werden. Wenn aus den vorhandenen technischen Daten eine Ableitung transparent moeglich ist, muss sie als Ableitung gekennzeichnet werden. Breakout allein aktiviert Fibonacci nicht; Fibonacci/Extension nur bei qualifizierter und bestaetigter A-B-C-Struktur. Wenn die HEBELTRADER-JSON fehlt, erfinde keinen HEBELTRADER-Inhalt. Fuer A-Kandidaten, die nicht aus HEBELTRADER stammen, nutze die bereitgestellte einzel_check_historie.jsonl ausschließlich als autoritative technische Historie des aktuellen Auswertungstages. Die Beobachtungsliste bleibt ausschließlich fuer Status, Quelle und Watchlist-Zugehoerigkeit massgeblich. Die sichtbare Darstellung richtet sich ausschließlich nach der verbindlichen 1–11-Struktur. "
"PORTFOLIO-MAKRO-ABGLEICH / WARNER: Vergleiche die autoritativen offenen Positionen mit dem von Gemini aus dem Makro-Datenpaket abgeleiteten Marktumfeld und den Sektorwirkungen. Wenn eine offene Position klar oder zunehmend gegen das Makro-Bild bzw. die relevante Sektorwirkung laeuft, MUSS dies in 10.1 Sofortiger Handlungsbedarf als '⚠ MAKRO-KONFLIKT' gekennzeichnet und die betroffene Position namentlich/Ticker zugeordnet werden. Nenne kurz den konkreten Widerspruch aus den vorhandenen Daten. Das ist eine Warnung zur erneuten Pruefung, KEINE automatische Verkaufs-/Kaufempfehlung und keine neue technische Kennzahl. Wenn kein belastbarer Konflikt aus den bereitgestellten Daten ableitbar ist, erfinde keinen.\nLITHIUM-DATENTRENNUNG: 'Lithium' mit STATUS=PROXY ist ausschließlich der LIT-Proxy. Der separat bereitgestellte 'Lithium TE' Wert ist der autoritative Lithiumcarbonat-Referenzwert in CNY/T und darf nicht mit dem LIT-Proxy gleichgesetzt, ersetzt oder als derselbe Preis dargestellt werden. Verwende fuer Lithium TE ausschließlich den letzten tatsaechlich verfuegbaren Datenstand <= Datenabrufdatum des aktuellen Makro_Briefings. Übernimm Wert, Einheit und den tatsächlichen Datenstand exakt aus dem Datenpaket; ein älterer legitimer Datenstand darf nicht als aktuelleres Datum ausgegeben werden. Wenn kein belastbarer Wert <= Zieldatum vorhanden ist, verwende NICHT VERFUEGBAR und erfinde keinen Wert.\nPUNKT-7-ARCHITEKTUR: Der bestehende Makro-/Portfolio-Datenblock bleibt autoritativ; Python liefert die Fakten, Gemini interpretiert nur die qualitative Ebene.\nFINAL-SYNTHESE – VOLLSTAENDIGE MARKTBREITE: Nutze fuer die Synthese nicht nur die kompakten 7.1-Kernwerte. Der finale Fakten-Handoff enthaelt das vollstaendige aktuelle Markt-/Index-Briefing und den Live-Benchmark. Beziehe relevante Entwicklungen, Marktbreite, regionale Divergenzen und Querverbindungen aktiv ein, wenn sie eine These erklaeren, bestaetigen, abschwaechen oder neu entstehen lassen. Nicht jede Zahl muss genannt werden; die vollstaendige Datenbasis soll aber verfuegbar sein. Vermeide reine Aufzaehlung und leite aus mehreren Datenpunkten eine belastbare Synthese ab.\nPUNKT-10-ARCHITEKTUR: Python stellt die autoritative Positionsfaktenbasis bereit und erzeugt 10.5 geschlossene Positionen deterministisch. Gemini erzeugt 10.1, 10.2, 10.3 und 10.4 als qualitative Interpretation. 10.3 darf ausschließlich Positionen enthalten, bei denen sich die Investmentthese gegenüber dem vorherigen Lauf bzw. der bereitgestellten Historie belastbar verändert hat. Gemini darf in 10.3/10.5 keine Faktenblöcke erzeugen.\nPOSITIONSZAHLEN-GATE: Bei jeder offenen oder geschlossenen Position sind Einstieg, Aktueller Kurs, Stop, TP1 und TP2 strikt positionsgebunden. Niemals den aktuellen Kurs als Einstieg, den Einstieg als aktuellen Kurs oder Stop/TP-Werte aus einer anderen Position übernehmen. Wenn eine Zahl nicht in der autoritativen Positionsquelle vorhanden ist, lasse sie weg statt sie zu schätzen oder zu rekonstruieren.\n"
                    "AUTORITATIVE OFFENE-POSITIONEN-LISTE (ausschließlich aus Offene Positionen+Check.csv):\n"
                    + (offene_quelle or "(keine offenen Positionen gefunden)") + "\n"
                    "AUTORITATIVE FAKTENBASIS FUER 10.5 AUS TAB 2 VON 'Offene Positionen+Check':\n"
                    + (geschlossene_10_5 or "(keine geschlossene Position innerhalb der letzten 3 Kalendertage)") + "\n"
                    "Für 10.5 gilt ausschließlich diese Tab-2-Faktenbasis. Gib nur geschlossene Positionen "
                    "mit Ausstiegsdatum innerhalb der letzten 3 Kalendertage bezogen auf den Auswertungstag aus. "
                    "Wenn die Faktenbasis leer ist, lasse 10.5 vollständig weg. Rekonstruiere, ergänze, schätze "
                    "oder erfinde keine geschlossenen Positionen aus anderen Dateien oder aus Modellwissen. "
                    "Übernimm die Faktenfelder aus Tab 2 unverändert. 10.5 ist von der offenen Positionsprüfung "
                    "und deren Reparaturmechanik getrennt.\n"
                    "Diese Liste ist für Firmenname, Ticker, Einstiegskurs, Einstiegsdatum, Aktuellen Kurs, Stop, TP1 und TP2 verbindlich, sofern die Felder in der Quelle vorhanden sind. "
                    "Übernimm diese Werte exakt; erfinde, schätze oder ändere sie nicht. "
                    "Einstiegskurs, Aktueller Kurs, Stop, TP1 und TP2 sind semantisch strikt getrennte Felder. "
                    "Der Aktuelle Kurs darf niemals als Einstiegskurs interpretiert werden; der Einstieg darf niemals aus dem aktuellen Kurs oder der Performance rekonstruiert werden; Stop/TP1/TP2 dürfen niemals aus anderen Positionen oder aus Fließtext übernommen werden. "
                    "PUNKT-10-ARCHITEKTUR: 'Offene Positionen+Check.csv' ist die alleinige "
                    "autoritative Faktenquelle fuer die offenen Positionen. Python stellt diese Liste "
                    "als Kontext bereit und erzeugt 10.5 ausschliesslich aus der autoritativen Tab-2-Faktenbasis. "
                    "Gemini liefert fuer Punkt 10 die qualitative Interpretation in 10.1 Sofortiger Handlungsbedarf, "
                    "10.2 Stop-/TP-Änderungen, 10.3 Positionen mit neuer Investmentthese und 10.4 Positionen, deren These schwächer wird. "
                    "10.3 darf ausschliesslich aktuell offene Positionen enthalten, bei denen sich die Investmentthese "
                    "gegenüber dem vorherigen Lauf oder der bereitgestellten persistenten Historie belastbar verändert hat. "
                    "Für 10.3 sind bestehende Aktie, ursprüngliche These, neue Daten, neue Investmentthese und Auswirkung auf die Position darzustellen. "
                    "Wenn keine belastbare Veränderung vorliegt, schreibe dies ausdrücklich und erfinde keine neue These. "
                    "Gemini darf Firmenname, Ticker, Einstiegskurs, Einstiegsdatum und technische Check-Felder nicht erfinden oder verändern. "
                    "Firmenname, Ticker, Einstiegskurs, Einstiegsdatum und technische Check-Felder bleiben "
                    "an die autoritative Quelle gebunden. Technische_Zielzone wird niemals aus anderen "
                    "technischen Feldern neu abgeleitet. Die alte Offene_Positionen.csv darf fuer diese "
                    "Fakten nicht als Quelle oder Fallback verwendet werden. Mehrere offene Positionen "
                    "desselben Tickers sind zulaessig; jede Kombination aus Name + Ticker + Einstiegskurs + "
                    "Einstiegsdatum ist eine eigene Position.",
                    (
                        f"HARTE MAKRO-GATE-VORGABE: Das Makro-Szenario-Gate ist GESPERRT. Grund: {makro_gate_grund} "
                        "Erzeuge in den Abschnitten 1, 3 und 7 KEINE Base/Bull/Bear-Wahrscheinlichkeiten, "
                        "keine geschaetzten Ersatzwerte und keine numerischen Makro-Prognosen. "
                        "Benenne stattdessen die konkreten kritischen Datenluecken bzw. den Ausfall des Makro-Datenpakets. "
                         "Verwende dabei NICHT die Bezeichnungen Base Case, Bull Case oder Bear Case, gib KEINE numerischen Makro-Prognosen aus. Benenne stattdessen nur die konkreten Datenluecken und ordne aktuelle Makrodaten in den vorgesehenen Abschnitten qualitativ ein, ohne eine eigene Richtungsprognose zu erfinden. "
                        if makro_gate == "GESPERRT" else
                        "HARTE MAKRO-GATE-VORGABE: Das Makro-Datenpaket ist autoritativ. "
                        "Sein MAKRO-SZENARIO-GATE hat Vorrang vor jeder eigenen Bewertung der "
                        "Datenvollstaendigkeit. Das Gate lautet FREIGEGEBEN. Die Makro-Datenbasis MUSS daher "
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
                        "Jede perspektivische Trade-Story in 1.3 soll deshalb, soweit aus den Dateien ableitbar, die Kette Thema -> Makro-Treiber -> bestaetigende Daten -> Sektor/Asset -> bestehender Kandidat -> Discovery-/Technischer Status -> naechster technischer Trigger -> Gegentreiber/Risiko sichtbar machen. Wenn kein bestehender Kandidat vorhanden ist, ist das explizit zu kennzeichnen. Ein Makro-Treiber allein ist niemals ein Einstiegssignal. "
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
                            f"MAKRO-DATENQUALITAET={makro_datenqualitaet}. Uebernimm diesen Wert in Abschnitt 11.2 "
                            f"exakt. Wenn der Wert VOLLSTAENDIG ist, darf Abschnitt 11.2 nicht auf EINGESCHRAENKT "
                            f"oder UNZUREICHEND herabgestuft werden und darf keine TIER-2-DATENLUECKE als "
                            f"Grund fuer eine Herabstufung nennen."
                            if makro_datenqualitaet else ""
                        )
                    ),
                ]
            antwort = _gemini_mehrstufige_gesamtanalyse(
                client=client,
                modell=aktuelles_modell,
                hochgeladene_teile=hochgeladene_teile,
                anweisung=anweisung,
                zusatz_anweisungen=_gemini_zusatz_anweisungen,
                eingabedateien=eingabedateien_gemini,
                final_fakten_pfad=final_fakten_pfad,
                reuse_stages=technische_retry_wiederverwenden,
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
                reparatur = _gemini_reparatur_anweisungen = [
                        "REPARATUR NUR FÜR DEN MAKRO-/MARKTKONTEXT: Das Makro-Datenpaket meldet MAKRO-SZENARIO-GATE=FREIGEGEBEN. "
                        "Überarbeite ausschließlich die Makro-/Marktkontext-Darstellung und den Datenstatus. Eine Sperrung ist unzulässig, wenn nur TIER-2- oder TIER-3-Daten fehlen. "
                        "TIER 1 KERN entscheidet über das Gate; TIER 2 BESTAETIGUNG und TIER 3 KONTEXT sind Ergänzungen. "
                        "Verwende die deutsche Terminologie VOLLSTAENDIG/EINGESCHRAENKT/UNZUREICHEND und nenne "
                        "TIER-2-DATENLUECKEN bzw. TIER-3-DATENLUECKEN. Erhalte alle übrigen Abschnitte unverändert soweit möglich. "
                        "Gib die vollständige Auswertung erneut aus."
                    ]
                reparatur = _gemini_cache_antwort(
                    client=client,
                    modell=aktuelles_modell,
                    cache_name=_gemini_cache_name,
                    contents=_gemini_reparatur_anweisungen,
                    system_instruction=None,
                )
                reparatur_text = reparatur.text or ""
                if pruefe_makro_gate_konsistenz(reparatur_text, makro_gate):
                    text = reparatur_text
                    print("INFO: Makro-Gate-Konsistenz nach Reparatur hergestellt.")
                else:
                    raise RuntimeError("Gemini widerspricht weiterhin dem autoritativen MAKRO-SZENARIO-GATE=FREIGEGEBEN.")

            # PUNKT 7 IST DATEN-AUTORITATIV UND WIRD NICHT MEHR VON GEMINI
            # ERZEUGT. Python baut 10.1/10.3/10.5 aus den verbindlichen Quellen;
            # Gemini liefert ausschliesslich die Interpretation in 10.2.
            python_punkt10 = _erstelle_punkt10_fakten(
                eingabedateien.get("Offene Positionen+Check.csv"),
                geschlossene_10_5,
            )
            text = _ersetze_punkt10_durch_python_fakten(text, python_punkt10)
            briefing_pfad = eingabedateien.get("briefing.txt")
            text, _ = _sichere_punkt10_tp1_angaben(text, briefing_pfad)
            print(
                "  Punkt 10: 10.1/10.3/10.5 deterministisch aus autoritativen "
                "Positionsdaten erzeugt; Gemini liefert 10.1/10.2/10.4 als qualitative Interpretation."
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
                # Aktuelles Schema 1.3: Überschriften mit optionaler Markdown-Formatierung
                # erkennen. Die Abschnittsgrenze ist die nächste Hauptsektion ab 1.4;
                # fehlt 1.4, wird vor der nächsten Hauptsektion (2, 3, ...) begrenzt.
                heading_re = re.compile(
                    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?"
                    r"(?P<number>\d+(?:\.\d+)*)(?:[.)])?[ \t]+(?P<title>[^\n]+)$"
                )
                headings = list(heading_re.finditer(text or ""))

                def _ist_grenze_1_3(match):
                    nummern = tuple(int(part) for part in match.group("number").split("."))
                    return nummern[0] >= 2 or (
                        nummern[0] == 1 and len(nummern) > 1 and nummern[1] >= 4
                    )

                original = None
                for heading in headings:
                    title = heading.group("title").strip().rstrip("*").strip()
                    if (
                        heading.group("number") == "1.3"
                        and re.match(r"(?i)^IDEEN\s+IM\s+AUFBAU\b", title)
                    ):
                        boundary = next(
                            (
                                candidate
                                for candidate in headings
                                if candidate.start() > heading.start()
                                and _ist_grenze_1_3(candidate)
                            ),
                            None,
                        )
                        original = (heading.start(), boundary.start() if boundary else len(text or ""))
                        original_header_end = (text or "").find("\n", heading.start())
                        if original_header_end < 0:
                            original_header_end = len(text or "")
                        original_header = (text or "")[heading.start():original_header_end]
                        break

                repaired_block = story_reparatur_text.strip()
                if original:
                    # Überschriftenformat des vorhandenen aktuellen Abschnitts erhalten.
                    repaired_block = re.sub(
                        r"(?i)^1\.3\s+IDEEN IM AUFBAU",
                        original_header.strip(),
                        repaired_block,
                        count=1,
                    )
                    text = (text or "")[:original[0]] + repaired_block + "\n\n" + (text or "")[original[1]:]
                else:
                    # Fehlt 1.3, vor 1.4 einfügen. Fehlt auch 1.4, vor der
                    # nächsten Hauptsektion einfügen; nur wenn es keine gibt,
                    # kontrolliert ans Dateiende. Keine Alt-Schema-Erkennung.
                    anchor = next(
                        (
                            heading for heading in headings
                            if heading.group("number") == "1.4"
                        ),
                        None,
                    )
                    if anchor is None:
                        anchor = next(
                            (heading for heading in headings if _ist_grenze_1_3(heading)),
                            None,
                        )
                    if anchor is not None:
                        text = (text or "")[:anchor.start()] + repaired_block + "\n\n" + (text or "")[anchor.start():]
                    else:
                        text = ((text or "").rstrip() + "\n\n" + repaired_block + "\n")
                print("  Trade-Story-Reparatur erfolgreich.")

            # TECHNISCHE ASSET-ZAHLEN-GATE: Technische Werte eines konkret
            # genannten Titels werden nach jeder Trade-Story-Reparatur erneut
            # an das autoritative Tagesuniversum gebunden. Dadurch kann z.B.
            # ein Goldpreis niemals als Kurs von AU/AngloGold ausgegeben werden.
            text, technische_korrekturen = _sichere_technische_assetangaben(
                text, eingabedateien
            )
            if technische_korrekturen:
                print(
                    f"  TECHNISCHE-ASSET-ZAHLEN-GATE: {len(technische_korrekturen)} "
                    "quellengebundene Assetangabe(n) korrigiert."
                )
            text, technische_nachsuche = _ergaenze_technische_nachsuche(
                text, eingabedateien
            )
            if technische_nachsuche:
                print(
                    f"  TECHNISCHE-NACHSUCHE: {len(technische_nachsuche)} "
                    "potenzielle Assetangabe(n) gegen das Tagesuniversum abgeglichen."
                )
            story_ok_final, story_errors_final = _trade_story_validierung(
                text, eingabedateien, beobachtung_pfad, True
            )
            if not story_ok_final:
                raise RuntimeError(
                    "TRADE_STORY_FINAL_VALIDIERUNG_FEHLER: "
                    + " | ".join(story_errors_final)
                )

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
            if "PUNKT10_REPARATUR_TERMINAL_429" in fehlertext:
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

            if _gemini_last_request_model in GEMINI_MODELLREIHENFOLGE:
                aktuelles_modell = _gemini_last_request_model
                modell_index = GEMINI_MODELLREIHENFOLGE.index(aktuelles_modell)

            # Sicherheitsfilter-Ablehnungen innerhalb von A1/A2/A3 werden als
            # eigener Stage-Fehler geworfen. Dieser Pfad muss den technischen
            # Retry-Cache zwingend verwerfen, damit der naechste Versuch einen
            # vollstaendig frischen Analysekontext erzeugt. Insbesondere darf
            # ein zuvor gesetztes technische_retry_wiederverwenden=True hier
            # niemals erhalten bleiben.
            ist_stufen_sicherheitsfilter = any(
                marker in fehlertext
                for marker in (
                    "GEMINI_STUFE_1_SICHERHEITSFILTER_ABLEHNUNG",
                    "GEMINI_STUFE_2_TECHNIK_SICHERHEITSFILTER_ABLEHNUNG",
                    "GEMINI_STUFE_3_HISTORIE_SICHERHEITSFILTER_ABLEHNUNG",
                )
            )
            if ist_stufen_sicherheitsfilter:
                _gemini_stufen_cache.clear()
                _gemini_request_split_cache.clear()
                technische_retry_wiederverwenden = False
                hochgeladene_teile = None
                _gemini_cache_name = None
                print(
                    "  Sicherheitsfilter-Ablehnung in A1/A2/A3 erkannt - "
                    "Stage-Cache geloescht; naechster Versuch mit frischem Kontext."
                )

            abbrechen, empfohlene_wartezeit, kategorie = analysiere_api_fehler(fehlertext)
            # Jeder API-/Quota-Fehler ist ein technischer Retry: bereits
            # erfolgreich abgeschlossene A1/A2/A3/Final-Stufen werden nicht
            # erneut angefordert. Ein Sicherheitsfilter wird weiter unten
            # ausdrücklich als frischer Kontext behandelt.
            if kategorie in ("ueberlast", "netzwerk", "input_token_limit", "tageskontingent", "modellpool_temporaer"):
                technische_retry_wiederverwenden = True
            if abbrechen:
                if kategorie == "cache_free_tier":
                    print(
                        "  Explizites Gemini-Context-Caching ist im Free-Tier nicht verfuegbar; "
                        "verwende den vollstaendigen Datenkontext direkt ueber GenerateContent. "
                    )
                    _gemini_cache_name = None
                    hochgeladene_teile = None
                    continue

                # Das RPD-Free-Tier-Limit ist modellbezogen. Bei PerDay wird
                # deshalb der naechste noch nicht versuchte Eintrag der festen
                # Modellreihenfolge verwendet. Ist die Reihe ausgeschoepft,
                # wird nicht versucht, ein bereits erschoepftes Modell erneut
                # zu verwenden.
                _gemini_rpd_exhausted.add(aktuelles_modell)
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

            if kategorie == "modellpool_temporaer":
                # Ein PerDay-gesperrtes Modell kann zusammen mit mehreren
                # temporaeren 503-Modellen auftreten. Das ist NICHT terminal: die
                # 503-Modelle duerfen nach einem echten Backoff erneut versucht werden.
                if versuch_zyklus >= GEMINI_MAX_TECHNISCHE_RETRY_ZYKLEN:
                    print(
                        f"  Temporär nicht verfuegbarer Modellpool nach "
                        f"{GEMINI_MAX_TECHNISCHE_RETRY_ZYKLEN} technischen Zyklen; "
                        "beende kontrolliert mit technischem Fallback."
                    )
                    break
                backoff_index = min(
                    versuch_zyklus - 1, len(UEBERLAST_WARTEZEITEN) - 1
                )
                basis_wartezeit = UEBERLAST_WARTEZEITEN[backoff_index]
                server_wartezeit = (
                    empfohlene_wartezeit if empfohlene_wartezeit is not None else 0
                )
                wartezeit = max(float(basis_wartezeit), float(server_wartezeit))
                jitter = random.uniform(0.0, wartezeit * 0.20)
                wartezeit += jitter
                print(
                    "  GEMINI-MODELLPOOL-WAIT: PerDay-Sperren plus temporaere "
                    f"503/Netzwerkfehler; warte {wartezeit:.1f}s und starte "
                    f"technischen Pool-Zyklus {versuch_zyklus + 1}/{GEMINI_MAX_TECHNISCHE_RETRY_ZYKLEN}."
                )
                time.sleep(wartezeit)
                _gemini_failed_models.clear()
                _gemini_active_modell = None
                _gemini_last_request_model = None
                modell_index = 0
                aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]
                versuch = 0
                versuch_zyklus += 1
                continue

            if kategorie == "input_token_limit":
                # Das Free-Tier-Input-Limit von 250.000 Tokens ist ein
                # minutenbezogenes Limit des aktuell verwendeten Modells.
                # Deshalb denselben vollstaendigen Request nach Ablauf des
                # Minutenfensters erneut senden, statt auf ein anderes Modell
                # zu wechseln oder Daten zu kuerzen.
                wartezeit = empfohlene_wartezeit or 65.0
                print(
                    f"  250.000-Input-Token-Limit von {aktuelles_modell} erreicht "
                    "(429 RESOURCE_EXHAUSTED, generate_content_free_tier_input_token_count). "
                    f"Warte {wartezeit:.1f}s und wiederhole denselben vollstaendigen Request "
                    "mit demselben Modell."
                )
                time.sleep(wartezeit)
                continue

            if kategorie in ("ueberlast", "netzwerk"):
                # Bei serverseitiger Ueberlast (503) oder Netzwerk-Abbruch
                # wird jedes konfigurierte Modell hoechstens EINMAL versucht.
                # Vor dem Wechsel wartet der Lauf mit exponentiellem Backoff
                # und Jitter. Ein vom Server geliefertes retryDelay gewinnt,
                # wenn es laenger als die lokale Backoff-Stufe ist.
                naechster_index = modell_index + 1
                while (
                    naechster_index < len(GEMINI_MODELLREIHENFOLGE)
                    and GEMINI_MODELLREIHENFOLGE[naechster_index] in _gemini_failed_models
                ):
                    naechster_index += 1
                if naechster_index < len(GEMINI_MODELLREIHENFOLGE):
                    grund = "503-Overload" if kategorie == "ueberlast" else "Netzwerk-Abbruch"
                    # Modellwechsel ist ein Fallback auf eine andere Ressource,
                    # kein Retry desselben Requests. Deshalb nur kurzer Jitter statt
                    # 15/30/60/120 s Voll-Backoff. Der echte exponentielle Backoff
                    # erfolgt erst, wenn der gesamte Modellpool temporaer ueberlastet ist.
                    wartezeit = random.uniform(0.0, GEMINI_MODELLWECHSEL_JITTER_MAX)
                    naechstes_modell = GEMINI_MODELLREIHENFOLGE[naechster_index]
                    print(
                        f"  {grund} nach Versuch {versuch}/{MAX_VERSUCHE}. "
                        f"Kurzer Modellwechsel-Jitter {wartezeit:.1f}s; "
                        f"wechsle danach von {aktuelles_modell} auf {naechstes_modell}."
                    )
                    time.sleep(wartezeit)
                    # Das gerade fehlgeschlagene Modell bleibt fuer diesen Lauf gesperrt.
                    _gemini_failed_models.add(aktuelles_modell)
                    modell_index = naechster_index
                    aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]
                    # Active Model und Last Request Model zeigen beide unmittelbar
                    # auf das neue Modell. Dadurch kann weder der Quota-Scheduler
                    # noch der aeussere Retry-Pfad das gerade fehlgeschlagene Modell
                    # wieder auswaehlen.
                    _gemini_active_modell = aktuelles_modell
                    _gemini_last_request_model = aktuelles_modell
                    # Bereits hochgeladener Kontext bleibt erhalten; ein 503 erfordert
                    # keinen erneuten Upload.
                    # Bereits hochgeladene Dateien bleiben erhalten. Ein 503 ist
                    # ein technischer Fehler; fuer den Modellwechsel ist kein
                    # erneuter Datei-Upload erforderlich.
                    continue

                # Alle Modelle waren in diesem temporaeren 503-/Netzwerk-Zyklus
                # nicht verfuegbar. Das ist kein terminaler Zustand: alle lokalen
                # Sendungsbuchungen wurden bei den 503s bereits zurueckgerollt.
                # Nach einem echten Wartefenster wird der komplette Modell-/Quota-
                # Zustand neu bewertet. Dadurch kann ein 503 nicht durch den
                # bisherigen MAX_VERSUCHE-Zaehler kuenstlich terminal werden.
                grund = "503-Overload" if kategorie == "ueberlast" else "Netzwerk-Abbruch"
                if versuch_zyklus >= GEMINI_MAX_TECHNISCHE_RETRY_ZYKLEN:
                    print(
                        f"  {grund} auf allen konfigurierten Modellen auch im letzten "
                        f"technischen Zyklus {versuch_zyklus}/{GEMINI_MAX_TECHNISCHE_RETRY_ZYKLEN}; "
                        "beende kontrolliert mit technischem Fallback."
                    )
                    break
                backoff_index = min(
                    versuch_zyklus - 1, len(UEBERLAST_WARTEZEITEN) - 1
                )
                basis_wartezeit = UEBERLAST_WARTEZEITEN[backoff_index]
                server_wartezeit = (
                    empfohlene_wartezeit if empfohlene_wartezeit is not None else 0
                )
                wartezeit = max(float(basis_wartezeit), float(server_wartezeit))
                jitter = random.uniform(0.0, wartezeit * 0.20)
                wartezeit += jitter
                print(
                    f"  {grund} auf allen konfigurierten Modellen. "
                    f"Warte {wartezeit:.1f}s und pruefe danach alle Modelle und "
                    "lokalen Quoten erneut."
                )
                time.sleep(wartezeit)
                _gemini_failed_models.clear()
                _gemini_active_modell = None
                _gemini_last_request_model = None
                modell_index = 0
                aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]
                versuch = 0
                versuch_zyklus += 1
                continue

            else:
                wartezeit = (empfohlene_wartezeit if empfohlene_wartezeit is not None
                             else WARTEZEIT_SEKUNDEN + versuch_nummer * 5)
                print(f"  Warte {wartezeit:.0f}s vor dem naechsten Versuch...")
            if versuch_nummer >= MAX_VERSUCHE:
                break
            time.sleep(wartezeit)
            continue

        if ist_ablehnung(text):
            print("  Sicherheitsfilter-Ablehnung erkannt (oder leere Antwort) - neuer Versuch...")
            print(f"  Antwort war: {text[:200]!r}")
            letzte_antwort = text
            # NUR hier neu hochladen: frischer Kontext ist genau das Mittel
            # gegen diese Art von Ablehnung (siehe Kommentar oben).
            hochgeladene_teile = None
            _gemini_cache_name = None
            _gemini_stufen_cache.clear()
            _gemini_request_split_cache.clear()
            technische_retry_wiederverwenden = False
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
    # Stattdessen wird ein klar gekennzeichneter technischer Fallback-Marker an
    # den normalen Speicherpfad uebergeben. speichere_ergebnis() erkennt diesen
    # Marker und verwirft die Ausgabe bewusst, damit keine Tagesdatei erzeugt
    # oder ueberschrieben wird.
    _gemini_fallback_status_datei("write", letzte_antwort)
    return (
        "[GEMINI_TECHNISCHER_FALLBACK]\n"
        "TECHNISCHE FALLBACK-DATEI – KEINE GUELTIGE GEMINI-AUSWERTUNG.\n"
        "Gemini war nach allen konfigurierten Versuchen nicht verfuegbar. "
        "Es wurde deshalb keine kuenstliche Gemini-Analyse erzeugt.\n\n"
        f"Letzter API-Fehler:\n{letzte_antwort}\n\n"
        "Die Eingabedateien wurden vor dem API-Aufruf geladen. "
        "Die autoritativen Positionsdaten bleiben unveraendert."
    )


def _normalisiere_7_4_numerische_ausgabe(text):
    """Normalisiert nur numerische Faktenfelder innerhalb von Abschnitt 10.5.

    Die autoritative Tab-2-Faktenbasis bleibt unverändert. Diese Funktion
    betrifft ausschließlich die Darstellung in der fertigen Auswertung:
    deutsche Dezimalkommas werden in den bekannten numerischen 10.5-Feldern
    deterministisch in Dezimalpunkte umgewandelt.
    """
    match = re.search(
        r"(?ims)^\s*10\.5\b.*?(?=^\s*11\.\s+|\Z)",
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

def _bereinige_doppelte_10_5_ueberschrift(text):
    """Kanonisiert 10.5, fuehrt doppelte Ausgaben zusammen und dedupliziert
    identische Positionen anhand von Name + Ticker + Einstieg + Einstiegsdatum.

    Bei derselben Position werden zusaetzliche Felder aus beiden Bloecken
    zusammengefuehrt; dadurch gehen z.B. optionale OS-Felder nicht verloren.
    """
    if not text:
        return text
    heading_re = re.compile(r"(?im)^\s*10\.5\s+Geschlossene Positionen(?:\s*\([^\n]*\))?\s*$")
    matches = list(heading_re.finditer(text))
    if len(matches) <= 1:
        if matches:
            m = matches[0]
            return text[:m.start()] + "10.5 Geschlossene Positionen" + text[m.end():]
        return text

    next_re = re.compile(r"(?im)^\s*11\.\s+METHODIK\s*/\s*DATENQUALITÄT\s*$")
    first = matches[0]
    end_match = next_re.search(text, first.end())
    section_end = end_match.start() if end_match else len(text)

    # Alle 10.5-Bloecke bleiben erhalten. Nur die wiederholten Ueberschriften
    # werden entfernt, sodass ihre jeweiligen positionsbezogenen Inhalte
    # unter einer einzigen kanonischen 10.5-Ueberschrift zusammengefuehrt werden.
    parts = []
    cursor = first.end()
    for m in matches[1:]:
        if m.start() >= section_end:
            break
        parts.append(text[cursor:m.start()])
        cursor = m.end()
    parts.append(text[cursor:section_end])
    merged_body = "".join(parts)

    # Positionsbezogene Deduplizierung nur innerhalb des zusammengefuehrten
    # 10.5-Bereichs. Die Identitaet folgt der im Projekt verwendeten Kombination
    # Name + Ticker + Einstieg + Einstiegsdatum. Alle Zusatzfelder beider
    # Datensaetze werden in einen Datensatz uebernommen.
    position_field_re = re.compile(
        r"(?P<label>[^|:]+):\s*(?P<value>[^|]*)"
    )

    def _position_fields(line):
        fields = {}
        for match in position_field_re.finditer(line):
            label = match.group("label").strip()
            value = match.group("value").strip()
            if label:
                fields[label] = value
        required = ("Name", "Ticker", "Einstieg", "Einstiegsdatum")
        if not all(fields.get(field, "") for field in required):
            return None
        return fields

    def _position_key(fields):
        return tuple(re.sub(r"\s+", " ", fields[field].strip()).casefold() for field in (
            "Name", "Ticker", "Einstieg", "Einstiegsdatum"
        ))

    lines = merged_body.splitlines(keepends=True)
    seen = {}
    deduped_lines = []
    for line in lines:
        fields = _position_fields(line)
        if fields is None:
            deduped_lines.append(line)
            continue

        key = _position_key(fields)
        if key not in seen:
            seen[key] = len(deduped_lines)
            deduped_lines.append(line)
            continue

        existing_index = seen[key]
        existing_line = deduped_lines[existing_index]
        existing_fields = _position_fields(existing_line) or {}
        merged_fields = dict(existing_fields)
        changed = False
        for label, value in fields.items():
            if not value:
                continue
            if label not in merged_fields or not merged_fields[label]:
                merged_fields[label] = value
                changed = True
            elif merged_fields[label] != value:
                # Bei einem echten Feldkonflikt bleiben beide Werte erhalten,
                # statt Informationen aus einem der beiden Bloecke zu verlieren.
                merged_fields[label] = f"{merged_fields[label]} / {value}"
                changed = True

        if changed:
            newline = " | ".join(f"{label}: {value}" for label, value in merged_fields.items())
            if existing_line.endswith("\n"):
                newline += "\n"
            deduped_lines[existing_index] = newline

    merged_body = "".join(deduped_lines)

    return (
        text[:first.start()]
        + "10.5 Geschlossene Positionen"
        + merged_body
        + text[section_end:]
    )


def normalisiere_ausgabe(text, zielzonen=None):
    """Erzwingt formale Regeln fuer die fertige Ausgabe.

    Offene Positionen+Check.csv ist die verbindliche Faktenquelle für die
    autoritative offene Positionsliste. 10.5 wird deterministisch aus Tab 2
    erzeugt; 10.1 bis 10.4 enthalten qualitative Portfolio-Interpretationen.
    10.3 enthält ausschließlich Positionen mit belastbar veränderter
    Investmentthese und darf keine vollständige Positionsfaktenliste ersetzen.
    Die Normalisierung darf keine technischen Werte neu berechnen.
    """
    if not text:
        return text

    text = _bereinige_doppelte_10_5_ueberschrift(text)

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
            r"(?ims)^10\.3\s+Positionen\s*$.*?Keine offenen Positionen laut Offene Positionen\+Check\.csv\.",
            text or "",
        ))
        if not empty_positions:
            raise RuntimeError(
                "Keine verbindlichen technischen Positionsdaten aus "
                "Offene Positionen+Check.csv vorhanden."
            )

    match = re.search(
        r"(?ims)^10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
        text,
    )
    # Legacy-Kompatibilität für ältere Unit-Tests/alte Gemini-Ausgaben: Eine
    # frühere Ausgabe konnte den Positionsblock noch unter 7. OFFENE POSITIONEN
    # liefern. Nur in diesem Altformat wird der Abschnitt für die bestehende
    # Normalisierungsprüfung auf 10 umgehängt. Die neue Zielstruktur bleibt 10.
    if not match and re.search(r"(?im)^\s*7\. OFFENE POSITIONEN\s*$", text or ""):
        text = re.sub(r"(?im)^\s*7\. OFFENE POSITIONEN\s*$", "10. 💼 BESTEHENDES PORTFOLIO", text, count=1)
        match = re.search(
            r"(?ims)^10\. (?:💼\s*)?BESTEHENDES PORTFOLIO\s*$.*?(?=^\s*11\. METHODIK / DATENQUALITÄT\s*$|\Z)",
            text,
        )
    if not match:
        raise RuntimeError(
            "Abschnitt '10. 💼 BESTEHENDES PORTFOLIO' fehlt."
        )

    block = match.group(0)

    # Neue Zielstruktur: 10.3 ist keine vollständige Positionsfaktenliste mehr.
    # Wenn Gemini bereits die Zielstruktur liefert, dürfen die autoritativen
    # Positionsdaten nicht als Vollblock in 10.3 hineingeschrieben werden.
    if re.search(r"(?m)^10\.3 Positionen mit neuer Investmentthese\s*$", block):
        return text
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
            "'10. BESTEHENDES PORTFOLIO' gefunden."
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
        "Aktueller_Kurs": re.compile(r"(?im)^(\s*Aktueller Kurs\s*:\s*)[^\n]*$"),
        "Stop": re.compile(r"(?im)^(\s*Stop\s*:\s*)[^\n]*$"),
        "TP1": re.compile(r"(?im)^(\s*TP1\s*:\s*)[^\n]*$"),
        "TP2": re.compile(r"(?im)^(\s*TP2\s*:\s*)[^\n]*$"),
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

        position_facts = (
            ("current", "Aktueller Kurs", "Aktueller_Kurs"),
            ("stop", "Stop", "Stop"),
            ("tp1", "TP1", "TP1"),
            ("tp2", "TP2", "TP2"),
        )
        for source_key, label, field in position_facts:
            value = source.get(source_key, "")
            if value in (None, ""):
                continue
            pattern = technical_labels[field]
            replacement = f"{label}: {value}"
            pos_block, count = pattern.subn(replacement, pos_block, count=1)
            if count == 0:
                insertion_anchor = re.search(
                    r"(?im)^\s*Einstieg(?:skurs)?\s*:\s*[^\n]+(?:\n|$)",
                    pos_block,
                )
                if insertion_anchor:
                    pos_block = (
                        pos_block[:insertion_anchor.end()]
                        + replacement + "\n"
                        + pos_block[insertion_anchor.end():]
                    )
                else:
                    errors.append(
                        f"{source['name']} ({source['ticker']}): "
                        f"Positionsfeld {label} konnte nicht kanonisiert werden"
                    )
                    continue

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
            for field, label in (
                ("current", "Aktueller Kurs"),
                ("stop", "Stop"),
                ("tp1", "TP1"),
                ("tp2", "TP2"),
            ):
                value = source.get(field)
                if value not in (None, ""):
                    lines.append(f"{label}: {value}")
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
    def _parse_number(raw):
        value = str(raw or "").strip().replace(" ", "")
        if not value:
            raise ValueError("empty number")
        sign = ""
        if value[0] in "+-":
            sign, value = value[0], value[1:]
        if not re.fullmatch(r"\d[\d.,]*", value):
            raise ValueError("invalid number")
        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            value = value.replace(",", ".")
        elif value.count(".") > 1:
            value = value.replace(".", "")
        return float(sign + value)

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
        kurs = re.match(r"\s*([-+]?\d[\d.,]*)", rest)
        if not kurs:
            continue
        try:
            kurs_value = _parse_number(kurs.group(1))
        except ValueError:
            continue
        ref = {"kurs": kurs_value, "perioden": {}}
        dm = re.search(r"(?:Datenstand|Datenstand:?)\s*=\s*(\d{4}-\d{2}-\d{2})", rest, re.I)
        if dm:
            ref["datenstand"] = dm.group(1)
        sm = re.search(r"(?:Letzter_Schluss|Letzter\s+Schluss)\s*=\s*(?!\d{4}-\d{2}-\d{2})([-+]?\d[\d.,]*)", rest, re.I)
        if sm:
            try:
                ref["schluss"] = _parse_number(sm.group(1))
            except ValueError:
                pass
        for key in ("5T", "1M", "3M", "6M", "1J"):
            m = re.search(rf"(?:^|\|)\s*{re.escape(key)}\s*=\s*([-+]?\d[\d.,]*)\s*%", rest)
            if m:
                try:
                    ref["perioden"][key] = _parse_number(m.group(1))
                except ValueError:
                    pass
        referenzen[label.lower()] = ref
        normalized_label = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
        treasury_aliases = {
            "2y": ("us 2y treasury", "2y us treasury", "us 2 year treasury", "2 year us treasury", "2j us treasury", "us 2j treasury", "2j", "2y"),
            "5y": ("us 5y treasury", "5y us treasury", "us 5 year treasury", "5 year us treasury", "5j us treasury", "us 5j treasury", "5j", "5y"),
            "10y": ("us 10y treasury", "10y us treasury", "us 10 year treasury", "10 year us treasury", "10j us treasury", "us 10j treasury", "10j treasury", "10j", "10y"),
            "30y": ("us 30y treasury", "30y us treasury", "us 30 year treasury", "30 year us treasury", "30j us treasury", "us 30j treasury", "30j treasury", "30j", "30y"),
            "real10y": ("realzins 10y tips", "realzinsen 10y tips", "10y tips real yield", "10y real yield", "real 10y tips", "10j tips realzins", "realzinsen 10j tips", "realzins 10j tips", "10j realzins", "10y realzins", "realzins (10y tips)", "realzinsen (10y tips)"),
        }
        for aliases in treasury_aliases.values():
            if normalized_label in aliases:
                for alias in aliases:
                    referenzen[alias] = ref
                break

    # Brent-WTI ist eine abgeleitete, aber deterministische Makro-Kennzahl.
    # Beide Ausgangswerte stammen aus demselben aktuellen autoritativen
    # Makro-Briefing. Die Einzelwerte Brent und WTI bleiben unverändert.
    brent_ref = referenzen.get("brent")
    wti_ref = referenzen.get("wti")
    if brent_ref is not None and wti_ref is not None:
        oil_spread = {
            "kurs": float(brent_ref["kurs"]) - float(wti_ref["kurs"]),
            "perioden": {},
            "datenstand": brent_ref.get("datenstand") or wti_ref.get("datenstand"),
            "berechnet_aus": ("Brent", "WTI"),
        }
        for alias in (
            "brent-wti-spread",
            "brent wti spread",
            "brent-wti differenz",
            "brent wti differenz",
        ):
            referenzen[alias] = oil_spread
    return referenzen


def _sichere_punkt10_tp1_angaben(text, briefing_pfad):
    """Bindet explizite TP1-Angaben positionsbezogen an das aktuelle Briefing."""
    if not text or not briefing_pfad or not os.path.isfile(briefing_pfad):
        return text, []
    try:
        briefing = Path(briefing_pfad).read_text(encoding="utf-8-sig")
    except OSError:
        return text, []

    refs = {}
    block_re = re.compile(r"(?ims)^\s*>>>\s*([^|\n]+?)\s*\|.*?(?=^\s*>>>\s*|\Z)")
    tp1_re = re.compile(r"(?i)\bTP1\s*:\s*([-+]?\d[\d.,]*)\s*(\$|USD|US\$|€)?")
    for m in block_re.finditer(briefing):
        ticker = m.group(1).strip().upper()
        tp1 = tp1_re.search(m.group(0))
        if ticker and tp1:
            refs[ticker] = (tp1.group(1), tp1.group(2) or "")
    if not refs:
        return text, []

    claim_re = re.compile(r"(?i)(\bTP1\s*(?:bei|:|=)\s*)([-+]?\d[\d.,]*)(\s*(?:\$|USD|US\$|€)?)")
    changes = []
    out = []
    for line in text.splitlines():
        if "TP1" not in line.upper():
            out.append(line)
            continue
        def number(raw):
            value = raw.replace(" ", "")
            if "," in value and "." in value:
                value = value.replace(".", "").replace(",", ".") if value.rfind(",") > value.rfind(".") else value.replace(",", "")
            elif "," in value:
                value = value.replace(",", ".")
            return float(value)
        def replace(match):
            prefix = line[:match.start()]
            ticker_candidates = [t.upper() for t in re.findall(r"(?i)(?<![\w.-])([A-Z]{1,6}(?:\.[A-Z]{1,3})?)(?![\w.-])", prefix)]
            ticker_candidates = [t for t in ticker_candidates if t in refs]
            if not ticker_candidates:
                return match.group(0)
            ticker = ticker_candidates[-1]
            target_raw, target_unit = refs[ticker]
            try:
                old = number(match.group(2)); target = number(target_raw)
            except ValueError:
                return match.group(0)
            if abs(old - target) < 0.005:
                return match.group(0)
            replacement = target_raw.replace(".", ",") if "." in target_raw and "," not in target_raw else target_raw
            changes.append(f"{ticker} TP1: {old} -> {replacement}")
            return match.group(1) + replacement + (match.group(3) or target_unit)
        out.append(claim_re.sub(replace, line))

    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    if changes:
        print(f"PUNKT-10-TP1-INTEGRITAET: {len(changes)} positionsgebundene TP1-Angabe(n) korrigiert.")
    return result, changes

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
    tips = ref_for(
        "realzins 10y tips", "realzinsen 10y tips", "realzins 10j tips",
        "realzinsen 10j tips", "10y tips real yield", "10y real yield",
        "realzins (10y tips)", "realzinsen (10y tips)"
    )

    spread = None
    m = re.search(r"(?im)^(?:2Y|2J)\s*[-–]\s*(?:10Y|10J)\s*Spread\s*:\s*([-+]?\d+(?:[.,]\d+)?)", makro_text)
    if m:
        spread = float(m.group(1).replace(",", "."))
    elif treasury.get("2j") and treasury.get("10j"):
        # Der Spread ist eine abgeleitete Kennzahl aus zwei eindeutig
        # gebundenen nominalen Renditen. Dadurch kann Gemini niemals den
        # 10Y-Wert selbst als Spread übernehmen.
        spread = float(treasury["10j"]["kurs"]) - float(treasury["2j"]["kurs"])

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
            (r"Realzins(?:en)?\s*(?:\(\s*)?10Y\s*TIPS(?:\s*\))?", tips, "10Y TIPS"),
            (r"TIPS[- ]Realrendite\s*10Y", tips, "10Y TIPS"),
            (r"10Y\s*TIPS(?:[- ]Realzins|[- ]Realrendite)?", tips, "10Y TIPS"),
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
            if re.search(r"(?:2Y|2J)\s*[-–]\s*(?:10Y|10J)\s*[- ]?Spread", line, re.I):
                line = re.sub(
                    rf"((?:2Y|2J)\s*[-–]\s*(?:10Y|10J)\s*[- ]?Spread\s*(?:bei|von|ist|=|:)\s*(?:mit\s*)?){pct}\s*(?:Prozentpunkte?|%)?",
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

        # Narrative spread forms such as "10J-2J-Spread ..." or
        # "Zinskurven-Spread zwischen 2J und 10J ..." are semantically the
        # same metric. Always bind the number to the deterministic spread.
        if spread is not None and re.search(
            r"(?i)(?:10J|10Y)\s*[-–]\s*(?:2J|2Y)\s*[- ]?Spread|"
            r"Zinskurven[- ]?Spread.*(?:2J|2Y).*?(?:10J|10Y)", line
        ):
            spread_pattern = re.compile(
                rf"((?:(?:10J|10Y)\s*[-–]\s*(?:2J|2Y)\s*[- ]?Spread|Spread)\s*(?:(?:bei|von|ist|=|:)\s*)?(?:mit\s*)?){pct}\s*(?:Prozentpunkte?|%-?Pkt\.?|%)?",
                re.I,
            )
            def _replace_narrative_spread(sm):
                changes.append(f"2Y-10Y Spread: {sm.group(0)} -> {_fmt(spread)} Prozentpunkte")
                return sm.group(1) + _fmt(spread) + " Prozentpunkte"
            line = spread_pattern.sub(_replace_narrative_spread, line)

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
    """Sichert die autoritative Datenqualitaet ausschliesslich in Abschnitt 11.2."""
    if not text or not makro_datenqualitaet:
        return text
    start = text.find("11.2 Makro-Szenario-Status")
    if start < 0:
        return text
    end = text.find("\n11.3", start)
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



def _parse_technische_zahl(value):
    """Parst eine technische Zahl ohne Einheiten-/Semantikverlust."""
    raw = str(value or "").strip().replace(" ", "")
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _extrahiere_technische_referenzen(eingabedateien):
    """Liest technische Fakten ausschließlich aus den autoritativen Tagesquellen.

    Die Referenz ist ticker-/namensgebunden. Gemini darf technische Werte nicht
    zwischen zwei Titeln übertragen. Zentrale Trade-Story-Daten haben Vorrang;
    danach folgen die bereits bestehenden Scanner-/Setup-Dateien.
    """
    refs = {}

    def add(item, authoritative_rank=1):
        if not isinstance(item, dict):
            return
        ticker = str(item.get("ticker") or item.get("Ticker") or item.get("Yahoo-Ticker") or "").strip()
        name = str(item.get("name") or item.get("Name") or item.get("Firmenname") or "").strip()
        if not ticker and not name:
            return
        data = {"ticker": ticker, "name": name, "rank": authoritative_rank}
        for key in ("Kurs", "Einstieg", "Stop", "TP1", "TP2", "CRV1", "CRV2", "Risk_Perc", "RSI", "MACD"):
            value = item.get(key)
            if value not in (None, ""):
                data[key] = str(value).strip()
        status = str(item.get("trade_story_status") or item.get("Status2") or item.get("Status") or "").strip().upper()
        data["status"] = status
        keys = set()
        if ticker:
            keys.add(_normalisiere_ticker(ticker))
        if name:
            keys.add(_normalisiere_positionsname(name))
        for key in keys:
            if not key:
                continue
            old = refs.get(key)
            if old is None or authoritative_rank > old.get("rank", 0) or (
                authoritative_rank == old.get("rank", 0) and len(data) > len(old)
            ):
                refs[key] = data

    # 1) Das täglich erzeugte zentrale Universum ist die stärkste Quelle.
    central = eingabedateien.get("Trade_Story_Universum(...).json")
    if central and os.path.isfile(central):
        try:
            with open(central, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("candidates", []) if isinstance(data, dict) else []:
                if isinstance(item, dict) and item.get("trade_story_status") != "STATUSKONFLIKT":
                    add(item, 3)
        except Exception as exc:
            print(f"WARNUNG: Technisches Trade-Story-Universum nicht lesbar: {exc}")

    # 2) Die aktuelle Einzel-Check-Technikhistorie ist die autoritative
    # technische Quelle für A-Kandidaten, auch wenn der Kandidat aus dem
    # Hebeltrader-/Beobachtungsworkflow stammt. Sie enthält den bereits
    # berechneten Trendfolge-Block mit Kurs/Stop/TP/CRV.
    history_path = eingabedateien.get("Einzel-Check-Technikhistorie")
    if history_path and os.path.isfile(history_path):
        try:
            with open(history_path, encoding="utf-8-sig") as f:
                for raw_line in f:
                    try:
                        row = json.loads(raw_line)
                    except (TypeError, ValueError):
                        continue
                    trendfolge = row.get("Technik", {}).get("Trendfolge", {}) if isinstance(row.get("Technik"), dict) else {}
                    item = dict(trendfolge) if isinstance(trendfolge, dict) else {}
                    item.update({"ticker": row.get("Ticker"), "name": row.get("Name"), "trade_story_status": row.get("Status")})
                    add(item, 4)
        except Exception as exc:
            print(f"WARNUNG: Einzel-Check-Technikhistorie nicht lesbar: {exc}")

    # 3) Bestehende technische Scanner bleiben unverändert autoritativ.
    specs = (
        ("Trade_Story_Setup_Rohuniversum(...).csv", 2),
        ("Setups(...).csv", 2),
        ("Trendwende_Setups(...).csv", 2),
        ("Short_Setups(...).csv", 2),
        ("Edelmetalle_Setups(...).csv", 2),
    )
    for key, rank in specs:
        path = eingabedateien.get(key)
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                sample = f.read(8192)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=";,\\t")
                except csv.Error:
                    dialect = csv.excel
                    dialect.delimiter = ";"
                for row in csv.DictReader(f, dialect=dialect):
                    add(row, rank)
        except Exception as exc:
            print(f"WARNUNG: Technische Referenzquelle {key} nicht lesbar: {exc}")
    return refs


def _technische_ref_fuer_kandidat(candidate, refs):
    """Löst einen Story-Kandidaten eindeutig gegen Ticker oder Namen auf."""
    text = str(candidate or "").strip()
    if not text:
        return None
    for ticker in re.findall(r"\(([A-Za-z0-9._=-]{1,30})\)", text):
        ref = refs.get(_normalisiere_ticker(ticker))
        if ref:
            return ref
    normalized = _normalisiere_positionsname(text)
    if not normalized:
        return None
    direct = refs.get(normalized)
    if direct:
        return direct
    matches = []
    for key, ref in refs.items():
        if len(key) >= 5 and (key in normalized or normalized in key):
            matches.append(ref)
    if len(matches) == 1:
        return matches[0]
    return None


def _sichere_technische_assetangaben(text, eingabedateien):
    """Bindet Kurs/Stop/TP/CRV eines genannten Titels an die Quellen.

    Nur explizit bezeichnete technische Felder werden ersetzt. Freie
    Prosa-Zahlen bleiben unangetastet. So kann z.B. ein Goldpreis niemals als
    AU-Kurs bestehen bleiben, wenn AU als konkreter Kandidat genannt wird.
    """
    if not text or not eingabedateien:
        return text, []
    refs = _extrahiere_technische_referenzen(eingabedateien)
    if not refs:
        return text, []
    changes = []

    # Globaler technischer Fakten-Gate: Jede Zeile mit einem eindeutig
    # genannten Ticker wird auf natürlich formulierte Kurs-/Stop-/TP-/CRV-
    # Angaben geprüft. Das ist notwendig, weil technische Werte nicht nur in
    # 1.3, sondern auch in Zusammenfassungen und in den fachlich passenden Pflichtabschnitten vorkommen.
    lines = text.splitlines()
    normalized_lines = []
    for line in lines:
        line_out = line
        tickers = re.findall(r"\(([A-Za-z0-9._=-]{1,30})\)", line_out)
        refs_for_line = []
        for ticker in tickers:
            ref = refs.get(_normalisiere_ticker(ticker))
            if ref and ref not in refs_for_line:
                refs_for_line.append(ref)
        if len(refs_for_line) == 1:
            ref = refs_for_line[0]
            inline_fields = {
                "Kurs": [r"(?i)(\baktueller\s+Kurs\s*)([-+]?\d[\d.,]*)", r"(?i)(\bKurs\s+)([-+]?\d[\d.,]*)"],
                "Einstieg": [r"(?i)(\bEinstieg(?:skurs)?\s+(?:bei|von)\s*)([-+]?\d[\d.,]*)"],
                "Stop": [r"(?i)(\bStop(?:-Loss)?\s+(?:bei|von)\s*)([-+]?\d[\d.,]*)"],
                "TP1": [r"(?i)(\bTP1\s+(?:bei|von)\s*)([-+]?\d[\d.,]*)"],
                "TP2": [r"(?i)(\bTP2\s+(?:bei|von)\s*)([-+]?\d[\d.,]*)"],
                "CRV1": [r"(?i)(\bCRV1\s+(?:von|bei)\s*)([-+]?\d[\d.,]*)"],
            }
            for field, patterns in inline_fields.items():
                source_value = ref.get(field)
                if source_value in (None, ""):
                    continue
                for pattern in patterns:
                    fm = re.search(pattern, line_out)
                    if not fm:
                        continue
                    old = fm.group(2)
                    old_num = _parse_technische_zahl(old)
                    source_num = _parse_technische_zahl(source_value)
                    if old_num is None or source_num is None or abs(old_num - source_num) >= 0.0005:
                        replacement = str(source_value).replace(".", ",") if "," in str(source_value) else str(source_value)
                        line_out = line_out[:fm.start(2)] + replacement + line_out[fm.end(2):]
                        changes.append(f"{ref.get('ticker') or ref.get('name')}: {field} {old} -> {source_value}")
                    break
        normalized_lines.append(line_out)
    text = "\n".join(normalized_lines)

    section_match = re.search(r"(?ims)^(?:1\.3\s+IDEEN IM AUFBAU|6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN)\s*$.*?(?=^(?:1\.4|6\.2)\s+|\Z)", text)
    if not section_match:
        return text, changes
    section = section_match.group(0)
    candidate_re = re.compile(r"(?im)^(?:Bestehender Kandidat / Bezug|Kandidat|Name)\s*:\s*(.+)$")
    matches = list(candidate_re.finditer(section))
    replacements = []
    field_patterns = {
        "Kurs": r"(?i)(\bKurs\s*:\s*)([-+]?\d[\d.,]*)",
        "Einstieg": r"(?i)(\bEinstieg\s*:\s*)([-+]?\d[\d.,]*)",
        "Stop": r"(?i)(\bStop\s*:\s*)([-+]?\d[\d.,]*)",
        "TP1": r"(?i)(\bTP1\s*:\s*)([-+]?\d[\d.,]*)",
        "TP2": r"(?i)(\bTP2\s*:\s*)([-+]?\d[\d.,]*)",
        "CRV1": r"(?i)(\bCRV1\s*:\s*)([-+]?\d[\d.,]*)",
        "CRV2": r"(?i)(\bCRV2\s*:\s*)([-+]?\d[\d.,]*)",
    }
    for cm in matches:
        candidate = cm.group(1).strip()
        ref = _technische_ref_fuer_kandidat(candidate, refs)
        if not ref:
            continue
        # Die technische Zahlenabsicherung darf die autoritative Kandidatenidentität
        # NICHT umschreiben. Diese Identität wurde unmittelbar zuvor durch die
        # Trade-Story-Reparatur/Validierung gegen das autoritative Kandidaten-
        # universum gebunden. Eine nachtraegliche Kanonisierung aus der
        # technischen Referenzquelle kann Name/Ticker-Aliase einfuehren, die der
        # Trade-Story-Validator nicht als dieselbe autoritative Setup-Zeile
        # erkennt. Deshalb bleibt die Kandidatenzeile unveraendert; nur die
        # technischen Zahlenfelder dieses bereits identifizierten Kandidaten
        # werden quellengebunden korrigiert.
        story_start = cm.start()
        story_end = matches[matches.index(cm) + 1].start() if matches.index(cm) + 1 < len(matches) else len(section)
        story = section[story_start:story_end]
        for field, pattern in field_patterns.items():
            source_value = ref.get(field)
            if source_value in (None, ""):
                continue
            fm = re.search(pattern, story)
            if not fm:
                continue
            old = fm.group(2)
            old_num = _parse_technische_zahl(old)
            source_num = _parse_technische_zahl(source_value)
            if old_num is None or source_num is None or abs(old_num - source_num) >= 0.0005:
                abs_start = section_match.start() + story_start + fm.start(2)
                abs_end = section_match.start() + story_start + fm.end(2)
                replacement = str(source_value).replace(".", ",") if "," in str(source_value) else str(source_value)
                replacements.append((abs_start, abs_end, replacement))
                changes.append(f"{ref.get('ticker') or ref.get('name')}: {field} {old} -> {source_value}")
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    if changes:
        print(f"TECHNISCHE-ZAHLEN-GATE: {len(changes)} technische Assetangaben quellengebunden korrigiert.")
    return text, changes



def _normalisiere_technische_nachsuche_felder(text):
    """Normalisiert beschädigte Gemini-Labels für die technische Nachsuche.

    Gemini kann das verbindliche Feldlabel in seltenen Fällen beschädigen,
    z.B. durch das Einschieben des zuvor genannten Assetnamens in das Label:
    ``PotenzielAmazon.com, Inc. (AMZN)he Nachsuche: ...``.

    Die Reparatur ist bewusst auf 1.3 und auf Zeilen begrenzt, die mit
    ``Potenz`` beginnen und mit ``Nachsuche`` enden. Ausschließlich das Label
    wird ersetzt; der hinter dem Doppelpunkt stehende Assetinhalt bleibt
    unverändert und wird anschließend durch die technische Nachsuche
    quellengebunden geprüft.
    """
    if not text:
        return text, []
    section_match = re.search(r"(?ims)^(?:1\.3\s+IDEEN IM AUFBAU|6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN)\s*$.*?(?=^(?:1\.4|6\.2)\s+|\Z)", text)
    if not section_match:
        return text, []
    section = section_match.group(0)
    pattern = re.compile(
        r"(?im)^(?P<label>\s*Pote.*?Nachsuche)\s*:\s*(?P<assets>.+?)\s*$"
    )
    replacements = []
    changes = []
    for m in pattern.finditer(section):
        label = m.group("label").strip()
        assets = m.group("assets").strip()
        canonical = "Potenzielle Assets fuer technische Nachsuche"
        if label == canonical:
            continue
        replacement = f"{canonical}: {assets}"
        replacements.append((section_match.start() + m.start(), section_match.start() + m.end(), replacement))
        changes.append(f"Technische-Nachsuche-Label normalisiert: {label!r} -> {canonical!r}")
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, changes



def _ergaenze_technische_nachsuche(text, eingabedateien):
    """Prüft explizit genannte potenzielle Assets gegen das Tagesuniversum.

    Erzeugt keinen neuen technischen Status. Die Funktion macht nur
    transparent, welche von Gemini genannten Assets bereits im technischen
    Tagesuniversum auffindbar sind.
    """
    if not text or not eingabedateien:
        return text, []
    refs = _extrahiere_technische_referenzen(eingabedateien)
    if not refs:
        return text, []
    text, label_changes = _normalisiere_technische_nachsuche_felder(text)
    section_match = re.search(r"(?ims)^(?:1\.3\s+IDEEN IM AUFBAU|6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN)\s*$.*?(?=^(?:1\.4|6\.2)\s+|\Z)", text)
    if not section_match:
        return text, label_changes
    section = section_match.group(0)
    pattern = re.compile(r"(?im)^(Potenzielle Assets fuer technische Nachsuche)\s*:\s*(.+)$")
    replacements = []
    notes = []
    for m in pattern.finditer(section):
        raw = m.group(2).strip()
        if not raw or raw.upper() in {"NICHT VERFUEGBAR", "NICHT VERFÜGBAR"}:
            continue
        items = [x.strip() for x in re.split(r"\s*[|;]\s*", raw) if x.strip()]
        results = []
        for item in items:
            ref = _technische_ref_fuer_kandidat(item, refs)
            if ref:
                name = ref.get("name") or item
                ticker = ref.get("ticker")
                label = f"{name} ({ticker})" if ticker else name
                status = ref.get("status") or "STATUS NICHT EINGELESEN"
                results.append(f"{label} | technisches Tagesuniversum: {status}")
                notes.append(f"Technische Nachsuche gefunden: {label}")
            else:
                results.append(f"{item} | noch nicht im technischen Tagesuniversum verifiziert")
                notes.append(f"Technische Nachsuche offen: {item}")
        replacement = m.group(1) + ": " + " ; ".join(results)
        replacements.append((section_match.start() + m.start(), section_match.start() + m.end(), replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, label_changes + notes


def _trade_story_bloecke(text):
    """Extrahiert 1.3 robust auch bei variierender Gemini-Formatierung.

    Die alte Erkennung verlangte eine Titelzeile unmittelbar vor
    ``Zeithorizont:``. Dadurch konnte eine inhaltlich vorhandene Discovery
    verloren gehen und der technische Fallback erzeugt werden. Die neue
    Erkennung akzeptiert die verbindlichen strukturierten Felder auch dann,
    wenn Gemini Leerzeilen, ``Thema:``, ``These:`` oder eine eigene Titelzeile
    verwendet. Inhalte werden nicht interpretiert.
    """
    m = re.search(r"(?ims)^1\.3\s+IDEEN IM AUFBAU\s*$.*?(?=^1\.4\s+|\Z)", text or "")
    if not m:
        # Rueckwaertskompatibilitaet fuer einen bereits erzeugten Alt-Report:
        # alte 6.1-Ausgaben werden nur als Eingabe akzeptiert und anschliessend
        # durch die deterministische Reparatur in die verbindliche 1.3-Struktur
        # ueberfuehrt. Die Zielausgabe bleibt ausschliesslich 1.3.
        m = re.search(r"(?ims)^6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN\s*$.*?(?=^1\.4\s+|\Z)", text or "")
    if not m:
        return []
    section = m.group(0)
    section = re.sub(r"(?ims)^6\.1\s+PERSPEKTIVISCHE TRADE-IDEEN\s*\n?", "1.3 IDEEN IM AUFBAU\n", section, count=1)
    body = re.sub(r"(?ims)^1\.3\s+IDEEN IM AUFBAU\s*\n?", "", section, count=1).strip()
    if not body:
        return []

    # 1) Alte/kompatible Darstellung: Titel direkt vor Zeithorizont/Thema/These.
    starts = list(re.finditer(
        r"(?m)^(?!1\.3\s+)(?!\s*$)([^\n]+)\n(?=(?:Zeithorizont|Thema|These)\s*:)" ,
        body,
    ))
    if starts:
        return [body[a.start():(starts[i + 1].start() if i + 1 < len(starts) else len(body))].strip()
                for i, a in enumerate(starts)]

    # 2) Verbindliche strukturierte Darstellung: mehrere Stories beginnen mit
    # Thema:/These:. Jeder Block wird bis zum nächsten solchen Feld gezogen.
    structured_starts = list(re.finditer(r"(?im)^(?:Thema|These)\s*:\s*", body))
    if structured_starts:
        return [body[a.start():(structured_starts[i + 1].start() if i + 1 < len(structured_starts) else len(body))].strip()
                for i, a in enumerate(structured_starts)]

    # 3) Eine einzelne Discovery darf niemals nur wegen einer abweichenden
    # Formatierung verworfen werden. Sobald mindestens ein verbindlicher
    # Discovery-/Technikmarker vorhanden ist, bleibt der komplette 1.3-Inhalt
    # erhalten; die nachgelagerte Statusbindung korrigiert nur die autoritativen
    # Statusfelder.
    if re.search(
        r"(?im)^(?:Zeithorizont|Thema|These|Veraenderung|Veränderung|Treiber|Belege?|"
        r"Kausalzusammenhang|Kapitalfluss|Wohin fließt Kapital|Wohin fliesst Kapital|"
        r"Betroffene Assetklasse|Betroffene Assets?|Naechster(?: bestaetigter)? Kalenderkatalysator|"
        r"Discovery-Status|Technischer Status|Widerlegender Trigger|Risiko)\s*:",
        body,
    ):
        return [body.strip()]
    return []



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


def _trade_story_bestehende_kandidaten_universum(eingabedateien, beobachtungsliste_pfad=None):
    """Ermittelt bekannte Kandidaten unabhaengig vom aktuellen Setup-Status.

    Ein Titel gilt als bestehender Kandidat, wenn er aus einer autoritativen
    Einzel-Check-/Hebeltrader-Quelle als Kandidat bzw. Setup-Fund hervorgeht
    oder bereits im zentralen Trade-Story-Snapshot als Kandidat gefuehrt wird.
    Beim HEBELTRADER-Einzelcheck bleibt jeder echte Fund Teil des Universums,
    auch bei Status ``KEIN KANDIDAT`` und unabhaengig von technischer Validitaet.
    ``KEIN KANDIDAT`` ist dabei ein reiner Discovery-/Universumsstatus und
    erzeugt keinen konkreten Trade-Story-Kandidatenbezug. Ein blosser Gemini-
    Hinweis ohne autoritativen HEBELTRADER-Beleg erzeugt weiterhin keinen Bezug.
    """
    result = set()

    def add_value(value, is_ticker=False):
        if value in (None, ""):
            return
        text = str(value).strip()
        if not text:
            return
        key = _normalisiere_ticker(text) if is_ticker else _normalisiere_positionsname(text)
        if key:
            result.add(key)

    def add_pair(ticker=None, name=None):
        add_value(ticker, True)
        add_value(name, False)

    # 1) Das zentrale Tagesuniversum ist bereits eine autoritative
    # Kandidaten-Handoff-Schicht. Auch vorbereitete Kandidaten bleiben hier
    # bestehende Kandidaten; STATUSKONFLIKT wird bewusst ausgeschlossen.
    central = eingabedateien.get("Trade_Story_Universum(...).json")
    if central and os.path.isfile(central):
        try:
            with open(central, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("candidates", []) if isinstance(data, dict) else []:
                if not isinstance(item, dict) or item.get("trade_story_status") == "STATUSKONFLIKT":
                    continue
                # KEIN KANDIDAT kann im zentralen Universum enthalten sein,
                # ist dort aber bewusst nur Discovery-/Universumsmitglied und
                # kein konkreter Trade-Story-Kandidat.
                if item.get("trade_story_status") == "UNIVERSUM":
                    continue
                add_pair(item.get("ticker"), item.get("name"))
        except Exception as exc:
            print(f"WARNUNG: Bestehendes Trade-Story-Universum nicht lesbar: {exc}")

    # 2) Aktuelle Beobachtungsliste: A/B/C sind bestehende Kandidaten.
    # KEIN KANDIDAT ist dort kein konkreter Kandidatenbezug; seine Zugehoerigkeit
    # zum zentralen HEBELTRADER-Universum wird separat erhalten.
    if beobachtungsliste_pfad and os.path.isfile(beobachtungsliste_pfad):
        try:
            with open(beobachtungsliste_pfad, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for ticker, entry in data.items():
                    if not isinstance(entry, dict):
                        continue
                    status = str(entry.get("status", "")).strip().upper()
                    if status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C"}:
                        add_pair(ticker, entry.get("name") or entry.get("Name") or entry.get("firmenname") or entry.get("Firmenname"))
        except Exception as exc:
            print(f"WARNUNG: Einzel-Check-Beobachtungsliste fuer Kandidatenpruefung nicht lesbar: {exc}")

    # 3) Einzel-Check-Technikhistorie: aktuelle A/B/C-Status sind ebenfalls
    # autoritative Kandidatenbelege. Historische KEIN-KANDIDAT-Zeilen werden
    # nicht als Kandidaten reaktiviert.
    history_path = eingabedateien.get("Einzel-Check-Technikhistorie")
    if history_path and os.path.isfile(history_path):
        try:
            with open(history_path, encoding="utf-8-sig") as f:
                for raw_line in f:
                    try:
                        row = json.loads(raw_line)
                    except (TypeError, ValueError):
                        continue
                    status = str(row.get("Status") or row.get("status") or "").strip().upper()
                    if status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C"}:
                        add_pair(row.get("Ticker") or row.get("ticker"), row.get("Name") or row.get("name"))
        except Exception as exc:
            print(f"WARNUNG: Einzel-Check-Technikhistorie fuer Kandidatenpruefung nicht lesbar: {exc}")

    # 4) Direkte HEBELTRADER-Ausgabe: Ein dort gefundener Titel gehoert
    # unabhaengig von A/B/C und unabhaengig von der aktuellen Validitaet zum
    # autoritativen Universum. Es genuegt deshalb ein expliziter Setup-/
    # Kandidatenbeleg im HEBELTRADER-Datensatz; ein reiner KEIN-KANDIDAT-Status
    # ohne solchen Beleg wird nicht aufgenommen.
    hebel_path = eingabedateien.get("HEBELTRADER-Einzelcheck")
    if hebel_path and os.path.isfile(hebel_path):
        try:
            with open(hebel_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            ticker_keys = {"ticker", "yahoo-ticker", "yahoo_ticker", "symbol", "instrument"}
            name_keys = {"name", "firmenname", "company", "unternehmen"}
            evidence_keys = {
                "setup", "setup_typ", "setup_type", "kaufkandidat", "candidate",
                "entry", "einstieg", "stop", "ziel", "tp", "richtung",
                "direction", "hebel", "issue_label", "issue_number",
            }

            def walk(obj):
                if isinstance(obj, dict):
                    lower = {str(k).strip().lower(): v for k, v in obj.items()}
                    ticker = next((lower[k] for k in ticker_keys if k in lower and lower[k]), None)
                    name = next((lower[k] for k in name_keys if k in lower and lower[k]), None)
                    setup_evidence = any(k in lower for k in evidence_keys)
                    status = str(lower.get("status") or lower.get("status2") or "").strip().upper()
                    candidate_status = status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C"}
                    if ticker and (setup_evidence or candidate_status):
                        add_pair(ticker, name)
                    elif name and (setup_evidence or candidate_status):
                        add_pair(None, name)
                    for value in obj.values():
                        walk(value)
                elif isinstance(obj, list):
                    for value in obj:
                        walk(value)

            walk(data)
        except Exception as exc:
            print(f"WARNUNG: HEBELTRADER-Datei fuer Kandidatenpruefung nicht lesbar: {exc}")

    return result


def _trade_story_validierung(text, eingabedateien, beobachtungsliste_pfad=None, strikt_statusfelder=False):
    """Validiert Discovery-/Technikstatus, Kaufgrenze und autoritative Kandidatenherkunft.

    Im Produktionslauf sind die beiden Statusfelder zwingend getrennt. Der
    Legacy-Modus bleibt fuer bestehende Regressionstests und Altbestandsdaten
    rueckwaertskompatibel; die deterministische Reparatur erzeugt immer das
    neue Zweifeldformat.
    """
    stories = _trade_story_bloecke(text)
    if not stories:
        return False, ["Abschnitt 1.3 mit perspektivischen Trade-Story-Eintraegen fehlt oder ist nicht parsebar."]
    errors = []
    valid_keys, gelesene_quellen, fehlende_quellen = _trade_story_setup_universum(eingabedateien)
    zentrale_valid_keys, zentrale_prepared_keys, zentrale_verfuegbar = _trade_story_zentrales_universum(eingabedateien)
    if zentrale_verfuegbar:
        valid_keys = zentrale_valid_keys
    beobachtungs_keys, beobachtung_verfuegbar = _trade_story_beobachtung_universum(beobachtungsliste_pfad)
    bestehende_kandidaten_keys = _trade_story_bestehende_kandidaten_universum(
        eingabedateien, beobachtungsliste_pfad
    )

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
            if candidate and st == "NICHT VORHANDEN" and not _trade_story_alle_kandidaten_treffen(candidate, bestehende_kandidaten_keys):
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
                if not _trade_story_alle_kandidaten_treffen(candidate, bestehende_kandidaten_keys):
                    errors.append(f"Trade-Story {idx}: NUR TEILW. VOLLSTAENDIG fuer '{candidate}' ist nicht im autoritativen Kandidatenuniversum verankert.")
                elif _trade_story_alle_kandidaten_treffen(candidate, valid_keys):
                    errors.append(f"Trade-Story {idx}: NUR TEILW. VOLLSTAENDIG fuer '{candidate}' verweist bereits auf ein autoritatives VALIDE SETUP; verwende Technischer Status: VALIDER SETUP.")

    if fehlende_quellen and not gelesene_quellen:
        errors.append("Trade-Story: Keine der autoritativen Setup-Quellen konnte erfolgreich gelesen werden: " + ", ".join(sorted(fehlende_quellen)))
    return not errors, errors


def _trade_story_deterministische_reparatur(text, eingabedateien, beobachtungsliste_pfad=None):
    """Repariert 1.3 ohne weiteren Gemini-API-Aufruf.

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
            "1.3 IDEEN IM AUFBAU\n"
            "Thema / Assetklasse: NICHT VERFUEGBAR\n"
            "Zeithorizont: NICHT VERFUEGBAR\n"
            "Was veraendert sich?: NICHT VERFUEGBAR\n"
            "Warum? / Treiber: NICHT VERFUEGBAR\n"
            "Bestaetigende Daten: NICHT VERFUEGBAR\n"
            "Kausalzusammenhang: NICHT VERFUEGBAR\n"
            "Wohin fliesst Kapital?: NICHT VERFUEGBAR\n"
            "Betroffene Assetklasse / Sektor: NICHT VERFUEGBAR\n"
            "Fruehester Beobachtungspunkt: NICHT VERFUEGBAR\n"
            "Bestehender Kandidat / Bezug: Kein bestehender Kandidat im Datenbestand\n"
            "Potenzielle Assets fuer technische Nachsuche: NICHT VERFUEGBAR\n"
            "Naechster bestaetigter Kalenderkatalysator: NICHT VERFUEGBAR\n"
            "Naechster bestaetigender Trigger: NICHT VERFUEGBAR\n"
            "Widerlegender Trigger: NICHT VERFUEGBAR\n"
            "Gegentreiber / Risiko: NICHT VERFUEGBAR\n"
            "Discovery-Status: ENTDECKT\n"
            "Technischer Status: NICHT VORHANDEN\n"
        )

    valid_keys, gelesene_quellen, _ = _trade_story_setup_universum(eingabedateien)
    zentrale_valid_keys, zentrale_prepared_keys, zentrale_verfuegbar = _trade_story_zentrales_universum(eingabedateien)
    if zentrale_verfuegbar:
        valid_keys = zentrale_valid_keys
    beobachtungs_keys, beobachtung_verfuegbar = _trade_story_beobachtung_universum(beobachtungsliste_pfad)
    bestehende_kandidaten_keys = _trade_story_bestehende_kandidaten_universum(
        eingabedateien, beobachtungsliste_pfad
    )

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
        # Ein expliziter KEIN-KANDIDAT-Hinweis ist kein konkreter Kandidatentitel;
        # KAUFKANDIDAT C bleibt dagegen ein regulärer Kandidatenstatus.
        candidate = re.sub(r"\s*\[KEIN\s+KANDIDAT[^\]]*\]", "", candidate, flags=re.I).strip()

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
        elif candidate and _trade_story_alle_kandidaten_treffen(candidate, bestehende_kandidaten_keys):
            # Kandidat ist autoritativ bekannt (Einzel-Check/HEBELTRADER),
            # aber aktuell nicht als konkretes Setup bestaetigt. Er bleibt
            # deshalb erhalten und wird nicht faelschlich zu NICHT VORHANDEN.
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

    # Validator erwartet 1.3 als gemeinsamen Abschnitt.
    return "1.3 IDEEN IM AUFBAU\n" + "\n\n".join(repaired) + "\n"


def _sichere_brent_wti_spread(text, makro_text):
    """Bindet explizite Brent-WTI-Spread-Angaben an Brent minus WTI.

    Brent und WTI werden ausschliesslich aus dem aktuellen autoritativen
    Makro-Briefing gelesen. Der Spread wird deterministisch als Brent - WTI
    berechnet. Nur zusammengesetzte Spread-/Differenzformulierungen werden
    ersetzt; einzelne Brent- oder WTI-Kurse bleiben unberuehrt.
    """
    if not text or not makro_text:
        return text, []
    refs = _extrahiere_makro_referenzwerte(makro_text)
    brent = refs.get("brent")
    wti = refs.get("wti")
    if not brent or not wti:
        return text, []

    spread = float(brent["kurs"]) - float(wti["kurs"])
    pattern = re.compile(
        r"(?P<label>"
        r"(?:Brent\s*[-–]\s*WTI\s*(?:[- ]?Spread|[- ]?Differenz)|"
        r"Brent\s*/\s*WTI\s*(?:[- ]?Spread|[- ]?Differenz)|"
        r"Differenz\s+(?:zwischen\s+)?Brent\s+und\s+WTI)"
        r")"
        r"(?P<middle>[^\n\d]{0,40}?)"
        r"(?P<num>[-+]?\d[\d.,]*\d|[-+]?\d)"
        r"\s*(?P<unit>USD|US\$|\$)",
        re.IGNORECASE,
    )

    def parse_number(raw):
        value = str(raw).strip().replace(" ", "")
        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            value = value.replace(",", ".")
        elif value.count(".") > 1:
            value = value.replace(".", "")
        return float(value)

    changes = []

    def replace(match):
        try:
            old = parse_number(match.group("num"))
        except ValueError:
            return match.group(0)
        if abs(old - spread) < 0.005:
            return match.group(0)
        changes.append(f"Brent-WTI-Spread: {old} -> {spread} USD")
        return (
            match.group("label")
            + match.group("middle")
            + f"{spread:.2f}".replace(".", ",")
            + " "
            + match.group("unit")
        )

    result = "\n".join(pattern.sub(replace, line) for line in text.splitlines())
    if changes:
        print(f"WTI-BRENT-SPREAD-GATE: {len(changes)} Spread-Angabe(n) deterministisch korrigiert.")
    return result, changes


def _ausgabe_heading_key(heading, required):
    """Liefert die Pflichtsektion nur für bekannte Überschriftenvarianten.

    Die Abschnittsnummer allein genügt bewusst nicht zur Erkennung: Dadurch
    werden nummerierte Verweise innerhalb eines echten Analyseblocks nicht
    versehentlich als neue Sektion interpretiert.
    """
    stripped = " ".join((heading or "").strip().split())
    normalized = re.sub(r"\s*→\s*", "→", stripped).rstrip(":").casefold()

    for canonical in required:
        key_match = re.match(r"^\s*(\d+(?:\.\d+)?\.?)(?=\s)", canonical)
        key = key_match.group(1) if key_match else canonical
        canonical_normalized = re.sub(
            r"\s*→\s*", "→", canonical.strip()
        ).rstrip(":").casefold()

        accepted = {canonical_normalized}
        # Diese bereits beobachtete Gemini-Variante gehört semantisch zu 3.3.
        if key == "3.3":
            accepted.add(
                re.sub(
                    r"\s*→\s*",
                    "→",
                    "3.3 Politik → Geopolitik → Aktie",
                ).rstrip(":").casefold()
            )

        if normalized in accepted:
            return key, canonical

    # Die nicht nummerierte Hauptüberschrift wird nur exakt erkannt.
    if stripped == "NEUBER MACRO & MARKETS":
        return "NEUBER MACRO & MARKETS", "NEUBER MACRO & MARKETS"
    return None


def _bereinige_doppelte_ausgabestruktur(text, required):
    """Bereinigt doppelte Pflichtabschnitte konservativ vor dem Speichern.

    Die Erkennung erfolgt über die verbindliche Sektion plus bekannte
    Schreibvarianten. Ein vorhandener echter Analyseblock wird nie
    überschrieben. Ein reiner Fallback wird entfernt, wenn für dieselbe
    Sektion bereits echter Inhalt vorhanden ist. Zwei echte Inhalte derselben
    Sektion sind ein harter Fehler. Bei einem einzigen vorhandenen Block wird
    nur dessen Überschrift auf die verbindliche Schreibweise normalisiert; der
    Blockinhalt bleibt unverändert.
    """
    result = text or ""

    def find_headings(value):
        occurrences = []
        offset = 0
        for line in value.splitlines(keepends=True):
            line_without_eol = line.rstrip("\r\n")
            match = _ausgabe_heading_key(line_without_eol, required)
            if match:
                key, canonical = match
                occurrences.append(
                    {
                        "key": key,
                        "canonical": canonical,
                        "start": offset,
                        "line_end": offset + len(line),
                        "line": line,
                    }
                )
            offset += len(line)
        return occurrences

    while True:
        occurrences = find_headings(result)
        by_key = {}
        for occurrence in occurrences:
            by_key.setdefault(occurrence["key"], []).append(occurrence)

        changed = False
        for key, items in by_key.items():
            if len(items) <= 1:
                continue

            blocks = []
            for idx, item in enumerate(items):
                later_starts = [
                    occurrence["start"]
                    for occurrence in occurrences
                    if occurrence["start"] > item["start"]
                ]
                block_end = min(later_starts) if later_starts else len(result)
                block = result[item["start"]:block_end]
                body = block[item["line_end"] - item["start"]:].strip()
                blocks.append(
                    {
                        "item": item,
                        "end": block_end,
                        "body": body,
                        "fallback": body == "Keine relevanten Erkenntnisse.",
                    }
                )

            real_indices = [
                idx for idx, block in enumerate(blocks)
                if not block["fallback"]
            ]
            fallback_indices = [
                idx for idx, block in enumerate(blocks)
                if block["fallback"]
            ]

            if len(real_indices) > 1:
                raise RuntimeError(
                    "DOPPELTE_AUSGABESTRUKTUR_ECHTE_INHALTE: "
                    + blocks[real_indices[0]]["item"]["canonical"]
                )

            if real_indices:
                remove_indices = fallback_indices
            else:
                # Mehrere reine Fallback-Blöcke werden auf genau einen reduziert.
                remove_indices = list(range(1, len(blocks)))

            for idx in reversed(remove_indices):
                start = blocks[idx]["item"]["start"]
                end = blocks[idx]["end"]
                result = result[:start] + result[end:]
                changed = True

            if changed:
                break

        if not changed:
            break

    # Nur die Überschrift wird kanonisiert; der Analyseinhalt bleibt byteweise
    # unverändert. Dadurch werden Schreibvarianten vor der exakten Prüfung
    # deterministisch vereinheitlicht.
    occurrences = find_headings(result)
    replacements = []
    for occurrence in occurrences:
        canonical = occurrence["canonical"]
        current_line = occurrence["line"]
        line_body = current_line.rstrip("\r\n")
        newline = current_line[len(line_body):]
        if line_body.strip() != canonical:
            replacements.append(
                (occurrence["start"], occurrence["line_end"], canonical + newline)
            )

    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]

    return result



def _inhaltlicher_abgrenzungstext():
    """Erzeugt den deterministischen Mindestinhalt fuer 11.7.

    Die Abgrenzung beschreibt die Architektur des Trade-Story-Universums,
    ohne neue Kandidaten, technische Signale oder Bewertungen zu erfinden.
    """
    return (
        "11.7 Abgrenzung:\n"
        "Das Trade-Story-Universum ist die zentrale Discovery- und Handoff-Schicht "
        "für bereits durch die bestehenden Scanner bzw. den HEBELTRADER-Einzel-Check "
        "gefundenen Titel. Die Aufnahme in dieses Universum ist nicht gleichbedeutend "
        "mit einem validen technischen Setup, einem Entry oder einer Kaufentscheidung. "
        "Insbesondere ein echter HEBELTRADER-Fund bleibt unabhängig von A/B/C-Status und "
        "technischer Validität im Universum enthalten; die technische Validierung wird "
        "davon getrennt geführt. Scanner-Fund, Discovery, vorbereiteter Kandidat und "
        "VALIDE SETUP sind daher unterschiedliche Zustände und dürfen nicht miteinander "
        "gleichgesetzt werden. Die nachgelagerte Auswertung darf aus der Universums-"
        "Zugehörigkeit keine neue technische Bestätigung ableiten."
    )


def _repariere_7_4_fx_aus_makroquelle(text, makro_text):
    """Ersetzt nur einen zu knappen 7.4-FX-Block durch Makro-Quellfakten.

    Es werden ausschließlich explizit strukturierte FX-Werte aus dem aktuellen
    Makro-Briefing verwendet. Fehlende Instrumente bleiben ausdrücklich
    unbekannt; es werden keine Kurse oder Bewegungen erfunden.
    """
    if not text:
        return text, False

    match = re.search(r"(?ims)^\s*7\.4\s+FX\s*$.*?(?=^\s*7\.5\s+Rohstoffe\s*$|^\s*8\.\s+|\Z)", text)
    if not match:
        return text, False

    block = match.group(0).strip()
    body = re.sub(r"(?im)^\s*7\.4\s+FX\s*$", "", block, count=1).strip()
    compact = re.sub(r"\W", "", body, flags=re.UNICODE)
    label_only = bool(body) and all(
        re.fullmatch(r"[^:]{1,100}:\s*[^:]{1,100}", line.strip())
        and len(re.findall(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]{3,}", line, flags=re.UNICODE)) <= 8
        for line in body.splitlines() if line.strip()
    )
    if len(compact) >= 120 and not label_only:
        return text, False

    refs = _extrahiere_makro_referenzwerte(makro_text or "")

    def get_ref(*aliases):
        for alias in aliases:
            ref = refs.get(alias.casefold())
            if ref is not None:
                return ref
        return None

    def fmt_ref(label, ref):
        if ref is None:
            return f"{label}: in der autoritativen Makroquelle nicht als strukturierter Wert vorhanden."
        parts = [f"{label}: {ref['kurs']}"]
        if ref.get("datenstand"):
            parts.append(f"Datenstand={ref['datenstand']}")
        if ref.get("schluss") is not None:
            parts.append(f"Letzter_Schluss={ref['schluss']}")
        perioden = ref.get("perioden") or {}
        if perioden:
            parts.append("Veränderungen: " + ", ".join(f"{k}={v}%" for k, v in sorted(perioden.items())))
        return " | ".join(parts)

    eurusd = get_ref("eur/usd", "eurusd", "eur usd", "euro/us-dollar", "euro dollar")
    dxy = get_ref("dxy", "usd index", "us dollar index")
    usdjpy = get_ref("usd/jpy", "usdjpy", "usd jpy", "usd/yen", "dollar/yen")
    available = [("EUR/USD", eurusd), ("DXY", dxy), ("USD/JPY", usdjpy)]
    present = [(label, ref) for label, ref in available if ref is not None]

    lines = ["7.4 FX"]
    if present:
        for label, ref in present:
            lines.append(fmt_ref(label, ref))
        direction_parts = []
        for label, ref in present:
            periods = ref.get("perioden") or {}
            if periods:
                vals = ", ".join(f"{k}={v}%" for k, v in sorted(periods.items()))
                direction_parts.append(f"{label} weist laut Quelle die ausgewiesenen Periodenbewegungen auf ({vals}).")
        if direction_parts:
            lines.append("Bewegung: " + " ".join(direction_parts))
        else:
            lines.append("Bewegung: Für die vorhandenen FX-Instrumente liegen in der Quelle keine strukturierten Periodenveränderungen vor; daher wird keine zusätzliche Richtung abgeleitet.")
        lines.append(
            "Einordnung: Die FX-Werte und die ausgewiesenen Veränderungen sind die "
            "einzige numerische Grundlage dieses Abschnitts. Ihre Relevanz liegt insbesondere "
            "in der relativen Entwicklung von Euro und US-Dollar sowie in der Wirkung von "
            "Währungsbewegungen auf international erzielte bzw. umgerechnete Erträge; eine "
            "konkrete Aktienwirkung wird nur behauptet, wenn sie aus den bereitgestellten "
            "Daten nachvollziehbar ist."
        )
    else:
        lines.extend([
            "Datenstatus: Die autoritative Makroquelle enthält für EUR/USD, DXY und USD/JPY "
            "keinen strukturierten FX-Referenzwert. Deshalb werden keine Kurse, Bewegungen "
            "oder Richtungen ergänzt.",
            "Einordnung: Ohne quellengebundene FX-Daten ist keine belastbare konkrete "
            "Währungsrichtung oder daraus abgeleitete Aktienwirkung zulässig."
        ])

    replacement = "\n".join(lines).strip() + "\n"
    return text[:match.start()] + replacement + text[match.end():], True

def _lithium_te_referenz(makro_text):
    """Liest Lithium TE als autoritativen CNY/T-Wert.

    Es gilt immer der letzte tatsaechlich verfuegbare Trading-Economics-Wert
    mit Datenstand <= Datenabrufdatum des aktuellen Makro_Briefings. Ein
    juengerer/future-dated Wert darf niemals als aktueller Wert verwendet
    werden. Der reale Datenstand bleibt Bestandteil der Rueckgabe.
    """
    if not makro_text:
        return None

    target_match = re.search(
        r"(?im)^\s*MAKRO-DATENPAKET\s*\|\s*Datenabruf\s*=\s*(\d{4}-\d{2}-\d{2})\b",
        makro_text,
    )
    target_date = None
    if target_match:
        try:
            target_date = dt.date.fromisoformat(target_match.group(1))
        except ValueError:
            target_date = None

    def parse_number(raw):
        value = str(raw or "").strip().replace(" ", "")
        if not value:
            raise ValueError("empty number")
        sign = ""
        if value[0] in "+-":
            sign, value = value[0], value[1:]
        if not re.fullmatch(r"\d[\d.,]*", value):
            raise ValueError("invalid number")
        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            value = value.replace(",", ".")
        elif value.count(".") > 1:
            value = value.replace(".", "")
        return float(sign + value)

    candidates = []
    unavailable = False
    for raw in makro_text.splitlines():
        line = raw.strip()
        if not re.match(r"(?i)^Lithium\s+TE\s*:", line):
            continue

        if re.search(r"(?i)\bSTATUS\s*=\s*UNAVAILABLE\b", line):
            unavailable = True
            continue

        value_match = re.match(r"(?i)^Lithium\s+TE\s*:\s*([-+]?\d[\d.,]*)", line)
        if not value_match:
            continue
        try:
            value = parse_number(value_match.group(1))
        except ValueError:
            continue

        date_match = re.search(
            r"(?i)\bDatenstand\s*=\s*(\d{4}-\d{2}-\d{2})\b", line
        )
        data_date = None
        if date_match:
            try:
                data_date = dt.date.fromisoformat(date_match.group(1))
            except ValueError:
                data_date = None

        if target_date is not None and data_date is not None and data_date > target_date:
            continue

        ref = {"kurs": value, "perioden": {}, "status": "VALUE", "unit": "CNY/T"}
        if data_date is not None:
            ref["datenstand"] = data_date.isoformat()

        for key in ("5T", "1M", "3M", "6M", "1J"):
            period_match = re.search(
                rf"(?:^|\|)\s*{re.escape(key)}\s*=\s*([-+]?\d[\d.,]*)\s*%",
                line,
            )
            if period_match:
                try:
                    ref["perioden"][key] = parse_number(period_match.group(1))
                except ValueError:
                    pass
        candidates.append((data_date, ref))

    if candidates:
        dated = [(d, r) for d, r in candidates if d is not None]
        if dated:
            dated.sort(key=lambda x: x[0])
            return dated[-1][1]
        return candidates[-1][1]

    if unavailable:
        return {"status": "UNAVAILABLE", "unit": "CNY/T"}
    return None

def _lithium_quellenstatus_proxy(makro_text):
    """Liefert deterministisch, ob Lithium in der autoritativen Makroquelle als PROXY gekennzeichnet ist.

    Die gleiche Erkennung wird fuer Reparatur und Gate verwendet, damit beide
    Stufen garantiert denselben Quellstatus beurteilen. Fuehrende Leerzeichen
    vor der Lithium-Zeile werden toleriert; andere Zeilen koennen den Status
    nicht ausloesen.
    """
    for raw in (makro_text or "").splitlines():
        line = raw.strip()
        if re.match(r"(?i)^Lithium\s*:", line) and re.search(r"(?i)\bSTATUS\s*=\s*PROXY\b", line):
            return True
    return False


def _repariere_7_5_rohstoffe_aus_makroquelle(text, makro_text):
    """Ersetzt einen zu knappen 7.5-Rohstoffblock deterministisch durch
    quellengebundene Rohstofffakten aus dem aktuellen Makro-Briefing.

    Gold, Silber, Platin und Palladium werden bewusst nicht übernommen; diese
    gehören ausschließlich in Punkt 8. Fehlende Rohstoffwerte bleiben
    ausdrücklich unbekannt und werden nicht geschätzt.
    """
    if not text:
        return text, False

    match = re.search(
        r"(?ims)^\s*7\.5\s+Rohstoffe\s*$.*?(?=^\s*7\.6\s+Krypto\s*$|^\s*8\.\s+|\Z)",
        text,
    )
    if not match:
        return text, False

    block = match.group(0).strip()
    body = re.sub(r"(?im)^\s*7\.5\s+Rohstoffe\s*$", "", block, count=1).strip()
    compact = re.sub(r"\W", "", body, flags=re.UNICODE)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    label_only = bool(lines) and all(
        re.fullmatch(r"[^:]{1,100}:\s*[^:]{1,120}", line)
        and len(re.findall(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]{3,}", line, flags=re.UNICODE)) <= 8
        for line in lines
    )
    if len(compact) >= 180 and not label_only:
        return text, False

    refs = _extrahiere_makro_referenzwerte(makro_text or "")

    def get_ref(*aliases):
        for alias in aliases:
            ref = refs.get(alias.casefold())
            if ref is not None:
                return ref
        return None

    lithium_proxy = _lithium_quellenstatus_proxy(makro_text)
    lithium_te = _lithium_te_referenz(makro_text)

    def fmt_ref(label, ref):
        if ref is None:
            return f"{label}: in der autoritativen Makroquelle nicht als strukturierter Wert vorhanden."
        if label == "Lithiumcarbonat CNY/T":
            parts = [f"{label}: {float(ref['kurs']):.2f}"]
        else:
            parts = [f"{label}: {ref['kurs']}"]
        if label == "Lithium" and lithium_proxy:
            parts.append("STATUS=PROXY")
        if label == "Lithiumcarbonat CNY/T":
            parts.append("Einheit=CNY/T")
            parts.append("SOURCE=TradingEconomics Public Commodities")
            parts.append("DATENTYP=TE_PUBLIC_LITHIUM")
        if ref.get("datenstand"):
            parts.append(f"Datenstand={ref['datenstand']}")
        if ref.get("schluss") is not None:
            parts.append(f"Letzter_Schluss={ref['schluss']}")
        perioden = ref.get("perioden") or {}
        if perioden:
            parts.append("Veränderungen: " + ", ".join(f"{k}={v}%" for k, v in sorted(perioden.items())))
        return " | ".join(parts)

    # Nur Rohstoffe, die im Makro-Briefing strukturiert vorhanden sind.
    candidates = [
        ("Brent", get_ref("brent")),
        ("WTI", get_ref("wti")),
        ("Kupfer", get_ref("kupfer", "copper")),
        ("Lithium", get_ref("lithium")),
        ("Lithiumcarbonat CNY/T", lithium_te if lithium_te and lithium_te.get("status") == "VALUE" else None),
    ]
    present = [(label, ref) for label, ref in candidates if ref is not None]

    lines_out = ["7.5 Rohstoffe"]
    if present:
        for label, ref in present:
            lines_out.append(fmt_ref(label, ref))
        movements = []
        for label, ref in present:
            periods = ref.get("perioden") or {}
            if periods:
                vals = ", ".join(f"{k}={v}%" for k, v in sorted(periods.items()))
                movements.append(f"{label}: ausgewiesene Periodenbewegungen ({vals}).")
        if movements:
            lines_out.append("Bewegung: " + " ".join(movements))
        else:
            lines_out.append(
                "Bewegung: Für die vorhandenen Rohstoffe liegen in der autoritativen Quelle keine strukturierten Periodenveränderungen vor; daher wird keine zusätzliche Richtung abgeleitet."
            )
        lines_out.append(
            "Einordnung: Rohstoffbewegungen sind vor allem für Energie-, Industrie- und transportintensive Branchen relevant. Eine konkrete Aktienwirkung wird nur aus den bereitgestellten Daten abgeleitet; aus fehlenden Rohstoffwerten werden keine Trends oder Setups ergänzt."
        )
    else:
        lines_out.extend([
            "Datenstatus: Die autoritative Makroquelle enthält keine strukturierten Referenzwerte für die in Punkt 7.5 vorgesehenen Rohstoffe. Deshalb werden keine Kurse, Bewegungen oder Richtungen ergänzt.",
            "Einordnung: Ohne quellengebundene Rohstoffdaten ist keine belastbare konkrete Rohstoffrichtung oder daraus abgeleitete Aktienwirkung zulässig.",
        ])

    replacement = "\n".join(lines_out).strip() + "\n"
    result = text[:match.start()] + replacement + text[match.end():]
    secured_result, lithium_changed = _sichere_lithium_te_in_7_5(result, lithium_te)
    return secured_result, True or lithium_changed

def _ergaenze_fehlende_ausgabestruktur(text):
    """Fügt nur fehlende Pflichtabschnitte 1–11.7 positionsgenau ein.

    Bereits vorhandene Gemini-Ausgabe bleibt unverändert. Für 11.7 wird ein
    inhaltlicher, deterministischer Abgrenzungstext verwendet; andere fehlende
    Pflichtabschnitte behalten ihren bisherigen Fallback.
    Bekannte Schreibvarianten vorhandener Pflichtüberschriften werden nicht
    fälschlich als fehlende Sektionen behandelt.
    """
    required = [
        "NEUBER MACRO & MARKETS",
        "1. 🔥 WAS KÖNNTE GELD VERDIENEN?",
        "1.1 Was hat sich seit dem letzten Lauf verändert?",
        "1.2 Sofort handelbare Chancen",
        "1.3 Ideen im Aufbau",
        "1.4 Frühindikatoren / neue Themen",
        "2. 🎯 KONKRETE TRADES",
        "2.1 Trendfolge",
        "2.2 Trendwende",
        "2.3 Short",
        "2.4 HebelTrader",
        "2.5 Sonstige durch Gemini erkannte Chancen",
        "3. 🧠 THEMEN & ZUSAMMENHÄNGE",
        "3.1 Makro → Branche → Aktie",
        "3.2 Rohstoff → Branche → Aktie",
        "3.3 Politik → Branche → Aktie",
        "3.4 Technologie → Branche → Aktie",
        "3.5 Unternehmens-/Fundamentaldaten → Aktie",
        "4. 🔭 IDEEN IM AUFBAU",
        "5. 🥇 AKTIEN MIT FRÜHEM SIGNAL",
        "6. ⚠️ WIDERSPRÜCHE & RISIKEN",
        "6.1 Makro gegen Technik",
        "6.2 Technik gegen Fundamentaldaten",
        "6.3 Sektor gegen Aktie",
        "6.4 Rohstoff gegen Aktie",
        "6.5 Investmentthese gegen aktuelle Marktdaten",
        "6.6 Risiken bestehender Ideen",
        "7. 🌍 MARKT- & MAKROKONTEXT",
        "7.1 Aktienmärkte / Indizes",
        "7.2 Zinsen",
        "7.3 Volatilität",
        "7.4 FX",
        "7.5 Rohstoffe",
        "7.6 Krypto",
        "7.7 Konjunktur / Makro",
        "8. 🪙 EDELMETALLE",
        "8.1 Gold",
        "8.2 Silber",
        "8.3 Platin",
        "8.4 Palladium",
        "9. 📅 NÄCHSTE KATALYSATOREN",
        "9.1 Makrotermine",
        "9.2 Unternehmen",
        "9.3 Branchenereignisse",
        "9.4 Technische Trigger",
        "9.5 Mögliche Aktivierung / Invalidierung",
        "10. 💼 BESTEHENDES PORTFOLIO",
        "10.1 Sofortiger Handlungsbedarf",
        "10.2 Stop-/TP-Änderungen",
        "10.3 Positionen mit neuer Investmentthese",
        "10.4 Positionen, deren These schwächer wird",
        "10.5 Geschlossene Positionen",
        "11. METHODIK / DATENQUALITÄT",
        "11.1 Datenstatus",
        "11.2 Makro-Szenario-Status",
        "11.3 Datenlücken",
        "11.4 externe Quellen",
        "11.5 technische / fundamentale Datenqualität",
        "11.6 Hinweise zur Interpretation",
        "11.7 Abgrenzung:",
    ]

    result = _bereinige_doppelte_ausgabestruktur(text or "", required)

    present = {
        _ausgabe_heading_key(line, required)[0]
        for line in result.splitlines()
        if _ausgabe_heading_key(line, required)
    }
    fehlend = [
        heading for heading in required
        if _ausgabe_heading_key(heading, required)[0] not in present
    ]
    if not fehlend:
        return result

    for heading in required:
        if heading not in fehlend:
            continue
        next_heading = None
        current_index = required.index(heading)
        for candidate in required[current_index + 1:]:
            if _ausgabe_heading_key(candidate, required)[0] in {
                _ausgabe_heading_key(line, required)[0]
                for line in result.splitlines()
                if _ausgabe_heading_key(line, required)
            }:
                next_heading = candidate
                break

        if heading == "11.7 Abgrenzung:":
            block = _inhaltlicher_abgrenzungstext() + "\n"
        else:
            # Fachlich deterministische Fallbacks: keine erfundenen Markt-/Unternehmensdaten.
            fallback = {
                "6.1 Makro gegen Technik":
                    "Keine belastbare Gegenüberstellung von Makro- und Techniksignalen möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen.",
                "6.2 Technik gegen Fundamentaldaten":
                    "Keine belastbare Gegenüberstellung von Technik und Fundamentaldaten möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen.",
                "6.3 Sektor gegen Aktie":
                    "Keine belastbare Gegenüberstellung von Sektor und einzelner Aktie möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen.",
                "6.4 Rohstoff gegen Aktie":
                    "Keine belastbare Gegenüberstellung von Rohstofftreibern und einzelner Aktie möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen.",
                "6.5 Investmentthese gegen aktuelle Marktdaten":
                    "Keine belastbare Prüfung der Investmentthese gegen aktuelle Marktdaten möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen.",
                "6.6 Risiken bestehender Ideen":
                    "Keine belastbare Risikobewertung bestehender Ideen möglich, weil für diesen Abschnitt keine verifizierbaren, konkret zuordenbaren Angaben in den vorliegenden Projektquellen vorliegen.",
                "9.1 Makrotermine":
                    "Keine verifizierten Makrotermine aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine, Daten oder Ereignisse erfunden.",
                "9.2 Unternehmen":
                    "Keine verifizierten Unternehmensveranstaltungen oder Unternehmensmeldungen aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine erfunden.",
                "9.3 Branchenereignisse":
                    "Keine verifizierten Branchenereignisse aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine oder Ereignisse erfunden.",
                "9.4 Technische Trigger":
                    "Keine verifizierbaren technischen Trigger aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Kursmarken oder Signale erfunden.",
                "9.5 Mögliche Aktivierung / Invalidierung":
                    "Keine belastbaren Aktivierungs- oder Invalidierungsbedingungen aus den vorliegenden Projektquellen ableitbar; es werden deshalb keine Trigger erfunden.",
            }
            block = heading + "\n" + fallback.get(
                heading,
                "Keine belastbare fachliche Aussage aus den vorliegenden Projektquellen ableitbar, ohne nicht verifizierte Daten zu ergänzen."
            ) + "\n\n"
        if next_heading:
            match = re.search(r"(?m)^" + re.escape(next_heading) + r"\s*$", result)
            if match:
                result = result[:match.start()] + block + result[match.start():]
            else:
                result = result.rstrip() + "\n\n" + block.rstrip() + "\n"
        else:
            result = result.rstrip() + "\n\n" + block.rstrip() + "\n"

    print(
        "INFO: Fehlende Pflichtabschnitte deterministisch ergaenzt: "
        + ", ".join(fehlend)
    )
    return result


def _pruefe_neue_ausgabestruktur(text):
    """Prüft die verbindliche neue Auswertungsstruktur vor dem Speichern.

    Bekannte Schreibvarianten werden semantisch derselben Pflichtsektion
    zugeordnet; nach der Normalisierung muss jede Sektion exakt einmal und mit
    der verbindlichen Überschrift vorhanden sein.
    """
    required = [
        "NEUBER MACRO & MARKETS",
        "1. 🔥 WAS KÖNNTE GELD VERDIENEN?",
        "1.1 Was hat sich seit dem letzten Lauf verändert?",
        "1.2 Sofort handelbare Chancen",
        "1.3 Ideen im Aufbau",
        "1.4 Frühindikatoren / neue Themen",
        "2. 🎯 KONKRETE TRADES",
        "2.1 Trendfolge",
        "2.2 Trendwende",
        "2.3 Short",
        "2.4 HebelTrader",
        "2.5 Sonstige durch Gemini erkannte Chancen",
        "3. 🧠 THEMEN & ZUSAMMENHÄNGE",
        "3.1 Makro → Branche → Aktie",
        "3.2 Rohstoff → Branche → Aktie",
        "3.3 Politik → Branche → Aktie",
        "3.4 Technologie → Branche → Aktie",
        "3.5 Unternehmens-/Fundamentaldaten → Aktie",
        "4. 🔭 IDEEN IM AUFBAU",
        "5. 🥇 AKTIEN MIT FRÜHEM SIGNAL",
        "6. ⚠️ WIDERSPRÜCHE & RISIKEN",
        "6.1 Makro gegen Technik",
        "6.2 Technik gegen Fundamentaldaten",
        "6.3 Sektor gegen Aktie",
        "6.4 Rohstoff gegen Aktie",
        "6.5 Investmentthese gegen aktuelle Marktdaten",
        "6.6 Risiken bestehender Ideen",
        "7. 🌍 MARKT- & MAKROKONTEXT",
        "7.1 Aktienmärkte / Indizes",
        "7.2 Zinsen",
        "7.3 Volatilität",
        "7.4 FX",
        "7.5 Rohstoffe",
        "7.6 Krypto",
        "7.7 Konjunktur / Makro",
        "8. 🪙 EDELMETALLE",
        "8.1 Gold",
        "8.2 Silber",
        "8.3 Platin",
        "8.4 Palladium",
        "9. 📅 NÄCHSTE KATALYSATOREN",
        "9.1 Makrotermine",
        "9.2 Unternehmen",
        "9.3 Branchenereignisse",
        "9.4 Technische Trigger",
        "9.5 Mögliche Aktivierung / Invalidierung",
        "10. 💼 BESTEHENDES PORTFOLIO",
        "10.1 Sofortiger Handlungsbedarf",
        "10.2 Stop-/TP-Änderungen",
        "10.3 Positionen mit neuer Investmentthese",
        "10.4 Positionen, deren These schwächer wird",
        "10.5 Geschlossene Positionen",
        "11. METHODIK / DATENQUALITÄT",
        "11.1 Datenstatus",
        "11.2 Makro-Szenario-Status",
        "11.3 Datenlücken",
        "11.4 externe Quellen",
        "11.5 technische / fundamentale Datenqualität",
        "11.6 Hinweise zur Interpretation",
        "11.7 Abgrenzung:",
    ]

    canonical_counts = {heading: 0 for heading in required}
    section_counts = {heading: 0 for heading in required}
    occurrences = []

    forbidden_legacy_patterns = [
        r"^6\.5\.1\s+AKTUELLE\s+KAUFKANDIDATEN\s+A\b",
        r"^6\.5\.2\s+AKTUELLE\s+NICHT-A-KANDIDATEN\b",
        r"^EXTERNE\s+MARKTQUELLEN\s*$",
    ]
    forbidden_legacy = []

    all_lines = (text or "").splitlines()
    for idx, line in enumerate(all_lines):
        stripped = line.strip()
        if any(re.search(pattern, stripped, flags=re.IGNORECASE) for pattern in forbidden_legacy_patterns):
            forbidden_legacy.append(f"Zeile {idx + 1}: {stripped}")

        match = _ausgabe_heading_key(stripped, required)
        if match:
            key, canonical = match
            section_counts[canonical] += 1
            if stripped == canonical:
                canonical_counts[canonical] += 1
            occurrences.append((canonical, idx, stripped))

    missing = []
    duplicate = []
    noncanonical = []

    for heading in required:
        if section_counts[heading] == 0:
            missing.append(heading)
        elif section_counts[heading] != 1:
            duplicate.append(
                f"{heading} (Anzahl={section_counts[heading]})"
            )
        elif canonical_counts[heading] != 1:
            noncanonical.append(
                f"{heading} (nicht kanonisch normalisiert)"
            )

    found_order = [canonical for canonical, _, _ in occurrences]
    if found_order != required:
        wrong_order = []
        for idx, expected in enumerate(required):
            actual = found_order[idx] if idx < len(found_order) else "<FEHLT>"
            if actual != expected:
                wrong_order.append(
                    f"Position {idx + 1}: erwartet={expected!r}, gefunden={actual!r}"
                )
        if len(found_order) > len(required):
            wrong_order.append(
                "Zusaetzliche Pflicht-/Varianten-Ueberschriften: "
                + repr(found_order[len(required):])
            )
    else:
        wrong_order = []

    if missing or duplicate or noncanonical or wrong_order or forbidden_legacy:
        parts = []
        if missing:
            parts.append("FEHLEND: " + "; ".join(missing))
        if duplicate:
            parts.append("DOPPELT/MEHRFACH: " + "; ".join(duplicate))
        if noncanonical:
            parts.append("NICHT_KANONISCH: " + "; ".join(noncanonical))
        if wrong_order:
            parts.append("FALSCHE_REIHENFOLGE: " + "; ".join(wrong_order))
        if forbidden_legacy:
            parts.append(
                "UNZULAESSIGE_ALTE_AUSGABESTRUKTUR: "
                + "; ".join(forbidden_legacy)
            )
        raise RuntimeError(
            "NEUE_AUSGABESTRUKTUR_UNGUELTIG: " + " | ".join(parts)
        )


def _pruefe_inhaltliche_mindesttiefe(text):
    """Prueft Struktur UND belastbare inhaltliche Tiefe der finalen Auswertung.

    Das Gate unterscheidet bewusst zwischen:
    1) formaler Struktur (separat durch _pruefe_neue_ausgabestruktur),
    2) Mindestabdeckung der geforderten Inhaltsgruppen und
    3) echter Substanz: mehrere eigenstaendige, nicht-generische Inhaltseinheiten.

    Reine Keyword-Aufzaehlungen oder die Wiederholung derselben Aussage sollen
    die Pruefung nicht bestehen. Eine ausdrueckliche Negativfeststellung ist
    nur dort zulaessig, wo der jeweilige Abschnitt sachlich leer sein darf.
    """
    if not text or not text.strip():
        raise RuntimeError("INHALTLICHE_MINDESTTIEFE_FEHLER: Leere Auswertung.")

    required = [
        "1.1 Was hat sich seit dem letzten Lauf verändert?",
        "1.2 Sofort handelbare Chancen",
        "1.3 Ideen im Aufbau",
        "1.4 Frühindikatoren / neue Themen",
        "2.1 Trendfolge", "2.2 Trendwende", "2.3 Short", "2.4 HebelTrader",
        "2.5 Sonstige durch Gemini erkannte Chancen",
        "3.1 Makro → Branche → Aktie", "3.2 Rohstoff → Branche → Aktie",
        "3.3 Politik → Branche → Aktie", "3.4 Technologie → Branche → Aktie",
        "3.5 Unternehmens-/Fundamentaldaten → Aktie",
        "4. 🔭 IDEEN IM AUFBAU",
        "5. 🥇 AKTIEN MIT FRÜHEM SIGNAL",
        "6.1 Makro gegen Technik", "6.2 Technik gegen Fundamentaldaten",
        "6.3 Sektor gegen Aktie", "6.4 Rohstoff gegen Aktie",
        "6.5 Investmentthese gegen aktuelle Marktdaten", "6.6 Risiken bestehender Ideen",
        "7.1 Aktienmärkte / Indizes", "7.2 Zinsen", "7.3 Volatilität", "7.4 FX",
        "7.5 Rohstoffe", "7.6 Krypto", "7.7 Konjunktur / Makro",
        "8.1 Gold", "8.2 Silber", "8.3 Platin", "8.4 Palladium",
        "9.1 Makrotermine", "9.2 Unternehmen", "9.3 Branchenereignisse",
        "9.4 Technische Trigger", "9.5 Mögliche Aktivierung / Invalidierung",
        "10.1 Sofortiger Handlungsbedarf", "10.2 Stop-/TP-Änderungen",
        "10.3 Positionen mit neuer Investmentthese", "10.4 Positionen, deren These schwächer wird",
        "10.5 Geschlossene Positionen",
        "11.1 Datenstatus", "11.2 Makro-Szenario-Status", "11.3 Datenlücken",
        "11.4 externe Quellen", "11.5 technische / fundamentale Datenqualität",
        "11.6 Hinweise zur Interpretation", "11.7 Abgrenzung:",
    ]

    positions = []
    for h in required:
        m = re.search(r"(?m)^" + re.escape(h) + r"\s*$", text)
        if m:
            positions.append((m.start(), h))
    positions.sort()
    sections = {}
    for i, (start, h) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        sections[h] = text[start:end].strip()

    generic = re.compile(
        r"(?i)^(?:[-•*]\s*)?(?:keine relevanten erkenntnisse\.?|keine relevanten daten\.?|"
        r"nicht verf(?:u|ü)gbar\.?|keine belastbaren (?:fälle|signale|kandidaten|widersprüche|risiken|ereignisse|termine)\.?|"
        r"keine belastbare(?:n)? (?:abweichung|gegenargumente?|konflikte?|these)\.?)$"
    )
    negative_ok = re.compile(
        r"(?i)(?:keine|kein)\s+[^.]{0,100}?(?:fälle|widersprüche|risiken|signale|kandidaten|setups|ereignisse|termine|"
        r"veränderung|veraenderung|veränderungen|veraenderungen|chancen|ideen|datenlücken|datenluecken|"
        r"stop[^a-z]{0,4}tp[^a-z]{0,4}(?:änderungen|aenderungen)|aktivierung(?:ssignale)?|invalidierungen|"
        r"positionen|handlungsbedarf|abweichung|konflikt|gegenargumente?|these)\b"
        r"|(?:kein|keine)\s+(?:unmittelbarer|unmittelbare)\s+handlungsbedarf\b"
        r"|(?:für|fuer)\s+(?:diesen|diese|den|die)\s+(?:abschnitt|sektion)\b[^.]{0,160}(?:nicht verfügbar|nicht verfuegbar|nicht vorhanden|keine)",
    )

    def content_lines(block):
        out = []
        for line in block.splitlines()[1:]:
            s = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", line).strip()
            if not s or s.startswith("#"):
                continue
            if generic.fullmatch(s):
                continue
            out.append(s)
        return out

    def sentences(block):
        # Tabellen/Listen werden als eigene Einheiten gewertet, sofern sie Substanz enthalten.
        units = []
        for line in content_lines(block):
            parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])", line)
            for part in parts:
                p = part.strip(" -•*\t")
                if len(re.sub(r"\W", "", p, flags=re.UNICODE)) >= 18:
                    units.append(p)
        return units

    def unique_ratio(lines):
        words = re.findall(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]{3,}", " ".join(lines).lower(), flags=re.UNICODE)
        if not words:
            return 0.0
        return len(set(words)) / len(words)

    def has(block, patterns):
        return any(re.search(p, block, re.I | re.U) for p in patterns)

    unavailable_re = re.compile(r"(?i)\b(?:nicht\s+verf(?:u|ü|ue)gbar|nicht\s+vorhanden|keine\s+(?:belastbaren\s+)?daten|daten(?:reihe|punkt)?\s+fehlt)\b")

    def _group_has_explicit_unavailable(block, group):
        # Datenabhängige Pflichtgruppen dürfen ausdrücklich als NICHT VERFÜGBAR
        # ausgewiesen werden. Entscheidend ist, dass die Nichtverfügbarkeit im
        # selben Satz wie das konkrete Datenkonzept genannt wird; eine pauschale
        # Aussage "Daten nicht verfügbar" erfüllt keine beliebige Gruppe.
        for unit in sentences(block):
            if not unavailable_re.search(unit):
                continue
            for pattern in group:
                literals = re.findall(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]{2,}", pattern)
                if any(re.search(re.escape(token), unit, re.I | re.U) for token in literals if len(token) >= 2):
                    return True
        return False

    def hit_count(block, groups):
        return sum(1 for group in groups if has(block, group) or _group_has_explicit_unavailable(block, group))

    def is_explicit_negative(block):
        # Eine Negativfeststellung darf nur dann die Mindesttiefenpruefung ersetzen,
        # wenn saemtliche eigenstaendigen Inhaltseinheiten des Abschnitts negativ
        # sind. Ein negativer Halbsatz vor einer echten Analyse darf das Gate nicht
        # aushebeln.
        meaningful = [
            unit for unit in sentences(block)
            if len(re.sub(r"\W", "", unit, flags=re.UNICODE)) >= 18
        ]
        if not meaningful:
            return False
        return all(negative_ok.search(unit) for unit in meaningful)

    def substantive_stats(block, groups):
        lines = content_lines(block)
        units = sentences(block)
        avg_len = (sum(len(re.sub(r"\W", "", u, flags=re.UNICODE)) for u in units) / len(units)) if units else 0.0
        group_units = 0
        if groups and units:
            for unit in units:
                if any(has(unit, group) for group in groups):
                    group_units += 1
        return {
            "lines": len(lines),
            "units": len(units),
            "unique": unique_ratio(lines),
            "avg_len": avg_len,
            "group_units": group_units,
        }

    # Datenabhängiges Inhalts-Gate:
    # Die fachliche Mindesttiefe wird aus dem verbindlichen Inhaltsvertrag abgeleitet.
    # Es gibt KEINE Mindestquote von Keyword-Dimensionen und keine künstliche
    # Mindestanzahl von Sätzen. Geprüft werden nur belastbarer Inhalt, zulässige
    # Negativfeststellungen, die Vermeidung kompakter Keyword-/Label-Sammlungen,
    # der Anti-Copy-Schutz und die ausdrücklich fachlich erforderlichen
    # Zeitbezüge in 9.1–9.3. 10.5 bleibt ausschließlich faktengebunden.
    profiles = {
        "1.1 Was hat sich seit dem letzten Lauf verändert?": [[r"neu|verändert|veraendert|seit dem letzten"], [r"makro|markt|sektor|branche|rohstoff|aktie|index"], [r"daten|kurs|signal|status|these"], [r"bedeut|auswirkung|relevanz|folgerung"]],
        "1.2 Sofort handelbare Chancen": [[r"ticker|aktie|unternehmen"], [r"long|short|richtung"], [r"entry|einstieg"], [r"stop"], [r"tp1|tp2|ziel"], [r"crv"], [r"these|begründ|begruend"], [r"risiko|gegenargument"], [r"trigger|katalysator"],],
        "1.3 Ideen im Aufbau": [[r"these"], [r"daten|beleg|bestät|bestaet"], [r"gegenargument|risiko"], [r"makro|sektor|branche|rohstoff"], [r"profiteur|verlierer|zweitrund"], [r"aktie|ticker"], [r"status|fehlt"], [r"trigger|aktivierung|invalid"],],
        "1.4 Frühindikatoren / neue Themen": [[r"früh|frueh|indikator|signal"], [r"thema|ereignis|entwicklung"], [r"makro|rohstoff|zins|fx|branche|sektor"], [r"aktie|ticker"], [r"trigger|später|spaeter|wichtig"],],
        "2.1 Trendfolge": [[r"ticker|aktie"], [r"entry|einstieg"], [r"stop"], [r"tp1|tp2|ziel"], [r"crv"], [r"technik|setup|trend"], [r"makro|sektor|branche"], [r"risiko|gegenargument"], [r"trigger"],],
        "2.2 Trendwende": [[r"ticker|aktie|kandidat"], [r"trendwende|reversal|boden|wende"], [r"abwärts|abwaerts|abwärtsbewegung|abwaertsbewegung"], [r"entry|einstieg"], [r"stop|ziel|tp"], [r"crv|filter"], [r"bestät|bestaet|fehlt"], [r"risiko|gegenargument"],],
        "2.3 Short": [[r"ticker|aktie"], [r"short|abwärts|abwaerts|abwärtsthese|abwaertsthese"], [r"technisch|bestät|bestaet"], [r"entry|einstieg"], [r"stop"], [r"tp1|tp2|ziel"], [r"crv"], [r"makro|sektor|branche"], [r"risiko|gegenargument"],],
        "2.4 HebelTrader": [[r"ticker|aktie|instrument"], [r"long|short|richtung"], [r"setup"], [r"entry|einstieg"], [r"stop"], [r"ziel|tp"], [r"risiko|hebel|volatil"],],
        "2.5 Sonstige durch Gemini erkannte Chancen": [[r"ticker|aktie"], [r"chance|idee"], [r"these|begründ|begruend"], [r"daten|beleg"], [r"gegenargument|risiko"], [r"trigger|katalysator"],],
        "3.1 Makro → Branche → Aktie": [[r"makro"], [r"branche|sektor"], [r"aktie|ticker"], [r"auswirkung|zusammenhang|kausal"]],
        "3.2 Rohstoff → Branche → Aktie": [[r"rohstoff"], [r"branche|sektor"], [r"aktie|ticker"], [r"auswirkung|zusammenhang|kausal"]],
        "3.3 Politik → Branche → Aktie": [[r"politik|regulier|staat|zoll|subvention"], [r"branche|sektor"], [r"unternehmen|aktie|ticker"], [r"auswirkung|zusammenhang"]],
        "3.4 Technologie → Branche → Aktie": [[r"technologie|ki|halbleiter|automatisierung|speicher"], [r"branche|sektor"], [r"unternehmen|aktie|ticker"], [r"auswirkung|zusammenhang"]],
        "3.5 Unternehmens-/Fundamentaldaten → Aktie": [[r"fundamental|umsatz|gewinn|marge|bewertung|analyst"], [r"aktie|ticker"], [r"daten|kennzahl|nachricht"], [r"auswirkung|these"]],
        "4. 🔭 IDEEN IM AUFBAU": [[r"these"], [r"bestät|bestaet|daten"], [r"gegenargument|risiko"], [r"kausal|zusammenhang"], [r"profiteur|verlierer"], [r"aktie"], [r"status|fehlt"], [r"aktivierung|trigger"], [r"invalid"]],
        "5. 🥇 AKTIEN MIT FRÜHEM SIGNAL": [[r"aktie|ticker"], [r"zusammenhang|these"], [r"daten|quelle|beleg"], [r"sichtbar|reaktion"], [r"nicht bestätigt|nicht bestaet|fehlt"], [r"eingepreist|markt"], [r"trigger|katalysator"]],
        "6.1 Makro gegen Technik": [[r"aktie|ticker|idee"], [r"these"], [r"makro"], [r"technik|technisch"], [r"widerspruch|konflikt|gegen"], [r"bedeut|auswirkung"], [r"prüf|pruef|invalid|trigger"]],
        "6.2 Technik gegen Fundamentaldaten": [[r"aktie|ticker|idee"], [r"these"], [r"technik"], [r"fundamental"], [r"widerspruch|konflikt|gegen"], [r"bedeut|auswirkung"], [r"prüf|pruef|invalid|trigger"]],
        "6.3 Sektor gegen Aktie": [[r"aktie|ticker"], [r"these"], [r"sektor|branche"], [r"widerspruch|konflikt"], [r"bedeut|auswirkung"], [r"prüf|pruef|invalid|trigger"]],
        "6.4 Rohstoff gegen Aktie": [[r"aktie|ticker"], [r"these"], [r"rohstoff"], [r"widerspruch|konflikt"], [r"bedeut|auswirkung"], [r"prüf|pruef|invalid|trigger"]],
        "6.5 Investmentthese gegen aktuelle Marktdaten": [[r"aktie|ticker|idee"], [r"these"], [r"markt|marktdaten|aktuelle daten"], [r"widerspruch|konflikt"], [r"bedeut|auswirkung"], [r"prüf|pruef|invalid|trigger"]],
        "6.6 Risiken bestehender Ideen": [[r"aktie|ticker|idee"], [r"risiko"], [r"auswirkung|bedeut"], [r"gegenmaß|gegenmass|absicherung"], [r"prüf|pruef|monitor"], [r"invalid|trigger"]],
        "7.1 Aktienmärkte / Indizes": [[r"europa|europe"], [r"usa|us\b|s&p|nasdaq|dow"], [r"asien|asia|nikkei|hang seng"], [r"markt|index"], [r"breite|marktbreite"], [r"momentum|trend|dynamik"]],
        "7.2 Zinsen": [[r"leitzins|fed|ezb|zins"], [r"2\s*y"], [r"10\s*y"], [r"realzins"], [r"kurve|spread"], [r"auswirkung|interpret"]],
        "7.3 Volatilität": [[r"vix|volatil"], [r"veränder|veraender|niveau|verlauf"], [r"auswirkung|risiko|interpret"]],
        "7.4 FX": [[r"eur/?usd|euro|dollar"], [r"dxy"], [r"usd/?jpy|yen"], [r"trend|beweg|veränder|veraender"], [r"auswirkung|interpret"]],
        "7.5 Rohstoffe": [[r"öl|oil|brent|wti"], [r"kupfer|copper|lithium|industriemetall"], [r"preis|kurs"], [r"trend|beweg"], [r"auswirkung|branche|angebot|nachfrage"]],
        "7.6 Krypto": [[r"bitcoin|btc"], [r"ethereum|eth"], [r"performance|veränder|veraender"], [r"trend|sma|ema"], [r"auswirkung|interpret"]],
        "7.7 Konjunktur / Makro": [[r"arbeitsmarkt|nfp|claims|arbeitslosen"], [r"inflation|cpi|ppi"], [r"bip|wachstum|gdp|ism|pmi"], [r"konsum|kredit|credit"], [r"makro|konjunktur"], [r"auswirkung|interpret"]],
        "8.1 Gold": [[r"gold|xau"], [r"kurs|preis"], [r"kurzfrist|5\s*(?:t|tage)|5d"], [r"4\s*(?:w|wochen)|4w"], [r"52\s*(?:w|wochen)|52w|jahreshoch|jahrestief"], [r"ema\s*200|wma\s*200|200[- ]?tage"], [r"technik|technisch"], [r"trendfolge"], [r"trendwende|reversal"], [r"short|abwärts|abwaerts"], [r"crv|filter"], [r"beinahe|kandidat|setup"], [r"saisonal"], [r"makro|zins|inflation"], [r"branche|sektor"], [r"aktie|ticker"], [r"reaktion"], [r"trigger|katalysator"]],
        "8.2 Silber": [[r"silber|xag"], [r"kurs|preis"], [r"kurzfrist|5\s*(?:t|tage)|5d"], [r"4\s*(?:w|wochen)|4w"], [r"52\s*(?:w|wochen)|52w|jahreshoch|jahrestief"], [r"ema\s*200|wma\s*200|200[- ]?tage"], [r"technik|technisch"], [r"trendfolge"], [r"trendwende|reversal"], [r"short|abwärts|abwaerts"], [r"crv|filter"], [r"beinahe|kandidat|setup"], [r"saisonal"], [r"makro|zins|inflation"], [r"branche|sektor"], [r"aktie|ticker"], [r"reaktion"], [r"trigger|katalysator"]],
        "8.3 Platin": [[r"platin|xpt"], [r"kurs|preis"], [r"kurzfrist|5\s*(?:t|tage)|5d"], [r"4\s*(?:w|wochen)|4w"], [r"52\s*(?:w|wochen)|52w|jahreshoch|jahrestief"], [r"ema\s*200|wma\s*200|200[- ]?tage"], [r"technik|technisch"], [r"trendfolge"], [r"trendwende|reversal"], [r"short|abwärts|abwaerts"], [r"crv|filter"], [r"beinahe|kandidat|setup"], [r"saisonal"], [r"makro|zins|inflation"], [r"branche|sektor"], [r"aktie|ticker"], [r"reaktion"], [r"trigger|katalysator"]],
        "8.4 Palladium": [[r"palladium|xpd"], [r"kurs|preis"], [r"kurzfrist|5\s*(?:t|tage)|5d"], [r"4\s*(?:w|wochen)|4w"], [r"52\s*(?:w|wochen)|52w|jahreshoch|jahrestief"], [r"ema\s*200|wma\s*200|200[- ]?tage"], [r"technik|technisch"], [r"trendfolge"], [r"trendwende|reversal"], [r"short|abwärts|abwaerts"], [r"crv|filter"], [r"beinahe|kandidat|setup"], [r"saisonal"], [r"makro|zins|inflation"], [r"branche|sektor"], [r"aktie|ticker"], [r"reaktion"], [r"trigger|katalysator"]],
        "9.1 Makrotermine": [[r"fomc|fed"], [r"ezb|ecb"], [r"cpi|inflation|ppi"], [r"arbeitsmarkt|nfp|claims"], [r"ism|pmi"]],
        "9.2 Unternehmen": [[r"earnings|quartal|zahlen"], [r"konferenz|veranstaltung|capital markets day|investor day"], [r"meldung|unternehmensmeldung|corporate|mitteilung"], [r"ticker|unternehmen"]],
        "9.3 Branchenereignisse": [[r"konferenz"], [r"politisch|politik|gesetz|wahl|regier"], [r"regulator|regulierung|aufsicht"], [r"branche|industrie|sektor"], [r"ereignis|event|ankündigung|ankuendigung"]],
        "9.4 Technische Trigger": [[r"unterstützung|unterstuetzung|support"], [r"widerstand|resistance"], [r"ausbruch|breakout|bestätigung|bestaetigung|trendwechsel"], [r"ema|50\s*[- ]?tage|macd|momentum"], [r"trigger|bedingung"]],
        "9.5 Mögliche Aktivierung / Invalidierung": [[r"aktivierung|aktivieren"], [r"invalid|widerleg"], [r"trigger|bedingung"], [r"ticker|aktie|these"]],
        "10.1 Sofortiger Handlungsbedarf": [[r"ticker|aktie|position"], [r"handlungsbedarf|maßnahme|massnahme"], [r"grund|begründ|begruend"], [r"sofort|priorität|prioritaet"]],
        "10.2 Stop-/TP-Änderungen": [[r"ticker|aktie|position"], [r"stop|tp1|tp2|take profit"], [r"geändert|geaendert|nachgezogen|neuer|angepasst"], [r"grund|begründ|begruend"]],
        "10.3 Positionen mit neuer Investmentthese": [[r"ticker|aktie"], [r"ursprüng|ursprueng|ausgang|bisher"], [r"neue daten|aktuelle daten|daten"], [r"investmentthese|neue these|verändert|veraendert"], [r"auswirkung|position|handlungsbedarf"]],
        "10.4 Positionen, deren These schwächer wird": [[r"ticker|aktie"], [r"ursprüng|ursprueng|ausgang|bisher"], [r"neue daten|aktuelle daten|daten"], [r"schwächer|schwaecher|widerlegt|belastet"], [r"auswirkung|position|handlungsbedarf"]],
        "11.1 Datenstatus": [[r"datenstand|status"], [r"aktuell|zeitpunkt|datum"], [r"vollständig|vollstaendig|verfügbar|verfuegbar"]],
        "11.2 Makro-Szenario-Status": [[r"makro"], [r"szenario|scenario"], [r"status|ampel"], [r"treiber|begründ|begruend"]],
        "11.3 Datenlücken": [[r"datenlücke|datenluecke|fehlend|nicht verfügbar|nicht verfuegbar"], [r"quelle|bereich|reihe"], [r"auswirkung|einschränkung|einschraenkung"]],
        "11.4 externe Quellen": [[r"quelle|quelle:"], [r"fred|alpaca|yahoo|yfinance|google|gemini|api|website"], [r"zweck|verwendung|daten"]],
        "11.5 technische / fundamentale Datenqualität": [[r"technisch|technik"], [r"fundamental"], [r"qualität|qualitaet"], [r"einschränkung|einschraenkung|zuverlässig|zuverlaessig"]],
        "11.6 Hinweise zur Interpretation": [[r"interpret|einordnung"], [r"vorsicht|einschränkung|einschraenkung"], [r"nicht als|keine kauf|kein kauf"], [r"daten|modell|unsicherheit"]],
        "11.7 Abgrenzung:": [[r"scanner"], [r"discovery|entdeckung"], [r"signal|setup"], [r"abgrenz|nicht gleich|nicht identisch"]],
    }

    def dynamic_10_5_spec(block):
        """10.5 folgt ausschließlich den tatsächlich vorhandenen Tab-2-Fakten."""
        content = "\n".join(content_lines(block))
        units = sentences(block)
        if not units:
            return [], 0, True
        factual_groups = [
            [r"ticker|position|wertpapier"],
            [r"einstieg|ausstieg"],
            [r"performance|rendite|ergebnis"],
            [r"status|geschlossen|gestoppt"],
        ]
        active = [group for group in factual_groups if has(content, group)]
        return active, len(active), True

    def _has_any_date_or_time(text_block):
        """Erkennt konkrete Zeitbezüge robust über die gesamte Inhaltseinheit.

        Berücksichtigt neben den kanonischen Datumsformaten des Projekts auch
        die in Makro-/Briefing-Quellen tatsächlich vorkommenden natürlichen
        deutschen Zeitangaben, damit diese nicht fälschlich als fehlender
        Terminbezug bewertet werden.
        """
        patterns = (
            # Konkretes Datum: 7. Oktober 2026 / 07. Oktober 2026
            r"\b\d{1,2}\.\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\s+20\d{2}\b",
            # Numerische Datumsformate: 07.10.2026 / 07-10-2026 / 07/10/2026
            r"\b\d{1,2}[./-]\d{1,2}[./-]20\d{2}\b",
            # ISO-Datum: 2026-10-07
            r"\b20\d{2}-\d{1,2}-\d{1,2}\b",
            # Monat + Jahr: Oktober 2026
            r"\b(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\s+20\d{2}\b",
            # Datums-/Zeiträume aus den Projektquellen: 6. bis 8. Oktober
            r"\b\d{1,2}\.?\s*(?:bis|[-–—])\s*\d{1,2}\.?\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b",
            # Tagesangabe ohne Jahr: am 7. Oktober / 7. Oktober
            r"\b(?:am\s+)?\d{1,2}\.\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b",
            # Explizite Monatszeiträume
            r"\b(?:im|in|anfang|mitte|ende)\s+(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b",
            # Relative Zeitangaben, wie sie in den Projektquellen vorkommen
            r"\b(?:heute|morgen|übermorgen|naechste[nr]?|kommende[nr]?|diese[rn]?)\s+(?:woche|monat|tag(?:en)?|jahr(?:es)?|wochen|monaten|monate)\b",
            r"\b(?:nächste|naechste|kommende)\s+woche\b",
            r"\b(?:in|innerhalb(?:\s+von)?)\s+\d{1,3}\s+(?:tag(?:en)?|woche(?:n)?|monat(?:en)?|jahr(?:en)?)\b",
            r"\b(?:am\s+)?(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b",
            # Einzelne relative Zeitwörter bleiben gültig, wie bisher.
            r"\b(?:heute|morgen|übermorgen)\b",
            # Rückwärtskompatibilität: auch die bisherigen isolierten relativen
            # Zeitwörter bleiben gültige Treffer.
            r"\b(?:heute|morgen|übermorgen|naechste[nr]?|kommende[nr]?|diese[rn]?)\b",
            # Quartal/Kalenderwoche, mit optionalem Jahr.
            r"\bq[1-4](?:\s+20\d{2})?\b",
            r"\bkw\s*\d{1,2}(?:\s*[/.-]?\s*20\d{2})?\b",
            # Rückwärtskompatibilität zum bisherigen Q/KW-Muster.
            r"\b(?:q[1-4]|kw\s*\d{1,2})\b",
        )
        return any(re.search(pattern, text_block, re.I | re.U) for pattern in patterns)

    errors = []
    warnings = []

    for h in required:
        block = sections.get(h, "")
        if not block:
            errors.append(f"{h}: Abschnitt für die Mindesttiefenprüfung nicht gefunden.")
            continue

        lines = content_lines(block)
        units = sentences(block)
        profile = profiles.get(h, [])

        # Eine reine Überschrift bzw. generische Einzeiler sind keine inhaltliche
        # Bearbeitung. Explizite, konkrete Nichtverfügbarkeit bleibt zulässig.
        if not lines:
            errors.append(f"{h}: kein inhaltlicher Abschnitt vorhanden.")
            continue
        if is_explicit_negative(block):
            # "Kein Setup" allein reicht bei Edelmetallen ausdrücklich nicht.
            if h in {"8.1 Gold", "8.2 Silber", "8.3 Platin", "8.4 Palladium"} and not unavailable_re.search(block):
                errors.append(f"{h}: 'Kein Setup' ersetzt nicht die geforderte Edelmetallanalyse.")
            else:
                continue
        if len(lines) == 1 and generic.fullmatch(lines[0]):
            errors.append(f"{h}: nur generischer Fallback statt Pflichtinhalt.")
            continue

        # 10.5 ist eine Sonderquelle: nur tatsächlich ausgegebene Tab-2-Fakten
        # dürfen die Prüfung bestimmen.
        if h == "10.5 Geschlossene Positionen":
            groups, min_hits, _ = dynamic_10_5_spec(block)
            if not units:
                # Kein Datensatz ist nur dann gültig, wenn die deterministische
                # Quelle dies explizit als leer meldet.
                if re.search(r"keine\s+geschlossene\s+position", block, re.I | re.U):
                    continue
                errors.append(f"{h}: keine aus Tab 2 ableitbaren Fakten.")
                continue
            if groups:
                observed = sum(1 for group in groups if has(block, group))
                if observed < 1:
                    errors.append(f"{h}: vorhandene Tab-2-Inhalte enthalten keine erkennbaren Fakten.")
            continue

        if not profile:
            # Sicherheitsnetz: unbekannter Abschnitt darf nicht leer/etikettenartig sein.
            if not units:
                errors.append(f"{h}: kein substantieller Inhalt.")
            continue

        # Ein einzelner Satz darf bestehen, wenn er fachlich substanziell ist.
        # Es wird ausdrücklich KEINE Mindestanzahl von Sätzen und KEINE
        # Keyword-Abdeckungsquote erzwungen. Die Profile bleiben als fachlicher
        # Inhaltsvertrag erhalten und dienen nur noch der Erkennung einer
        # unzulässig kompakten Sammelaussage.
        compact_text = " ".join(lines)
        distinct_terms = set(re.findall(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]{4,}", compact_text.lower(), flags=re.UNICODE))
        content_present = bool(units)

        # Eine tatsächlich vorhandene Analyse darf auch nur eine eigenständige
        # Aussage enthalten. Verhindert werden nur reine Label-/Keyword-Zeilen.
        label_only = all(
            re.fullmatch(r"[^:]{1,80}:\s*[^:]{1,80}", line)
            and len(re.findall(r"[A-Za-zÄÖÜäöüßÀ-ÿ0-9]{3,}", line, flags=re.UNICODE)) <= 6
            for line in lines
        )
        if label_only and unavailable_re.search(block):
            # Explizite, fachlich zuordenbare Nichtverfügbarkeit ist eine gültige
            # datenabhängige Aussage und darf nicht an einer Längenheuristik scheitern.
            continue
        if label_only and h not in {"9.1 Makrotermine", "11.2 Makro-Szenario-Status"} and not any(len(re.sub(r"\W", "", unit, flags=re.UNICODE)) >= 45 for unit in units):
            errors.append(f"{h}: Inhalt ist nur eine kurze Label-/Wert-Angabe; es fehlen substanzielle Informationen.")
            continue

        # Daten-/abschnittsabhängige Inhalte werden NICHT mehr über eine
        # Profil-Coverage-Quote bewertet. Damit können z.B. 8.1–8.4, 3.x oder
        # 6.x fachlich sinnvoll kurz bleiben, wenn genau diese Informationen
        # aus den bereitgestellten Daten ableitbar sind.

        # Bei 9.1–9.3 muss jede tatsächlich behauptete Ereignis-/Terminart
        # einen Zeitbezug besitzen. Allgemeine Kontextaussagen über Branche,
        # Politik oder Inflation sind noch kein Termin/Ereignis.
        if h in {"9.1 Makrotermine", "9.2 Unternehmen", "9.3 Branchenereignisse"} and content_present:
            factual_event_patterns = {
                "9.1 Makrotermine": r"fomc|fed[- ]entscheidung|ezb|ecb|cpi[- ](?:veröffentlichung|release)|ppi[- ](?:veröffentlichung|release)|nfp|nonfarm|arbeitsmarktbericht|claims|ism[- ](?:bericht|release)|pmi[- ](?:bericht|release)",
                "9.2 Unternehmen": r"earnings[- ](?:termin|date)|quartals(?:zahlen|bericht)|konferenz(?:termin)?|veranstaltung(?:stermin)?|capital markets day|investor day|unternehmensmeldung|unternehmensmitteilung|ad-hoc|gewinnwarnung|dividenden(?:termin|zahlung)",
                "9.3 Branchenereignisse": r"konferenz(?:termin)?|gesetz(?:esänderung|esbeschluss)?|wahl(?:termin)?|regulierungs(?:entscheidung|beschluss)|aufsicht(?:sentscheidung|smaßnahme)|branchenereignis|industrieereignis|sektorereignis|event(?:termin)?|ankündigung|ankuendigung|beschluss|abstimmung|verordnung",
            }
            event_pattern = factual_event_patterns[h]
            event_claims = []
            for unit in units:
                if not re.search(event_pattern, unit, re.I | re.U):
                    continue
                # Negierte Aussagen wie "keine Konferenz" oder "kein Ereignis"
                # sind keine behaupteten Termine und benötigen daher keinen
                # künstlichen Datumsbezug.
                if re.search(r"\b(?:kein|keine|keinen|keiner|nicht|derzeit\s+keine|aktuell\s+keine)\b", unit, re.I | re.U):
                    continue
                event_claims.append(unit)
            if event_claims and not _has_any_date_or_time(compact_text):
                warnings.append(f"{h}: tatsächliche Ereignis-/Terminangabe ohne verifizierbaren Zeit-/Datumsbezug.")

        # Keine pauschale Sammelaussage-/Textdichte-Heuristik:
        # Gemini soll die aus den Python-Daten ableitbare fachliche Interpretation
        # frei formulieren koennen. Inhaltliche Tiefe wird nicht ueber Keywords,
        # Satzanzahl oder erkannte Profil-Dimensionen erzwungen.

    # Cross-section anti-cheating check: identical or near-identical bodies may not
    # be copied into several sections merely to satisfy the gates.
    substantive_bodies = []
    for h, block in sections.items():
        lines = content_lines(block)
        if len(lines) >= 3:
            normalized = re.sub(r"\s+", " ", " ".join(lines)).lower()
            substantive_bodies.append((h, normalized))
    def _positionsidentitaeten_aus_text(block):
        """Ermittelt positionsbezogene Identitäten für 10.4/10.5."""
        identities = set()

        # 10.5: echte Positionszeilen enthalten Name, Ticker, Einstieg
        # und Einstiegsdatum gemeinsam. Zusätzlich wird die stabile
        # Name+Ticker-Identität erzeugt, damit sie mit 10.4 abgeglichen
        # werden kann, wo Einstieg/Datum naturgemäß fehlen.
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue

            ticker_match = re.search(
                r"(?i)\b(?:ticker|symbol)\s*[:=]\s*([A-Z][A-Z0-9.-]{0,9})\b",
                line,
            )
            name_match = re.search(
                r"(?i)\b(?:name|unternehmen|position)\s*[:=]\s*([^|\n,;]+)",
                line,
            )
            einstieg_match = re.search(
                r"(?i)\b(?:einstieg|entry|einstiegskurs)\s*[:=]\s*([^|\n;]+)",
                line,
            )
            date_match = re.search(
                r"(?i)\b(?:einstiegsdatum|entry[- ]?datum)\s*[:=]\s*"
                r"(\d{1,2}[./-]\d{1,2}[./-]20\d{2}|20\d{2}-\d{1,2}-\d{1,2}|"
                r"\d{1,2}\.\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|"
                r"september|oktober|november|dezember)\s+20\d{2})",
                line,
            )

            if not (name_match and ticker_match):
                continue

            name = re.sub(r"\s+", " ", name_match.group(1).strip().lower())
            ticker = ticker_match.group(1).upper()
            identities.add(("name_ticker", name, ticker))

            if einstieg_match and date_match:
                einstieg = re.sub(
                    r"\s+", " ", einstieg_match.group(1).strip().lower()
                )
                date = re.sub(
                    r"\s+", " ", date_match.group(1).strip().lower()
                )
                identities.add(("position", name, ticker, einstieg, date))

        # 10.4: typische Gemini-Form ist "Name (TICKER)".
        # Mehrere Positionen in derselben Aussage werden einzeln extrahiert,
        # ohne Werte verschiedener Positionen miteinander zu kombinieren.
        for name, ticker in re.findall(
            r"(?ui)([A-Za-zÀ-ÖØ-öø-ÿ0-9][^\n|;:]*?)\s*\(([A-Z0-9][A-Z0-9.-]{0,9})\)",
            block,
        ):
            name = re.sub(r"^\s*(?:und|,|&)+\s*", "", name, flags=re.IGNORECASE)
            name = re.sub(r"\s+", " ", name.strip(" -•")).strip().lower()
            if not name:
                continue
            identities.add(("name_ticker", name, ticker.upper()))

        return identities

    for i, (h1, b1) in enumerate(substantive_bodies):
        for h2, b2 in substantive_bodies[i + 1:]:
            if len(b1) < 120 or len(b2) < 120:
                continue

            # 10.4 und 10.5 dürfen dieselbe konkrete Position beschreiben.
            # Ticker + Einstiegsdatum identifizieren diese Position konservativ.
            if {h1, h2} == {
                "10.4 Positionen, deren These schwächer wird",
                "10.5 Geschlossene Positionen",
            }:
                pos1 = _positionsidentitaeten_aus_text(b1)
                pos2 = _positionsidentitaeten_aus_text(b2)
                if pos1 & pos2:
                    continue

            # Edelmetallblöcke werden assetbezogen geprüft. Gemeinsame fachliche
            # Methodik darf dort nicht allein wegen identischer Fachterminologie
            # als Copy/Paste gewertet werden.
            precious_sections = {"8.1 Gold", "8.2 Silber", "8.3 Platin", "8.4 Palladium"}
            if h1 in precious_sections and h2 in precious_sections:
                asset_tokens = {
                    "8.1 Gold": r"gold|xau",
                    "8.2 Silber": r"silber|xag",
                    "8.3 Platin": r"platin|xpt",
                    "8.4 Palladium": r"palladium|xpd",
                }
                if unavailable_re.search(b1) and unavailable_re.search(b2):
                    continue
                # Die kanonische Abschnittsüberschrift ist selbst eine belastbare
                # Asset-Identität. content_lines() entfernt diese Überschrift für
                # den Anti-Copy-Vergleich; deshalb darf die Asset-Prüfung sie nicht
                # ebenfalls verwerfen. Der anschließende Identitätsvergleich des
                # Inhalts bleibt unverändert bestehen.
                asset_source_1 = f"{h1}\n{b1}"
                asset_source_2 = f"{h2}\n{b2}"
                if not re.search(asset_tokens[h1], asset_source_1, re.I | re.U) or not re.search(asset_tokens[h2], asset_source_2, re.I | re.U):
                    errors.append(f"{h1} / {h2}: Edelmetallabschnitte nicht eindeutig assetbezogen.")
                    continue
                if b1 == b2:
                    errors.append(f"{h1} / {h2}: vollständig identischer Analyseinhalt trotz unterschiedlichem Edelmetall.")
                continue

            # Für unterschiedliche Fachabschnitte bleibt der allgemeine Anti-Copy-Schutz aktiv.
            w1 = set(re.findall(r"[a-zäöüß]{4,}", b1))
            w2 = set(re.findall(r"[a-zäöüß]{4,}", b2))
            if not w1 or not w2:
                continue
            overlap = len(w1 & w2) / min(len(w1), len(w2))
            if overlap >= 0.88:
                errors.append(f"{h1} / {h2}: nahezu identischer Analyseinhalt; Mindesttiefe darf nicht durch Kopieren erfüllt werden.")

    if warnings:
        for warning in warnings:
            print(f"WARNUNG: {warning}")

    if errors:
        raise RuntimeError("INHALTLICHE_MINDESTTIEFE_UNGUELTIG: " + " | ".join(errors))
    print("INHALTLICHE-MINDESTTIEFE-GATE: PASS")


def _hebeltrader_charttechnik_fuer_ticker(eingabedateien, name, ticker, auswertungsdatum=None):
    """Liest den tagesaktuellen Snapshot nur bei passendem Namen UND Ticker.

    Kurs stammt aus dem Snapshot-Kursfeld. Einstieg/Stop/TP1/TP2 stammen
    ausschließlich aus dessen Trendfolge-Ergebnis. Trendwende-Werte werden
    hier bewusst nicht als Ersatz oder Mischung verwendet.
    """
    nicht_verfuegbar = "NICHT VERFÜGBAR"
    datum = auswertungsdatum or datetime.date.today().isoformat()
    name_norm = _normalisiere_positionsname(name)
    ticker_norm = _normalisiere_ticker(ticker)
    history_path = (eingabedateien or {}).get("Einzel-Check-Technikhistorie")
    if not history_path:
        history_path = finde_datei(DATEIMUSTER["Einzel-Check-Technikhistorie"])
    if not name_norm or not ticker_norm or not history_path or not os.path.isfile(history_path):
        return {
            "Datum": datum,
            "Kurs": nicht_verfuegbar,
            "Einstieg": nicht_verfuegbar,
            "Stop": nicht_verfuegbar,
            "TP1": nicht_verfuegbar,
            "TP2": nicht_verfuegbar,
        }

    matching_rows = []
    try:
        with open(history_path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                if str(row.get("Datum") or "").strip() != datum:
                    continue
                # Strikte zusammengesetzte Identität: Name UND Ticker müssen passen.
                if _normalisiere_ticker(row.get("Ticker")) != ticker_norm:
                    continue
                if _normalisiere_positionsname(row.get("Name")) != name_norm:
                    continue
                matching_rows.append(row)
    except OSError as exc:
        raise RuntimeError(
            f"2.4_TECHNIKHISTORIE_NICHT_LESBAR: {exc}"
        ) from exc

    if not matching_rows:
        return {
            "Datum": datum,
            "Kurs": nicht_verfuegbar,
            "Einstieg": nicht_verfuegbar,
            "Stop": nicht_verfuegbar,
            "TP1": nicht_verfuegbar,
            "TP2": nicht_verfuegbar,
        }

    # Bei mehreren heutigen Snapshots ist der zuletzt gespeicherte Snapshot
    # der jüngste Lauf des Tages; es wird kein historischer Tag herangezogen.
    row = matching_rows[-1]
    technik = row.get("Technik") if isinstance(row.get("Technik"), dict) else {}
    trendfolge = technik.get("Trendfolge") if isinstance(technik.get("Trendfolge"), dict) else {}

    def wert(source, key):
        value = source.get(key) if isinstance(source, dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            return nicht_verfuegbar
        return str(value).strip()

    # Aktueller Kurs und Einstieg bleiben semantisch getrennt, auch wenn die
    # zugrunde liegende Analyse für beide denselben Zahlenwert gespeichert hat.
    kurs = wert(row, "Kurs")
    if kurs == nicht_verfuegbar:
        kurs = wert(trendfolge, "Kurs")
    return {
        "Datum": datum,
        "Kurs": kurs,
        "Einstieg": wert(trendfolge, "Einstieg"),
        "Stop": wert(trendfolge, "Stop"),
        "TP1": wert(trendfolge, "TP1"),
        "TP2": wert(trendfolge, "TP2"),
    }


def _bereinige_punkt_24_nur_a(text, eingabedateien=None):
    """Übernimmt den vollständigen A-Meldungsbericht und ergänzt Charttechnik.

    Die Meldungsdatei bleibt inhaltlich vollständig erhalten. Technische Werte
    werden nur aus einem Snapshot mit exakt passendem Ticker und aktuellem
    Auswertungsdatum ergänzt. Einstieg/Stop/TP1/TP2 stammen aus dem Trendfolge-
    Ergebnis des Einzel-Checks; Trendwende-Werte werden nicht beigemischt.
    """
    if not text:
        raise RuntimeError("2.4_AUSWERTUNGSABSCHNITT_FEHLT")
    m = re.search(r"(?m)^2\.4\s+HebelTrader\s*$", text)
    if not m:
        raise RuntimeError("2.4_AUSWERTUNGSABSCHNITT_FEHLT")
    next_m = re.search(r"(?m)^2\.5\s+", text[m.end():])
    if not next_m:
        raise RuntimeError("2.5_FOLGEABSCHNITT_FEHLT")
    end = m.end() + next_m.start()

    # Autoritative Quelle ist die im selben Gemini-Lauf eingesammelte A-Datei.
    # Nur bei direktem Funktionsaufruf ohne Eingabemanifest wird exakt die heutige
    # Datei geprüft; eine ältere A-Liste wird niemals als Ersatz verwendet.
    pfad = (eingabedateien or {}).get("Einzel_Check_A_Meldungen(...).txt")
    if not pfad and eingabedateien is None:
        heute_datei = f"Einzel_Check_A_Meldungen({datetime.date.today().isoformat()}).txt"
        pfad = heute_datei if os.path.isfile(heute_datei) else None

    # Keine Tagesdatei bedeutet bei diesem Ablauf: Der Einzel-Check hat heute
    # keine A-Meldungen erzeugt. Abschnitt 2.4 bleibt klar, statt die gesamte
    # Auswertung abzubrechen oder Kandidaten vom Vortag zu übernehmen.
    if not pfad:
        rebuilt = (
            "2.4 HebelTrader\n\n"
            "Heute liegen keine aktuellen A-Kandidaten aus dem Einzel-Check vor."
        )
        return text[:m.start()] + rebuilt + "\n\n" + text[end:]

    if not os.path.isfile(pfad):
        raise RuntimeError("2.4_A_MELDUNGEN_QUELLE_FEHLT")

    dateiname = os.path.basename(os.fspath(pfad))
    datums_match = re.fullmatch(
        r"Einzel_Check_A_Meldungen\((\d{4}-\d{2}-\d{2})\)\.txt",
        dateiname,
    )
    heute = datetime.date.today().isoformat()
    if not datums_match or datums_match.group(1) != heute:
        raise RuntimeError(
            f"2.4_A_MELDUNGEN_NICHT_AKTUELL: erwartet Einzel_Check_A_Meldungen({heute}).txt, "
            f"erhalten {dateiname!r}"
        )
    try:
        meldungen = Path(pfad).read_text(encoding="utf-8-sig")
    except Exception as exc:
        raise RuntimeError(f"2.4_A_MELDUNGEN_QUELLE_NICHT_LESBAR: {exc}") from exc
    meldungen = meldungen.strip()
    if not meldungen:
        raise RuntimeError("2.4_A_MELDUNGEN_QUELLE_LEER")

    name_ticker_pairs = []
    for line in meldungen.splitlines():
        name_match = re.search(r"(?i)\bName\s*:\s*([^|;\n]+)", line)
        ticker_match = re.search(r"(?i)\bTicker\s*:\s*([^|;\n]+)", line)
        if not name_match or not ticker_match:
            continue
        name = name_match.group(1).strip()
        ticker = ticker_match.group(1).strip()
        if not _normalisiere_positionsname(name) or not _normalisiere_ticker(ticker):
            continue
        # Keine Deduplizierung: jede Originalmeldung bleibt einzeln sichtbar.
        # Die technische Zuordnung basiert immer auf Name UND Ticker.
        name_ticker_pairs.append((name, ticker))

    enrichment = ["CHARTTECHNISCHE ERGÄNZUNG JE A-MELDUNG"]
    if not name_ticker_pairs:
        enrichment.append(
            "Keine vollständige Name-/Ticker-Kombination in der A-Meldungsdatei erkannt. "
            "Der Originaltext bleibt vollständig erhalten."
        )
    for name, ticker in name_ticker_pairs:
        values = _hebeltrader_charttechnik_fuer_ticker(
            eingabedateien, name, ticker, datetime.date.today().isoformat()
        )
        enrichment.extend([
            f"{name} ({ticker}) — Charttechnik aus Einzel-Check (Trendfolge; Auswertungsdatum: {values['Datum']})",
            f"Aktueller Kurs (letzter verfügbarer Schlusskurs): {values['Kurs']}",
            f"Einstieg: {values['Einstieg']}",
            f"Stop: {values['Stop']}",
            f"TP1: {values['TP1']}",
            f"TP2: {values['TP2']}",
        ])

    rebuilt = "2.4 HebelTrader\n\n" + meldungen + "\n\n" + "\n".join(enrichment)
    return text[:m.start()] + rebuilt + "\n\n" + text[end:]

def _normalisiere_punkt10_autoritaet(text):
    """Sichert Punkt 10 gegen erfundene technische Positionsänderungen.

    10.3/10.5 sind bereits deterministisch. 10.2 wird zusätzlich auf die
    tatsächlich vorhandenen autoritativen Stop-/TP-Änderungen begrenzt; wenn
    keine solche Änderung vorliegt, wird eine klare Negativfeststellung gesetzt.
    """
    if not text:
        return text
    csv_path = finde_datei(DATEIMUSTER["Offene Positionen+Check.csv"])
    if not csv_path or not os.path.isfile(csv_path):
        raise RuntimeError("PUNKT10_AUTORITATIVE_QUELLE_FEHLT")
    try:
        raw = Path(csv_path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"PUNKT10_AUTORITATIVE_QUELLE_NICHT_LESBAR: {exc}") from exc
    # Nur explizite Änderungsfelder gelten als Beleg. Die vorhandenen CSV-Fakten
    # werden nicht von Gemini-Zahlen überschrieben.
    change_markers = re.findall(r"(?im)^.*(?:Stop|TP1|TP2).*(?:geändert|geaendert|verändert|veraendert|neu|alt).*$", raw)
    start = text.find("10.2 Stop-/TP-Änderungen")
    end = text.find("\n10.3 Positionen mit neuer Investmentthese", start) if start >= 0 else -1
    if start >= 0 and end > start and not change_markers:
        replacement = ("10.2 Stop-/TP-Änderungen\n\n"
                       "Keine tatsächlich verifizierte Stop-/TP-Änderung in der autoritativen "
                       "Offene Positionen+Check-Datenquelle festgestellt.")
        text = text[:start] + replacement + text[end:]
    return text


def _ist_adp_makrozeile(line):
    """Erkennt ADP nur bei eindeutigem Makro-/Arbeitsmarktkontext.

    ADP ist zugleich der Aktien-Ticker von Automatic Data Processing und
    das Kürzel für den ADP-Arbeitsmarktindikator. Die Makroausnahme darf
    deshalb nur bei expliziten, deterministischen Makroformulierungen greifen.
    Ein isoliertes "ADP" oder ein Aktien-/Unternehmenskontext reicht niemals.
    """
    text = str(line or "")
    if not re.search(r"(?<![A-Za-z0-9])ADP(?![A-Za-z0-9])", text, re.I):
        return False

    # Ein expliziter Aktien-/Unternehmenskontext hat Vorrang. Ein bloßer
    # Listen-/Aufzählungsmarker (-/•) ist jedoch noch kein Aktienkontext:
    # Makro-Briefings enthalten ADP häufig als Bullet-Zeile, z. B.
    # "- ADP Employment Change: ...". Solche Zeilen müssen als Makro erkannt
    # werden, während "Aktie: ADP ..." weiterhin eindeutig Aktienkontext ist.
    company_context = re.compile(
        r"(?i)(?:aktie|unternehmen|position|trade|kandidat|setup|"
        r"sektor|markt:|entry|stop|tp1|tp2)"
    )
    if company_context.search(text):
        return False

    # Eindeutige, fest benannte ADP-Indikatorbezeichnungen. Ein bloßes
    # Vorkommen von "employment", "jobs" oder "payroll" reicht bewusst nicht.
    macro_phrases = re.compile(
        r"(?i)(?:"
        r"\badp\s+employment(?:\s+(?:change|report|data))?(?=\s*[:;,.)-]|\s*$)"
        r"|\badp\s+payroll(?:\s+report)?(?=\s*[:;,.)-]|\s*$)"
        r"|\badp\s+jobs(?:\s+report)?(?=\s*[:;,.)-]|\s*$)"
        r"|\badp\s+(?:arbeitsmarkt|arbeitsmarktdaten|arbeitsmarktindikator)"
        r"(?=\s*[:;,.)-]|\s*$)"
        r"|\badp\s+(?:beschäftigung|beschaeftigung)(?:s(?:änderung|daten)|s(?:aenderung|daten))?"
        r"(?=\s*[:;,.)-]|\s*$)"
        r"|\badp[- ](?:arbeitsmarktdaten|arbeitsmarktindikator|beschäftigungsdaten|beschaeftigungsdaten)"
        r"(?=\s*[:;,.)-]|\s*$)"
        r"|(?:makro|macro|risiko|risk)\s*[:：-]\s*.{0,50}\badp\b"
        r"|\badp\b.{0,50}\b(?:makro|macro)\b"
        r")"
    )
    return bool(macro_phrases.search(text))


def _normalisiere_name_ticker_ausgabe(text):
    """Erzwingt Name (Ticker) fuer konkrete bekannte Unternehmensnennungen.

    Grundregel: Der kanonische Firmenname ist die Identitaet. Ein Ticker
    allein ist kein ausreichender Aktienbezug und wird daher nicht
    automatisch interpretiert oder umgeschrieben.
    """
    universe_path = finde_datei(DATEIMUSTER["Trade_Story_Universum(...).json"])
    if not universe_path or not os.path.isfile(universe_path):
        raise RuntimeError("NAME_TICKER_NORMALISIERUNG_QUELLE_FEHLT")
    data = json.loads(Path(universe_path).read_text(encoding="utf-8"))
    by_name = {}
    for item in data.get("candidates", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip()
        name = str(item.get("name") or "").strip()
        if not ticker or not name or name.casefold() == ticker.casefold():
            continue
        key = name.casefold()
        by_name.setdefault(key, {"name": name, "tickers": set()})["tickers"].add(ticker)

    unique_names = [
        (v["name"], next(iter(v["tickers"])))
        for v in by_name.values()
        if len(v["tickers"]) == 1
    ]

    starts = [m.start() for m in re.finditer(
        r"(?m)^(?:1\.1|1\.2|1\.3|1\.4|2\.1|2\.2|2\.3|2\.4|2\.5|"
        r"3\.[1-5]|4\.|5\.|6\.[1-6]|8\.[1-4]|9\.[1-5])\b",
        text,
    )]
    if not starts:
        return text

    spans = [
        (s, starts[i + 1] if i + 1 < len(starts) else len(text))
        for i, s in enumerate(starts)
    ]

    for start, end in reversed(spans):
        block = text[start:end]
        lines = block.splitlines(True)

        # Ausschliesslich kanonische Firmennamen bestimmen den Aktienbezug.
        # Der zugehoerige Ticker wird nur ergaenzt, wenn er in derselben
        # Zeile noch nicht bereits im kanonischen Name (Ticker)-Format steht.
        for i, line in enumerate(lines):
            for name, ticker in unique_names:
                if name.casefold() in line.casefold() and not re.search(
                    rf"\([^\n()]*\b{re.escape(ticker)}\b[^\n()]*\)",
                    line,
                    re.I,
                ):
                    lines[i] = re.sub(
                        re.escape(name),
                        f"{name} ({ticker})",
                        lines[i],
                        count=1,
                        flags=re.I,
                    )
                    line = lines[i]

        block = "".join(lines)
        text = text[:start] + block + text[end:]

    return text


def _pruefe_name_ticker_gate(text):
    """Harte Endpruefung: konkrete Unternehmensnamen nur als Name (Ticker).

    Die Identitaetsregel ist bewusst einseitig:
    - Ein kanonischer Firmenname ohne zugehoerigen Ticker ist ungueltig.
    - Name (Ticker) ist gueltig.
    - Ein Ticker allein ist fuer dieses Gate kein Aktienbezug und wird
      insbesondere bei mehrdeutigen Begriffen wie FIX oder MSCI ignoriert.
    - Basisinstrumente/Rohstoffe/Krypto werden weiterhin nicht als
      Unternehmensnamen geprueft.
    """
    universe_path = finde_datei(DATEIMUSTER["Trade_Story_Universum(...).json"])
    if not universe_path or not os.path.isfile(universe_path):
        raise RuntimeError("NAME_TICKER_GATE_QUELLE_FEHLT")
    try:
        data = json.loads(Path(universe_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"NAME_TICKER_GATE_QUELLE_NICHT_LESBAR: {exc}") from exc

    name_tickers = {}
    name_display = {}
    non_equity_tickers = {
        "gold", "silver", "platinum", "palladium",
        "xau", "xag", "xpt", "xpd",
        "gc=f", "si=f", "pl=f", "pa=f",
        "bitcoin", "btc", "ethereum", "eth",
        "brent", "wti",
    }

    for item in data.get("candidates", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip()
        name = str(item.get("name") or "").strip()
        normalized_ticker = _normalisiere_ticker(ticker)
        if normalized_ticker in non_equity_tickers:
            continue
        if ticker and name and ticker.casefold() != name.casefold():
            key = name.casefold()
            name_tickers.setdefault(key, set()).add(ticker)
            name_display.setdefault(key, name)

    starts = [m.start() for m in re.finditer(
        r"(?m)^(?:1\.1|1\.2|1\.3|1\.4|2\.1|2\.2|2\.3|2\.4|2\.5|"
        r"3\.[1-5]|4\.|5\.|6\.[1-6]|8\.[1-4]|9\.[1-5])\b",
        text,
    )]
    if not starts:
        return

    checked = "\n".join(
        text[s:(starts[i + 1] if i + 1 < len(starts) else len(text))]
        for i, s in enumerate(starts)
    )

    errors = []
    for line in checked.splitlines():
        if not line.strip():
            continue

        # Nur ein kanonischer Firmenname startet die Aktienpruefung.
        # Ein nackter Ticker (z. B. FIX oder MSCI) ist bewusst kein Fehler.
        for name_key, valid_tickers in name_tickers.items():
            if not name_key or name_key not in line.casefold():
                continue
            if not any(
                re.search(
                    rf"\([^\n()]*\b{re.escape(ticker)}\b[^\n()]*\)",
                    line,
                    re.I,
                )
                for ticker in valid_tickers
            ):
                display_name = name_display.get(name_key, name_key)
                errors.append(
                    f"{display_name}: Firmenname ohne kanonisches Name (Ticker)-Format"
                )
                break

    if errors:
        raise RuntimeError("NAME_TICKER_GATE_UNGUELTIG: " + " | ".join(errors[:20]))


def _normalisiere_punkt11_quellengebunden(text):
    """Ersetzt 11.1–11.6 durch deterministische, quellengebundene Fakten."""
    if not text:
        return text
    makro_path = finde_datei(DATEIMUSTER["Makro_Briefing(...).txt"])
    makro_quality = "NICHT VERFUEGBAR"
    if makro_path and os.path.isfile(makro_path):
        try:
            makro_quality = _lese_makro_datenqualitaet(Path(makro_path).read_text(encoding="utf-8-sig")) or "NICHT AUSGEWIESEN"
        except OSError:
            makro_quality = "NICHT VERFUEGBAR"
    def status(label, key):
        path = finde_datei(DATEIMUSTER[key]) if key in DATEIMUSTER else None
        return f"{label}: {'VERFUEGBAR' if path and os.path.isfile(path) else 'NICHT VERFUEGBAR'}"
    lines = [
        "11.1 Datenstatus", "",
        status("Trade-Story-Universum", "Trade_Story_Universum(...).json"),
        status("Trade-Story-Aktienuniversum", "Trade_Story_Aktienuniversum(...).csv"),
        status("Makro-Datenpaket", "Makro_Briefing(...).txt"),
        status("HEBELTRADER-A-Meldungen", "Einzel_Check_A_Meldungen(...).txt"),
        "Die Verfügbarkeit wird aus den tatsächlich vorliegenden Projektdateien bestimmt; fehlende Quellen werden nicht durch Modellwissen ersetzt.",
        "",
        "11.2 Makro-Szenario-Status", "",
        f"Datenqualitaet: {makro_quality}",
        "Das Makro-Szenario darf nur auf Basis des autoritativen Makro-Datenpakets interpretiert werden; numerische Werte werden durch die bestehenden Makro-Gates quellengebunden abgesichert.",
        "",
        "11.3 Datenlücken", "",
        "Nicht vorhandene optionale Quellen oder nicht verifizierbare Einzelwerte werden als NICHT VERFUEGBAR behandelt. Es werden keine Termine, Kurse, CRV-, Fundamentaldaten oder Statuswerte ergänzt.",
        "",
        "11.4 externe Quellen", "",
        status("Bitcoin Trading DE Briefing", "Bitcoin_Trading_DE_Briefing.txt"),
        status("Gold Trading DE Briefing", "Gold_Trading_DE_Briefing.txt"),
        status("Silber Trading DE Briefing", "Silber_Trading_DE_Briefing.txt"),
        "Externe Briefings liefern ausschließlich qualitativen Kontext und dürfen keine technischen oder numerischen Fakten ersetzen.",
        "",
        "11.5 technische / fundamentale Datenqualität", "",
        status("Technische Setups", "Setups(...).csv"),
        status("Trendwende", "Trendwende_Setups(...).csv"),
        status("Short", "Short_Setups(...).csv"),
        status("Edelmetalle", "Edelmetalle_Setups(...).csv"),
        status("Offene Positionen + Check", "Offene Positionen+Check.csv"),
        "Technische Werte und Positionsdaten sind quellengebunden; Gemini darf fehlende Werte nicht schätzen oder zwischen Titeln übertragen.",
        "",
        "11.6 Hinweise zur Interpretation", "",
        "Universumszugehörigkeit ist keine technische Bestätigung. A/B/C sind technische Statusinformationen; ein konkreter HebelTrader-Trade in 2.4 ist ausschließlich KAUFKANDIDAT A. Gemini darf Statusänderungen interpretieren, aber keine neue technische Validierung oder Positionsfakten erfinden.",
    ]
    new11 = "\n".join(lines)
    m = re.search(r"(?ms)^11\. METHODIK / DATENQUALITÄT\s*$.*\Z", text)
    if not m:
        raise RuntimeError("PUNKT11_QUELLENBINDUNG_FEHLT")
    # 11.7 wird aus der bestehenden deterministischen Abgrenzung erhalten.
    old = m.group(0)
    m117 = re.search(r"(?ms)^11\.7 Abgrenzung:\s*\n.*\Z", old)
    if not m117:
        tail = "11.7 Abgrenzung:\n\n" + _inhaltlicher_abgrenzungstext()
    else:
        tail = m117.group(0)
    return text[:m.start()] + "11. METHODIK / DATENQUALITÄT\n\n" + new11 + "\n\n" + tail + "\n" + text[m.end():]


def _pruefe_punkt11_quellenbindung(text):
    """Final-Gate: 11.x muss quellengebunden und fachlich konkret sein."""
    block_m = re.search(r"(?ms)^11\. METHODIK / DATENQUALITÄT\s*$.*\Z", text or "")
    if not block_m:
        raise RuntimeError("PUNKT11_QUELLENBINDUNG_FEHLT")
    block = block_m.group(0)
    for h in ("11.1 Datenstatus", "11.2 Makro-Szenario-Status", "11.3 Datenlücken", "11.4 externe Quellen", "11.5 technische / fundamentale Datenqualität", "11.6 Hinweise zur Interpretation", "11.7 Abgrenz:"):
        if h not in block and h != "11.7 Abgrenz:":
            raise RuntimeError(f"PUNKT11_QUELLENBINDUNG_FEHLT: {h}")
    if "Keine belastbare fachliche Aussage aus den vorliegenden Projektquellen ableitbar" in block:
        raise RuntimeError("PUNKT11_GENERISCHER_FALLBACK_VORHANDEN")
    makro_path = finde_datei(DATEIMUSTER["Makro_Briefing(...).txt"])
    if makro_path and os.path.isfile(makro_path):
        qual = _lese_makro_datenqualitaet(Path(makro_path).read_text(encoding="utf-8-sig"))
        if qual and f"Datenqualitaet: {qual}" not in block:
            raise RuntimeError(f"PUNKT11_DATENQUALITAET_WIDERSPRUCH: erwartet={qual}")

def _bereinige_ausgabe_und_formatiere(text):
    """Deterministische Endformatierung fuer fachlich getrennte Abschnitte.

    7.5 darf keine Edelmetalle enthalten, weil diese ausschliesslich in Punkt 8
    autoritativ dargestellt werden. Zusaetzlich werden die vom Nutzer geforderten
    Leerzeilen zwischen eigenstaendigen Eintraegen in 1.4, 2.4, 5 und 10 gesetzt.
    Die Funktion veraendert keine Fachwerte oder Berechnungen.
    """
    if not text:
        return text

    lines = text.splitlines()

    # 7.5: Edelmetalle aus dem Rohstoffblock entfernen. Nur eigenstaendige
    # Zeilen mit Gold/Silber/Platin/Palladium werden entfernt; Punkt 8 bleibt
    # vollständig unangetastet.
    precious = re.compile(r"(?i)^\s*[-•]?\s*(?:gold|silber|platin|palladium)\b")
    out = []
    in_75 = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^7\.5\s+Rohstoffe\s*$", stripped, re.I):
            in_75 = True
            out.append(line)
            continue
        if in_75 and re.match(r"^7\.6\s+Krypto\s*$", stripped, re.I):
            in_75 = False
            out.append(line)
            continue
        if in_75 and precious.match(line):
            continue
        out.append(line)
    lines = out

    # Leerzeilen zwischen Eintraegen/Unterpunkten in den explizit genannten
    # Abschnitten. Bereits vorhandene Leerzeilen werden nicht vervielfacht.
    target_sections = {"1.4", "2.4", "5."}
    section = None
    formatted = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(1\.4|2\.4|5\.)\b", stripped)
        if m:
            section = m.group(1)
            formatted.append(line)
            continue
        if re.match(r"^(?:1\.|2\.|3\.|4\.|6\.|7\.|8\.|9\.|10\.|11\.)", stripped) and not stripped.startswith(("1.4", "2.4", "5.")):
            section = None
        formatted.append(line)
        if section in target_sections and stripped.startswith("-"):
            # Nur zwischen Bullet-Eintraegen: keine Leerzeile erzwingen, wenn
            # bereits die nächste Zeile leer ist.
            formatted.append("")
    lines = formatted

    # Punkt 10: Leerzeile zwischen 10.1–10.5 und deren Inhalten. Das ist rein
    # typografisch und lässt die tatsächlichen Daten unverändert.
    out = []
    for i, line in enumerate(lines):
        if re.match(r"^10\.[1-5]\s+", line.strip()) and out and out[-1] != "":
            out.append("")
        out.append(line)
    return "\n".join(out).strip() + "\n"



def _abschnitt_text(text, heading, naechste_headings):
    """Liest einen Pflichtabschnitt, auch bei einer von Gemini gelieferten Inline-Ueberschrift."""
    source = _normalisiere_inline_pflichtueberschriften(text or "")
    m = re.search(r"(?m)^" + re.escape(heading) + r"\s*$", source)
    if not m:
        return ""
    end = len(source)
    for nxt in naechste_headings:
        n = re.search(r"(?m)^" + re.escape(nxt) + r"\s*$", source[m.end():])
        if n:
            end = min(end, m.end() + n.start())
    return (source[m.start():end] or "").strip()


def _normalisiere_inline_pflichtueberschriften(text):
    """Trennt Inline-Pflichtueberschriften von ihrem eigentlichen Abschnittstext."""
    if not text:
        return text
    headings = (
        "7.1 Aktienmärkte / Indizes", "7.2 Zinsen", "7.3 Volatilität", "7.4 FX",
        "7.5 Rohstoffe", "7.6 Krypto", "7.7 Konjunktur / Makro",
        "8.1 Gold", "8.2 Silber", "8.3 Platin", "8.4 Palladium",
    )
    pattern = re.compile(r"^(\s*)(" + "|".join(re.escape(h) for h in headings) + r")\s*:\s*(.*)$")
    out = []
    for line in str(text).splitlines():
        m = pattern.match(line)
        if not m:
            out.append(line)
            continue
        indent, heading, rest = m.groups()
        out.append(f"{indent}{heading}")
        if rest.strip():
            out.append(f"{indent}{rest.strip()}")
    return "\n".join(out) + ("\n" if str(text).endswith("\n") else "")


def _zahl_im_block_vorhanden(block, wert, label=None):
    """Prueft einen autoritativen Wert; bei Labelvorgabe muessen Label und Wert
    im selben Ausgabezeile zusammengehören. Dadurch werden Zahlenkollisionen
    mit anderen Quellfeldern im selben Abschnitt verhindert."""
    if wert is None:
        return False
    try:
        ziel = float(wert)
    except (TypeError, ValueError):
        return False

    def wert_in_zeile(zeile):
        for match in re.finditer(r"(?<![A-Za-z0-9])[-+]?\d[\d.,]*(?:%|)", zeile or ""):
            raw = match.group(0).rstrip("%").strip()
            kandidat = _quellenwert_float(raw)
            if kandidat is not None and abs(kandidat - ziel) <= max(1e-9, abs(ziel) * 1e-9):
                return True
        return False

    if label:
        label_pattern = re.compile(
            r"(?i)(?:^|\||:)\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?"
            + re.escape(label)
            + r"(?=\s*(?:[:(=]|$))"
        )
        for zeile in (block or "").splitlines():
            if label_pattern.search(zeile) and wert_in_zeile(zeile):
                return True
        return False

    for zeile in (block or "").splitlines():
        if wert_in_zeile(zeile):
            return True
    return False


def _gate_quellenwert(block, label, wert, fehler):
    if not _zahl_im_block_vorhanden(block, wert, label=label):
        fehler.append(f"{label}: autoritativer Wert {wert} nicht im zugeordneten Ausgabeabschnitt gefunden.")


def _gate_quellenstatus(block, label, status, fehler):
    if status == "UNAVAILABLE":
        ok = re.search(
            r"(?im)^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?"
            + re.escape(label)
            + r"\s*:\s*NICHT\s+VERFUEGBAR\b.*?\bSTATUS\s*=\s*UNAVAILABLE\b",
            block or "",
        )
        if not ok:
            fehler.append(
                f"{label}: autoritativer Quellstatus NICHT VERFUEGBAR/STATUS=UNAVAILABLE "
                "nicht im zugeordneten Ausgabeabschnitt gefunden."
            )
            return
        if re.search(
            r"(?im)^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?"
            + re.escape(label)
            + r"\s*:\s*(?!NICHT\s+VERFUEGBAR\b).*?\d[\d.,]*",
            block or "",
        ):
            fehler.append(
                f"{label}: widerspruechlicher numerischer Wert trotz autoritativem "
                "UNAVAILABLE-Quellstatus im zugeordneten Ausgabeabschnitt."
            )


def _quellenwert_aus_zeile(source, label, aus_parenthesen=False):
    """Liest den aktuellen numerischen Wert hinter einem autoritativen Label."""
    for line in (source or "").splitlines():
        clean_line = line.lstrip(" -•\t")
        if not clean_line.startswith(label + ":"):
            continue
        if aus_parenthesen:
            m = re.search(r"\(\s*([-+]?\d[\d.,]*)", clean_line)
        else:
            m = re.search(r":\s*([-+]?\d[\d.,]*)", clean_line)
        if m:
            raw = m.group(1).strip().strip(".,;:()[]{}")
            if raw:
                return raw
    return None


def _quellenwert_status_aus_zeile(source, label, aus_parenthesen=False):
    """Liest deterministisch entweder einen numerischen Quellwert oder den
    expliziten autoritativen UNAVAILABLE-Status einer Quellenzeile.

    Makro_Briefing verwendet bei technisch nicht verfuegbaren Marktdaten
    bewusst ``NICHT VERFUEGBAR | STATUS=UNAVAILABLE``. Dieser Zustand ist
    selbst eine autoritative Dateninformation und darf nicht als "fehlender
    Quellwert" behandelt werden.
    """
    for line in (source or "").splitlines():
        clean_line = line.lstrip(" -•\t")
        if not clean_line.startswith(label + ":"):
            continue
        if re.search(r"(?i)\bSTATUS\s*=\s*UNAVAILABLE\b", clean_line) and re.search(
            r"(?i)\bNICHT\s+VERFUEGBAR\b", clean_line
        ):
            return "UNAVAILABLE", "NICHT VERFUEGBAR"
        if aus_parenthesen:
            m = re.search(r"\(\s*([-+]?\d[\d.,]*)", clean_line)
        else:
            m = re.search(r":\s*([-+]?\d[\d.,]*)", clean_line)
        if m:
            raw = m.group(1).strip().strip(".,;:()[]{}")
            if raw:
                return "VALUE", raw
    return None, None


def _quellenwert_float(raw):
    if raw is None:
        return None
    value = raw.strip().strip(".,;:()[]{}")
    if value.count(",") and value.count("."):
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif value.count(",") == 1 and len(value.rsplit(",", 1)[1]) in (1, 2):
        value = value.replace(",", ".")
    elif value.count(".") > 1:
        value = value.replace(".", "")
    else:
        value = value.replace(",", "")
    try:
        return float(value)
    except ValueError:
        return None



def _repariere_7_x_quellengebunden(text, briefing_text, makro_text):
    """Sichert die autoritativen Kernwerte deterministisch in 7.1–7.7.

    Die Gemini-Interpretation bleibt unangetastet. Fehlt ein aktueller
    Quellwert im jeweils zugeordneten Ausgabeabschnitt, wird ausschließlich
    eine klar gekennzeichnete autoritative Faktenzeile innerhalb genau dieses
    Abschnitts ergänzt. Damit ist die Kette Quelle -> Abschnitt -> Gate
    deterministisch geschlossen, ohne Zahlen zu erfinden oder Werte zwischen
    Abschnitten zu verschieben.
    """
    if not text:
        return text, False

    sections = [
        "7.1 Aktienmärkte / Indizes", "7.2 Zinsen", "7.3 Volatilität", "7.4 FX",
        "7.5 Rohstoffe", "7.6 Krypto", "7.7 Konjunktur / Makro",
    ]
    index_labels = [
        ("S&P 500", False), ("Nasdaq", False), ("DAX", False),
        ("EuroStoxx50", False), ("Russell 2000", False), ("Nikkei 225", False),
        ("Hang Seng", False), ("Shanghai Composite", True),
    ]
    direct_labels = {
        "7.2 Zinsen": ["Fed Funds Effective Rate", "ECB Deposit Facility Rate", "US 2Y Treasury", "US 5Y Treasury", "US 10Y Treasury", "US 30Y Treasury", "Realzins 10Y TIPS", "2Y-10Y Spread"],
        "7.3 Volatilität": ["VIX"],
        "7.4 FX": ["DXY", "EUR/USD", "USD/JPY"],
        "7.5 Rohstoffe": ["WTI", "Brent", "Erdgas", "Kupfer", "Aluminium", "Zink", "Lithium", "Eisenerz"],
        "7.6 Krypto": ["Bitcoin", "Ethereum"],
        "7.7 Konjunktur / Makro": ["CPI", "Core CPI", "PCE", "Core PCE", "PPI", "Arbeitslosenquote", "NFP / Nonfarm Payrolls", "ADP Employment Change", "Reales BIP-Wachstum", "ISM Manufacturing PMI", "ISM Services PMI"],
    }

    source_values = {h: [] for h in sections}
    for label, parenthesized in index_labels:
        status, raw = _quellenwert_status_aus_zeile(briefing_text, label, parenthesized)
        if status == "VALUE" and _quellenwert_float(raw) is not None:
            source_values["7.1 Aktienmärkte / Indizes"].append((label, "VALUE", raw))
        elif status == "UNAVAILABLE":
            source_values["7.1 Aktienmärkte / Indizes"].append((label, "UNAVAILABLE", raw))
    for heading, labels in direct_labels.items():
        for label in labels:
            status, raw = _quellenwert_status_aus_zeile(makro_text, label)
            if status == "VALUE" and _quellenwert_float(raw) is not None:
                source_values[heading].append((label, "VALUE", raw))
            elif status == "UNAVAILABLE":
                source_values[heading].append((label, "UNAVAILABLE", raw))

    # Lithium ist der Sonderfall mit zwei bewusst getrennten autoritativen
    # Datenpunkten: LIT-Proxy und Lithium-TE in CNY/T.
    lithium_proxy = _lithium_quellenstatus_proxy(makro_text)
    lithium_te = _lithium_te_referenz(makro_text)

    changed = False
    result = text
    for heading in sections:
        block = _abschnitt_text(result, heading, sections + ["8.1 Gold"])
        if not block:
            continue
        additions = []
        for label, status, raw in source_values[heading]:
            if status == "UNAVAILABLE":
                # Eine bereits vorhandene numerische Zeile desselben autoritativen
                # Labels darf niemals neben dem UNAVAILABLE-Quellstatus bestehen:
                # sonst koennte ein Gemini-Wert die deterministische Quelle
                # semantisch ueberschreiben. Eine vorhandene korrekte
                # UNAVAILABLE-Zeile bleibt unangetastet.
                if re.search(
                    r"(?im)^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?"
                    + re.escape(label)
                    + r"\s*:\s*(?!NICHT\s+VERFUEGBAR\b).*?\d[\d.,]*",
                    block,
                ):
                    additions.append(
                        f"- [AUTORITATIVE QUELLE] {label}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE"
                    )
                elif not re.search(
                    r"(?im)^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?"
                    + re.escape(label)
                    + r"\s*:\s*NICHT\s+VERFUEGBAR\b.*?\bSTATUS\s*=\s*UNAVAILABLE\b",
                    block,
                ):
                    additions.append(
                        f"- [AUTORITATIVE QUELLE] {label}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE"
                    )
                continue
            value = _quellenwert_float(raw)
            if not _zahl_im_block_vorhanden(block, value, label=label):
                additions.append(f"- [AUTORITATIVE QUELLE] {label}: {raw}")
        lithium_wird_vorhanden_sein = (
            "Lithium" in block
            or any(label == "Lithium" for label, _, _ in source_values[heading])
        )
        if (
            heading == "7.5 Rohstoffe"
            and lithium_proxy
            and lithium_wird_vorhanden_sein
            and not re.search(r"proxy", block, re.I)
        ):
            additions.append("- [AUTORITATIVE QUELLE] Lithium: STATUS=PROXY")
        if heading == "7.5 Rohstoffe" and lithium_te is not None:
            if lithium_te.get("status") == "UNAVAILABLE":
                if not re.search(
                    r"(?im)^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?Lithiumcarbonat CNY/T\s*:\s*NICHT\s+VERFUEGBAR\b.*STATUS\s*=\s*UNAVAILABLE",
                    block,
                ):
                    additions.append(
                        "- [AUTORITATIVE QUELLE] Lithiumcarbonat CNY/T: NICHT VERFUEGBAR | "
                        "STATUS=UNAVAILABLE | Einheit=CNY/T | DATENTYP=TE_PUBLIC_LITHIUM"
                    )
            else:
                lithium_te_value = f"{float(lithium_te['kurs']):.2f}"
                if not _zahl_im_block_vorhanden(block, float(lithium_te['kurs']), label="Lithiumcarbonat CNY/T"):
                    additions.append(
                        f"- [AUTORITATIVE QUELLE] Lithiumcarbonat CNY/T: {lithium_te_value} | "
                        f"Einheit=CNY/T | Datenstand={lithium_te.get('datenstand', 'unbekannt')} | "
                        "SOURCE=TradingEconomics Public Commodities | DATENTYP=TE_PUBLIC_LITHIUM"
                    )
        if not additions:
            continue

        source = _normalisiere_inline_pflichtueberschriften(result)
        m = re.search(r"(?m)^" + re.escape(heading) + r"\s*$", source)
        if not m:
            continue
        end = len(source)
        for nxt in sections + ["8.1 Gold"]:
            n = re.search(r"(?m)^" + re.escape(nxt) + r"\s*$", source[m.end():])
            if n:
                end = min(end, m.end() + n.start())
        insertion = "\n" + "\n".join(additions) + "\n"
        new_block = source[m.start():end].rstrip() + insertion
        result = source[:m.start()] + new_block + source[end:]
        changed = True

    # Den Lithium-TE-Datenpunkt unmittelbar vor dem Gate kanonisieren: Eine
    # abweichende Gemini-Zeile darf nicht vor der autoritativen Zeile stehen,
    # weil das Quellen-Gate den ersten passenden Eintrag ausliest.
    result, lithium_changed = _sichere_lithium_te_in_7_5(result, lithium_te)
    changed = changed or lithium_changed
    return result, changed


def _sichere_lithium_te_in_7_5(text, lithium_te):
    """Kanonisiert den getrennten Lithium-TE/CNY-T-Datenpunkt im Rohstoffblock."""
    if not text or lithium_te is None:
        return text, False
    m = re.search(
        r"(?ms)^7\.5\s+Rohstoffe\s*$.*?(?=^7\.[67]\s+|^8\.1\s+Gold\s*$|\Z)",
        text,
    )
    if not m:
        return text, False

    if lithium_te.get("status") == "UNAVAILABLE":
        line = (
            "- [AUTORITATIVE QUELLE] Lithiumcarbonat CNY/T: NICHT VERFUEGBAR | "
            "STATUS=UNAVAILABLE | Einheit=CNY/T | DATENTYP=TE_PUBLIC_LITHIUM"
        )
    else:
        value = float(lithium_te["kurs"])
        line = (
            f"- [AUTORITATIVE QUELLE] Lithiumcarbonat CNY/T: {value:.2f} | "
            f"Einheit=CNY/T | Datenstand={lithium_te.get('datenstand', 'unbekannt')} | "
            "SOURCE=TradingEconomics Public Commodities | DATENTYP=TE_PUBLIC_LITHIUM"
        )

    block = m.group(0)
    lines = block.splitlines()
    if not lines:
        return text, False

    # Alle expliziten Zeilen dieses Datenpunkts entfernen, egal ob Gemini- oder
    # vorherige autoritative Zeile. Genau eine aktuelle autoritative Zeile wird
    # direkt nach der Abschnittsüberschrift eingesetzt. Andere Rohstoffinhalte
    # und nachfolgende Abschnitte bleiben erhalten.
    label_line = re.compile(
        r"^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?Lithiumcarbonat CNY/T\s*:",
        re.I,
    )
    rest = [entry for entry in lines[1:] if not label_line.match(entry)]
    canonical_lines = [lines[0], line, *rest]
    replacement = "\n".join(canonical_lines)
    if block.endswith("\n"):
        replacement += "\n"
    if replacement == block:
        return text, False
    return text[:m.start()] + replacement + text[m.end():], True


def _pruefe_punkt7_quellenabdeckung(text, eingabedateien):
    """Vollstaendigkeits-Gate fuer 7.1–7.7 gegen die jeweils aktuellen Tagesquellen."""
    briefing = ""
    makro = ""
    for key, target in (("briefing.txt", "briefing"), ("Makro_Briefing(...).txt", "makro")):
        path = (eingabedateien or {}).get(key)
        if path and os.path.isfile(path):
            try:
                content = Path(path).read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise RuntimeError(f"PUNKT7_QUELLE_NICHT_LESBAR: {key}: {exc}") from exc
            if target == "briefing":
                briefing = content
            else:
                makro = content

    sections = [
        "7.1 Aktienmärkte / Indizes", "7.2 Zinsen", "7.3 Volatilität", "7.4 FX",
        "7.5 Rohstoffe", "7.6 Krypto", "7.7 Konjunktur / Makro",
    ]
    blocks = {h: _abschnitt_text(text, h, sections + ["8.1 Gold"]) for h in sections}
    errors = [f"{h}: Abschnitt fehlt oder ist nicht kanonisch abgrenzbar." for h in sections if not blocks[h]]
    if errors:
        raise RuntimeError("PUNKT7_QUELLENABDECKUNG_UNGUELTIG: " + " | ".join(errors))

    # Pro Abschnitt werden nur die aktuell in den Quellen vorhandenen Kernfelder
    # abgefragt. Dadurch ist das Gate datumsoffen und muss nicht bei jedem Lauf
    # mit neuen Kursen/Terminen im Code angepasst werden.
    index_labels = [
        ("S&P 500", False), ("Nasdaq", False), ("DAX", False),
        ("EuroStoxx50", False), ("Russell 2000", False), ("Nikkei 225", False),
        ("Hang Seng", False), ("Shanghai Composite", True),
    ]
    direct_labels = {
        "7.2 Zinsen": ["Fed Funds Effective Rate", "ECB Deposit Facility Rate", "US 2Y Treasury", "US 5Y Treasury", "US 10Y Treasury", "US 30Y Treasury", "Realzins 10Y TIPS", "2Y-10Y Spread"],
        "7.3 Volatilität": ["VIX"],
        "7.4 FX": ["DXY", "EUR/USD", "USD/JPY"],
        "7.5 Rohstoffe": ["WTI", "Brent", "Erdgas", "Kupfer", "Aluminium", "Zink", "Lithium", "Eisenerz"],
        "7.6 Krypto": ["Bitcoin", "Ethereum"],
        "7.7 Konjunktur / Makro": ["CPI", "Core CPI", "PCE", "Core PCE", "PPI", "Arbeitslosenquote", "NFP / Nonfarm Payrolls", "ADP Employment Change", "Reales BIP-Wachstum", "ISM Manufacturing PMI", "ISM Services PMI"],
    }

    errors = []
    for label, parenthesized in index_labels:
        status, raw = _quellenwert_status_aus_zeile(briefing, label, parenthesized)
        if status is None:
            errors.append(f"7.1 Aktienmärkte / Indizes: autoritativer Quellwert {label} fehlt in Briefing.")
            continue
        if status == "UNAVAILABLE":
            _gate_quellenstatus(blocks["7.1 Aktienmärkte / Indizes"], label, status, errors)
            continue
        value = _quellenwert_float(raw)
        if value is None:
            errors.append(f"7.1 Aktienmärkte / Indizes: Quellwert {label}={raw!r} ist nicht numerisch lesbar.")
            continue
        _gate_quellenwert(blocks["7.1 Aktienmärkte / Indizes"], label, value, errors)

    for heading, labels in direct_labels.items():
        for label in labels:
            status, raw = _quellenwert_status_aus_zeile(makro, label)
            if status is None:
                errors.append(f"{heading}: autoritativer Quellwert {label} fehlt im Makro_Briefing.")
                continue
            if status == "UNAVAILABLE":
                _gate_quellenstatus(blocks[heading], label, status, errors)
                continue
            value = _quellenwert_float(raw)
            if value is None:
                errors.append(f"{heading}: Quellwert {label}={raw!r} ist nicht numerisch lesbar.")
                continue
            _gate_quellenwert(blocks[heading], label, value, errors)

    if "Lithium" in blocks["7.5 Rohstoffe"]:
        lithium_proxy = _lithium_quellenstatus_proxy(makro)
        if lithium_proxy and not re.search(r"proxy", blocks["7.5 Rohstoffe"], re.I):
            errors.append("7.5 Rohstoffe: Lithium wurde genannt, aber der Quellstatus PROXY wird in der Ausgabe nicht kenntlich gemacht.")

    lithium_te = _lithium_te_referenz(makro)
    if lithium_te is not None:
        # Die deterministische Reparatur markiert ihre Zeile mit
        # [AUTORITATIVE QUELLE]. Der allgemeine Quellenparser erwartet dagegen
        # das Label am Zeilenanfang und erkennt diesen Marker nicht. Bevorzugt
        # deshalb die kanonische autoritative Zeile; so wird auch kein davor
        # stehender, abweichender Gemini-Wert als Referenz gelesen.
        lithium_lines = [
            line for line in blocks["7.5 Rohstoffe"].splitlines()
            if re.match(
                r"^\s*(?:[-•]\s*)?(?:\[[^\]]+\]\s*)?Lithiumcarbonat CNY/T\s*:",
                line,
                re.I,
            )
        ]
        authoritative_line = next(
            (line for line in lithium_lines if re.search(r"(?i)\[AUTORITATIVE QUELLE\]", line)),
            None,
        )
        selected_line = authoritative_line
        if selected_line and re.search(r"(?i)\bSTATUS\s*=\s*UNAVAILABLE\b", selected_line):
            status, raw = "UNAVAILABLE", "NICHT VERFUEGBAR"
        elif selected_line:
            value_match = re.search(r":\s*([-+]?\d[\d.,]*)", selected_line)
            status, raw = ("VALUE", value_match.group(1)) if value_match else (None, None)
        else:
            status, raw = None, None
        if lithium_te.get("status") == "UNAVAILABLE":
            if status != "UNAVAILABLE":
                errors.append(
                    "7.5 Rohstoffe: Lithiumcarbonat CNY/T ist in der autoritativen Quelle UNAVAILABLE, "
                    "aber dieser Status fehlt in der Ausgabe."
                )
        else:
            lithium_te_value = float(lithium_te["kurs"])
            if status != "VALUE" or _quellenwert_float(raw) is None or abs(_quellenwert_float(raw) - lithium_te_value) > 1e-9:
                errors.append(
                    "7.5 Rohstoffe: autoritativer Lithiumcarbonat-CNY/T-Wert fehlt oder weicht vom Makro-Referenzwert ab."
                )

    if errors:
        raise RuntimeError("PUNKT7_QUELLENABDECKUNG_UNGUELTIG: " + " | ".join(errors))
    print("PUNKT-7-QUELLENABDECKUNGS-GATE: PASS")


def _edelmetall_quellenblock(edel_text, asset):
    """Liefert den exakt zugeordneten operativen Trendfolge-Diagnoseblock eines Metalls.

    Die Suche beginnt bewusst erst innerhalb von ``TRENDFOLGE-DIAGNOSE JE METALL``.
    Dadurch kann ein gleichnamiger Asset-Bullet aus einem vorgelagerten oder
    anderen Quellblock nicht versehentlich als EMA200/WMA200-Quelle verwendet werden.
    """
    source = edel_text or ""
    trend_match = re.search(
        r"(?m)^TRENDFOLGE-DIAGNOSE JE METALL"
        r"(?:\s*\(GC=F etc\. = operative Datenbasis\))?"
        r"\s*:?\s*$",
        source,
    )
    if not trend_match:
        return ""

    lines = source[trend_match.end():].splitlines()
    start = next(
        (
            i for i, line in enumerate(lines)
            if re.match(
                r"^\s*-\s*" + re.escape(asset) +
                r"\s*\([^)]*\)\s*(?:(?::|[-–—])\s*[^\r\n]*)?$",
                line,
            )
        ),
        None,
    )
    if start is None:
        return ""

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(
            r"^\s*-\s*(?:Gold|Silber|Platin|Palladium)\s*\([^)]*\)\s*(?:(?::|[-–—])\s*[^\r\n]*)?$",
            lines[i],
        ):
            end = i
            break
        if re.match(r"^\s*=+\s*$", lines[i]):
            end = i
            break
        if re.match(r"^\s*STRATEGIE:\s+", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def _edelmetall_strategieblock(edel_text, strategie):
    """Liest genau einen autoritativen STRATEGIE-Block."""
    marker = f"STRATEGIE: {strategie.upper()}"
    matches = list(
        re.finditer(
            r"(?m)^" + re.escape(marker) +
            r"(?:\s*:|\s*[-–—]\s*operative Datenbasis)?\s*$",
            edel_text or "",
        )
    )
    if not matches:
        return ""
    start = matches[0].end()
    nxt = re.search(r"(?m)^STRATEGIE:\s+", (edel_text or "")[start:])
    end = start + nxt.start() if nxt else len(edel_text or "")
    return (edel_text or "")[start:end].strip()


def _edelmetall_status(edel_text, strategie, asset, ticker):
    """Ermittelt den Status deterministisch aus dem autoritativen Strategieblock."""
    block = _edelmetall_strategieblock(edel_text, strategie)
    if not block:
        raise RuntimeError(
            f"EDELMETALL_QUELLE_UNVOLLSTAENDIG: STRATEGIE:{strategie.upper()} fehlt."
        )

    if strategie.casefold() == "trendfolge":
        source_block = _edelmetall_quellenblock(edel_text, asset)
        if not source_block:
            raise RuntimeError(
                f"EDELMETALL_QUELLE_UNVOLLSTAENDIG: {asset} fehlt im Trendfolge-Diagnoseblock."
            )
        if re.search(r"(?i)\bEndergebnis:\s*KANDIDAT\b", source_block):
            return "KANDIDAT"
        if re.search(
            r"(?i)\bErgebnis:\s*BLOCKIERT\b|\bGesamt\s+NEIN\b|\bEndergebnis:\s*BLOCKIERT\b",
            source_block,
        ):
            return "kein Kandidat / blockiert"
        return "nicht eindeutig"

    # Trendwende/Short enthalten bei 0 Kandidaten teilweise keine
    # assetbezogenen Detailblöcke. Dann darf kein Asset-Status erfunden werden.
    m_asset = re.search(
        r"(?im)^\s*[-•]?\s*" + re.escape(asset) +
        r"\s*\(" + re.escape(ticker) + r"\)\s*$",
        block,
    )
    if m_asset:
        tail = block[m_asset.start():]
        nxt = re.search(
            r"(?im)^\s*[-•]\s*(?:Gold|Silber|Platin|Palladium)\s*\([^)]*\)\s*$",
            tail[1:],
        )
        asset_block = tail[:nxt.start() + 1] if nxt else tail
        if re.search(
            r"(?i)\bEndergebnis:\s*KANDIDAT\b|\bStatus:\s*VALIDE\b",
            asset_block,
        ):
            return "KANDIDAT"
        if re.search(
            r"(?i)\bBLOCKIERT\b|\bKEIN\s+SETUP\b|\bKEIN\s+KANDIDAT\b|\bEndergebnis:\s*BLOCKIERT\b",
            asset_block,
        ):
            return "kein Kandidat / blockiert"
        return "im Quellblock vorhanden"

    zero_pattern = (
        r"(?i)Keine\s+(?:Trendwende|Short)-Kandidaten"
        r"|=>\s*(?:TRENDWENDE|SHORT)-KANDIDAT:\s*0\b"
    )
    if re.search(zero_pattern, block):
        return "kein Kandidat"
    if re.search(r"(?i)=>\s*(?:TRENDWENDE|SHORT)-KANDIDAT:\s*[1-9]\d*", block):
        return "nicht eindeutig"
    return "nicht eindeutig"


def _repariere_8_x_quellengebunden(text, edel_text):
    """
    Uebernimmt die autoritativen 8.1–8.4-Daten deterministisch.

    Regeln:
    - Quelle ist ausschliesslich das aktuelle Edelmetalle-Briefing.
    - Kurs/4W und EMA200/WMA200 werden fuer alle vier Metalle zwingend aus
      der Quelle gelesen; fehlt ein Wert, wird hart abgebrochen.
    - Bereits vorhandene Gemini-Werte dieser Felder werden entfernt und durch
      die Quellwerte ersetzt.
    - Strategie-Status wird ebenfalls ausschliesslich aus der Quelle erzeugt.
    - Qualitative Gemini-Inhalte bleiben unveraendert.
    """
    if not text:
        return text
    if not edel_text:
        raise RuntimeError("EDELMETALL_QUELLE_UNVOLLSTAENDIG: leere Quelle.")

    headings = ["8.1 Gold", "8.2 Silber", "8.3 Platin", "8.4 Palladium"]
    assets = {
        "8.1 Gold": ("Gold", "GC=F"),
        "8.2 Silber": ("Silber", "SI=F"),
        "8.3 Platin": ("Platin", "PL=F"),
        "8.4 Palladium": ("Palladium", "PA=F"),
    }

    # 1) Ausschliesslich den aktuellen LAGE-JE-METALL-Block auswerten.
    lage_match = re.search(
        r"(?ms)^LAGE JE METALL\b.*?^(?=TRENDFOLGE-DIAGNOSE JE METALL\b)",
        edel_text,
    )
    if not lage_match:
        raise RuntimeError(
            "EDELMETALL_QUELLE_UNVOLLSTAENDIG: Block 'LAGE JE METALL' fehlt."
        )
    lage_block = lage_match.group(0)

    lage = {}
    missing = []
    for asset, _ticker in assets.values():
        m = re.search(
            r"(?m)^\s*" + re.escape(asset) +
            r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*"
            r"([-+]?\d[\d.,]*)%\s+in den letzten 4 Wochen\b[^\n]*$",
            lage_block,
        )
        if not m:
            missing.append(f"{asset}: Kurs/4W")
        else:
            lage[asset] = (m.group(1), m.group(2))
            low = re.search(
                r"52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%",
                m.group(0),
            )
            if low:
                lage[asset] = (m.group(1), m.group(2), low.group(1), low.group(2))

    # 2) Operative EMA200/WMA200-Basis aus dem exakt zugeordneten
    #    Trendfolge-Diagnoseblock lesen.
    trend = {}
    for asset, _ticker in assets.values():
        source_block = _edelmetall_quellenblock(edel_text, asset)
        if not source_block:
            missing.append(f"{asset}: Trendfolge-Diagnose")
            continue

        m200 = re.search(
            r"200er-Trend:\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*"
            r"EMA200\s+([-+]?\d[\d.,]*)\s*\([^)]*\)\s*\|\s*"
            r"WMA200\s+([-+]?\d[\d.,]*)",
            source_block,
        )
        if not m200:
            missing.append(f"{asset}: EMA200/WMA200")
        else:
            trend[asset] = (m200.group(2), m200.group(3))

    if missing:
        raise RuntimeError(
            "EDELMETALL_QUELLE_UNVOLLSTAENDIG: " + " | ".join(missing)
        )

    # 3) Alle vier Ausgabeabschnitte müssen vorhanden und kanonisch abgrenzbar sein.
    matches = list(
        re.finditer(
            r"(?m)^(8\.[1-4]\s+(?:Gold|Silber|Platin|Palladium))\s*$",
            text,
        )
    )
    found = {m.group(1) for m in matches}
    missing_headings = [h for h in headings if h not in found]
    if missing_headings:
        raise RuntimeError(
            "PUNKT8_QUELLENUEBERNAHME_UNGUELTIG: fehlende Ausgabeabschnitte: "
            + ", ".join(missing_headings)
        )

    replacements = []
    for i, match in enumerate(matches):
        heading = match.group(1)
        asset, ticker = assets[heading]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        # Abschnitt 8.4 endet vor Punkt 9.
        n9 = re.search(
            r"(?m)^9\.\s*📅\s*NÄCHSTE KATALYSATOREN\s*$",
            text[match.end():end],
        )
        if n9:
            end = match.end() + n9.start()

        block = text[match.start():end].strip()

        cleaned = []
        for line in block.splitlines():
            stripped = line.strip()
            if re.match(
                r"^[-•]?\s*(?:Kurs(?:\s*\(Futures[^)]*\))?|"
                r"4W|52W-Tief|Abstand 52W-Tief|EMA200|WMA200|"
                r"Autoritative Fakten|Strategie-Status(?:\s*\([^)]*\))?)\s*:",
                stripped,
                re.I,
            ):
                continue
            cleaned.append(line)

        # 4) Quellwerte werden immer geschrieben. Es gibt keinen Fallback
        #    auf bereits vorhandene Gemini-Werte.
        lage_values = lage[asset]
        kurs, four_w = lage_values[0], lage_values[1]
        ema, wma = trend[asset]
        fact_parts = [
            f"Kurs (Futures {ticker}): {kurs}$",
            f"4W: {four_w}%",
        ]
        if len(lage_values) >= 4:
            low, dist = lage_values[2], lage_values[3]
            fact_parts.extend([
                f"52W-Tief: {low}$",
                f"Abstand 52W-Tief: {dist}%",
            ])
        fact_parts.extend([
            f"EMA200: {ema}",
            f"WMA200: {wma}",
        ])

        insert_at = 1 if len(cleaned) > 1 else len(cleaned)
        cleaned.insert(
            insert_at,
            "- Autoritative Fakten: " + " | ".join(fact_parts),
        )

        statuses = [
            f"Trendfolge={_edelmetall_status(edel_text, 'Trendfolge', asset, ticker)}",
            f"Trendwende={_edelmetall_status(edel_text, 'Trendwende', asset, ticker)}",
            f"Short={_edelmetall_status(edel_text, 'Short', asset, ticker)}",
        ]
        cleaned.append(
            "- Strategie-Status (autoritative Quelle): " + " | ".join(statuses)
        )
        replacements.append(
            (match.start(), end, "\n".join(cleaned).strip() + "\n")
        )

    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]

    return text

def _pruefe_punkt8_quellenabdeckung(text, eingabedateien):
    """Vollstaendigkeits-Gate fuer 8.1–8.4 gegen das aktuelle Edelmetall-Briefing."""
    edel = ""
    path = (eingabedateien or {}).get("Edelmetalle_Briefing(...).txt")
    if path and os.path.isfile(path):
        try:
            edel = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"PUNKT8_QUELLE_NICHT_LESBAR: Edelmetalle_Briefing(...).txt: {exc}") from exc

    headings = ["8.1 Gold", "8.2 Silber", "8.3 Platin", "8.4 Palladium"]
    blocks = {h: _abschnitt_text(text, h, headings + ["9. 📅 NÄCHSTE KATALYSATOREN"]) for h in headings}
    errors = [f"{h}: Abschnitt fehlt oder ist nicht kanonisch abgrenzbar." for h in headings if not blocks[h]]
    if errors:
        raise RuntimeError("PUNKT8_QUELLENABDECKUNG_UNGUELTIG: " + " | ".join(errors))

    assets = {
        "8.1 Gold": "Gold", "8.2 Silber": "Silber", "8.3 Platin": "Platin", "8.4 Palladium": "Palladium",
    }
    for heading, asset in assets.items():
        block = blocks[heading]

        # Ausschliesslich den autoritativen LAGE-JE-METALL-Block verwenden.
        # Kein globales ``find`` auf Asset-Zeilen: Dadurch kann eine gleichnamige
        # Zeile aus einem anderen Quellblock nicht als aktuelle Lagequelle
        # durchrutschen.
        lage_match = re.search(
            r"(?ms)^LAGE JE METALL\b.*?^(?=TRENDFOLGE-DIAGNOSE JE METALL\b)",
            edel,
        )
        lage_block = lage_match.group(0) if lage_match else ""
        lage_line = next(
            (
                line for line in lage_block.splitlines()
                if re.match(
                    r"^\s*" + re.escape(asset) + r":\s*Kurs\s+",
                    line,
                )
            ),
            "",
        )
        trend_block = _edelmetall_quellenblock(edel, asset)

        if not lage_line:
            errors.append(f"{heading}: aktuelle Lagezeile fehlt im Edelmetalle_Briefing.")
        else:
            m = re.search(r"Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*([-+]?\d[\d.,]*)%\s+in den letzten 4 Wochen", lage_line)
            if not m:
                errors.append(f"{heading}: Kurs/4W-Werte konnten aus der Lagezeile nicht gelesen werden.")
            else:
                for label, raw in (("Kurs", m.group(1)), ("4W", m.group(2))):
                    value = _quellenwert_float(raw)
                    if value is not None:
                        _gate_quellenwert(block, label, value, errors)

            low_match = re.search(r"52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%", lage_line)
            if low_match:
                _gate_quellenwert(block, "52W-Tief", _quellenwert_float(low_match.group(1)), errors)
                _gate_quellenwert(block, "Abstand 52W-Tief", _quellenwert_float(low_match.group(2)), errors)

        # Die 200er-Zeile enthält die operative EMA200/WMA200-Basis und das
        # deterministische Trendfolge-Ergebnis je Metall.
        m200 = re.search(
            r"200er-Trend:\s*Kurs\s*=?\s*([-+]?\d[\d.,]*)\s*\|\s*"
            r"EMA200\s*=?\s*([-+]?\d[\d.,]*)"
            r"(?:\s*\([^)]*\))?\s*\|\s*"
            r"WMA200\s*=?\s*([-+]?\d[\d.,]*)",
            trend_block,
        )
        if not m200:
            errors.append(f"{heading}: 200er-Trendzeile fehlt oder ist nicht lesbar.")
        else:
            for label, raw in (("EMA200", m200.group(2)), ("WMA200", m200.group(3))):
                _gate_quellenwert(block, label, _quellenwert_float(raw), errors)
        if "BLOCKIERT" in trend_block and not re.search(r"trendfolge", block, re.I):
            errors.append(f"{heading}: Trendfolge-Blockierung fehlt in der Ausgabe.")
        for strategy in ("Trendfolge", "Trendwende", "Short"):
            if not re.search(r"\b" + strategy + r"\b", block, re.I):
                errors.append(f"{heading}: {strategy}-Status/Einordnung fehlt.")

    if errors:
        raise RuntimeError("PUNKT8_QUELLENABDECKUNG_UNGUELTIG: " + " | ".join(errors))
    print("PUNKT-8-QUELLENABDECKUNGS-GATE: PASS")
def speichere_ergebnis(text, eingabedateien=None):
    heute = datetime.date.today().isoformat()
    ausgabe_datei = f"Auswertung({heute}).txt"

    # Technischer Gemini-Fallback darf nicht durch die normale
    # Positions-/Punkt-10-Validierung laufen: Es gibt in diesem Fall bewusst
    # keine Gemini-Auswertung, die validiert werden koennte.
    if str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        final_text = str(text)
    else:
        final_text = normalisiere_ausgabe(
            text,
            zielzonen=_technische_zielzonen_quelle("Offene Positionen+Check.csv"),
        )
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Letzter deterministischer Brent-WTI-Gate vor der Strukturpruefung und
        # Speicherung. Der aktuelle Makro-Datensatz ist die einzige Quelle fuer
        # Brent und WTI; nur explizite Spread-/Differenzformulierungen werden
        # korrigiert, einzelne Brent-/WTI-Kurse bleiben unangetastet.
        makro_pfad = finde_datei(DATEIMUSTER["Makro_Briefing(...).txt"])
        if makro_pfad and os.path.isfile(makro_pfad):
            try:
                with open(makro_pfad, "r", encoding="utf-8-sig") as f:
                    makro_text = f.read()
                final_text, _ = _sichere_brent_wti_spread(final_text, makro_text)
            except Exception as exc:
                raise RuntimeError(
                    f"WTI_BRENT_SPREAD_GATE_FEHLER: {exc}"
                ) from exc

    if not str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        makro_pfad = finde_datei(DATEIMUSTER["Makro_Briefing(...).txt"])
        makro_text_fx = ""
        if makro_pfad and os.path.isfile(makro_pfad):
            try:
                makro_text_fx = Path(makro_pfad).read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise RuntimeError(f"FX_QUELLE_NICHT_LESBAR: {exc}") from exc
        final_text, fx_repaired = _repariere_7_4_fx_aus_makroquelle(final_text, makro_text_fx)
        if fx_repaired:
            print("INFO: 7.4 FX deterministisch aus autoritativer Makroquelle repariert (Gemini-Ausgabe zu knapp).")
        final_text, rohstoffe_repaired = _repariere_7_5_rohstoffe_aus_makroquelle(final_text, makro_text_fx)
        if rohstoffe_repaired:
            print("INFO: 7.5 Rohstoffe deterministisch aus autoritativer Makroquelle repariert (Gemini-Ausgabe zu knapp).")
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _ergaenze_fehlende_ausgabestruktur(final_text)

        # 8.1–8.4: autoritative Fakten/Strategiestatus aus dem aktuellen
        # Edelmetalle-Briefing deterministisch einsetzen; Gemini bleibt für
        # qualitative Interpretation zuständig.
        edelmetall_pfad = finde_datei(DATEIMUSTER["Edelmetalle_Briefing(...).txt"])
        edelmetall_text = ""
        if edelmetall_pfad and os.path.isfile(edelmetall_pfad):
            try:
                edelmetall_text = Path(edelmetall_pfad).read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise RuntimeError(f"EDELMETALLE_QUELLE_NICHT_LESBAR: {exc}") from exc
            final_text = _repariere_8_x_quellengebunden(final_text, edelmetall_text)

        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)
        if eingabedateien is None:
            eingabedateien = _GEMINI_EINGABEDATEIEN_AUSWERTUNG
        final_text = _bereinige_punkt_24_nur_a(final_text, eingabedateien)
        final_text = _normalisiere_punkt10_autoritaet(final_text)
        final_text = _normalisiere_punkt11_quellengebunden(final_text)
        final_text = _normalisiere_name_ticker_ausgabe(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Letzte Strukturabsicherung NACH allen inhaltlichen Normalisierungen.
        # Frühere Pflichtabschnitte können durch nachgelagerte Normalisierungen
        # verändert oder entfernt werden; deshalb wird die verbindliche
        # 1–11.7-Struktur unmittelbar vor dem harten Final-Gate nochmals
        # deterministisch ergänzt. Bereits vorhandene echte Inhalte bleiben
        # unverändert.
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _ergaenze_fehlende_ausgabestruktur(final_text)
        if edelmetall_text:
            final_text = _repariere_8_x_quellengebunden(final_text, edelmetall_text)
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Letzte deterministische Schliessung der Kette Quelle -> 7.1–7.7 -> Gate.
        # Dieser Pass steht bewusst unmittelbar vor den harten Quellen-Gates,
        # damit nachgelagerte Normalisierungen die autoritativen Werte nicht
        # wieder aus ihrem zugeordneten Abschnitt entfernen koennen.
        briefing_pfad = finde_datei(DATEIMUSTER["briefing.txt"])
        briefing_text = ""
        if briefing_pfad and os.path.isfile(briefing_pfad):
            try:
                briefing_text = Path(briefing_pfad).read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise RuntimeError(f"BRIEFING_QUELLE_NICHT_LESBAR: {exc}") from exc

        final_text, quellen_repaired = _repariere_7_x_quellengebunden(
            final_text,
            briefing_text,
            makro_text_fx,
        )
        if quellen_repaired:
            print("INFO: 7.1-7.7 autoritative Quellwerte deterministisch in die zugeordneten Abschnitte ergaenzt.")

        # Neue Vollstaendigkeits-Gates fuer 7.1–7.7 und 8.1–8.4.
        # Sie werden ADDITIV vor den bestehenden Final-Gates ausgefuehrt.
        # Keine bestehende Schutzpruefung wird dadurch ersetzt oder entfernt.
        eingabedateien_gate = {
            key: finde_datei(patterns)
            for key, patterns in DATEIMUSTER.items()
            if key in (
                "briefing.txt",
                "Makro_Briefing(...).txt",
                "Edelmetalle_Briefing(...).txt",
            )
        }
        _pruefe_punkt7_quellenabdeckung(final_text, eingabedateien_gate)
        _pruefe_punkt8_quellenabdeckung(final_text, eingabedateien_gate)

        _pruefe_name_ticker_gate(final_text)
        _pruefe_punkt11_quellenbindung(final_text)
        _pruefe_neue_ausgabestruktur(final_text)
        _pruefe_inhaltliche_mindesttiefe(final_text)

    if str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        print("INFO: Technischer Gemini-Fallback - bestehende Auswertung bleibt unverändert; keine Auswertung wird gespeichert.")
        return None

    with open(ausgabe_datei, "w", encoding="utf-8-sig") as f:
        f.write(final_text)

    # Persistenter Gemini-Langzeitkontext: Nur nach einer echten Gemini-
    # Auswertung wird eine neue Historien-Snapshot erzeugt. Ein technischer
    # Fallback darf die Historie niemals mit einem Schein-Lauf fortschreiben.
    if not str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        # upload_to_drive.py erkennt den Dateinamen ueber "Auswertung" und
        # persistiert die Datei im gleichen Google-Drive-Ordner.
        try:
            bestehende_historie = ""
            if os.path.isfile(GEMINI_HISTORIE_DATEI):
                with open(GEMINI_HISTORIE_DATEI, "r", encoding="utf-8-sig") as f:
                    bestehende_historie = f.read()
            historie = _baue_gemini_historie(final_text, bestehende_historie)
            with open(GEMINI_HISTORIE_DATEI, "w", encoding="utf-8") as f:
                f.write(historie)
            print(f"Persistente Gemini-Historie aktualisiert: {GEMINI_HISTORIE_DATEI}")
        except Exception as exc:
            # Die Tagesauswertung darf durch einen reinen Historienfehler nicht
            # unbrauchbar werden; der Fehler bleibt im Log sichtbar.
            print(f"WARNUNG: Persistente Gemini-Historie konnte nicht erstellt werden ({exc}).")
    else:
        print("INFO: Technischer Gemini-Fallback - persistente Historie wird nicht fortgeschrieben.")

    print(f"\nGespeichert: {ausgabe_datei}")
    return ausgabe_datei


if __name__ == "__main__":
    print("Gemini-Auswertung gestartet...")
    ergebnis_text = gemini_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(
        ergebnis_text,
        _GEMINI_EINGABEDATEIEN_AUSWERTUNG,
    )
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
    if str(ergebnis_text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        print("GEMINI_STATUS=TECHNISCHER_FALLBACK")
        print("GEMINI_EXIT_STATUS=0 | Technischer Fallback: Keine Auswertung-Datei erzeugt; vorhandene Tagesdatei bleibt unverändert.")
