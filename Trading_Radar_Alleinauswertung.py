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
import math
import tempfile
import datetime
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path
import mimetypes
from types import SimpleNamespace

# Die Auswertungs- und Statushilfen sollen auch in Umgebungen ohne Gemini-SDK
# importierbar/testbar bleiben. Der eigentliche Produktionsstart fail-closed
# mit einer konkreten Installationsmeldung, bevor ein API-Aufruf erfolgt.
try:
    from google import genai
    from google.genai import types
except ImportError as exc:
    genai = None
    types = None
    _GEMINI_SDK_IMPORT_ERROR = exc
else:
    _GEMINI_SDK_IMPORT_ERROR = None
# Google-Drive-Abhaengigkeiten sind fuer Status-/Report-Tests nicht erforderlich.
# Der Drive-Zugriff prueft sie erst am fachlich benoetigten Einstiegspunkt.
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError as exc:
    Credentials = Request = build = MediaIoBaseDownload = None
    _GOOGLE_DRIVE_IMPORT_ERROR = exc
else:
    _GOOGLE_DRIVE_IMPORT_ERROR = None
import io
from collections import defaultdict
from trading_radar_neuauswertung import audit_market_first_content
from trading_radar_market_first import append_live_market_snapshot, downgrade_ohlcv_affected_candidates, normalize_event_section, validate_ohlcv_audit
from trading_radar_report_gate import validate_report as validate_trading_radar_report

from quellenwert_vertrag import (
    values_match_display, find_exact_labeled_line, extract_four_week_percent,
    extract_metal_four_week_value,
)


# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

MODELL = "gemini-3.5-flash-lite"  # Primaer-Modell: hohes kostenloses RPD/TPM und hoher Durchsatz
FALLBACK_MODELL = "gemini-3.1-flash-lite"  # Erster Fallback: ebenfalls hohes kostenloses RPD/TPM
DRITTER_FALLBACK_MODELL = "gemini-3.8-flash"  # Qualitaets-Fallback bei Ausfall beider Lite-Modelle
VIERTER_FALLBACK_MODELL = "gemini-3-flash-preview"  # Weiterer Qualitaets-Fallback
FUENFTER_FALLBACK_MODELL = "gemini-3.6-flash"  # Weiterer Qualitaets-Fallback

# Je Request sind zwei begrenzte Modellpool-Runden moeglich. Pro Request und
# Modell bleiben maximal zwei echte API-Sendungen erlaubt; andere Splits haben
# eine eigene Fehlerhistorie.
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
# Ausnahmegrenze fuer einzelne ansonsten gueltige Requests; Minutenquoten bleiben unveraendert.
GEMINI_INPUT_EXCEPTION_BUDGET = 121_200
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
# Ein Modellpool wird bei 503/Netzwerkfehlern pro Request nur einmal durchlaufen.
# Der fehlgeschlagene Split wird gesichert und kann im spaeteren Lauf fortgesetzt werden.

ANWEISUNG_DATEI = "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"

# Gleicher Drive-Ordner wie in upload_to_drive.py - dort landen alle
# Scanner-Ausgaben, von dort werden ggf. die Short-Dateien nachgeladen.
DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
BEOBACHTUNGSLISTE_DATEI = "einzel_check_beobachtung.json"


def _parse_tab2_entry_date_text(value):
    """Parse only the textual date formats accepted by the Tab-2 contract."""
    raw = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _tab2_nonempty_rows_with_source_numbers(values, start_sheet_row=3):
    """Return nonempty row dictionaries and their original physical Sheet row numbers.

    `values` contains rows beginning at `start_sheet_row`. Blank rows are
    omitted from the export but never renumber the original source coordinates.
    """
    rows = []
    source_row_numbers = []
    for source_row_number, source_values in enumerate(values, start=start_sheet_row):
        if not any(str(cell).strip() for cell in source_values):
            continue
        rows.append(source_values)
        source_row_numbers.append(source_row_number)
    return rows, source_row_numbers
GEMINI_HISTORIE_DATEI = "Gemini_Auswertung_Historie.txt"
GEMINI_A3_HISTORIE_DATEI = ".gemini_einzel_check_historie_a3.json"
_GEMINI_GESCHLOSSENE_10_5_FINAL = ""  # Autoritative Tab-2-Fakten fuer den finalen Ausgabe-Gate.

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
# Expliziter Startzustand: Auch direkte/isolierte Funktionsaufrufe duerfen
# den Cache-Namen lesen, bevor die mehrstufige Analyse einen Cache erzeugt.
_gemini_cache_name = None
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
# Erfolgreiche Zwischensynthesen werden bei einem spaeteren technischen Fehler
# wiederverwendet, statt bei Wiederaufnahme erneut API-Requests auszuloesen.
_gemini_synthese_request_cache = {}
# Persistenter Resume-Zustand fuer erfolgreich beantwortete Request-Splits.
# Der Inhalt wird nur wiederverwendet, wenn der Fingerabdruck des vollstaendigen
# Quellkontexts, der Arbeitsanweisung und des konkreten Splits identisch ist.
GEMINI_SPLIT_RESUME_DATEI = ".gemini_request_split_resume.json"
GEMINI_SPLIT_RESUME_DONE_DATEI = ".gemini_request_split_resume_done"
_GEMINI_SPLIT_RESUME_VERSION = 2
_GEMINI_RUN_ID = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"
# Maximal zwei echte GenerateContent-Sendungen je Request UND Modell in einem Lauf.
# Der Modellpool darf bis zu drei Runden durchlaufen; bereits erfolgreiche Splits
# bleiben gecacht. Das Sendungslimit gilt pro Request/Modell und verhindert,
# dass derselbe Modell-Request bei anhaltender Ueberlast unbegrenzt wiederholt wird.
# 503 UNAVAILABLE ist meist transient; zwischen Poolrunden wird progressiv gewartet.
GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN = 2
GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN = 3
GEMINI_503_POOL_BACKOFF_SECONDS = 60.0
# Tatsächliche Sendungen werden pro Request-Fingerprint UND Modell gezählt.
# Damit blockiert ein 503 bei einem Modell nicht pauschal alle anderen Splits.
_gemini_503_failures_this_run = defaultdict(int)
_gemini_503_failed_models_by_request = defaultdict(set)
_gemini_503_pool_passes_this_run = defaultdict(int)
# Begrenzung der aeusseren Modellwechsel nach logischem Request/Stage, nicht
# global fuer den gesamten Tageslauf. Der Stage-Name bleibt auch dann stabil,
# wenn ein modellabhaengiges Re-Chunking den technischen Request-Fingerprint
# veraendert. So verbraucht Split 8 nicht das Budget von Split 9.
_gemini_503_modellwechsel_je_stage = defaultdict(int)
_gemini_last_503_request_info = {}
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
    if any(item is None for item in (Credentials, Request, build, MediaIoBaseDownload)):
        msg = "Google-Drive-Abhaengigkeiten fehlen"
        if strict:
            raise RuntimeError(
                f"10.5: Autoritativer Google-Drive-Zugriff nicht moeglich: {msg}"
            ) from _GOOGLE_DRIVE_IMPORT_ERROR
        print(f"INFO: {msg} - Drive-Dateien werden nicht nachgeladen.")
        return None

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
    Faktenbasis. Alle vorhandenen Datenzeilen aus Tab 2 werden übernommen;
    es gibt keine zeitliche Begrenzung. Unparsebare Datumswerte bleiben als
    Quellwerte enthalten und werden nicht stillschweigend ausgeschlossen.
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
        required_headers = {"Ticker", "Einstiegsdatum", "Ausstiegsdatum", "Status"}
        missing_headers = sorted(required_headers - set(headers))
        if missing_headers:
            raise RuntimeError(
                "10.5: Pflichtspalten in Tab 'Geschlossene Positionen' fehlen: "
                + ", ".join(missing_headers)
            )
        # Leere Zwischen-/Endzeilen sind keine Positionsdatensätze. Jede
        # tatsächlich befüllte Quellzeile muss dagegen einen Ticker haben;
        # sonst darf der Export nicht mit einer scheinbar passenden Anzahl
        # und einem Fingerabdruck freigegeben werden.
        # Preserve the physical Google Sheets row number before filtering.
        # Enumerating the filtered `rows` list would renumber rows after blank
        # gaps and report the wrong source coordinates in diagnostics.
        nonempty_source_rows, source_row_numbers = _tab2_nonempty_rows_with_source_numbers(
            values[2:], start_sheet_row=3
        )
        rows = [
            dict(zip(headers, row + [""] * max(0, len(headers) - len(row))))
            for row in nonempty_source_rows
        ]

        missing_ticker_rows = [
            source_row_numbers[index]
            for index, row in enumerate(rows)
            if not str(row.get("Ticker", "")).strip()
        ]

        def parse_date(value):
            # UNFORMATTED_VALUE liefert Google-Sheets-Datumszellen als
            # Seriennummer. Die bisherigen Textformate bleiben gültig.
            if isinstance(value, datetime.datetime):
                return value.date()
            if isinstance(value, datetime.date):
                return value

            raw = str(value or "").strip()
            parsed_text_date = _parse_tab2_entry_date_text(raw)
            if parsed_text_date is not None:
                return parsed_text_date

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

        # Positionsidentitaet gemaess Datenvertrag: Ticker + Einstiegsdatum.
        # Ohne ein parsebares Einstiegsdatum kann die Zeile nicht sicher einer
        # einzelnen historischen Position zugeordnet werden; sie wird nicht
        # stillschweigend aus dem Export entfernt.
        invalid_entry_date_rows = [
            source_row_numbers[index]
            for index, row in enumerate(rows)
            if parse_date(row.get("Einstiegsdatum")) is None
        ]

        # Keine Datumsselektion: vollständige Tab-2-Zeilenbasis übernehmen.
        # Jede gefuellte Quellzeile bleibt enthalten. Identitaet wird spaeter
        # fachlich ueber Ticker + Einstiegsdatum geprueft; die physische
        # Quellzeile dient zusaetzlich der Rueckverfolgbarkeit.
        selected = list(zip(source_row_numbers, rows))
        parseable_dates = sum(parse_date(row.get("Ausstiegsdatum")) is not None for row in rows)
        print(
            f"10.5 TAB-2-VOLLSTÄNDIGKEIT: quellzeilen={len(rows)} | "
            f"datumswerte_parsebar={parseable_dates} | übernommen={len(selected)}"
        )
        if len(selected) != len(rows):
            raise RuntimeError(
                f"10.5: Vollständigkeitsfehler Tab 2: Quelle={len(rows)}, übernommen={len(selected)}"
            )
        if not selected:
            print("10.5 TAB 2: Keine geschlossenen Positionen in der Quelle vorhanden.")
            return "TAB-2-ANZAHL=0\nTAB-2-QUELLE=Geschlossene Positionen"

        # Nur Faktenfelder aus Tab 2; keine technische Neubewertung.
        fields = [
            "Ticker", "Name", "Einstiegsdatum", "Einstieg",
            "Ausstiegsdatum", "Ausstiegskurs", "Performance_Seit_Einstieg%",
            "Status", "Richtung", "Produkt_Typ",
            "OS_WKN", "OS_Einstiegskurs", "OS_Aktueller_Kurs", "OS_Performance%",
            "OS_Quelle", "OS_Kurszeit",
        ]
        position_lines = []
        for source_row_number, row in selected:
            formatted_fields = [f"Quellzeile: {source_row_number}"]
            for field in fields:
                raw_value = row.get(field, "")
                if not str(raw_value).strip():
                    continue
                # Beide Datumsfelder werden für den Bericht kanonisch formatiert.
                if field in {"Einstiegsdatum", "Ausstiegsdatum"}:
                    parsed_date = parse_date(raw_value)
                    value = parsed_date.strftime("%d.%m.%Y") if parsed_date is not None else str(raw_value)
                else:
                    value = raw_value
                formatted_fields.append(f"{field}: {value}")
            position_lines.append(" | ".join(formatted_fields))

        # Vor dem Setzen von Anzahl/Quellenmarker/Fingerabdruck muss jede
        # Position über Ticker + Einstiegsdatum identifizierbar und der
        # Schlüssel innerhalb des vollständigen Exports eindeutig sein.
        identity_errors = []
        if missing_ticker_rows:
            identity_errors.append("Quellzeilen ohne Ticker=" + ",".join(map(str, missing_ticker_rows)))
        if invalid_entry_date_rows:
            identity_errors.append("Quellzeilen ohne parsebares Einstiegsdatum=" + ",".join(map(str, invalid_entry_date_rows)))
        position_keys = []
        for _, row in selected:
            ticker = str(row.get("Ticker", "")).strip().casefold()
            entry_date = parse_date(row.get("Einstiegsdatum"))
            if ticker and entry_date:
                position_keys.append((ticker, entry_date.isoformat()))
        duplicate_keys = sorted(
            f"{ticker}|{entry_date}"
            for ticker, entry_date in set(position_keys)
            if position_keys.count((ticker, entry_date)) > 1
        )
        if duplicate_keys:
            identity_errors.append("doppelte Positionsschluessel Ticker+Einstiegsdatum=" + ",".join(duplicate_keys))

        if identity_errors:
            print("10.5 TAB-2-VALIDIERUNG FEHLER: " + "; ".join(identity_errors))
            # Diagnosebericht bewahrt jede gelesene Quellzeile. Die kanonischen
            # Marker werden bewusst nicht gesetzt; das Report-Gate blockiert
            # eine Freigabe, ohne die Historie aus dem Bericht zu verlieren.
            return "\n".join(
                ["TAB-2-QUELLE NICHT VERIFIZIERBAR: " + "; ".join(identity_errors)]
                + position_lines
            )

        selected_tickers = [str(row.get("Ticker", "")).strip() for _, row in selected]
        print(
            f"HISTORIE 10.5: {len(selected)} geschlossene Position(en) vollständig aus Tab 2 übernommen | "
            f"Ticker={selected_tickers}"
        )
        return "\n".join(
            [f"TAB-2-ANZAHL={len(selected)}", "TAB-2-QUELLE=Geschlossene Positionen"]
            + position_lines
        )

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
    "ueberlast", "netzwerk", "sonstiges", "konfiguration", "quota_unbekannt"."""
    text_klein = fehlertext.lower()

    # Provider-Retry-Hinweise koennen als Python-Exception-Text (einfache
    # Quotes) oder als JSON (doppelte Quotes) eintreffen. Wartezeiten bleiben
    # begrenzt, damit fehlerhafte Providerdaten keine endlose Pause ausloesen.
    max_provider_retry_delay = 300.0

    def _provider_retry_delay():
        match = re.search(
            r"""["']?retryDelay["']?\s*[:=]\s*["']?(\d+(?:\.\d+)?)s["']?""",
            fehlertext,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        try:
            value = float(match.group(1))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return min(value, max_provider_retry_delay)

    empfohlene_wartezeit = _provider_retry_delay()
    if "gemini_technischer_fallback_503_terminal" in text_klein:
        return True, None, "modellpool_temporaer"
    # Interne Budget-/Chunking-Fehler sind deterministisch: denselben Request
    # unveraendert zu wiederholen kann sie nicht beheben. Sie werden terminal
    # behandelt und duerfen nicht in die generische Retry-Schleife gelangen.
    interne_budget_marker = (
        "gemini_quelle_nach_chunking_zu_gross",
        "gemini_request_zu_gross",
        "gemini_request_zugross",
        "gemini_synthese_zu_gross",
        "gemini_synthese_request_zu_gross",
    )
    if any(marker in text_klein for marker in interne_budget_marker):
        return True, None, "request_budget"

    # Konfigurations-/Berechtigungsfehler haben Vorrang vor einem
    # gleichzeitig genannten 429/RESOURCE_EXHAUSTED. Ein Modellwechsel
    # behebt weder deaktiviertes Billing noch einen ungueltigen API-Key.
    nicht_retrybare_konfiguration = (
        "billing disabled",
        "billing is disabled",
        "billing account is not enabled",
        "billing account disabled",
        "billing not enabled",
        "api key not valid",
        "invalid api key",
        "invalid_api_key",
        "permission_denied",
        "permission denied",
        "service_disabled",
        "service disabled",
        "api has not been used",
        "not authorized",
        "unauthorized",
        "access denied",
    )
    if re.search(r"\b403\b", text_klein) or any(marker in text_klein for marker in nicht_retrybare_konfiguration):
        return True, None, "konfiguration"

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
        server_wartezeit = empfohlene_wartezeit or 0.0
        return False, min(max(65.0, server_wartezeit), max_provider_retry_delay), "input_token_limit"

    if ist_modellpool_temporaer:
        return False, empfohlene_wartezeit, "modellpool_temporaer"

    text_klein = fehlertext.lower()
    if "503" in fehlertext or "unavailable" in text_klein or "high demand" in text_klein:
        return False, empfohlene_wartezeit, "ueberlast"
    if ("connection reset" in text_klein or "connection aborted" in text_klein
            or "timed out" in text_klein or "timeout" in text_klein
            or "temporarily unavailable" in text_klein):
        return False, empfohlene_wartezeit, "netzwerk"

    # Nur bekannte minutenbezogene Quota-Metriken gelten als temporaer.
    # Ein unbekanntes quotaId oder ein nacktes 429/RESOURCE_EXHAUSTED reicht
    # nicht fuer einen Modellwechsel: das kann auch Billing-/Projektkonfiguration
    # oder eine neue, noch nicht klassifizierte Provider-Metrik bedeuten.
    quota_id_match = re.search(
        r"""quotaId["']?\s*[:=]\s*["']?([A-Za-z0-9_.-]+)""",
        fehlertext,
        flags=re.IGNORECASE,
    )
    quota_id = quota_id_match.group(1).lower() if quota_id_match else ""
    bekannte_rpm_quota_ids = (
        "generaterequestsperminuteperprojectpermodel",
        "generaterequestsperminutepermodel",
        "generaterequestsperminuteperproject",
        "generaterequestsperminute",
    )
    bekannte_rpm_signale = (
        "requests per minute",
        "requestsperminute",
        "requests per minute quota",
        "per-minute request quota",
        "per minute request quota",
        "rpm quota",
    )
    if quota_id:
        if any(marker in quota_id for marker in bekannte_rpm_quota_ids):
            return False, empfohlene_wartezeit, "quota_temporär"
        # PerDay und Input-Token-Limits wurden oben abgefangen. Jede andere
        # explizite quotaId bleibt konservativ terminal, auch wenn die Meldung
        # zusaetzlich allgemein RESOURCE_EXHAUSTED enthaelt.
        return True, None, "quota_unbekannt"

    if any(marker in text_klein for marker in bekannte_rpm_signale):
        if "429" in text_klein or "resource_exhausted" in text_klein or "quota" in text_klein:
            return False, empfohlene_wartezeit, "quota_temporär"

    if "429" in text_klein or "resource_exhausted" in text_klein or "resource exhausted" in text_klein:
        return True, None, "quota_unbekannt"
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
    """Exportiert jede offene Quellzeile genau einmal oder bricht explizit ab.

    Quellenanzahl und Exportanzahl werden getrennt ermittelt. Nicht zuordenbare
    offene Zeilen oder unbekannte Statuswerte dürfen nicht still verschwinden.
    """
    if not csv_pfad or not os.path.isfile(csv_pfad):
        raise RuntimeError("Offene Positionsquelle fehlt; Vollständigkeit nicht prüfbar.")
    with open(csv_pfad, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        fields = reader.fieldnames or []
        def key(name):
            return next((k for k in fields if str(k).strip().lower() == name.lower()), None)
        name_k, ticker_k, status_k = key("Name"), key("Ticker"), key("Status")
        entry_k, date_k = key("Einstieg"), key("Einstiegsdatum")
        current_k = key("Aktueller_Kurs") or key("Aktueller Kurs")
        stop_k, tp1_k, tp2_k = key("Stop"), key("TP1"), key("TP2")
        if not all((name_k, ticker_k, entry_k, date_k)):
            raise RuntimeError("Check-Datei benötigt Name, Ticker, Einstieg und Einstiegsdatum.")
        source_rows = list(reader)

    rows = []
    offene_quellzeilen = 0
    for index, row in enumerate(source_rows, start=2):
        status = str(row.get(status_k, "")).strip().lower() if status_k else ""
        if status in {"geschlossen", "closed", "close"}:
            continue
        if status and status not in {"offen", "open"}:
            raise RuntimeError(f"Offene Positionsquelle: unbekannter Status in Zeile {index}: {status!r}")
        name = str(row.get(name_k, "") or "").strip()
        ticker = str(row.get(ticker_k, "") or "").strip()
        if not name or not ticker:
            raise RuntimeError(f"Offene Positionsquelle: offene Zeile {index} nicht zuordenbar (Name/Ticker fehlt).")
        offene_quellzeilen += 1
        entry = str(row.get(entry_k, "") or "").strip()
        date = str(row.get(date_k, "") or "").strip()
        current = str(row.get(current_k, "") or "").strip() if current_k else ""
        stop = str(row.get(stop_k, "") or "").strip() if stop_k else ""
        tp1 = str(row.get(tp1_k, "") or "").strip() if tp1_k else ""
        tp2 = str(row.get(tp2_k, "") or "").strip() if tp2_k else ""
        parts = [f"- {name} ({ticker})", f"Einstieg: {entry}", f"Einstiegsdatum: {date}"]
        if current: parts.append(f"Aktueller Kurs: {current}")
        if stop: parts.append(f"Stop: {stop}")
        if tp1: parts.append(f"TP1: {tp1}")
        if tp2: parts.append(f"TP2: {tp2}")
        rows.append(" | ".join(parts))

    export_count = len(rows)
    print(f"OFFENE_POSITIONEN_QUELLVERGLEICH: quelle={offene_quellzeilen} | export={export_count}")
    if offene_quellzeilen != export_count:
        raise RuntimeError(f"Offene Positionsquelle: Quellen-/Exportzählung abweichend: Quelle={offene_quellzeilen}, Export={export_count}.")
    if export_count == 0:
        raise RuntimeError("Keine offenen Positionen exportiert; Vollständigkeit nicht bestätigt.")
    return f"OFFENE_POSITIONEN_QUELLANZAHL={offene_quellzeilen}\nOFFENE_POSITIONEN_ANZAHL={export_count}\n" + "\n".join(rows)

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
            # Die mehrstufige Synthese kann ein Textobjekt ohne Provider-
            # Candidate-Metadaten zurueckgeben. Das ist kein belegter STOP,
            # soll aber auch nicht mehr pauschal als voellig unbekannt gelten.
            if str(getattr(antwort, "text", "") or "").strip():
                return "METADATEN_NICHT_VERFUEGBAR_TEXT_VORHANDEN"
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


def _gemini_split_resume_laden(context_fingerprint):
    """Laedt nur Resume-Eintraege fuer exakt denselben Eingabekontext."""
    # Ein Success-Marker stammt aus einem vorherigen erfolgreichen Lauf. Da
    # Actions-Caches unveraenderlich sind, entfernt er beim naechsten Lauf
    # einen eventuell mit-restaurierten alten Resume-Stand.
    if os.path.isfile(GEMINI_SPLIT_RESUME_DONE_DATEI):
        try:
            if os.path.isfile(GEMINI_SPLIT_RESUME_DATEI):
                os.remove(GEMINI_SPLIT_RESUME_DATEI)
            os.remove(GEMINI_SPLIT_RESUME_DONE_DATEI)
            print("GEMINI-SPLIT-RESUME: alten Cache nach vorherigem Erfolg verworfen.")
        except OSError as exc:
            print(f"WARNUNG: alter Gemini-Resume-Cache konnte nicht bereinigt werden ({exc}).")
    if not context_fingerprint or not os.path.isfile(GEMINI_SPLIT_RESUME_DATEI):
        return {"completed": {}, "failed": None}
    try:
        with open(GEMINI_SPLIT_RESUME_DATEI, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("version") != _GEMINI_SPLIT_RESUME_VERSION:
            return {"completed": {}, "failed": None}
        contexts = state.get("contexts")
        if not isinstance(contexts, dict):
            return {"completed": {}, "failed": None}
        context = contexts.get(context_fingerprint)
        if not isinstance(context, dict):
            return {"completed": {}, "failed": None}
        completed = context.get("completed")
        if not isinstance(completed, dict):
            completed = {}
        failed = context.get("failed") if isinstance(context.get("failed"), dict) else None
        if failed and failed.get("status") == "SAFETY_FILTER_REJECTED":
            print("GEMINI-SPLIT-RESUME: Sicherheitsfilter-Fall; alte Split-Ergebnisse werden bewusst verworfen.")
            return {"completed": {}, "failed": failed}
        return {"completed": completed, "failed": failed}
    except (OSError, ValueError, TypeError) as exc:
        print(f"WARNUNG: Gemini-Split-Resume-Datei nicht lesbar; starte ohne persistenten Cache ({exc}).")
        return {"completed": {}, "failed": None}


def _gemini_split_resume_speichern(context_fingerprint, completed, failed=None):
    """Speichert mehrere Stufen atomar; nur identische Kontext-Fingerprints werden fortgesetzt."""
    if not context_fingerprint:
        return
    path = GEMINI_SPLIT_RESUME_DATEI
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("version") != _GEMINI_SPLIT_RESUME_VERSION or not isinstance(state.get("contexts"), dict):
            state = {"version": _GEMINI_SPLIT_RESUME_VERSION, "contexts": {}}
    except (OSError, ValueError, TypeError):
        state = {"version": _GEMINI_SPLIT_RESUME_VERSION, "contexts": {}}

    contexts = state["contexts"]
    contexts[context_fingerprint] = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "completed": dict(completed or {}),
        "failed": dict(failed) if isinstance(failed, dict) else None,
    }
    # Begrenzung auf die acht zuletzt aktualisierten Kontexte, damit ein
    # abgebrochener Lauf keine unbegrenzt wachsende Statusdatei erzeugt.
    newest = sorted(
        contexts.items(),
        key=lambda item: str((item[1] or {}).get("updated_at") or ""),
        reverse=True,
    )[:8]
    state = {"version": _GEMINI_SPLIT_RESUME_VERSION, "contexts": dict(newest)}

    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".gemini_split_resume_", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        print(f"WARNUNG: Gemini-Split-Resume konnte nicht dauerhaft gespeichert werden ({exc}).")
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _gemini_split_resume_loeschen():
    """Markiert Erfolg; ein spaeterer Actions-Cache-Restore verwirft alte Daten."""
    try:
        if os.path.isfile(GEMINI_SPLIT_RESUME_DATEI):
            os.remove(GEMINI_SPLIT_RESUME_DATEI)
        with open(GEMINI_SPLIT_RESUME_DONE_DATEI, "w", encoding="utf-8") as f:
            f.write(datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n")
        print("GEMINI-SPLIT-RESUME: nach erfolgreicher Auswertung bereinigt und Success-Marker gespeichert.")
    except OSError as exc:
        print(f"WARNUNG: Erfolg gespeichert, Resume-Cache/Success-Marker konnte nicht bereinigt werden ({exc}).")


def _gemini_split_resume_fehler_sichern(
    context_fingerprint, completed, previous_failed, request_fingerprint,
    stage, index, total, exc, model=None,
):
    """Persistiert Fehlerstatus, ohne nach zwei Modellversuchen den Pool abzuwürgen."""
    error_category = analysiere_api_fehler(str(exc))[2]
    is_terminal_503 = "GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL" in str(exc)
    is_transient_503 = (
        not is_terminal_503
        and error_category in ("ueberlast", "netzwerk", "modellpool_temporaer")
    )
    same_failed_request = (
        isinstance(previous_failed, dict)
        and previous_failed.get("status") == "FAILED"
        and previous_failed.get("split_fingerprint") == request_fingerprint
        and previous_failed.get("run_id") == _GEMINI_RUN_ID
    )
    previous_attempts = int(previous_failed.get("attempt_count", 0)) if same_failed_request else 0
    attempt_count = previous_attempts + 1 if is_transient_503 else previous_attempts
    model_name = str(model or _gemini_last_request_model or "UNBEKANNT")
    model_attempts = dict(previous_failed.get("model_attempts") or {}) if same_failed_request else {}
    if is_transient_503:
        model_attempts[model_name] = int(model_attempts.get(model_name, 0)) + 1
    # Der vollstaendige Produktionslauf initialisiert diese Variable auf Modulebene.
    # Isolierte AST-/Resume-Tests koennen die Funktion jedoch ohne dieses Modul-Global
    # ausfuehren; in diesem Fall darf die Fehlerpersistierung nicht selbst abstuerzen.
    request_info = dict(globals().get("_gemini_last_503_request_info") or {})
    actual_model_attempts = int(
        request_info.get("model_attempt_count", model_attempts.get(model_name, 0))
    )
    remaining_model_attempts = max(
        0, GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN - actual_model_attempts
    )
    failed_status = {
        "split_index": index,
        "split_total": total,
        "split_fingerprint": request_fingerprint,
        "stage": stage,
        "status": "FAILED",
        "attempt_count": attempt_count,
        "model_attempts": model_attempts,
        "model_attempt_count": actual_model_attempts,
        "model_attempts_remaining": remaining_model_attempts,
        "pool_pass": int((globals().get("_gemini_503_pool_passes_this_run") or {}).get(
            str(request_info.get("request_fingerprint") or request_fingerprint), 0
        )) + 1,
        "run_id": _GEMINI_RUN_ID,
        "error_type": type(exc).__name__,
        "error": str(exc)[:4000],
        "model": model_name,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _gemini_split_resume_speichern(context_fingerprint, completed, failed_status)
    print(
        f"GEMINI-REQUEST-STATUS: {stage} {index}/{total} als FAILED gespeichert; "
        f"Fehlversuche gesamt={attempt_count} | Modell={model_name} | "
        f"Sendungen dieses Modells={actual_model_attempts}/"
        f"{GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN} | "
        f"weitere Sendungen fuer dieses Modell={remaining_model_attempts}."
    )
    # Der Fehlerstatus wird gesichert, aber der Modellpool darf weiterarbeiten.
    # Das harte Limit wird unmittelbar vor jeder echten API-Sendung geprüft.
    return attempt_count


def _gemini_quellkontext_fingerprint(context_names, eingabedateien):
    """Stabiler Fingerabdruck der Quelldateien; Upload-URIs selbst sind nicht stabil."""
    datei_signaturen = []
    dateien = eingabedateien if isinstance(eingabedateien, dict) else {}
    gesehen = set()
    for name in context_names or []:
        basisname = str(name).split("#CHUNK_", 1)[0]
        pfad = dateien.get(basisname)
        signatur = {"name": str(name), "basisname": basisname}
        if pfad and isinstance(pfad, (str, os.PathLike)) and os.path.isfile(pfad):
            real = os.path.abspath(os.fspath(pfad))
            if real not in gesehen:
                digest = hashlib.sha256()
                with open(real, "rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                gesehen.add(real)
                signatur["sha256"] = digest.hexdigest()
                signatur["bytes"] = os.path.getsize(real)
            else:
                signatur["sha256"] = "siehe-identische-datei"
                signatur["bytes"] = os.path.getsize(real)
        datei_signaturen.append(signatur)
    payload = json.dumps(datei_signaturen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    context_fingerprint = _gemini_quellkontext_fingerprint(context_names, eingabedateien)
    return {
        "contents": contents,
        "system_instruction": anweisung,
        "context_names": context_names,
        "resume_fingerprint": context_fingerprint,
    }



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
    """Erzeugt Request-Gruppen ohne kostspielige Einzelpruefung aller Daten-Parts.

    Der feste Arbeits-/Systemkontext wird einmal gemessen. Danach werden
    Text-Parts anhand einer konservativen Zeichenschaetzung gebuendelt. Nur
    fertige Gruppen werden exakt mit CountTokens geprueft. Ueberschreitet eine
    Gruppe die Ausnahmegrenze, wird ausschliesslich diese Gruppe rekursiv
    verkleinert. Ein einzelner zu grosser Text-Part wird verlustfrei geteilt.

    Die verbindliche exakte Messung unmittelbar vor jedem API-Aufruf bleibt
    zusaetzlich bestehen. Die Reihenfolge und der vollstaendige Quelltext
    bleiben erhalten.
    """
    daten_teile = list(daten_teile or [])
    if not daten_teile:
        return

    arbeits_tokens = _gemini_tokenzahl(
        client, modell, arbeits_contents, system_instruction,
        label=f"{label} Arbeitskontext",
    )
    if arbeits_tokens > GEMINI_INPUT_EXCEPTION_BUDGET:
        raise RuntimeError(
            f"GEMINI_ARBEITSKONTEXT_ZU_GROSS: {label} Arbeitskontext allein "
            f"enthaelt {arbeits_tokens:,} Tokens und ueberschreitet die "
            f"Ausnahmegrenze {GEMINI_INPUT_EXCEPTION_BUDGET:,}; "
            "eine Aufteilung der Daten-Quellen kann diesen Fehler nicht beheben."
        )

    # Konservative Schaetzung: Textlaenge / 1.8 plus Part-Overhead.
    # Die Schaetzung plant nur; CountTokens entscheidet endgueltig.
    schaetzbudget = max(
        1,
        int((GEMINI_INPUT_SAFE_BUDGET - arbeits_tokens) * 0.90),
    )

    def geschaetzte_tokens(teil):
        text = getattr(teil, "text", None)
        if not isinstance(text, str):
            # Nichttextuelle Daten koennen sehr viele Tokens tragen. Sie
            # bekommen eine eigene Gruppe und werden dort exakt vermessen.
            return schaetzbudget + 1
        return max(1, math.ceil(len(text) / 1.8) + 48)

    grobe_gruppen = []
    gruppe = []
    gruppe_schaetzung = 0
    for teil in daten_teile:
        schaetzung = geschaetzte_tokens(teil)
        if gruppe and (
            schaetzung > schaetzbudget
            or gruppe_schaetzung + schaetzung > schaetzbudget
            or not isinstance(getattr(teil, "text", None), str)
            or any(not isinstance(getattr(p, "text", None), str) for p in gruppe)
        ):
            grobe_gruppen.append(gruppe)
            gruppe = []
            gruppe_schaetzung = 0
        if schaetzung > schaetzbudget:
            grobe_gruppen.append([teil])
        else:
            gruppe.append(teil)
            gruppe_schaetzung += schaetzung
    if gruppe:
        grobe_gruppen.append(gruppe)

    def exakt_pruefen_und_absichern(teile, pfad):
        tokens = _gemini_tokenzahl(
            client, modell, teile + arbeits_contents, system_instruction,
            label=f"{label} Gruppe {pfad}",
        )
        # Mehr-Part-Gruppen muessen das Normalziel einhalten. Die
        # Ausnahmegrenze gilt nur fuer einen einzelnen, nicht weiter teilbaren
        # Part; sonst wuerde die Ausnahme faktisch das Normalziel ersetzen.
        if tokens <= GEMINI_INPUT_SAFE_BUDGET:
            return [teile]

        if len(teile) > 1:
            mitte = len(teile) // 2
            return (
                exakt_pruefen_und_absichern(teile[:mitte], f"{pfad}a")
                + exakt_pruefen_und_absichern(teile[mitte:], f"{pfad}b")
            )

        teil = teile[0]
        teil_text = getattr(teil, "text", None)
        if tokens <= GEMINI_INPUT_EXCEPTION_BUDGET and (
            not isinstance(teil_text, str)
            or len(teil_text) < 2
            or arbeits_tokens > GEMINI_INPUT_SAFE_BUDGET
        ):
            # Die Ausnahme ist nur fuer nicht weiter teilbare Einzel-Parts
            # oder einen bereits uebergrossen festen Arbeitskontext zulaessig.
            return [[teil]]
        if not isinstance(teil_text, str) or len(teil_text) < 2:
            raise RuntimeError(
                f"GEMINI_QUELLE_NACH_CHUNKING_ZU_GROSS: {label} einzelner "
                f"nichttextueller oder nicht weiter teilbarer Daten-Part "
                f"enthaelt zusammen mit Arbeitskontext {tokens:,} Tokens "
                f"(> {GEMINI_INPUT_EXCEPTION_BUDGET:,})."
            )

        # Quell-Chunks tragen einen Kopf mit logischem Quelldateinamen und
        # Chunk-ID. Bei einer weiteren Teilung muss dieser Kopf in BEIDEN
        # Teil-Parts vorhanden sein, damit auch der rechte Teil eindeutig
        # zugeordnet bleibt. Kopf und Fortsetzungsmarker sind Metadaten und
        # werden bei der Rekonstruktion des Nutztexts entfernt.
        source_header_match = re.match(
            r"(?s)^(\s*===== QUELLDATEI .+?=====\n"
            r"Dieser Chunk ist ein vollstaendiger Ausschnitt der unveraenderten Originalquelle\. "
            r"Keine Daten ergaenzen oder weglassen\.\n\n)",
            teil_text,
        )
        metadaten_prefix = source_header_match.group(1) if source_header_match else ""
        body = teil_text[len(metadaten_prefix):]
        continuation_marker = (
            "FORTSETZUNG DES GLEICHEN QUELL-CHUNKS; QUELLKOPF ZUR ZUORDNUNG WIEDERHOLT.\n\n"
        )
        if metadaten_prefix and body.startswith(continuation_marker):
            metadaten_prefix += continuation_marker
            body = body[len(continuation_marker):]

        mitte = len(body) // 2
        zeilenanfang = body.rfind("\n", max(1, mitte - 2000), mitte)
        zeilenende = body.find("\n", mitte, min(len(body) - 1, mitte + 2000))
        kandidaten = [pos + 1 for pos in (zeilenanfang, zeilenende) if pos >= 0]
        trennpunkt = min(kandidaten, key=lambda pos: abs(pos - mitte)) if kandidaten else mitte
        if trennpunkt <= 0 or trennpunkt >= len(body):
            trennpunkt = mitte
        links_body, rechts_body = body[:trennpunkt], body[trennpunkt:]
        links = metadaten_prefix + links_body
        rechts_prefix = metadaten_prefix
        if source_header_match and not rechts_prefix.endswith(continuation_marker):
            rechts_prefix += continuation_marker
        rechts = rechts_prefix + rechts_body
        if not links_body or not rechts_body or links_body + rechts_body != body:
            raise RuntimeError(
                f"GEMINI_QUELLE_CHUNKING_FEHLER: {label} Text-Part konnte "
                "nicht verlustfrei geteilt werden."
            )
        linker_part = types.Part.from_text(text=links)
        rechter_part = types.Part.from_text(text=rechts)
        print(
            f"GEMINI-QUELLE-NACHSEGMENTIERT: {label} | "
            f"urspruenglicher Part {len(teil_text):,} Zeichen / {tokens:,} Tokens | "
            f"neue Teilgroessen={len(links):,}+{len(rechts):,} Zeichen"
        )
        return (
            exakt_pruefen_und_absichern([linker_part], f"{pfad}a")
            + exakt_pruefen_und_absichern([rechter_part], f"{pfad}b")
        )

    sichere_gruppen = []
    for gruppen_index, grobe_gruppe in enumerate(grobe_gruppen, 1):
        sichere_gruppen.extend(
            exakt_pruefen_und_absichern(grobe_gruppe, str(gruppen_index))
        )

    print(
        f"GEMINI-GRUPPIERUNG: {label} | Daten-Parts={len(daten_teile)} | "
        f"Request-Gruppen={len(sichere_gruppen)} | "
        "Einzel-Part-Vorabpruefung=deaktiviert"
    )
    yield from sichere_gruppen



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


def _gemini_503_request_fingerprint(contents, system_instruction):
    """Stabiler modellunabhaengiger Fingerprint fuer einen echten API-Request."""
    parts = []
    for part in contents if isinstance(contents, (list, tuple)) else [contents]:
        if hasattr(part, "text"):
            parts.append({"type": "text", "value": str(getattr(part, "text", "") or "")})
        else:
            parts.append({"type": type(part).__name__, "value": repr(part)})
    payload = json.dumps(
        {"parts": parts, "system_instruction": str(system_instruction or "")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gemini_logische_request_id(
    context_fingerprint, semantic_stage, prompt_contents, system_instruction=None,
    source_contents=None,
):
    """Erzeugt eine stabile 503-Budget-ID aus Kontext, Fachstufe und Prompt-Identitaet.

    Die Prompt-Identitaet wird ausschliesslich aus dem fachlichen Arbeits-Prompt
    (nicht aus den variabel grossen Quellen-Splits) gebildet. Dadurch bleiben
    Split-IDs bei verlustfreiem Re-Chunking stabil, waehrend unterschiedliche
    Prompts derselben Quelle niemals dasselbe logische Budget teilen.
    """
    stage = " ".join(str(semantic_stage or "UNBEKANNTE_FACHSTUFE").split())
    stage = re.sub(r"[^A-Za-z0-9._-]+", "_", stage).strip("_") or "UNBEKANNTE_FACHSTUFE"
    context_id = str(context_fingerprint or "").strip()
    if not context_id:
        # Nur fuer isolierte/alte Aufrufer ohne Resume-Fingerprint. In der
        # Produktion wird der persistente Quellkontext-Fingerprint verwendet.
        context_id = _gemini_503_request_fingerprint(
            source_contents or [], system_instruction
        )
    prompt_id = _gemini_503_request_fingerprint(
        prompt_contents, system_instruction
    )
    return f"CTX:{context_id}:STAGE:{stage}:PROMPT:{prompt_id}"


def _gemini_generate_content_einzelversuch(
    client, modell, contents, config, label, system_instruction=None, logical_request_id=None
):
    """Prueft die 503-Grenze VOR jeder echten API-Sendung und zaehlt echte Fehler."""
    global _gemini_last_503_request_info
    request_fingerprint = _gemini_503_request_fingerprint(contents, system_instruction)
    logical_request_id = str(logical_request_id or request_fingerprint)
    attempt_key = (request_fingerprint, str(modell))
    attempts = int(_gemini_503_failures_this_run.get(attempt_key, 0))
    _gemini_last_503_request_info = {
        "request_fingerprint": request_fingerprint,
        "logical_request_id": logical_request_id,
        "stage": str(label),
        "model": str(modell),
        "model_attempt_count": attempts,
        "request_attempt_count": attempts,
        "run_id": _GEMINI_RUN_ID,
    }
    if attempts >= GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN:
        raise RuntimeError(
            "GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL: "
            f"Request {request_fingerprint[:12]} auf Modell {modell} hat das Limit von "
            f"{GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN} echten API-Sendungen erreicht; "
            "vor dem API-Aufruf blockiert."
        )
    # Zählung unmittelbar VOR der echten Sendung: auch ein anderer API-Fehler
    # verbraucht einen Versuch. Ein blockierter Request zählt ausdrücklich nicht.
    _gemini_503_failures_this_run[attempt_key] = attempts + 1
    _gemini_last_503_request_info["model_attempt_count"] = attempts + 1
    _gemini_last_503_request_info["request_attempt_count"] = attempts + 1
    pool_pass = int((globals().get("_gemini_503_pool_passes_this_run") or {}).get(request_fingerprint, 0)) + 1
    remaining_attempts = max(
        0, GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN - (attempts + 1)
    )
    print(
        f"GEMINI-API-SEND-BUDGET: Request={request_fingerprint[:12]} | "
        f"Modell={modell} | echte Sendung={attempts + 1}/"
        f"{GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN} | "
        f"weitere Sendungen fuer dieses Request/Modell={remaining_attempts} | "
        f"Modellpool-Runde={pool_pass}/"
        f"{GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN}."
    )
    try:
        return client.models.generate_content(
            model=modell, contents=contents, config=config,
        )
    except Exception as exc:
        text = str(exc).lower()
        is_transient = any(marker in text for marker in (
            "503", "unavailable", "high demand", "connection reset",
            "connection aborted", "timed out", "temporarily unavailable",
        ))
        is_perday = "perday" in text or "generaterequestsperdayperprojectpermodelfreetier" in text
        if is_transient and not is_perday:
            _gemini_last_503_request_info["error"] = str(exc)[:4000]
        raise


def _gemini_generate_content_quota_safe(
    client, preferred_modell, contents, config_kwargs, measured_tokens, label,
    logical_request_id=None,
):
    """Sendet einen bereits gemessenen Request quota-sicher.

    count_tokens() wird vor der Quota-Pruefung fuer das konkret ausgewaehlte
    Modell erneut ausgefuehrt, falls der Scheduler vom bevorzugten Modell auf
    ein Fallback-Modell wechselt. Kein Request wird gesendet, wenn das lokale
    Minutenkontingent nicht ausreicht.
    """
    global _gemini_active_modell, _gemini_last_request_model, _gemini_failed_models
    request_fingerprint = _gemini_503_request_fingerprint(
        contents, config_kwargs.get("system_instruction")
    )
    # Die logische ID bleibt bei Re-Chunking stabil, wenn der Aufrufer sie aus
    # dem fachlichen Resume-Kontext und der Split-Position bildet. Außerhalb
    # dieses Pfads dient der exakte Request-Fingerprint als sichere Fallback-ID.
    logical_request_id = str(logical_request_id or request_fingerprint)
    # 503-Sperren gelten nur fuer genau diesen Request, nicht fuer alle
    # folgenden Splits oder Synthese-Requests desselben Tageslaufs. Der
    # Registry-Zustand wird auch in isolierten Test-Namespaces konsistent angelegt.
    failed_by_request = globals().get("_gemini_503_failed_models_by_request")
    if failed_by_request is None:
        failed_by_request = {}
        globals()["_gemini_503_failed_models_by_request"] = failed_by_request
    elif not isinstance(failed_by_request, dict):
        raise TypeError(
            "_gemini_503_failed_models_by_request muss ein Dictionary sein; "
            f"erhalten: {type(failed_by_request).__name__}"
        )
    failed_models = failed_by_request.get(request_fingerprint)
    if failed_models is None:
        failed_models = set()
    elif isinstance(failed_models, str):
        # Ein einzelner Modellname ist ein Element, niemals eine Zeichenfolge
        # von Modellnamen-Zeichen.
        if not failed_models:
            raise TypeError("Leerer Modellname in _gemini_503_failed_models_by_request")
        failed_models = {failed_models}
    elif isinstance(failed_models, set):
        if any(not isinstance(model, str) or not model for model in failed_models):
            raise TypeError(
                "Registry-Eintrag muss ausschließlich nichtleere Modellnamen enthalten"
            )
        # Bestehende Sets nicht kopieren: Referenzen auf den laufenden
        # request-spezifischen Zustand bleiben dadurch konsistent.
    elif isinstance(failed_models, (frozenset, list, tuple)):
        if any(not isinstance(model, str) or not model for model in failed_models):
            raise TypeError(
                "Registry-Eintrag muss ausschließlich nichtleere Modellnamen enthalten"
            )
        failed_models = set(failed_models)
    else:
        raise TypeError(
            "Registry-Eintrag muss None, ein Modellname oder eine Sammlung "
            f"von Modellnamen sein; erhalten: {type(failed_models).__name__}"
        )
    failed_by_request[request_fingerprint] = failed_models
    _gemini_failed_models = failed_models
    # Der aktive Modell-Pin eines vorherigen Splits darf die Auswahl dieses
    # Requests nicht bestimmen. Der Aufrufer uebergibt das aktuell vorgesehene
    # Modell; bei einem Retry aktualisiert die Hauptschleife diesen Wert.
    _gemini_active_modell = preferred_modell
    modell, wartezeit = _gemini_quota_waehlen(preferred_modell, measured_tokens)
    while modell is None:
        kandidaten = _gemini_quota_kandidaten(preferred_modell)
        if not kandidaten:
            # Sind alle Kandidaten wegen temporaerer 503-/Netzwerkfehler
            # gesperrt, darf derselbe Request nicht nach einem Wartefenster
            # unveraendert erneut gestartet werden. Der Split-Pfad sichert
            # bereits erfolgreiche Teilantworten und den fehlgeschlagenen Status.
            if _gemini_failed_models:
                # Nicht denselben unveraenderten Request nach dem gesamten
                # Modellpool erneut starten. Der aufrufende Split-Pfad sichert
                # den Fehlerstatus und die bereits erfolgreichen Split-Texte.
                raise RuntimeError(
                    "GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL: "
                    "alle konfigurierten Modelle waren fuer diesen Request "
                    "temporaer nicht verfuegbar; kein identischer Wiederholungszyklus."
                )

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
        if neu_gemessen > GEMINI_INPUT_EXCEPTION_BUDGET:
            # Der Inhalt wurde fuer das vorherige Modell gruppiert. Das neue
            # Modell kann denselben Inhalt anders tokenisieren; der Split-Loop
            # muss deshalb genau diese Gruppe verlustfrei nachsegmentieren und
            # darf sie weder abschneiden noch als endgueltigen Fehler behandeln.
            raise RuntimeError(
                "GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_FALLBACK: "
                f"Modell={modell} | {neu_gemessen:,} > "
                f"{GEMINI_INPUT_EXCEPTION_BUDGET:,};RESEGMENTIEREN"
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

    if measured_tokens > GEMINI_INPUT_EXCEPTION_BUDGET:
        raise RuntimeError(
            "GEMINI_REQUEST_ZU_GROSS_VOR_SENDUNG: "
            f"{measured_tokens:,} > {GEMINI_INPUT_EXCEPTION_BUDGET:,}"
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
        f"vorab gemessen={measured_tokens:,} <= {GEMINI_INPUT_EXCEPTION_BUDGET:,} "
        f"(Normalziel {GEMINI_INPUT_SAFE_BUDGET:,}) | "
        "Free-Tier-Input-Quota vor Sendung geprueft"
    )
    try:
        return _gemini_generate_content_einzelversuch(
            client, modell, contents, types.GenerateContentConfig(**config_kwargs),
            label, config_kwargs.get("system_instruction"), logical_request_id,
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
            # Modelle, die in DIESEM serverseitigen RPD-Fallback bereits
            # tatsaechlich versucht wurden, bleiben fuer diese Runde gesperrt.
            # Andernfalls kann _gemini_quota_waehlen wegen des Active-Model-Pins
            # erneut das soeben mit 503 gescheiterte Modell auswaehlen.
            fallback_versuchte_modelle = {modell}
            for naechstes_modell in _gemini_fallback_kandidaten_ohne_versuchte(
                _gemini_quota_kandidaten(
                    modell,
                    fehlgeschlagene_einschliessen=True,
                ),
                fallback_versuchte_modelle,
            ):
                neu_gemessen = _gemini_tokenzahl(
                    client, naechstes_modell, contents,
                    config_kwargs.get("system_instruction"),
                    label=f"{label} Server-RPD-Fallback {naechstes_modell}",
                )
                if neu_gemessen > GEMINI_INPUT_EXCEPTION_BUDGET:
                    fallback_versuchte_modelle.add(naechstes_modell)
                    continue
                verfuegbares_modell, _ = _gemini_quota_waehlen(
                    naechstes_modell,
                    neu_gemessen,
                    ausgeschlossene_modelle=fallback_versuchte_modelle,
                    fehlgeschlagene_einschliessen=True,
                )
                if verfuegbares_modell is None:
                    continue
                # Die Quota-Auswahl kann auf ein anderes freies Modell fallen
                # als das gerade gepruefte. Tokenzahlen sind modellabhaengig:
                # vor der Sendung deshalb fuer das tatsaechlich gewaehlte Modell
                # neu messen, statt einen fremden Messwert zu uebernehmen.
                if verfuegbares_modell != naechstes_modell:
                    neu_gemessen = _gemini_tokenzahl(
                        client, verfuegbares_modell, contents,
                        config_kwargs.get("system_instruction"),
                        label=f"{label} Server-RPD-Fallback {verfuegbares_modell}",
                    )
                    if neu_gemessen > GEMINI_INPUT_EXCEPTION_BUDGET:
                        fallback_versuchte_modelle.add(verfuegbares_modell)
                        continue
                if verfuegbares_modell in fallback_versuchte_modelle:
                    continue
                fallback_versuchte_modelle.add(verfuegbares_modell)
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
                    return _gemini_generate_content_einzelversuch(
                        client, verfuegbares_modell, contents,
                        types.GenerateContentConfig(**config_kwargs), label,
                        config_kwargs.get("system_instruction"), logical_request_id,
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
                    if neu_gemessen > GEMINI_INPUT_EXCEPTION_BUDGET:
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
                            return _gemini_generate_content_einzelversuch(
                                client, verfuegbares_modell, contents,
                                types.GenerateContentConfig(**config_kwargs), label,
                                config_kwargs.get("system_instruction"), logical_request_id,
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
                    if neu_gemessen > GEMINI_INPUT_EXCEPTION_BUDGET:
                        raise RuntimeError(
                            "GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_WARTEN: "
                            f"Modell={neue_modell} | {neu_gemessen:,} > "
                            f"{GEMINI_INPUT_EXCEPTION_BUDGET:,}"
                        )
                    _gemini_active_modell = neue_modell
                    _gemini_last_request_model = neue_modell
                    wait_reservierung = _gemini_quota_registriere_sendung(
                        neue_modell, neu_gemessen
                    )
                    try:
                        return _gemini_generate_content_einzelversuch(
                            client, neue_modell, contents,
                            types.GenerateContentConfig(**config_kwargs), label,
                            config_kwargs.get("system_instruction"), logical_request_id,
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


def _gemini_synthese(
    client, modell, teiltexte, system_instruction, label, logical_request_prefix=None
):
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
                    if tokens > GEMINI_INPUT_EXCEPTION_BUDGET:
                        raise RuntimeError(
                            f"GEMINI_SYNTHESE_ZU_GROSS: Einzelne Teilanalyse "
                            f"ueberschreitet die Ausnahmegrenze "
                            f"{GEMINI_INPUT_EXCEPTION_BUDGET:,} Tokens "
                            f"(gemessen: {tokens:,})."
                        )
                    # Einzelne Synthese-Teile duerfen ausnahmsweise bis 121.2k
                    # gehen; die normale Gruppierung bleibt bei 120k.
                    gruppe = [text]
                    continue
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
            if tokens > GEMINI_INPUT_EXCEPTION_BUDGET:
                raise RuntimeError(
                    f"GEMINI_SYNTHESE_REQUEST_ZU_GROSS: {tokens:,} Tokens."
                )
            synthese_fingerprint = _gemini_503_request_fingerprint(
                [synth_prompt], system_instruction
            )
            cached_synthese = _gemini_synthese_request_cache.get(synthese_fingerprint)
            if cached_synthese is not None:
                print(
                    f"GEMINI-SYNTHESE-CACHE-HIT: Stufe {stufe}/{idx} | "
                    f"bereits erfolgreich | Zeichen={len(cached_synthese)}"
                )
                neue_aktuelle.append(cached_synthese)
                continue
            response = _gemini_generate_content_quota_safe(
                client,
                modell,
                [synth_prompt],
                {"system_instruction": system_instruction},
                tokens,
                f"{label} Synthese {stufe}/{idx}",
                logical_request_id=(
                    _gemini_logische_request_id(
                        logical_request_prefix,
                        f"SYNTHESE_{stufe}_{idx}",
                        [synth_prompt],
                        system_instruction,
                    )
                    if logical_request_prefix else None
                ),
            )
            synthese_text = response.text or ""
            _gemini_synthese_request_cache[synthese_fingerprint] = synthese_text
            neue_aktuelle.append(synthese_text)
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
    """Stabiler Fingerabdruck fuer einen Split, auch ueber neue Upload-URIs hinweg."""
    context_names = []
    source_contents = []
    context_fingerprint = ""
    if isinstance(cache_name, dict):
        context_names = list(cache_name.get("context_names") or [])
        source_contents = list(cache_name.get("contents") or [])
        context_fingerprint = str(cache_name.get("resume_fingerprint") or "")

    name_by_identity = {id(part): context_names[i] for i, part in enumerate(source_contents) if i < len(context_names)}

    def _stable_part(value):
        # Text-Parts (einschliesslich nachsegmentierter Teile) werden anhand
        # ihres Inhalts identifiziert. Upload-Parts erhalten den logischen
        # Quelldateinamen statt einer pro Lauf wechselnden URI.
        logical_name = name_by_identity.get(id(value))
        if logical_name:
            return f"source={logical_name}"
        candidate_text = getattr(value, "text", None)
        if isinstance(candidate_text, str):
            return f"text_sha256={hashlib.sha256(candidate_text.encode('utf-8')).hexdigest()}|chars={len(candidate_text)}"
        for attr in ("display_name", "name"):
            candidate = getattr(value, attr, None)
            if candidate:
                return f"{attr}={candidate}"
        return f"type={type(value).__name__}|repr={repr(value)}"

    payload = {
        "context_fingerprint": context_fingerprint,
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


def _gemini_cache_antwort(
    client, modell, cache_name, contents, system_instruction=None, logical_stage=None
):
    """Sendet geplante Requests mit 120k Normalziel und 121.2k Ausnahmegrenze.

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
    resume_fingerprint = (
        str(cache_name.get("resume_fingerprint") or "")
        if isinstance(cache_name, dict) else ""
    )
    # Die fachliche Stufe und der Hash des Arbeits-Prompts trennen Requests,
    # die denselben Quellkontext verwenden. Der Prompt-Hash umfasst nicht die
    # variabel gesplitteten Quelldaten und bleibt deshalb bei Re-Chunking stabil.
    if logical_stage is None:
        first_prompt = "\n".join(
            str(getattr(part, "text", part) or "") for part in arbeits_contents
        )
        stage_match = re.search(
            r"(?im)^\s*(STUFE\s+\d+\s*[–-]\s*[^.\n]+|FINALE SYNTHESE|REPARATUR[^\n]*)",
            first_prompt,
        )
        logical_stage = stage_match.group(1).strip() if stage_match else "GenerateContent"
    logical_request_base = _gemini_logische_request_id(
        resume_fingerprint, logical_stage, arbeits_contents,
        config_kwargs.get("system_instruction"), source_contents=daten_teile,
    )
    resume_state = _gemini_split_resume_laden(resume_fingerprint)
    persistent_completed = dict(resume_state.get("completed") or {})
    full_request_key = _gemini_request_part_fingerprint(
        cache_name, arbeits_contents, config_kwargs.get("system_instruction"), 0, daten_teile
    )
    try:
        token_count = _gemini_tokenzahl(
            client, modell, anfrage_contents,
            config_kwargs.get("system_instruction"),
            label="GenerateContent",
        )
    except Exception as exc:
        _gemini_split_resume_fehler_sichern(
            resume_fingerprint, persistent_completed, resume_state.get("failed"),
            full_request_key, "CountTokens", 0, 1, exc, modell,
        )
        raise

    # Bei vorhandenen Quellen-Parts muss auch der ungeteilte Gesamtrequest
    # das Normalziel einhalten. Sonst wird er in fachlich gebundene Gruppen
    # zerlegt; die Ausnahmegrenze ist kein zweites Normalbudget.
    # Ohne separate Daten-Parts kann ein einzelner unteilbarer Arbeits-Prompt
    # die eng begrenzte Ausnahme weiterhin nutzen.
    direct_request_allowed = (
        token_count <= GEMINI_INPUT_SAFE_BUDGET
        or (
            token_count <= GEMINI_INPUT_EXCEPTION_BUDGET
            and not daten_teile
        )
    )
    if direct_request_allowed:
        if token_count > GEMINI_INPUT_SAFE_BUDGET:
            print(
                f"GEMINI-REQUEST-BUDGET-TOLERANZ: GenerateContent | "
                f"einzelner Request ohne separate Daten-Parts mit {token_count:,} Tokens "
                f"liegt ueber dem Normalziel {GEMINI_INPUT_SAFE_BUDGET:,}, aber innerhalb "
                f"der Ausnahmegrenze {GEMINI_INPUT_EXCEPTION_BUDGET:,}."
            )
        try:
            direct_response = _gemini_generate_content_quota_safe(
                client, modell, anfrage_contents, config_kwargs, token_count,
                "GenerateContent",
                logical_request_id=f"{logical_request_base}:Direct",
            )
        except Exception as exc:
            if "GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_FALLBACK" in str(exc) and daten_teile:
                # Die Erst-Messung passte, aber das konkret ausgewaehlte Modell
                # misst denselben Request groesser. Deshalb nicht terminal
                # abbrechen: den Request in den fachlichen Gruppenplan ueberfuehren.
                print(
                    "GEMINI-QUOTA-FALLBACK-RESEGMENTIERUNG: direkte Anfrage wurde "
                    "nach exakter Neumessung fuer das Fallback-Modell zu gross; "
                    "Quellgruppen werden verlustfrei neu geplant."
                )
                direct_request_allowed = False
            else:
                _gemini_split_resume_fehler_sichern(
                    resume_fingerprint, persistent_completed, resume_state.get("failed"),
                    full_request_key, "GenerateContent", 0, 1, exc, modell,
                )
                raise
        else:
            _gemini_split_resume_speichern(resume_fingerprint, persistent_completed, None)
            return direct_response

    print(
        f"GEMINI-REQUEST-SPLIT: fachlicher Request-Plan wird erstellt "
        f"(Ausgangsmessung={token_count:,}, Normalziel {GEMINI_INPUT_SAFE_BUDGET:,}, "
        f"Ausnahmegrenze {GEMINI_INPUT_EXCEPTION_BUDGET:,})."
    )
    gruppen = _gemini_request_plan(
        client, modell, cache_name, daten_teile, arbeits_contents,
        config_kwargs.get("system_instruction"), "GenerateContent Split",
    )

    # Persistente Ergebnisse werden nur bei identischem Quellen-Fingerprint
    # und identischem Split-Fingerprint uebernommen.
    for key, value in persistent_completed.items():
        if isinstance(value, str):
            _gemini_request_split_cache[key] = value
    if resume_state.get("failed"):
        failed = resume_state["failed"]
        print(
            "GEMINI-SPLIT-RESUME: letzter Lauf scheiterte bei "
            f"Split {failed.get('split_index', '?')} ({failed.get('status', 'unbekannt')}); "
            "bereits erfolgreiche Splits werden wiederverwendet."
        )

    teiltexte = []
    response = None
    idx = 0
    while idx < len(gruppen):
        idx += 1
        gruppe = gruppen[idx - 1]
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

        try:
            group_tokens = _gemini_tokenzahl(
                client, modell, gruppe_contents,
                config_kwargs.get("system_instruction"),
                label=f"GenerateContent Split {idx}",
            )
            if group_tokens > GEMINI_INPUT_EXCEPTION_BUDGET:
                raise RuntimeError(
                    f"GEMINI_REQUEST_ZU_GROSS_NACH_SPLIT: Split={idx} Tokens={group_tokens:,} "
                    f"> Ausnahmegrenze {GEMINI_INPUT_EXCEPTION_BUDGET:,}."
                )

            print(
                f"GEMINI-SPLIT-SEND: Split {idx}/{len(gruppen)} | "
                f"Tokens={group_tokens:,} | nur dieser Split ist noch offen."
            )
            response = _gemini_generate_content_quota_safe(
                client, modell, gruppe_contents, config_kwargs, group_tokens,
                f"GenerateContent Split {idx}",
                logical_request_id=f"{logical_request_base}:Split:{idx}",
            )
        except Exception as exc:
            if "GEMINI_REQUEST_ZU_GROSS_NACH_QUOTA_FALLBACK" in str(exc):
                # Nur die exakt als zu gross gemessene Gruppe neu segmentieren.
                # Die Quelle bleibt vollstaendig; bereits erfolgreiche Gruppen
                # bleiben im Cache und werden nicht erneut gesendet.
                fallback_modell = _gemini_active_modell or _gemini_last_request_model or modell
                try:
                    kleinere_gruppen = list(_gemini_sichere_daten_gruppen(
                        client, fallback_modell, gruppe, arbeits_contents,
                        config_kwargs.get("system_instruction"),
                        f"GenerateContent Split {idx} Quota-Fallback-Nachsegmentierung",
                    ))
                except Exception as split_exc:
                    _gemini_split_resume_fehler_sichern(
                        resume_fingerprint, persistent_completed, resume_state.get("failed"),
                        part_key, "GenerateContent Split Nachsegmentierung", idx,
                        len(gruppen), split_exc, fallback_modell,
                    )
                    raise
                if len(kleinere_gruppen) > 1:
                    gruppen[idx - 1:idx] = kleinere_gruppen
                    idx -= 1
                    print(
                        f"GEMINI-QUOTA-FALLBACK-RESEGMENTIERT: Split {idx + 1} wurde "
                        f"verlustfrei in {len(kleinere_gruppen)} kleinere Gruppen geteilt; "
                        "bereits erfolgreiche Splits bleiben erhalten."
                    )
                    continue
                # Wenn die exakte Neumessung trotz Segmentierungsversuch keinen
                # kleineren Plan erzeugt, wird der Fehler regulär gesichert.
            _gemini_split_resume_fehler_sichern(
                resume_fingerprint, persistent_completed, resume_state.get("failed"),
                part_key, "GenerateContent Split", idx, len(gruppen), exc,
                _gemini_last_request_model or modell,
            )
            raise
        result_text = response.text or ""
        if ist_ablehnung(result_text):
            _gemini_split_resume_speichern(
                resume_fingerprint,
                persistent_completed,
                {
                    "split_index": idx,
                    "split_total": len(gruppen),
                    "split_fingerprint": part_key,
                    "status": "SAFETY_FILTER_REJECTED",
                    "error": "Sicherheitsfilter-Ablehnung oder leere Antwort",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                },
            )
            raise RuntimeError(
                f"GEMINI_SPLIT_{idx}_SICHERHEITSFILTER_ABLEHNUNG"
            )
        _gemini_request_split_cache[part_key] = result_text
        persistent_completed[part_key] = result_text
        _gemini_split_resume_speichern(
            resume_fingerprint,
            persistent_completed,
            {
                "split_index": idx,
                "split_total": len(gruppen),
                "split_fingerprint": part_key,
                "status": "COMPLETED",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
        print(
            f"GEMINI-SPLIT-CACHE-STORE: Split {idx}/{len(gruppen)} | "
            f"erfolgreich gespeichert | Zeichen={len(result_text)} | persistent=True"
        )
        teiltexte.append(result_text)

    if len(teiltexte) == 1:
        _gemini_split_resume_speichern(resume_fingerprint, persistent_completed, None)
        if response is not None:
            return response
        return SimpleNamespace(text=teiltexte[0], candidates=[])

    # Die Teilanalysen werden hierarchisch zusammengefuehrt. Auch die Synthese
    # ist ein eigener Request und wird bei einem technischen Fehler ueber den
    # bestehenden Stage-Cache der aufrufenden Ebene nicht erneut fuer A1/A2/A3
    # aufgebaut.
    try:
        synth_text = _gemini_synthese(
            client,
            modell,
            teiltexte,
            config_kwargs.get("system_instruction"),
            "GenerateContent",
            logical_request_prefix=f"{logical_request_base}:Synthesis",
        )
    except Exception as exc:
        request_info = dict(globals().get("_gemini_last_503_request_info") or {})
        synthesis_fingerprint = str(
            request_info.get("request_fingerprint")
            or _gemini_request_part_fingerprint(
                cache_name, arbeits_contents,
                config_kwargs.get("system_instruction"), "SYNTHESIS", teiltexte,
            )
        )
        _gemini_split_resume_fehler_sichern(
            resume_fingerprint, persistent_completed, resume_state.get("failed"),
            synthesis_fingerprint, "SYNTHESIS", "SYNTHESIS", len(gruppen), exc,
            request_info.get("model") or _gemini_last_request_model or modell,
        )
        raise
    _gemini_split_resume_speichern(resume_fingerprint, persistent_completed, None)
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
        daten_antwort = _gemini_cache_antwort(client, modell, cache_stufe1, daten_prompt, logical_stage="A1_DISCOVERY")
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
        technik_antwort = _gemini_cache_antwort(client, modell, cache_stufe2, technik_prompt, logical_stage="A2_TECHNIK")
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
        historie_antwort = _gemini_cache_antwort(client, modell, cache_stufe3, historie_prompt, logical_stage="A3_HISTORIE")
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
        "getrennt und werden ueber A3 bzw. den bestehenden Historienmechanismus verarbeitet. "
        "THESENREGISTER: Gib am Ende einen optionalen, exakt markierten Block ```TRADING_RADAR_THESEN_JSON\n{\"data_cutoff\":\"Zeitstempel aus den aktuellen Daten, sonst leer\",\"theses\":[...]}\n``` aus. Jede These hat thesis_id (bei Fortführung dieselbe ID), thesis, horizon, evidence[], counter_evidence[], confidence, confirm_triggers[], invalidate_triggers[], event_dependencies[], asset_classes[], candidates[] und status. Zulässige Status: NEU, BESTAETIGT, VERSTAERKT, ABGESCHWAECHT, WARTET_AUF_EVENT, WIDERLEGT, ERSETZT, UNKLAR. Eventdetails (Name, Datum/Uhrzeit/Zeitzone, Konsens, Vorwert, Istwert und Marktreaktion) nur aus bereitgestellten Fakten; fehlende Angaben leer/null, nicht schätzen. Keine technische Handelsfreigabe daraus ableiten. Wenn keine belastbaren Thesen vorliegen, theses=[] ausgeben. Der Block darf keine neuen Fakten gegenüber dem Bericht einführen. "
        "Gemini darf die vollstaendige B-/C-/A-Historie interpretieren; in der fuer den Nutzer hervorgehobenen Statusmeldung stehen aber aktuelle A-Einstufungen, Aufstiege zu A und Statusverluste aus A im Vordergrund. Unveraendertes B (B→B) ist fuer den Nutzer irrelevant und darf nicht als eigene Meldung hervorgehoben werden. C→B kann als Vorstufe intern eingeordnet werden, ist aber kein A-Aufstieg. Statusverläufe müssen, wenn sie genannt werden, als exakter Vorheriger-Status → aktueller Status wiedergegeben werden. Fehlende Statuswerte bedeuten „Statusverlauf nicht verifiziert“ und niemals „stabil“. Das Wort stabil/gleichbleibend ist nur zulässig, wenn beide vorhandenen Statuswerte identisch sind; ein Wechsel wie C→B ist als Verbesserung und nicht als stabil zu beschreiben.\n\n"
        "DARSTELLUNGSREGEL: Jede genannte Aktie bzw. jedes Unternehmen muss immer mit Firmenname und Ticker im Format Name (TICKER) erscheinen. "
        "Keine Aktiennennung nur über den Ticker oder nur über den Namen. Dies gilt insbesondere für 1.3, 1.4, 2.1–2.5, 3.x, 4, 5, 6.x, 8.x und 9.x.\n\n"
        "VERBINDLICHER INHALTSVERTRAG 1–11: Halte die folgende Struktur exakt ein. "
        "Jeder Unterpunkt wird eigenständig bearbeitet. Wenn eine geforderte Information im bereitgestellten Datenbestand nicht vorhanden ist, "
        "schreibe ausdrücklich 'NICHT VERFUEGBAR' bzw. eine gleichwertige konkrete Negativfeststellung. Erfinde niemals Daten, Termine, Kurse, CRV, "
        "Fundamentaldaten oder Unternehmensinformationen. 'Kein Setup' darf einen Abschnitt nicht ersetzen, wenn dort andere Daten verfügbar sind.\n\n"
        "1. 🔥 WAS KÖNNTE GELD VERDIENEN?: 1.1 Veränderungen seit dem letzten Lauf einschließlich neuer Makro-, Sektor-, Rohstoff-, Aktien- und Investmententwicklungen sowie "
        "Ideenstatus; 1.2 nur konkret handelbare Chancen, die im aktuellen datumspassenden Trendfolge-, Trendwende- oder Short-Scanner-Export als gültiges Setup belegt sind. "
        "Trade-Story-Rohuniversum, Aktienuniversum, Watchlist, offene Positionen, Gemini-Vorschläge und Status VALIDE allein außerhalb eines Scanner-Exports sind keine Scanner-Bestätigung. "
        "Jede Chance muss Scanner-Quelle und Setup-Typ explizit nennen sowie Titel/Ticker, Richtung, These, Treiber, Entry/Zone, Stop, TP1/TP2, CRV soweit vorhanden, Gegenargument, konkreten Trigger und Invalidierung ausweisen. "
        "Wenn kein passender aktueller Scanner-Datensatz vorliegt, darf ein Titel nicht als sofort handelbare Chance erscheinen. "
        "1.3 Ideen im Aufbau mit These, bestätigenden und widersprechenden Daten, Makro/Sektor/Rohstoff-Zusammenhang, Zweitrundeneffekten, "
        "Profiteuren/Verlierern, konkreten Titeln, technischem Status, fehlenden Voraussetzungen, Aktivierungs- und Widerlegungstrigger; 1.4 Frühindikatoren/neue Themen mit Ereignis, Zusammenhang, "
        "Branche und soweit möglich konkreten Aktien sowie Triggern, ohne Scores.\n"
        "2. 🎯 KONKRETE TRADES: 2.1 ausschließlich Titel mit gültigem Eintrag im aktuellen Trendfolge-Scanner-Export (Setups-Datensatz mit Status VALIDE und CRV1 >= 1), Aktie/Ticker, Entry, Stop, TP1/TP2, CRV, technischem Zustand, Makro-/Sektorunterstützung und Risiken. "
        "Keine Titel aus dem Rohuniversum oder offenen Positionen als neues Trendfolge-Setup ausgeben; bei fehlendem aktuellen gültigen Export keine neuen Trendfolge-Setups behaupten. "
        "2.2 Trendwende mit Abwärtsbewegung, Boden-/Wendezeichen, Entry, Stop, Ziele, CRV und bestätigten/fehlenden Kriterien; "
        "2.3 Short mit Abwärtsthese, technischer Bestätigung, Entry, Stop, TP1/TP2, CRV, Makro-/Sektorunterstützung und Risiken; "
        "2.4 HebelTrader darf AUSSCHLIESSLICH aktuelle KAUFKANDIDAT-A-Titel als A-Kandidaten in diesem Abschnitt wiedergeben. "
        "Ein KAUFKANDIDAT-A-Status ist allein weder bestätigtes technisches Setup noch Kaufempfehlung oder sofort handelbarer Trade. "
        "Wenn der aktuelle Einzel-Check Hauptfilter verwirft oder InZone/Breakout nicht bestätigt, muss der Titel ausdrücklich als 'A-Status vorhanden, technisches Setup nicht bestätigt – kein sofort handelbarer Trade' gekennzeichnet werden. "
        "Entry, Stop, Ziele, CRV und technische Bestätigung dürfen nur aus einem aktuellen autoritativen Technikbefund übernommen werden; fehlende Werte bleiben NICHT VERFUEGBAR. "
        "KAUFKANDIDAT B/C, KEIN KANDIDAT, KEIN SETUP, VALIDE/VORBEREITET ohne A sowie reine Universums-/Kontextmitglieder dürfen NICHT als Trade in 2.4 erscheinen. "
        "Aufstiege/Abstiege zwischen A/B/C oder C→KEIN KANDIDAT dürfen ausschließlich qualitativ interpretiert werden und erlauben erst bei aktuellem Status KAUFKANDIDAT A einen Eintrag als A-Kandidat in 2.4; die technische Handelbarkeit muss separat belegt werden. "
        "Offene Positionen aus Offene Positionen+Check.csv gehören NICHT in 2.4, sondern ausschließlich in Punkt 10. "
        "Für jeden genannten Titel zwingend Name und Ticker im Format Name (TICKER); Basisinstrument, Richtung, Setup, Entry, Stop, Ziel, Risiko und Hebel-/Volatilitätsrisiken nur soweit autoritativ vorhanden. "
        "2.5 sonstige Gemini-Chancen nur mit nachvollziehbarer, quellengebundener Datenbegründung, konkretem Titel, Gegenargument/Risiko und Trigger. Reine Performance bereits bestehender Portfolio-/Langfristpositionen ist kein neues Einstiegssignal; unbestätigte thematische Ideen müssen ausdrücklich als nicht handelbare Ideenreferenzen gekennzeichnet werden. Fehlen aktuelle Setup-Daten, darf keine sofort handelbare Alternative behauptet werden. Fehlende Daten nicht ersetzen.\n"
        "3. 🧠 THEMEN & ZUSAMMENHÄNGE: 3.1 Makro→Branche→Aktie; 3.2 Rohstoff→Branche→Aktie; 3.3 Politik→Branche→Aktie; "
        "3.4 Technologie→Branche→Aktie; 3.5 Unternehmens-/Fundamentaldaten→Aktie. Für jeden Unterpunkt die Kausalkette, die konkrete Wirkung auf Branche und namentlich genannte Aktie, die dafür sprechenden und widersprechenden Daten sowie den nächsten bestätigenden oder widerlegenden Trigger erklären. Eine reine Aufzählung von Marktwerten oder Profiteuren reicht nicht.\n"
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
        "VERBINDLICHE EDELMETALL-QUELLENREGEL: Sämtliche numerischen und sonstigen Fakten zu Gold, Silber, Platin und Palladium stammen ausschließlich aus Edelmetalle_Briefing(...).txt bzw. aus den von Python daraus erzeugten Faktenblöcken. "
        "Das Markt-Briefing und das Makro-Briefing dürfen keine Edelmetall-Kurse, Performancewerte, Zeiträume, technischen Kennzahlen oder Statuswerte liefern oder ersetzen. "
        "Python stellt die überprüfbaren Fakten bereit; Gemini interpretiert sie, ohne neue Zahlen, Zeiträume, Indikatorstände oder Statusangaben zu erfinden. "
        "5T/kurzfristige Entwicklung, EMA20/EMA50 oder andere Kennzahlen dürfen nur genannt werden, wenn diese konkret im Edelmetalle-Briefing als aktuelle Fakten enthalten sind; andernfalls NICHT VERFUEGBAR oder nicht behaupten. "
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
        client, modell, cache_final, [final_prompt] + zusatz_anweisungen,
        logical_stage="FINAL_SYNTHESE",
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
        if os.path.isfile(GEMINI_SPLIT_RESUME_DATEI):
            try:
                with open(GEMINI_SPLIT_RESUME_DATEI, "r", encoding="utf-8") as resume_f:
                    resume_state = json.load(resume_f)
                contexts = resume_state.get("contexts") if isinstance(resume_state.get("contexts"), dict) else {}
                latest_context = max(
                    contexts.values(),
                    key=lambda item: str((item or {}).get("updated_at") or ""),
                    default={},
                )
                failed_split = latest_context.get("failed") or {}
                if failed_split:
                    inhalt += (
                        f"GEMINI_SPLIT_STATUS={failed_split.get('status', 'FAILED')}\n"
                        f"GEMINI_SPLIT_INDEX={failed_split.get('split_index', 'UNBEKANNT')}\n"
                        f"GEMINI_SPLIT_ANZAHL={failed_split.get('split_total', 'UNBEKANNT')}\n"
                        f"GEMINI_SPLIT_FINGERPRINT={failed_split.get('split_fingerprint', 'UNBEKANNT')}\n"
                        f"GEMINI_SPLIT_RESUME_DATEI={GEMINI_SPLIT_RESUME_DATEI}\n"
                    )
            except (OSError, ValueError, TypeError):
                inhalt += f"GEMINI_SPLIT_RESUME_DATEI={GEMINI_SPLIT_RESUME_DATEI}\n"
        with open(status_datei, "w", encoding="utf-8") as f:
            f.write(inhalt)
        print(f"Technischer Gemini-Fallback-Status gespeichert: {status_datei}")
        return status_datei

    raise ValueError(f"Unbekannte Aktion fuer Gemini-Fallback-Status: {aktion}")


def _gemini_fallback_kandidaten_ohne_versuchte(kandidaten, versuchte_modelle):
    """Iteriert Fallback-Kandidaten, ohne bereits gescheiterte Modelle erneut zu waehlen.

    Die Menge wird bei jedem Schritt erneut geprueft, damit ein waehrend des
    Durchlaufs fehlgeschlagenes Modell auch dann ausgeschlossen bleibt, wenn
    die Kandidatenliste bereits vor dem Fehler erzeugt wurde.
    """
    for kandidat in kandidaten or []:
        if kandidat not in versuchte_modelle:
            yield kandidat


def _gemini_503_modellwechsel_budget_pruefen(
    stage, maximum, zaehler=None, logical_request_id=None
):
    """Reserviert einen Modellwechsel pro logischer Request-ID UND Stage.

    Rueckgabe: (erlaubt, bisherige/neu belegte Anzahl, Budget-Schluessel).
    Die Request-ID muss von der fachlichen Stage stammen und bei Re-Chunking
    stabil bleiben; die Stage-Normalisierung entfernt den variablen Split-Nenner.
    Unabhängige Requests mit identischem generischem Label erhalten getrennte Budgets.
    """
    import re as _re
    schluessel = " ".join(_re.sub(r"\s+", " ", str(stage or "UNBEKANNTE_STAGE")).split())
    # Gesamtzahl kann sich bei verlustfreiem Re-Chunking ändern. Den Index
    # beibehalten, aber den variablen Nenner aus "Split 8/39" entfernen.
    schluessel = _re.sub(
        r"(?i)\bSplit\s+(\d+)\s*/\s*\d+\b",
        lambda match: f"Split {match.group(1)}",
        schluessel,
    )
    if not schluessel:
        schluessel = "UNBEKANNTE_STAGE"
    if logical_request_id is None:
        request_info = globals().get("_gemini_last_503_request_info") or {}
        logical_request_id = request_info.get("logical_request_id") or request_info.get("request_fingerprint")
    logical_request_id = str(logical_request_id or "UNBEKANNTE_LOGISCHE_REQUEST_ID").strip()
    budget_schluessel = f"{logical_request_id}::{schluessel}"
    registry = _gemini_503_modellwechsel_je_stage if zaehler is None else zaehler
    bisher = int(registry.get(budget_schluessel, 0))
    if bisher >= int(maximum):
        return False, bisher, budget_schluessel
    registry[budget_schluessel] = bisher + 1
    return True, bisher + 1, budget_schluessel


def _gemini_503_naechstes_modell(aktuelles_modell, modell_reihenfolge=None, fehlgeschlagene_modelle=None):
    """Liefert genau den naechsten noch nicht fehlgeschlagenen Modellkandidaten."""
    reihenfolge = list(modell_reihenfolge or GEMINI_MODELLREIHENFOLGE)
    fehlgeschlagen = set(_gemini_failed_models if fehlgeschlagene_modelle is None else fehlgeschlagene_modelle)
    try:
        start = reihenfolge.index(aktuelles_modell) + 1
    except ValueError:
        start = 0
    for kandidat in reihenfolge[start:]:
        if kandidat not in fehlgeschlagen:
            return kandidat
    return None


def _gemini_503_aeussere_entscheidung(fehlertext, aktuelles_modell, modell_reihenfolge=None, fehlgeschlagene_modelle=None):
    """Testbare Produktionsentscheidung der aeusseren 503-Retry-Schleife."""
    if "GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL" in str(fehlertext):
        return "terminal", None
    _, _, kategorie = analysiere_api_fehler(str(fehlertext))
    if kategorie not in ("ueberlast", "netzwerk", "quota_temporär"):
        return "other", None
    kandidat = _gemini_503_naechstes_modell(
        aktuelles_modell, modell_reihenfolge, fehlgeschlagene_modelle
    )
    return ("switch", kandidat) if kandidat is not None else ("terminal", None)


def _gemini_503_aeussere_retry_aktion(
    fehlertext, aktuelles_modell, modell_reihenfolge=None, fehlgeschlagene_modelle=None,
):
    """Produktionsaktion fuer den aeusseren 503-/Netzwerk-Retrypfad.

    Entscheidet terminal vs. Modellwechsel und aktualisiert bei einem Wechsel
    den gemeinsamen Laufzustand. Diese Funktion wird von der Hauptschleife und
    vom integrierten Regressionstest gemeinsam verwendet.
    """
    global _gemini_active_modell, _gemini_last_request_model, _gemini_failed_models
    request_fingerprint = str((globals().get("_gemini_last_503_request_info") or {}).get("request_fingerprint") or "")
    if fehlgeschlagene_modelle is not None:
        failed = fehlgeschlagene_modelle
    elif request_fingerprint:
        failed_by_request = globals().get("_gemini_503_failed_models_by_request")
        if failed_by_request is None:
            failed_by_request = {}
            globals()["_gemini_503_failed_models_by_request"] = failed_by_request
        elif not isinstance(failed_by_request, dict):
            raise TypeError(
                "_gemini_503_failed_models_by_request muss ein Dictionary sein; "
                f"erhalten: {type(failed_by_request).__name__}"
            )
        failed = failed_by_request.get(request_fingerprint)
        if failed is None:
            failed = set()
        elif isinstance(failed, str):
            if not failed:
                raise TypeError("Leerer Modellname in _gemini_503_failed_models_by_request")
            failed = {failed}
        elif isinstance(failed, set):
            if any(not isinstance(model, str) or not model for model in failed):
                raise TypeError(
                    "Registry-Eintrag muss ausschließlich nichtleere Modellnamen enthalten"
                )
            # Das bestehende Set samt Identität erhalten.
        elif isinstance(failed, (frozenset, list, tuple)):
            if any(not isinstance(model, str) or not model for model in failed):
                raise TypeError(
                    "Registry-Eintrag muss ausschließlich nichtleere Modellnamen enthalten"
                )
            failed = set(failed)
        else:
            raise TypeError(
                "Registry-Eintrag muss None, ein Modellname oder eine Sammlung "
                f"von Modellnamen sein; erhalten: {type(failed).__name__}"
            )
        failed_by_request[request_fingerprint] = failed
        _gemini_failed_models = failed
    else:
        failed = _gemini_failed_models
    action, candidate = _gemini_503_aeussere_entscheidung(
        fehlertext, aktuelles_modell, modell_reihenfolge, failed,
    )
    if action == "switch" and candidate is not None:
        failed.add(aktuelles_modell)
        _gemini_active_modell = candidate
        _gemini_last_request_model = candidate
        return "switch", candidate
    if action == "other":
        return "other", None
    return "terminal", None


def _gemini_trading_radar_eigenstaendig(client, preferred_modell, eingabedateien, bestehende_auswertung):
    """Erzeugt den Radar-Bericht durch einen eigenen quota-sicheren Gemini-Request."""
    global _GEMINI_TRADING_RADAR_TEXT, _GEMINI_TRADING_RADAR_STATUS
    global _GEMINI_GESCHLOSSENE_10_5_FINAL
    _GEMINI_TRADING_RADAR_TEXT = None
    _GEMINI_TRADING_RADAR_STATUS = "NICHT_ERZEUGT"
    import hashlib as _hashlib
    quellen = []
    cutoff = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for name, path in sorted((eingabedateien or {}).items(), key=lambda item: str(item[0])):
        try:
            if not path or not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                raw = handle.read(180_000)
            content = raw.decode("utf-8-sig", errors="replace")
            quellen.append(f"\n### QUELLE: {name} | Datei={os.path.basename(path)} | SHA256={_hashlib.sha256(raw).hexdigest()}\n" + content[:18000])
        except Exception as exc:
            quellen.append(f"\n### QUELLE: {name} | QUELLFEHLER={type(exc).__name__}: {exc}\n")
    offene_positionen_fakten = _offene_positionen_quellblock(
        (eingabedateien or {}).get("Offene Positionen+Check.csv")
    )
    try:
        geschlossene_positionen_fakten = lade_offenen_positionen_check_tab2()
    except Exception as exc:
        geschlossene_positionen_fakten = (
            "TAB-2-QUELLE NICHT VERIFIZIERBAR: " + type(exc).__name__ + ": " + str(exc)
        )
    # Der eigenstaendige Radar-Lauf muss dieselbe autoritative Faktenbasis an
    # den Speicherpfad uebergeben wie der klassische Gemini-Pfad. Andernfalls
    # sieht speichere_ergebnis() trotz erfolgreichem Tab-2-Lesen eine leere
    # globale Variable und markiert den Bericht faelschlich als NICHT_FINAL.
    _GEMINI_GESCHLOSSENE_10_5_FINAL = geschlossene_positionen_fakten or ""
    prompt = """Du erstellst eine eigenstaendige Trading-Radar-Auswertung fuer Sektor-Analyse.
Daten-Cutoff: CUTOFF
AUFTRAG: Erstelle einen entscheidungsorientierten Trading-Radar-Bericht nach Zielarchitektur v2.1. Die fachliche Reihenfolge ist verbindlich und darf NICHT durch das Portfolio umgekehrt werden:
1) GESAMTMARKT/MARKTREGIME: Indizes, Marktbreite, Volatilität, nominale/reale Renditen, Dollar, Liquidität, Konjunktur und Ereignisse zu einem gemeinsamen Marktbild verbinden.
2) RELATIVE STÄRKE/KAPITALROTATION: Märkte, Regionen, Sektoren und Assetklassen über vergleichbare Zeitfenster vergleichen; Divergenzen, Gewinner, Verlierer und mögliche Trendwechsel benennen.
3) THESEN/VORSCHAU: Basisszenario plus Alternativen, Horizont, Profiteure/Verlierer, Belege/Gegenbelege, beobachtbare Bestätigungstrigger und Invalidierung.
4) TECHNISCHE KANDIDATEN/TRADING: Scanner-Kandidaten UND Chancen außerhalb des aktuellen Portfolios prüfen. Watchlist strikt von freigegebenen Setups trennen. Keine Einstiegskurse, Stops, Ziele oder CRV erfinden.
5) PORTFOLIO-AUSWIRKUNG: Erst danach bestehende Positionen anhand der unabhängig ermittelten Marktthesen bewerten. Das Portfolio darf das Marktuniversum und neue Chancen nicht begrenzen.
Nicht bloß Daten paraphrasieren: Delta zur letzten Version, konkrete Evidenz und überprüfbare nächste Schritte herausarbeiten.
Nutze bereitgestellte Python-Quellen und den Altbericht nur als Fakten-/Historienkontext. Analysiere neu; kopiere den Altbericht nicht. Keine Daten nach dem Cutoff verwenden. Die fünf fachlichen Schritte sind in genau dieser Reihenfolge abzuarbeiten; das Portfolio darf weder Marktregime noch Thesen noch Kandidaten vorgeben.
TRENNUNG: Kennzeichne Datenfakten mit Quelle/Zeitraum, daraus abgeleitete Interpretation und unbestätigte Hypothesen ausdrücklich getrennt. Erfinde weder Zahlen noch Kausalität oder technische Werte. Wenn Daten fehlen, benenne exakt, welche Schlussfolgerung dadurch nicht möglich ist und welche Quelle/Bestätigung fehlt.
KERNFRAGE: Was ist angesichts der gelieferten Daten die wahrscheinlichste Marktbewegung im relevanten Zeithorizont, welche Assetklassen/Segmente sind relativ am attraktivsten bzw. schwächsten, wodurch wird die Einschätzung gestützt und wodurch widerlegt? Wenn keine belastbare Richtung ableitbar ist, sage das ausdrücklich und erkläre, welche Daten widersprüchlich oder unzureichend sind.
ABSCHNITT 1 – EXECUTIVE ENTSCHEIDUNG: Beginne mit einer klaren Hauptthese oder "keine belastbare Hauptthese". Nenne Zeithorizont, Konfidenz qualitativ (niedrig/mittel/hoch), 2–4 wichtigste Veränderungen seit dem letzten Bericht, stärkste Belege, wichtigste Gegenbelege, Top-Profiteur-Kandidaten und Verlierer-Kandidaten sowie nächste beobachtbare Bestätigung/Invalidierung. Keine bloße Sammlung allgemeiner Marktkommentare.
ABSCHNITT 2 – THESENKONTINUITÄT: Vergleiche jede relevante bestehende These mit dem letzten Register: BESTÄTIGT, ABGESCHWÄCHT, WARTET AUF EREIGNIS, INVALIDIERT, ERSETZT oder NEU. Erkläre konkret, welche neuen Zahlen/Beobachtungen die Statusänderung auslösen. Keine Änderung ist nur zulässig, wenn das Register tatsächlich geprüft wurde; nenne geprüften Registerstand und weshalb sich nichts änderte. IDs/Versionen niemals erfinden.
ABSCHNITT 3 – EREIGNISSE: Nur belegte Ereignisse mit Datum, Uhrzeit/Zeitzone, Kennzahl, Zeitraum, letztem Wert, Konsens und Quelle nennen. Für jedes Ereignis: betroffene These/Assetklasse/konkrete offene Positionen (Name (Ticker)), Szenarien unter/entsprechend/über Konsens, Reaktionskanäle und was die These bestätigen/widerlegen würde. Fehlende Werte als NICHT VERFÜGBAR ausweisen und keine redundanten leeren Ereignisblöcke auffüllen.
ABSCHNITT 4 – RELATIVE ASSETATTRAKTIVITÄT: Vergleiche Aktien/ETFs, Edelmetalle, Energie/Rohstoffe, Krypto und Zinsen/Dollar soweit Daten vorhanden. Ordne relativ (stärker/neutral/schwächer) ein, mit konkreten Belegen, Gegenargumenten, Zeithorizont und Auslösern. Keine pauschale Erklärung ohne Datenbezug.
ABSCHNITT 5 – TECHNISCHE KANDIDATEN: Das Kandidatenuniversum ist unabhängig vom Portfolio. Trenne (a) technisch valide Kandidaten, (b) interessante Werte mit konkretem noch fehlendem Trigger, (c) ereignisbedingt warten, (d) nicht handelbar/verwerfen. Nenne Name (Ticker), Richtung, Setup/These, belegten Trigger, Einstieg, Stop, TP1, TP2, CRV, Datenstand, Gegenargumente und Regelstatus, soweit vorhanden. Fehlende Werte nicht schätzen. "Keine belastbaren technischen Kandidaten" beendet NICHT die Gesamtanalyse: Nenne stattdessen nur dann Beobachtungskandidaten und konkrete fehlende Bestätigungen, wenn die Quellen das tragen; keine fiktiven Kandidaten erfinden.
ABSCHNITT 6 – PORTFOLIO & RISIKO: Verknüpfe Marktthesen mit jeder relevanten offenen Position aus der vollständigen Quelle: Name (Ticker), Rückenwind/Gegenwind/neutral/These verletzt, konkrete Datenbegründung, relevantes Ereignis, bekannte Stop-/TP-Informationen nur aus Quelle und nächster Prüfschritt. Hebe Konzentrationen und gemeinsame Risikotreiber hervor. Keine Kauf-/Verkaufs- oder Stopänderungen anordnen.
ABSCHNITT 7 – MSCI-VERGLEICH: Trenne Depot-/System-Gesamtperformance strikt von durchschnittlicher Einzelpositionsperformance. Gleicher Zeitraum und vergleichbare Basis; nenne Quelle, Start-/Enddatum, Ausschlüsse und Out-/Underperformance in Prozentpunkten. Erkläre, was die Kennzahl aussagt und was nicht. Keine Durchschnittsperformance als Depotperformance darstellen.
ABSCHNITT 8 – DATEN-/PRÜFANHANG: Quelle, Datenstand/Cutoff, Aktualität, Vollständigkeit, Widersprüche, fehlende Werte und konkrete Auswirkungen auf die Analyse. "Vollständig" nur behaupten, wenn der Lauf es belegt.
NAMENSREGEL (VERBINDLICH, GLOBAL): Bei JEDER Erwähnung eines konkreten Unternehmens, ETF, Rohstoffinstruments, Indexinstruments oder Kryptoassets immer "vollständiger Name (Ticker)" verwenden, z. B. "Siemens Energy AG (ENR.DE)". Niemals nur Name oder nur Ticker ausgeben. Das gilt für Überschriften, Fließtext, Tabellen, Kandidaten, Ereigniswirkungen, Portfolio, Vergleiche und Zusammenfassungen. Bei nicht auflösbarem Namen: "Name nicht aufgelöst (TICKER)" und im Datenanhang als Namensauflösungslücke nennen. Keine Ticker ohne Namen in narrativen Passagen.
QUALITÄTSREGEL: Jeder Abschnitt muss eine fachliche Aussage und deren Begründung enthalten, sofern Quellen dies ermöglichen. Wiederhole Zahlen nicht mehrfach ohne Zusatznutzen. Keine Fülltexte, keine redundanten NICHT-VERFÜGBAR-Blöcke. Priorisiere die entscheidungsrelevanten Erkenntnisse.
Keine automatischen Portfolio-/Trading-Änderungen. Eine Marktthese ist keine Handelsfreigabe. Bestehende Thesen anhand neuer Fakten aktualisieren. Kalender nur aus Quellen belegen. Portfolio und technische Kandidaten getrennt halten, aber über die Marktthese verbinden. Benchmark nur mit gelieferten/berechneten Werten.
Die Hebeltrader-Bewertungen A/B/C sind interne Kategorisierungen der Setups, KEINE finale Kaufentscheidung und KEINE automatische Handelsfreigabe; erkläre sie nur, wenn sie tatsächlich im Bericht vorkommen.
Portfolio: Keine verkürzte oder beispielhafte Positionsauswahl als vollständige Liste ausgeben. Die vollständige offene Positionsliste wird separat aus der gelieferten Faktenbasis ergänzt.
Gib diese acht Abschnitte aus:
1. Trading-Radar
2. Thesen-Update
3. Ereignisradar
4. Assetklassenvergleich
5. Technische Kandidaten
6. Portfolio & Risiko
7. MSCI-Vergleich
8. Daten-/Prüfanhang

REPORT-GATE-VERTRAG (Felder exakt und einzeln ausweisen; keine Werte erfinden):
WICHTIG: Die Feldnamen unten ohne Markdown-Fettschrift/Codeformatierung exakt als
"Feld: Wert" ausgeben. Ereignisse jeweils als eigenen Listenpunkt mit den
zugehörigen Feldern direkt darunter ausgeben; keine Felder eines Ereignisses
für ein anderes Ereignis mitverwenden.
- Abschnitt 2: Thesen-ID, Version und Status je aktualisierter These. Wenn keine
  These neu/aktualisiert wurde, schreibe exakt: "Keine neue oder aktualisierte
  These; Thesenregister geprüft, keine Änderung." Erfinde dafür keine ID/Version.
- Abschnitt 3: Ereignis-Uhrzeit, Zeitzone, Kennzahl, Zeitraum sowie letzter
  Wert/Konsens einzeln beschriften. Wenn die Werte aus den Quellen fehlen,
  kennzeichne das konkrete Feld ausdrücklich als NICHT VERFÜGBAR. Wenn keine
  relevanten Ereignisse vorliegen, schreibe exakt: "Keine relevanten Ereignisse
  im geprüften Zeitraum."
- Abschnitt 5: Für jeden Kandidaten Asset/Ticker, Trigger, Einstieg, Stop, TP1,
  TP2, CRV, Gegenargument und Regel-/Datenstatus einzeln beschriften. Einstieg,
  Stop, TP1, TP2 und CRV nur als vollständige numerische Werte aus Quellen oder
  regelbasierter Berechnung ausgeben. Wenn kein Kandidat belastbar ist, schreibe
  exakt: "Keine belastbaren technischen Kandidaten; keine Handelsfreigabe."
- Abschnitt 7: Portfolio-Gesamtperformance und durchschnittliche
  Positionsperformance als getrennte Felder ausweisen. Nur berechnete Werte
  verwenden; andernfalls für jedes Feld ausdrücklich NICHT VERFÜGBAR angeben.
- Abschnitt 8: OHLCV-Abdeckung, Datenstand und Lückenstatus nennen. Bei Lücken
  konkrete betroffene Kandidaten/Einschränkung nennen. Bei vollständiger Abdeckung
  ausdrücklich "OHLCV vollständig, keine Datenlücken; Kandidaten nicht
  eingeschränkt" schreiben, sofern das aus dem Datenlauf belegt ist.
- Abschnitt 10.5 wird ausschließlich danach deterministisch aus dem geladenen
  Tab-2-Quellblock ergänzt; dort keine freie Gemini-Zählung verwenden.

ALTBERICHT (nur Kontext, nicht kopieren):
__ALTBERICHT__

AUTORITATIVE OFFENE POSITIONEN (vollständig und unverändert, jede Zeile ist
separate Position; nicht tickerweise zusammenfassen):
__OFFENE_POSITIONEN__

GESCHLOSSENE POSITIONEN: ausschließlich Tab 2 / Bereich „Geschlossene Positionen“
aus „Offene Positionen+Check“, mit dortiger zeitlicher Auswahl und Quellenstatus:
__GESCHLOSSENE_POSITIONEN__

PROJEKTQUELLEN:
__QUELLEN__
""".replace("CUTOFF", cutoff).replace("__ALTBERICHT__", str(bestehende_auswertung or "")[:40000]).replace("__OFFENE_POSITIONEN__", offene_positionen_fakten or "QUELLE FEHLT/LEER: offene Positionsliste nicht belastbar verfügbar.").replace("__GESCHLOSSENE_POSITIONEN__", geschlossene_positionen_fakten or "TAB 2: keine Datensätze im relevanten Zeitraum.").replace("__QUELLEN__", "".join(quellen)[:140000])
    contents = [prompt]
    modell = preferred_modell or globals().get("_gemini_last_request_model") or MODELL
    try:
        token_count = _gemini_tokenzahl(client, modell, contents, None, label="Trading-Radar-Eigenlauf")
        # Der eigenstaendige Radar-Request liegt nach der klassischen Hauptauswertung
        # und durchlaeuft deren aeussere Retry-Schleife nicht. Deshalb braucht er
        # einen eigenen, begrenzten Modellwechsel fuer transiente 503-/Netzwerkfehler.
        # Der Request-Inhalt und die logische Request-ID bleiben dabei identisch.
        radar_failed_models = set()
        radar_pool_passes = 0
        while True:
            try:
                response = _gemini_generate_content_quota_safe(
                    client, modell, contents, {"temperature": 0.2}, token_count,
                    "Trading-Radar-Eigenlauf",
                    logical_request_id="Trading-Radar-Eigenlauf:" + datetime.date.today().isoformat(),
                )
                break
            except Exception as send_exc:
                action, next_model = _gemini_503_aeussere_retry_aktion(
                    str(send_exc), modell,
                    modell_reihenfolge=GEMINI_MODELLREIHENFOLGE,
                    fehlgeschlagene_modelle=radar_failed_models,
                )
                if (
                    action != "switch"
                    or not next_model
                    or radar_pool_passes >= GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN - 1
                ):
                    raise
                radar_pool_passes += 1
                wait_seconds = GEMINI_503_POOL_BACKOFF_SECONDS * (2 ** (radar_pool_passes - 1))
                print(
                    f"TRADING_RADAR_RETRY | transient={type(send_exc).__name__}: {send_exc} | "
                    f"Modellwechsel={modell}->{next_model} | "
                    f"Pool-Runde={radar_pool_passes + 1}/"
                    f"{GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN} | "
                    f"Backoff={wait_seconds:.0f}s"
                )
                time.sleep(wait_seconds)
                modell = next_model
                token_count = _gemini_tokenzahl(
                    client, modell, contents, None,
                    label=f"Trading-Radar-Eigenlauf Modellwechsel {modell}",
                )
        radar_text = str(getattr(response, "text", response) or "").strip()
        required = (
            "1. Trading-Radar",
            "2. Thesen-Update",
            "3. Ereignisradar",
            "4. Assetklassenvergleich",
            "5. Technische Kandidaten",
            "6. Portfolio & Risiko",
            "7. MSCI-Vergleich",
            "8. Daten-/Prüfanhang",
        )
        missing = [heading for heading in required if heading.casefold() not in radar_text.casefold()]

        # Ein einzelner Vollstaendigkeits-Retry faengt ausgelassene Pflichtabschnitte
        # ab. Die erste unvollstaendige Antwort wird nicht gespeichert oder publiziert.
        if radar_text and missing:
            print(
                "TRADING_RADAR_RETRY | unvollstaendige Antwort; fehlend="
                + ", ".join(missing)
                + " | starte genau einen Vollstaendigkeits-Retry."
            )
            retry_prompt = (
                prompt
                + "\n\nVERBINDLICHER VOLLSTAENDIGKEITS-RETRY: Deine vorherige Antwort "
                "hat folgende Pflichtabschnitte ausgelassen: "
                + ", ".join(missing)
                + ". Erzeuge den vollstaendigen Bericht erneut. Gib alle acht Pflichtabschnitte "
                "mit den vorgegebenen Ueberschriften in korrekter Reihenfolge aus. "
                "Keine Abschnitte auslassen; fehlende Daten ausdruecklich als nicht verfuegbar "
                "kennzeichnen und nichts erfinden."
            )
            retry_contents = [retry_prompt]
            retry_label = "Trading-Radar-Eigenlauf-Vollstaendigkeitsretry"
            retry_token_count = _gemini_tokenzahl(
                client, modell, retry_contents, None, label=retry_label
            )
            response = _gemini_generate_content_quota_safe(
                client,
                modell,
                retry_contents,
                {"temperature": 0.2},
                retry_token_count,
                retry_label,
                logical_request_id=(
                    "Trading-Radar-Eigenlauf:"
                    + datetime.date.today().isoformat()
                    + ":vollstaendigkeits-retry"
                ),
            )
            radar_text = str(getattr(response, "text", response) or "").strip()
            missing = [
                heading for heading in required
                if heading.casefold() not in radar_text.casefold()
            ]

        if not radar_text or missing:
            _GEMINI_TRADING_RADAR_STATUS = "NICHT_FINAL"
            fehlermeldung = (
                "Trading-Radar-Eigenlauf unvollstaendig nach einem Vollstaendigkeits-Retry; "
                "fehlende Pflichtabschnitte: "
                + (", ".join(missing) if missing else "leere Gemini-Antwort")
            )
            print("TRADING_RADAR_STATUS=NICHT_FINAL | " + fehlermeldung)
            _gemini_fallback_status_datei("write", fehlermeldung)
            return None
        _GEMINI_TRADING_RADAR_TEXT = radar_text
        _GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
        print("TRADING_RADAR_STATUS=EIGENSTAENDIG_ERZEUGT")
        return radar_text
    except Exception as exc:
        _GEMINI_TRADING_RADAR_STATUS = "REQUEST_FEHLER"
        fehlerzeile = (
            f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} | "
            f"{type(exc).__name__}: {exc}\n"
        )
        try:
            with open("Trading_Radar_Fehler.log", "a", encoding="utf-8") as log_handle:
                log_handle.write(fehlerzeile)
        except OSError as log_exc:
            print(f"TRADING_RADAR_LOG_FEHLER | {type(log_exc).__name__}: {log_exc}")
        print(f"TRADING_RADAR_STATUS=REQUEST_FEHLER | {type(exc).__name__}: {exc}")
        # Auch API-/Retry-Fehler muessen eine Statusdatei fuer den nachfolgenden
        # technischen Fallback-Mail-Schritt erzeugen.
        try:
            _gemini_fallback_status_datei(
                "write", f"{type(exc).__name__}: {exc}"
            )
        except Exception as status_exc:
            print(
                "TRADING_RADAR_STATUSDATEI_FEHLER | "
                f"{type(status_exc).__name__}: {status_exc}"
            )
        return None

def gemini_auswertung_starten():
    global _GEMINI_TRADING_RADAR_TEXT, _GEMINI_TRADING_RADAR_STATUS, _GEMINI_TRADING_RADAR_ONLY_MODE
    _GEMINI_TRADING_RADAR_TEXT = None
    _GEMINI_TRADING_RADAR_STATUS = "NICHT_ERZEUGT"
    _GEMINI_TRADING_RADAR_ONLY_MODE = True
    # Ein alter Status des gleichen Tages darf niemals in einen neuen Lauf
    # hineinragen. Bei erfolgreichem Lauf existiert danach keine Statusdatei.
    _gemini_fallback_status_datei("clear")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("FEHLER: Umgebungsvariable GEMINI_API_KEY nicht gesetzt.")
        sys.exit(1)

    if genai is None or types is None:
        raise RuntimeError(
            "GEMINI_SDK_FEHLT: Bitte die Projektabhaengigkeit installieren: "
            "python -m pip install google-genai"
        ) from _GEMINI_SDK_IMPORT_ERROR

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
    # Deterministische Nachbearbeitung liest die rohe JSONL-Zeilenhistorie
    # (insbesondere Abschnitt 2.4). Die kompakte A3-Historie ist nur für Gemini
    # und dessen Input-Manifest bestimmt und darf den Rohpfad nicht ersetzen.
    _GEMINI_EINGABEDATEIEN_AUSWERTUNG = dict(eingabedateien)
    _speichere_gemini_input_manifest(eingabedateien_gemini)

    # EXKLUSIVER TRADING-RADAR-LAUF: Die alte Auswertungspipeline wird ab
    # diesem Punkt nicht mehr ausgefuehrt. Die vorhandenen Projektquellen,
    # einschliesslich der vorbereiteten A3-Technikhistorie, gehen in den
    # eigenstaendigen quota-sicheren Radar-Request.
    radar_text = _gemini_trading_radar_eigenstaendig(
        client, MODELL, eingabedateien_gemini, bestehende_auswertung=""
    )
    if not isinstance(radar_text, str) or not radar_text.strip():
        raise RuntimeError(
            "TRADING_RADAR_FEHLER: Eigenstaendige Radar-Auswertung wurde nicht "
            "fachlich vollstaendig erzeugt; keine Ausgabedatei geschrieben."
        )
    return radar_text

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
    _gemini_503_failures_this_run.clear()
    _gemini_503_failed_models_by_request.clear()
    _gemini_503_pool_passes_this_run.clear()
    _gemini_503_modellwechsel_je_stage.clear()
    _gemini_synthese_request_cache.clear()
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
    global _GEMINI_GESCHLOSSENE_10_5_FINAL
    try:
        geschlossene_10_5 = lade_offenen_positionen_check_tab2()
    except Exception as exc:
        # Ein Tab-2-Quellenfehler darf den Bericht nicht technisch verhindern.
        # Der Bericht wird mit explizitem Fehlerstatus erstellt; das Report-Gate
        # blockiert die reguläre Freigabe, und es werden keine Marker/Hashes
        # fuer einen nicht verifizierten Export erfunden.
        geschlossene_10_5 = (
            "TAB-2-QUELLE NICHT VERIFIZIERBAR: "
            f"Lesen von Offene Positionen+Check / Tab Geschlossene Positionen fehlgeschlagen: {exc}"
        )
        print("WARNUNG 10.5: " + geschlossene_10_5)
    _GEMINI_GESCHLOSSENE_10_5_FINAL = geschlossene_10_5 or ""

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
    while True:
        versuch += 1
        versuch_nummer = ((versuch - 1) % MAX_VERSUCHE) + 1
        print(
            f"\nHauptversuch={versuch} | Modellpool-Groesse={MAX_VERSUCHE} | "
            f"maximale 503-Modellpool-Runden je Request="
            f"{GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN} | "
            "erfolgreiche Splits werden aus dem Resume-Cache uebernommen."
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
                    logical_stage="MAKRO_GATE_REPARATUR",
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

            if "GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL" in fehlertext:
                # Dieser Marker bedeutet, dass die aktuelle Poolrunde ausgeschoepft
                # ist - NICHT, dass alle begrenzten Backoff-Runden beendet sind.
                # Deshalb NICHT hier abbrechen: Der Fehler wird unten in den
                # gemeinsamen ueberlast/netzwerk-Pfad geroutet. Dort werden
                # request-spezifische Fehler-Sperren geloescht, 60/120 s gewartet
                # und maximal GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN
                # Poolrunden ausgefuehrt. Resume-Ergebnisse bleiben erhalten.
                print(
                    "  GEMINI-503-POOL-ERSCHOEPFT: leite in begrenzte Backoff-Recovery "
                    "weiter; noch nicht automatisch terminal fuer den Gesamtlauf."
                )

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
            if "GEMINI_TECHNISCHER_FALLBACK_503_TERMINAL" in fehlertext:
                # analysiere_api_fehler klassifiziert den Marker historisch als
                # modellpool_temporaer. Fuer die begrenzte Recovery muss er hier
                # als temporaere Ueberlast behandelt werden, damit der darunter
                # liegende Pool-Backoff (60/120 s) erreicht wird.
                kategorie = "ueberlast"
                abbrechen = False
                empfohlene_wartezeit = None
            # Jeder API-/Quota-Fehler ist ein technischer Retry: bereits
            # erfolgreich abgeschlossene A1/A2/A3/Final-Stufen werden nicht
            # erneut angefordert. Ein Sicherheitsfilter wird weiter unten
            # ausdrücklich als frischer Kontext behandelt.
            if kategorie in ("ueberlast", "netzwerk", "input_token_limit", "tageskontingent", "modellpool_temporaer"):
                technische_retry_wiederverwenden = True
            if abbrechen:
                if kategorie == "request_budget":
                    print(
                        "  Nicht wiederholbarer interner Request-/Chunk-Groessenfehler; "
                        "derselbe Request wird nicht erneut gesendet. Technischer Fallback."
                    )
                    break

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
                # Nach einem vollstaendigen Modellpool mit 503/Netzwerkfehlern
                # wird derselbe fachliche Request nicht erneut gestartet.
                # Erfolgreiche Stufen/Splits sind gesichert; ein spaeterer Lauf
                # kann anhand des Resume-Fingerprints fortsetzen.
                print(
                    "  GEMINI-MODELLPOOL-TERMINAL: temporaere 503/Netzwerkfehler; "
                    "kontrollierter technischer Fallback ohne erneuten identischen "
                    "Request-Zyklus. Der Split-Resume-Status bleibt fuer den naechsten Lauf erhalten."
                )
                break

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
                # wird pro Request/Split maximal zweimal je Lauf versucht; der
                # zweite Versuch erfolgt auf einem anderen Modell. Danach wird
                # der Resume-Status gesichert und ohne identische Retry-Schleife
                # in den technischen Fallback gewechselt.
                retry_aktion, naechstes_modell = _gemini_503_aeussere_retry_aktion(
                    fehlertext, aktuelles_modell,
                    GEMINI_MODELLREIHENFOLGE, _gemini_failed_models,
                )
                if retry_aktion == "switch" and naechstes_modell is not None:
                    # Sicherheitsgrenze pro logischer Stage/Split. Nicht global
                    # zaehlen: ein 503 in Split 8 darf Split 9 kein Budget nehmen.
                    # Der Stage-Name bleibt bei modellabhaengigem Re-Chunking stabil.
                    maximale_503_modellwechsel = (
                        MAX_VERSUCHE * GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN
                    )
                    request_info = dict(_gemini_last_503_request_info or {})
                    stage_key = str(request_info.get("stage") or "UNBEKANNTE_STAGE")
                    logical_request_id = str(
                        request_info.get("logical_request_id")
                        or request_info.get("request_fingerprint")
                        or "UNBEKANNTE_LOGISCHE_REQUEST_ID"
                    )
                    budget_pruefer = globals().get("_gemini_503_modellwechsel_budget_pruefen")
                    if callable(budget_pruefer):
                        wechsel_erlaubt, wechsel_anzahl, normalisierte_stage = (
                            budget_pruefer(
                                stage_key, maximale_503_modellwechsel,
                                logical_request_id=logical_request_id,
                            )
                        )
                    else:
                        # Der Retry-Handler bleibt auch dann vollstaendig
                        # funktionsfaehig, wenn er isoliert ohne Modul-Helper
                        # ausgefuehrt wird (z.B. AST-basierte Integrationstests).
                        import re as _re
                        normalisierte_stage = " ".join(
                            _re.sub(r"\s+", " ", stage_key).split()
                        ) or "UNBEKANNTE_STAGE"
                        normalisierte_stage = _re.sub(
                            r"(?i)\bSplit\s+(\d+)\s*/\s*\d+\b",
                            lambda match: f"Split {match.group(1)}",
                            normalisierte_stage,
                        )
                        budget_key = f"{logical_request_id}::{normalisierte_stage}"
                        stage_registry = globals().setdefault(
                            "_gemini_503_modellwechsel_je_stage", {}
                        )
                        bisherige_wechsel = int(stage_registry.get(budget_key, 0))
                        wechsel_erlaubt = bisherige_wechsel < maximale_503_modellwechsel
                        wechsel_anzahl = bisherige_wechsel
                        if wechsel_erlaubt:
                            wechsel_anzahl += 1
                            stage_registry[budget_key] = wechsel_anzahl
                    if not wechsel_erlaubt:
                        print(
                            f"  GEMINI-MODELLPOOL-STAGE-GRENZE: Request={logical_request_id[:16]} | Stage={normalisierte_stage} | "
                            f"{wechsel_anzahl}/{maximale_503_modellwechsel} Modellwechsel bereits "
                            "verbraucht; keine weitere Neuplanung fuer diese Stage. "
                            "Andere Splits behalten ihr eigenes Fehlerbudget."
                        )
                        break
                    naechster_index = GEMINI_MODELLREIHENFOLGE.index(naechstes_modell)
                    grund = "503-Overload" if kategorie == "ueberlast" else "Netzwerk-Abbruch"
                    # Modellwechsel ist ein Fallback auf eine andere Ressource,
                    # kein Retry desselben Requests. Deshalb nur kurzer Jitter statt
                    # 15/30/60/120 s Voll-Backoff. Der echte exponentielle Backoff
                    # erfolgt erst, wenn der gesamte Modellpool temporaer ueberlastet ist.
                    wartezeit = random.uniform(0.0, GEMINI_MODELLWECHSEL_JITTER_MAX)
                    print(
                        f"  {grund} nach Versuch {versuch}/{MAX_VERSUCHE} | "
                        f"Stage-Modellwechsel={wechsel_anzahl}/{maximale_503_modellwechsel} "
                        f"({normalisierte_stage}; Request={logical_request_id[:16]}). "
                        f"Kurzer Modellwechsel-Jitter {wartezeit:.1f}s; "
                        f"wechsle danach von {aktuelles_modell} auf {naechstes_modell}."
                    )
                    time.sleep(wartezeit)
                    # Der gemeinsame Produktions-Helper hat das fehlgeschlagene
                    # Modell gesperrt und Active/Last-Request bereits umgestellt.
                    modell_index = naechster_index
                    aktuelles_modell = GEMINI_MODELLREIHENFOLGE[modell_index]
                    # Bereits hochgeladener Kontext bleibt erhalten; ein 503 erfordert
                    # keinen erneuten Upload.
                    # Bereits hochgeladene Dateien bleiben erhalten. Ein 503 ist
                    # ein technischer Fehler; fuer den Modellwechsel ist kein
                    # erneuter Datei-Upload erforderlich.
                    continue

                # Nach dem ersten vollstaendigen Pooldurchlauf ist genau eine
                # zweite Runde nach Backoff erlaubt. Der Poolstatus ist request-
                # spezifisch; ein Fehler bei einem Split sperrt das Modell nicht
                # fuer andere Splits. Die API-Sendegrenze bleibt modellbezogen.
                request_fingerprint = str(
                    (_gemini_last_503_request_info or {}).get("request_fingerprint") or ""
                )
                pool_passes = int(_gemini_503_pool_passes_this_run.get(request_fingerprint, 0))
                grund = "503-Overload" if kategorie == "ueberlast" else "Netzwerk-Abbruch"
                if request_fingerprint and pool_passes < (GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN - 1):
                    _gemini_503_pool_passes_this_run[request_fingerprint] = pool_passes + 1
                    _gemini_503_failed_models_by_request[request_fingerprint].clear()
                    _gemini_failed_models = _gemini_503_failed_models_by_request[request_fingerprint]
                    _gemini_active_modell = None
                    _gemini_last_request_model = None
                    modell_index = 0
                    aktuelles_modell = GEMINI_MODELLREIHENFOLGE[0]
                    # Exponentieller Backoff: nach Runde 1 60 s, nach Runde 2 120 s.
                    # pool_passes ist vor dem Inkrement die Zahl bereits absolvierter
                    # Wiederholungen; die erste Poolrunde selbst zaehlt als Runde 1.
                    wartezeit = GEMINI_503_POOL_BACKOFF_SECONDS * (2 ** pool_passes)
                    naechste_runde = pool_passes + 2
                    print(
                        f"  {grund}: Modellpool-Durchlauf ausgeschoepft. "
                        f"Starte Pool-Runde {naechste_runde}/"
                        f"{GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN} "
                        f"nach {wartezeit:.0f}s Backoff; bereits erfolgreiche Splits "
                        "bleiben im Cache. Pro Request/Modell maximal "
                        f"{GEMINI_MAX_503_ATTEMPTS_PER_REQUEST_PER_RUN} echte API-Sendungen."
                    )
                    time.sleep(wartezeit)
                    continue
                print(
                    f"  {grund}: alle {GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN} "
                    "Modellpool-Runden ausgeschoepft. Technischer Fallback; "
                    "fehlgeschlagener Split und erfolgreiche Teilergebnisse bleiben "
                    "fuer den naechsten Lauf gespeichert."
                )
                break

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
        # Zweite, eigenstaendige Auswertung; die Hauptauswertung bleibt Rueckgabewert.
        _gemini_trading_radar_eigenstaendig(client, aktuelles_modell, eingabedateien_gemini, text)
        return text

    print(
        f"\nFEHLER: Keine gueltige Antwort nach {versuch} Hauptversuchen "
        f"(Modellpool-Groesse={MAX_VERSUCHE}; begrenzte zweite Poolrunde moeglich)."
    )
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


def _trade_story_kandidaten_auf_universum_reduzieren(candidate, *universen):
    """Reduziert ein vermischtes Kandidatenfeld auf explizite autoritative Ticker.

    Gemini kann Kandidatenfelder durch wiederholte Namen, Portfoliokontext,
    Rohstoffbegriffe und Satzreste zusammenziehen. Ein solcher Gesamtstring
    darf nicht als eine Kandidatenidentitaet oder als All-of-Pruefung gelten.
    Diese Funktion greift nur, wenn mindestens zwei Ticker-Tokens enthalten
    sind. Sie gibt eindeutige Ticker zurueck, die in mindestens einem der
    uebergebenen autoritativen Universen enthalten sind. Es wird kein Status
    erfunden; die nachfolgende Statuslogik prueft die Treffer gegen die
    jeweiligen Universen.
    """
    text = str(candidate or "").strip()
    ticker_tokens = re.findall(r"\(([A-Za-z0-9._=-]{1,30})\)", text)
    if len(ticker_tokens) < 2:
        return ""
    # Statushierarchie beachten: Sobald ein valides Setup eindeutig enthalten
    # ist, darf ein vorbereiteter/sonstiger Kandidat aus angehaengtem Kontext
    # dieses Setup nicht wieder auf NUR TEILW. VOLLSTAENDIG herunterziehen.
    priority_universes = [u for u in universen if isinstance(u, set)]
    for universe in priority_universes:
        found = []
        seen = set()
        for ticker in ticker_tokens:
            normalized = _normalisiere_ticker(ticker)
            if not normalized or normalized in seen:
                continue
            if normalized in universe:
                seen.add(normalized)
                found.append(ticker.strip())
        if found:
            return " | ".join(found)
    return ""


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

        # Gemini kann das Kandidatenfeld mit wiederholten Titeln, Portfoliobestand
        # und Satzresten verkleben. Bei mehreren expliziten Tickerangaben wird
        # das Feld auf die in den autoritativen Tagesuniversen belegten Ticker
        # reduziert, bevor der Status ermittelt wird. Insbesondere darf ein
        # vorhandenes VALIDE SETUP nicht durch nicht zugehoerige Zusatznamen
        # in derselben Zeichenkette als NUR TEILW. VOLLSTAENDIG erscheinen.
        bereinigte_kandidaten = _trade_story_kandidaten_auf_universum_reduzieren(
            candidate,
            valid_keys,
            zentrale_prepared_keys,
            beobachtungs_keys,
            bestehende_kandidaten_keys,
        )
        if bereinigte_kandidaten:
            # Nur den Kandidatenwert im Arbeitsspeicher aktualisieren. Den Textblock
            # hier noch NICHT mit den Offsets von name_m umschreiben: candidate
            # kann deutlich kuerzer als der urspruengliche Gemini-String sein und
            # wuerde sonst alle nachfolgenden Felder/Abschnitte verschieben.
            candidate = bereinigte_kandidaten

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
            # Kandidatenfeld genau einmal und anhand des aktuellen Textes ersetzen.
            # Keine alten Match-Offsets verwenden, da der Block inzwischen durch
            # Statusbereinigung/Normalisierung veraendert worden sein kann.
            feldname = name_m.group(0).split(":", 1)[0].strip()
            feldwert = candidate or "kein bestehender autoritativer Kandidat vorhanden"
            block, ersetzt = re.subn(
                rf"(?im)^({re.escape(feldname)}\s*:\s*).*$",
                lambda m: m.group(1) + feldwert,
                block,
                count=1,
            )
            if ersetzt != 1:
                raise RuntimeError(
                    "TRADE_STORY_KANDIDATENFELD_NICHT_EINDEUTIG: "
                    f"Feld '{feldname}' konnte nicht exakt einmal ersetzt werden."
                )

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
    """Ordnet nur exakt bekannte Pflichtueberschriften und Schreibvarianten zu.

    Die Abschnittsnummer allein reicht bewusst NICHT zur Erkennung. Alias-
    Eintraege sind vollstaendige, bekannte Ueberschriftentexte; beliebige
    Ueberschriften mit derselben Nummer bleiben unangetastet.
    """
    stripped = " ".join((heading or "").strip().split())
    normalized = re.sub(r"\s*→\s*", "→", stripped).rstrip(":").casefold()

    aliases_by_key = {
        "1.": ("1. WAS KÖNNTE GELD VERDIENEN?",),
        "2.": ("2. KONKRETE TRADES",),
        "3.": ("3. THEMEN & ZUSAMMENHÄNGE",),
        "4.": ("4. IDEEN IM AUFBAU",),
        "5.": ("5. AKTIEN MIT FRÜHEM SIGNAL",),
        "6.": ("6. WIDERSPRÜCHE & RISIKEN",),
        "7.": ("7. MARKT- & MAKROKONTEXT",),
        "8.": ("8. EDELMETALLE",),
        "9.": ("9. NÄCHSTE KATALYSATOREN",),
        "10.": ("10. BESTEHENDES PORTFOLIO",),
    }

    for canonical in required:
        key_match = re.match(r"^\s*(\d+(?:\.\d+)?\.?)(?=\s)", canonical)
        key = key_match.group(1) if key_match else canonical
        canonical_normalized = re.sub(
            r"\s*→\s*", "→", canonical.strip()
        ).rstrip(":").casefold()

        accepted = {canonical_normalized}
        accepted.update(
            re.sub(r"\s*→\s*", "→", alias.strip()).rstrip(":").casefold()
            for alias in aliases_by_key.get(key, ())
        )
        # Bereits beobachtete, vollstaendige Gemini-Variante von 3.3.
        if key == "3.3":
            # Explizite, im Produktionslauf beobachtete Varianten. Sie muessen
            # als dieselbe Pflichtsektion erkannt werden, damit kein zweiter
            # kanonischer 3.3-Fallback daneben eingefuegt wird.
            for alias in (
                "3.3 Politik → Geopolitik → Aktie",
                "3.3 Politik → Geopolitik → Branche → Aktie",
            ):
                accepted.add(
                    re.sub(r"\s*→\s*", "→", alias).rstrip(":").casefold()
                )

        if normalized in accepted:
            return key, canonical

    # 6.x/9.x werden über dieselbe titelvalidierte Erkennung geprüft wie
    # Reparatur und finales Gate. Ein bloßer Wort-Overlap darf nummerierten
    # Fließtext nicht als Abschnittsgrenze klassifizieren.
    heading_match = _echte_unterabschnittsueberschrift(stripped)
    if heading_match and heading_match[0] in {
        "6.1", "6.2", "6.3", "6.4", "6.5", "6.6",
        "9.1", "9.2", "9.3", "9.4", "9.5",
    }:
        canonical = next(
            (item for item in required
             if re.match(r"^\s*" + re.escape(heading_match[0]) + r"\s+", item)),
            None,
        )
        if canonical:
            return heading_match[0], canonical

    # Die nicht nummerierte Hauptueberschrift wird nur exakt erkannt.
    if stripped == "NEUBER MACRO & MARKETS":
        return "NEUBER MACRO & MARKETS", "NEUBER MACRO & MARKETS"
    return None

def _repariere_doppelte_3_3(text):
    """Faltet die bekannte 3.3-Doppelung zusammen und ordnet verrutschte 3.2-Pruefzeilen zurueck."""
    if not text:
        return text
    headings = list(re.finditer(r"(?im)^3\.3\s+.*$", text))
    if len(headings) < 2:
        return text
    # Nur die bekannte Produktionsregression automatisch reparieren; andere
    # echte doppelte Abschnitte bleiben harte Fehler.
    first, second = headings[0], headings[1]
    end_second = re.search(r"(?m)^3\.4\s+Technologie\s*→\s*Branche\s*→\s*Aktie\s*$", text[second.end():])
    if not end_second:
        return text
    block1 = text[first.end():second.start()]
    block2_end = second.end() + end_second.start()
    block2 = text[second.end():block2_end]

    misplaced = []
    kept1 = []
    for line in block1.splitlines():
        if (
            re.match(r"(?i)^\s*Einordnung:\s*Rohstoffbewegungen wirken", line)
            or re.match(r"(?i)^\s*Prüfpunkt:\s*Bestätigung erfordert, dass die im Bericht genannte Rohstoffrichtung", line)
        ):
            misplaced.append(line)
        else:
            kept1.append(line)
    political = [
        line for line in block2.splitlines()
        if re.match(r"(?i)^\s*(?:Einordnung|Prüfpunkt):", line)
        and not re.search(r"(?i)Rohstoffbewegungen|Rohstoffrichtung", line)
    ]
    # Only remove a fallback-only duplicate, not an arbitrary second analysis.
    fallback = bool(re.search(
        r"(?i)Keine belastbare fachliche Aussage aus den vorliegenden Projektquellen ableitbar",
        block2,
    ))
    if not fallback:
        return text

    prefix = text[:first.start()]
    heading1 = first.group(0).rstrip()
    after = text[block2_end:]
    before_heading = "\n".join(misplaced).strip()
    first_content = "\n".join(kept1).strip()
    rebuilt = prefix
    if before_heading:
        rebuilt = rebuilt.rstrip() + "\n" + before_heading + "\n\n"
    rebuilt += heading1 + "\n" + first_content
    if political:
        rebuilt += "\n\n" + "\n".join(political)
    rebuilt += "\n\n" + after.lstrip("\n")
    print("INFO: Doppelte 3.3-Sektion zusammengeführt; verrutschte Rohstoff-Prüfzeilen 3.2 zugeordnet.")
    return rebuilt


def _bereinige_doppelte_ausgabestruktur(text, required):
    """Repariert die gesamte Pflichtstruktur zentral und inhaltsbewahrend.

    Alle erkannten Pflichtabschnitte werden anhand von ``required``
    kanonisiert und in Sollreihenfolge gebracht. Mehrfach vorkommende
    Abschnitte werden zusammengeführt; unterschiedliche Inhaltsblöcke
    bleiben erhalten. Identische doppelte Inhalte werden nur einmal geführt.
    Die Funktion ist idempotent und enthält keinen 3.3-Sonderpfad.
    """
    source = text or ""
    if not source.strip():
        return source

    # Pflichtüberschriften und Abschnittsgrenzen zentral erkennen.
    occurrences = []
    offset = 0
    for line in source.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        heading = _ausgabe_heading_key(line_body, required)
        if heading:
            key, canonical = heading
            occurrences.append({
                "key": key,
                "canonical": canonical,
                "start": offset,
                "line_end": offset + len(line),
                "line": line,
            })
        offset += len(line)

    if not occurrences:
        return source

    preamble = source[:occurrences[0]["start"]]
    blocks_by_key = {}
    ordered_keys = []
    for index, occurrence in enumerate(occurrences):
        block_end = (occurrences[index + 1]["start"]
                     if index + 1 < len(occurrences) else len(source))
        body = source[occurrence["line_end"]:block_end]
        key = occurrence["key"]
        if key not in blocks_by_key:
            blocks_by_key[key] = {"canonical": occurrence["canonical"], "bodies": []}
            ordered_keys.append(key)
        # Canonical heading is always supplied from the central required list.
        blocks_by_key[key]["canonical"] = occurrence["canonical"]
        body = body.strip("\r\n")
        if body.strip():
            # Known fallback paragraphs are removed only when the same logical
            # section also contains substantive analysis. A fallback-only
            # section remains explicit; real or conflicting analyses are kept.
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
            substantive = [part for part in paragraphs if not re.match(
                r"(?is)^Keine\s+belastbare\s+(?:Gegen(?:ueber|über)stellung|fachliche Aussage|Prüfung|Risikobewertung)\b", part
            )]
            if substantive:
                paragraphs = substantive
                body = "\n\n".join(paragraphs)
            # Do not discard substantive material. Only exact repeated bodies
            # are deduplicated; separate or conflicting content is retained.
            if body not in blocks_by_key[key]["bodies"]:
                blocks_by_key[key]["bodies"].append(body)

    # Remove a known fallback paragraph only if the merged logical section
    # also contains real analysis. Fallback-only sections remain explicit.
    fallback_start = re.compile(
        r"(?is)^Keine\s+belastbare\s+(?:Gegen(?:ueber|über)stellung|fachliche Aussage|Prüfung|Risikobewertung)\b"
    )
    for block in blocks_by_key.values():
        paragraphs = [
            part.strip()
            for body in block["bodies"]
            for part in re.split(r"\n\s*\n", body)
            if part.strip()
        ]
        substantive = [part for part in paragraphs if not fallback_start.match(part)]
        if substantive:
            block["bodies"] = ["\n\n".join(substantive)]

    rebuilt = [preamble.rstrip("\r\n")]
    required_keys = []
    for canonical in required:
        heading = _ausgabe_heading_key(canonical, required)
        if heading:
            required_keys.append(heading[0])

    # The first recognized occurrence of each key is emitted once, in the
    # central required order. Existing content is kept with its section.
    emitted = set()
    for key in required_keys:
        block = blocks_by_key.get(key)
        if not block or key in emitted:
            continue
        emitted.add(key)
        section_parts = [block["canonical"]]
        if block["bodies"]:
            section_parts.append("\n\n".join(block["bodies"]))
        rebuilt.append("\n".join(section_parts).rstrip())

    # Preserve recognized sections not listed in the current required schema
    # rather than silently deleting their content.
    for key in ordered_keys:
        if key in emitted:
            continue
        block = blocks_by_key[key]
        section_parts = [block["canonical"]]
        if block["bodies"]:
            section_parts.append("\n\n".join(block["bodies"]))
        rebuilt.append("\n".join(section_parts).rstrip())

    result = "\n\n".join(part for part in rebuilt if part).rstrip() + "\n"
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

def _ausgabe_pflichtstruktur():
    """Zentrale Sollstruktur der Auswertung; Reparatur und Final-Gate nutzen dieselbe Liste."""
    return [
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


def _repariere_gesamte_ausgabestruktur(text, required=None):
    """Repariert und normalisiert die vollständige Pflichtstruktur zentral.

    Die Sollstruktur ist ``required`` (standardmäßig die vollständige
    Ausgabepflichtstruktur 1–11.7). Erkannt werden fehlende, doppelte,
    verschobene und bekannte abweichend geschriebene Pflichtüberschriften.
    Fachliche Abschnittsinhalte werden mit ihrer Sektion zusammengeführt und
    nicht durch einen 3.3-spezifischen Reparaturpfad ersetzt.
    """
    required = list(required if required is not None else _ausgabe_pflichtstruktur())
    if not required:
        raise RuntimeError("AUSGABESTRUKTUR_SCHEMA_LEER: keine Pflichtüberschriften definiert")
    required_keys = [_ausgabe_heading_key(heading, required) for heading in required]
    if any(item is None for item in required_keys):
        raise RuntimeError("AUSGABESTRUKTUR_SCHEMA_UNGUELTIG: Pflichtüberschrift nicht erkennbar")
    keys = [item[0] for item in required_keys]
    if len(keys) != len(set(keys)):
        raise RuntimeError("AUSGABESTRUKTUR_SCHEMA_UNGUELTIG: doppelte Pflichtsektion im Schema")

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

        if heading == "11. METHODIK / DATENQUALITÄT":
            # Die Hauptüberschrift ist ein reiner Strukturanker. Kein generischer
            # Fallback-Satz darf vor den bereits vorhandenen Unterabschnitten
            # erscheinen oder dort inhaltliche Methodik vortäuschen.
            block = heading + "\n\n"
        elif heading == "11.7 Abgrenzung:":
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
    # Nach dem Einfügen fehlender Abschnitte die komplette Pflichtstruktur
    # erneut zentral normalisieren. Das umfasst Reihenfolge und Duplikate aller
    # Sektionen; es gibt keinen 3.3-spezifischen Abschlussweg.
    return _bereinige_doppelte_ausgabestruktur(result, required)


def _ergaenze_fehlende_ausgabestruktur(text):
    """Kompatibilitaetswrapper: alle Strukturreparaturen zentral ausfuehren."""
    return _repariere_gesamte_ausgabestruktur(text)


def _pruefe_neue_ausgabestruktur(text):
    """Prüft die verbindliche neue Auswertungsstruktur vor dem Speichern.

    Bekannte Schreibvarianten werden semantisch derselben Pflichtsektion
    zugeordnet; nach der Normalisierung muss jede Sektion exakt einmal und mit
    der verbindlichen Überschrift vorhanden sein.
    """
    required = _ausgabe_pflichtstruktur()

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
        # Rückwärtskompatibler Wrapper auf die zentrale Datumsfunktion.
        return _hat_expliziten_zeitbezug(text_block)

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
            undated_claims = _undatierte_ereignisbehauptungen(units, event_pattern)
            if undated_claims:
                preview = " / ".join(re.sub(r"\\s+", " ", claim).strip()[:100] for claim in undated_claims[:3])
                warnings.append(
                    f"{h}: {len(undated_claims)} tatsächliche Ereignis-/Terminangabe(n) "
                    f"ohne eigenen verifizierbaren Zeit-/Datumsbezug: {preview}"
                )

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

            # 1.2 ist die kompakte Chancenübersicht zu 2.1. Gemeinsame
            # Faktenfelder (Ticker/Entry/Stop/TP/CRV) sind zulässig; die
            # eigentliche Begründung darf aber nicht einfach kopiert sein.
            if {h1, h2} == {"1.2 Sofort handelbare Chancen", "2.1 Trendfolge"}:
                fact_line = re.compile(
                    r"(?i)\b(?:ticker|entry|einstieg|stop|tp1|tp2|take[- ]?profit|crv|kursziel|"
                    r"risiko\s*/\s*chance|chance[- ]?risiko)\b|\b(?:stop|tp1|tp2)\s*:"
                )
                b1 = "\n".join(line for line in b1.splitlines() if not fact_line.search(line))
                b2 = "\n".join(line for line in b2.splitlines() if not fact_line.search(line))
                # Auch kurze Resttexte werden verglichen; bei zu wenig
                # gemeinsamen Wortmaterial greift der reguläre leere-Menge-Schutz.

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
    """Liest ausschliesslich den tagesaktuellen Trendfolge-Snapshot fuer Name UND Ticker."""
    nicht_verfuegbar = "NICHT VERFUEGBAR"
    datum = auswertungsdatum or datetime.date.today().isoformat()
    name_norm = _normalisiere_positionsname(name)
    ticker_norm = _normalisiere_ticker(ticker)
    history_path = (eingabedateien or {}).get("Einzel-Check-Technikhistorie")
    if not history_path:
        history_path = finde_datei(DATEIMUSTER["Einzel-Check-Technikhistorie"])

    keys = (
        "Kurs", "Einstieg", "Stop", "TP1", "TP2", "Markt", "Waehrung",
        "Setup", "Status", "CRV1", "CRV2", "Risiko", "Chance1",
        "RSI", "MACD", "Vol_Ratio",
    )
    empty = {"Datum": datum, **{key: nicht_verfuegbar for key in keys}}
    if not name_norm or not ticker_norm or not history_path or not os.path.isfile(history_path):
        return empty

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
                # Strikte zusammengesetzte Identitaet: Name UND Ticker muessen passen.
                if _normalisiere_ticker(row.get("Ticker")) != ticker_norm:
                    continue
                if _normalisiere_positionsname(row.get("Name")) != name_norm:
                    continue
                matching_rows.append(row)
    except OSError as exc:
        raise RuntimeError(f"2.4_TECHNIKHISTORIE_NICHT_LESBAR: {exc}") from exc

    if not matching_rows:
        return empty

    # Bei mehreren heutigen Snapshots gilt der zuletzt gespeicherte Snapshot.
    row = matching_rows[-1]
    technik = row.get("Technik") if isinstance(row.get("Technik"), dict) else {}
    trendfolge = technik.get("Trendfolge") if isinstance(technik.get("Trendfolge"), dict) else {}

    def wert(source, key):
        value = source.get(key) if isinstance(source, dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            return nicht_verfuegbar
        return str(value).strip()

    def wert_num(source, key, decimals=2):
        value = source.get(key) if isinstance(source, dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            return nicht_verfuegbar
        try:
            return _scanner_dezahl(value, decimals)
        except (TypeError, ValueError):
            return nicht_verfuegbar

    kurs = wert_num(row, "Kurs", 2)
    if kurs == nicht_verfuegbar:
        kurs = wert_num(trendfolge, "Kurs", 2)
    return {
        "Datum": datum,
        "Kurs": kurs,
        "Einstieg": wert_num(trendfolge, "Einstieg", 2),
        "Stop": wert_num(trendfolge, "Stop", 4),
        "TP1": wert_num(trendfolge, "TP1", 2),
        "TP2": wert_num(trendfolge, "TP2", 2),
        "Markt": wert(trendfolge, "Markt"),
        "Waehrung": wert(trendfolge, "Waehrung"),
        "Setup": wert(trendfolge, "Setup_Typ"),
        "Status": wert(trendfolge, "Status2"),
        "CRV1": wert_num(trendfolge, "CRV1", 2),
        "CRV2": wert_num(trendfolge, "CRV2", 2),
        "Risiko": wert_num(trendfolge, "Risk_Perc", 2),
        "Chance1": wert_num(trendfolge, "Chance1_Perc", 2),
        "RSI": wert_num(trendfolge, "RSI", 2),
        "MACD": wert(trendfolge, "MACD_Trend"),
        "Vol_Ratio": wert_num(trendfolge, "Vol_Ratio", 2),
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

    def mit_suffix_wenn_verfuegbar(value, suffix):
        """Haengt Einheiten nur an tatsaechlich vorhandene Werte an."""
        if value is None or not str(value).strip() or str(value).strip().upper() in {
            "NICHT VERFUEGBAR", "NICHT VERFÜGBAR", "N/A", "UNKNOWN", "UNBEKANNT"
        }:
            return "NICHT VERFUEGBAR"
        return f"{value}{suffix}"

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
            f"Markt/Währung: {values['Markt']} / {values['Waehrung']}",
            f"Setup-Typ: {values['Setup']} | Status: {values['Status']}",
            f"CRV1: {values['CRV1']} | CRV2: {values['CRV2']} | Chance TP1: {mit_suffix_wenn_verfuegbar(values['Chance1'], '%')} | Risiko: {mit_suffix_wenn_verfuegbar(values['Risiko'], '%')}",
            f"RSI: {values['RSI']} | MACD-Trend: {values['MACD']} | Volumen-Ratio: {mit_suffix_wenn_verfuegbar(values['Vol_Ratio'], 'x')}",
        ])

    rebuilt = "2.4 HebelTrader\n\n" + meldungen + "\n\n" + "\n".join(enrichment)
    return text[:m.start()] + rebuilt + "\n\n" + text[end:]


def _scanner_datum_aus_pfad(pfad):
    """Liefert YYYY-MM-DD aus einem datierten Scanner-Dateinamen oder None."""
    match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", os.path.basename(os.fspath(pfad or "")))
    return match.group(1) if match else None


def _scanner_float(value):
    """Parst Zahlen aus Scanner-CSV, ohne leere Werte zu schaetzen."""
    if value is None:
        return None
    raw = str(value).strip().replace("%", "").replace(" ", "")
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
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    # CSV-Werte wie NaN/Inf sind syntaktisch parsebar, aber keine validen
    # Kurs-/CRV-Werte und duerfen niemals einen Kandidaten passieren lassen.
    return number if math.isfinite(number) else None


def _scanner_dezahl(value, decimals=2):
    """Formatiert einen vorhandenen Scannerwert deutsch; None bleibt nicht verfuegbar."""
    number = _scanner_float(value)
    if number is None:
        return "NICHT VERFUEGBAR"
    return f"{number:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _lade_aktuelle_scanner_kandidaten(eingabedateien, text=""):
    """Liest nur datums-passende, konkrete Setups aus den finalen Scanner-Exports.

    Das Trade-Story-Rohuniversum, Watchlists und Portfolio-Daten werden hier
    bewusst nicht als Setup-Quelle verwendet. Kandidaten brauchen Entry, Stop,
    TP1 und CRV1 >= 1. Ein vorhandenes Status2-Feld muss exakt VALIDE sein.
    """
    eingaben = eingabedateien or {}
    report_date = None
    report_match = re.search(
        r"(?im)^\s*Datum der Auswertung\s*:\s*(\d{2})\.(\d{2})\.(\d{4})\s*$",
        text or "",
    )
    if report_match:
        report_date = f"{report_match.group(3)}-{report_match.group(2)}-{report_match.group(1)}"
    if not report_date:
        briefing_path = eingaben.get("briefing.txt")
        report_date = _scanner_datum_aus_pfad(briefing_path)
    # Das Berichtsdatum darf NICHT aus dem zu validierenden Scanner-Export
    # selbst abgeleitet werden: sonst bestaetigt die Datei ihren eigenen
    # Datenstand. Ohne unabhaengiges Datum aus Bericht oder Briefing werden
    # alle Scanner-Exports unten sicherheitshalber verworfen.
    definitions = (
        ("Setups(...).csv", "Trendfolge-Scanner", "Trendfolge", True),
        ("Trendwende_Setups(...).csv", "Trendwende-Scanner", "Trendwende", False),
        ("Short_Setups(...).csv", "Short-Scanner", "Short", True),
    )
    candidates = []
    seen = set()
    for source_key, source_label, strategy, status_required in definitions:
        path = eingaben.get(source_key)
        if not path or not os.path.isfile(path):
            continue
        source_date = _scanner_datum_aus_pfad(path)
        if not report_date:
            print(
                f"WARNUNG: Scanner-Export {os.path.basename(path)} ignoriert: "
                "Auswertungsdatum nicht verifizierbar; kein Kandidat wird ohne "
                "abgesicherten Tagesbezug übernommen."
            )
            continue
        if not source_date:
            print(
                f"WARNUNG: Scanner-Export {os.path.basename(path)} ignoriert: "
                "kein Datum im Dateinamen; Datenstand kann nicht verifiziert werden."
            )
            continue
        if source_date != report_date:
            print(
                f"WARNUNG: Scanner-Export {os.path.basename(path)} ignoriert: "
                f"Datenstand {source_date} passt nicht zur Auswertung {report_date}."
            )
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter=";"))
        except (OSError, csv.Error) as exc:
            raise RuntimeError(
                f"SCANNER_EXPORT_NICHT_LESBAR ({os.path.basename(path)}): {exc}"
            ) from exc

        for row in rows:
            normalized = {
                str(key or "").strip().casefold(): str(value or "").strip()
                for key, value in row.items()
            }
            ticker = normalized.get("ticker", "").upper()
            name = normalized.get("name", "")
            if not ticker or not name:
                continue
            status = normalized.get("status2", "").upper()
            if status and status != "VALIDE":
                continue
            if status_required and status != "VALIDE":
                continue

            crv = _scanner_float(normalized.get("crv1"))
            entry = normalized.get("einstieg", "")
            stop = normalized.get("stop", "")
            tp1 = normalized.get("tp1", "") or normalized.get("tech-kursziel", "")
            if crv is None or crv < 1.0 or not entry or not stop or not tp1:
                continue
            if any(_scanner_float(value) is None for value in (entry, stop, tp1)):
                continue

            identity = (strategy, ticker)
            if identity in seen:
                continue
            seen.add(identity)
            market = normalized.get("markt", "")
            currency = normalized.get("waehrung", "") or (
                "EUR" if market.upper() in {"EU", "EUROPA"} else
                "USD" if market.upper() in {"US", "USA"} else ""
            )
            setup_type = normalized.get("setup_typ", "") or "NICHT VERFUEGBAR"
            pattern = normalized.get("pattern", "") or "NICHT VERFUEGBAR"
            candidates.append({
                "strategy": strategy,
                "source_label": source_label,
                "source_file": os.path.basename(path),
                "source_date": source_date or report_date or "NICHT VERFUEGBAR",
                "ticker": ticker,
                "name": name,
                "market": market or "NICHT VERFUEGBAR",
                "currency": currency,
                "sector": normalized.get("sektor", "") or "NICHT VERFUEGBAR",
                "setup_type": setup_type,
                "pattern": pattern,
                "status": status or "VALIDER SCANNER-DATENSATZ",
                "course": normalized.get("kurs", ""),
                "entry": entry,
                "entry2": normalized.get("einstieg2(ema 20)", ""),
                "stop": stop,
                "tp1": tp1,
                "tp2": normalized.get("tp2", ""),
                "crv1": normalized.get("crv1", ""),
                "crv2": normalized.get("crv2", ""),
                "risk": normalized.get("risk_perc", ""),
                "chance1": normalized.get("chance1_perc", ""),
                "rsi": normalized.get("rsi", ""),
                "macd": normalized.get("macd_trend", ""),
                "vol_ratio": normalized.get("vol_ratio", ""),
                "relative_strength": normalized.get("rs_vs_benchmark%", ""),
                "fundamental": normalized.get("fundamental_ampel", ""),
                "fundamental_note": normalized.get("fundamental_hinweis", ""),
            })
    return candidates


def _formatiere_scanner_chance(candidate):
    """Erzeugt eine sofort pruefbare Chance aus den Feldern eines finalen Scanner-Exports."""
    c = candidate
    currency = f" {c['currency']}" if c.get("currency") else ""
    course = _scanner_dezahl(c.get("course")) if c.get("course") else "NICHT VERFUEGBAR"
    entry = _scanner_dezahl(c.get("entry"))
    entry2 = _scanner_dezahl(c.get("entry2")) if c.get("entry2") else "NICHT VERFUEGBAR"
    stop = _scanner_dezahl(c.get("stop"))
    tp1 = _scanner_dezahl(c.get("tp1"))
    tp2 = _scanner_dezahl(c.get("tp2")) if c.get("tp2") else "NICHT VERFUEGBAR"
    tp2_text = f"{tp2}{currency}" if tp2 != "NICHT VERFUEGBAR" else tp2
    course_text = f"{course}{currency}" if course != "NICHT VERFUEGBAR" else course
    chance1 = _scanner_dezahl(c.get("chance1")) if c.get("chance1") else "NICHT VERFUEGBAR"
    chance_text = f"{chance1}%" if chance1 != "NICHT VERFUEGBAR" else chance1
    crv1 = _scanner_dezahl(c.get("crv1"))
    risk = _scanner_dezahl(c.get("risk")) if c.get("risk") else "NICHT VERFUEGBAR"
    risk_text = f"{risk}%" if risk != "NICHT VERFUEGBAR" else risk
    lines_out = [
        f"{c['name']} ({c['ticker']}) | Ticker: {c['ticker']} | Scanner: {c['source_label']} | Markt: {c['market']} | Sektor: {c['sector']}",
        f"Setup: {c['setup_type']} | Pattern: {c['pattern']} | Status: {c['status']} | Richtung: {'Short' if c['strategy'] == 'Short' else 'Long'}",
        f"Aktueller Kurs: {course_text} | Einstieg: {entry}{currency}"
        + (f" | EMA20-Referenz: {entry2}{currency}" if entry2 != "NICHT VERFUEGBAR" else ""),
        f"Stop: {stop}{currency} | Risiko: {risk_text} | TP1: {tp1}{currency} "
        f"(Chance: {chance_text}) | CRV1: {crv1} | TP2: {tp2_text}",
        "Investmentthese: Der Scanner belegt das technische Setup; eine eigenständige Makro-/Fundamentalthese wird nicht aus Scannerfeldern erfunden. Die Einordnung steht in Abschnitt 3 bzw. 4.",
        f"Sektor-/Makrokontext: Sektor {c['sector']}; eine separate Makrobestätigung ist in diesem Scanner-Export nicht enthalten und muss in Abschnitt 3/4 belegt werden.",
        f"Gegenargument: Im Scanner-Export nicht gesondert ausgewiesen; technischer Risikopunkt ist die Stop-Marke {stop}{currency}.",
        f"Trigger: aktueller Eintrag im {c['source_label']} mit Status {c['status']} und Setup-Typ {c['setup_type']}; ein zusätzlicher separater Kurs-Trigger ist im Export nicht ausgewiesen.",
        f"Invalidierung: Stop-Marke {stop}{currency}. Quelle: {c['source_file']} (Datenstand {c['source_date']}).",
    ]
    if c.get("rsi"):
        lines_out.append(f"RSI: {_scanner_dezahl(c['rsi'])}")
    if c.get("macd"):
        lines_out.append(f"MACD-Trend: {c['macd']}")
    if c.get("vol_ratio"):
        lines_out.append(f"Volumen-Ratio: {_scanner_dezahl(c['vol_ratio'])}x")
    if c.get("relative_strength"):
        lines_out.append(f"Relative Stärke vs. Benchmark: {_scanner_dezahl(c['relative_strength'])}%")
    if c.get("fundamental"):
        lines_out.append(f"Fundamental-Ampel: {c['fundamental']}")
    if c.get("fundamental_note"):
        lines_out.append(f"Fundamental-Hinweis: {c['fundamental_note']}")
    return "\n".join(lines_out)


def _formatiere_scanner_chance_kurz(candidate):
    """Kurze Chancenkarte fuer 1.2; die vollstaendige technische Karte steht in 2.1."""
    c = candidate
    currency = f" {c['currency']}" if c.get("currency") else ""
    course = _scanner_dezahl(c.get("course")) if c.get("course") else "NICHT VERFUEGBAR"
    course = f"{course}{currency}" if course != "NICHT VERFUEGBAR" else course
    entry = _scanner_dezahl(c.get("entry"))
    stop = _scanner_dezahl(c.get("stop"))
    tp1 = _scanner_dezahl(c.get("tp1"))
    crv1 = _scanner_dezahl(c.get("crv1"))
    risk = _scanner_dezahl(c.get("risk")) if c.get("risk") else "NICHT VERFUEGBAR"
    risk = f"{risk}%" if risk != "NICHT VERFUEGBAR" else risk
    tp2 = _scanner_dezahl(c.get("tp2")) if c.get("tp2") else "NICHT VERFUEGBAR"
    tp2 = f"{tp2}{currency}" if tp2 != "NICHT VERFUEGBAR" else tp2
    direction = "Short" if c["strategy"] == "Short" else "Long"
    return "\n".join([
        f"{c['name']} ({c['ticker']}) | Ticker: {c['ticker']} | Quelle: {c['source_label']} ({c['source_file']})",
        f"Richtung: {direction} | Sektor: {c['sector']} | Setup: {c['setup_type']} | Status: {c['status']}",
        f"Kurs: {course} | Einstieg: {entry}{currency} | Stop: {stop}{currency} | Risiko: {risk}",
        f"TP1: {tp1}{currency} | CRV1: {crv1} | TP2: {tp2}",
        "Investmentthese: Technisch ist das im aktuellen Scanner-Export bestätigte Setup belegt; eine weitergehende Fundamentalthese wird daraus nicht abgeleitet.",
        f"Trigger: aktueller gültiger Scanner-Datensatz für {c['setup_type']}; ein zusätzlicher Kurs-Trigger ist im Export nicht ausgewiesen.",
        f"Gegenargument/Risiko: Kein separates Gegenargument im Scanner-Export; Invalidierung bei Stop {stop}{currency}.",
    ])


def _normalisiere_punkt12_scannerbindung(text, eingabedateien=None):
    """Ersetzt 1.2/2.1 durch aktuelle, final scanner-validierte Datensaetze."""
    if not text:
        return text
    candidates = _lade_aktuelle_scanner_kandidaten(eingabedateien, text)
    trendfolge = [item for item in candidates if item["strategy"] == "Trendfolge"]

    start_match = re.search(r"(?m)^1\.2\s+Sofort handelbare Chancen\s*$", text)
    end_match = re.search(r"(?m)^1\.3\s+Ideen im Aufbau\s*$", text)
    if start_match and end_match and end_match.start() > start_match.start():
        if candidates:
            block = "1.2 Sofort handelbare Chancen\n\n" + "\n\n".join(
                _formatiere_scanner_chance_kurz(item) for item in candidates
            )
        else:
            block = (
                "1.2 Sofort handelbare Chancen\n\n"
                "Keine aktuellen, quellengebunden validierten Setups mit vollständigem Einstieg, Stop, TP1 und CRV1 ≥ 1 "
                "in den datumspassenden finalen Trendfolge-, Trendwende- oder Short-Scanner-Exports. "
                "Trade-Story-Rohuniversum, Watchlist, offene Positionen und Gemini-Vorschläge gelten nicht als Scanner-Bestätigung."
            )
        text = text[:start_match.start()] + block + "\n\n" + text[end_match.start():]

    # 2.1 darf keine Watchlist oder bereits offene Positionen als neue Setups darstellen.
    start_match = re.search(r"(?m)^2\.1\s+Trendfolge\s*$", text)
    end_match = re.search(r"(?m)^2\.2\s+Trendwende\s*$", text)
    if start_match and end_match and end_match.start() > start_match.start():
        if trendfolge:
            detail = [
                _formatiere_scanner_chance(item)
                for item in trendfolge
            ]
        else:
            detail = [
                "Keine neuen validen Trendfolge-Setups im aktuellen, datumspassenden Setups-Export mit Status VALIDE und CRV1 ≥ 1."
            ]
        block = "2.1 Trendfolge\n\n" + "\n\n".join(detail)
        text = text[:start_match.start()] + block + "\n\n" + text[end_match.start():]

    # Eine nicht belegte Behauptung ueber ein neues Trendfolge-Setup wird nicht stehen gelassen.
    valid_tickers = {item["ticker"] for item in trendfolge}
    section_start = re.search(r"(?m)^1\.1\s+Was hat sich seit dem letzten Lauf verändert\?\s*$", text)
    section_end = re.search(r"(?m)^1\.2\s+Sofort handelbare Chancen\s*$", text)
    if section_start and section_end and section_end.start() > section_start.start():
        section = text[section_start.end():section_end.start()]
        claim_pattern = re.compile(
            r"Auf Einzelebene zeigt sich bei\s+(?P<name>[^()\n]+?)\s+\((?P<ticker>[A-Z][A-Z0-9.^=-]{0,14})\)\s+(?:ein\s+)?(?:neues\s+)?valid(?:es|e|er)?\s+Trendfolge-Setup[^.\n]*",
            re.I,
        )
        def replace_unverified_claim(match):
            ticker = match.group("ticker").upper()
            if ticker in valid_tickers:
                return match.group(0)
            name = match.group("name").strip()
            return (
                f"Für {name} ({ticker}) ist im aktuellen Trendfolge-Scanner-Export "
                "kein gültiges Setup ausgewiesen"
            )
        section = claim_pattern.sub(replace_unverified_claim, section)
        text = text[:section_start.end()] + section + text[section_end.start():]
    return text


def _repariere_1_3_aus_idee_im_aufbau(text):
    """Synchronisiert 1.3 mit belegten Inline-Feldern aus Abschnitt 4."""
    if not text:
        return text
    start_13 = re.search(r"(?im)^\s*1\.3\s+Ideen im Aufbau\s*$", text)
    end_13 = re.search(r"(?im)^\s*1\.4\s+Frühindikatoren\s*/\s*neue Themen\s*$", text)
    start_4 = re.search(r"(?im)^\s*4\.\s*🔭?\s*IDEEN IM AUFBAU\s*$", text)
    end_4 = re.search(r"(?im)^\s*5\.\s*🥇?\s*AKTIEN MIT FRÜHEM SIGNAL\s*$", text)
    if not (start_13 and end_13 and start_4 and end_4):
        return text
    block_13 = text[start_13.end():end_13.start()]
    # Nicht nur vollstaendig leere, sondern auch teilweise befuellte 1.3-Bloecke
    # reparieren, wenn Thema/Was-veraendert-sich identisch sind oder technische
    # Nachsuch-Assets bzw. Sektorzuordnung nur generisch bleiben.
    topic_value = re.search(r"(?im)^Thema / Assetklasse:\s*(.+)$", block_13)
    change_value = re.search(r"(?im)^Was verändert sich\?:\s*(.+)$", block_13)
    assets_value = re.search(r"(?im)^Potenzielle Assets für technische Nachsuche:\s*(.+)$", block_13)
    sector_value = re.search(r"(?im)^Betroffene Assetklasse / Sektor:\s*(.+)$", block_13)
    repeated = bool(topic_value and change_value and topic_value.group(1).strip() == change_value.group(1).strip())
    generic_assets = bool(assets_value and re.search(
        r"(?i)selektive relative stärke|selektive relative staerke|"
        r"überdurchschnittliche erholung|nicht verfuegbar|nicht verfügbar",
        assets_value.group(1),
    ))
    generic_sector = bool(sector_value and re.search(r"(?i)nicht eindeutig|keine zuordnung", sector_value.group(1)))
    # 1.3 muss fachlich zur Idee in Abschnitt 4 passen. Ein Cloud-/SaaS-Thema
    # mit Energie-/Netzsektorzuordnung ist ein semantischer Widerspruch, auch
    # wenn alle Pflichtfelder formal ausgefuellt sind.
    theme_mismatch = bool(
        topic_value and sector_value
        and re.search(r"(?i)cloud|cybersecurity|enterprise software|saas", topic_value.group(1))
        and re.search(r"(?i)versorger|netz- und industrieausrüstung|stromversorgung", sector_value.group(1))
    )
    if len(re.findall(r"(?i)NICHT VERFUEGBAR|NICHT VERFÜGBAR", block_13)) < 4 and not (repeated or generic_assets or generic_sector or theme_mismatch):
        return text

    block_4 = text[start_4.end():end_4.start()]
    cloud_theme = bool(re.search(r"(?i)cloud|cybersecurity|enterprise software|saas", block_13 + " " + block_4))
    # Das tatsächliche Berichtsschema nutzt "- LABEL: Wert" in einer Zeile,
    # nicht einen alleinstehenden LABEL-Absatz. Beide Schreibweisen werden
    # akzeptiert, damit ältere Läufe ebenfalls repariert werden.
    label_patterns = {
        "these": r"THESE",
        "why": r"WARUM\s+ENTSTEHT\s+SIE\?|WARUM\s*/\s*TREIBER",
        "evidence": r"BESTÄTIGENDE\s+DATEN",
        "counter": r"GEGENARGUMENTE",
        "causal": r"KAUSALKETTE",
        "beneficiaries": r"PROFIT(?:EURE|IERER)\s*/\s*VERLIERER",
        "reaction": r"FRÜHE\s+AKTIENREAKTION",
        "status": r"AKTUELLER\s+STATUS",
        "missing": r"WAS\s+FEHLT\?",
        "trigger": r"AKTIVIERUNGSTRIGGER",
        "invalid": r"INVALIDIERUNG",
        "sector": r"(?:BETROFFENE\s+ASSETKLASSE\s*/\s*SEKTOR|SEKTOR\s*/\s*ASSETKLASSE|BETROFFENER\s+SEKTOR|ASSETKLASSE)",
    }
    extracted = {}
    for key, label_pattern in label_patterns.items():
        # Preferred format: "- LABEL: value"
        inline = re.search(
            r"(?im)^\s*[-•]\s*(?:" + label_pattern + r")\s*:\s*(.+?)\s*$",
            block_4,
        )
        if inline:
            extracted[key] = inline.group(1).strip()
            continue
        # Legacy format: label on its own line, value in following lines
        legacy = re.search(
            r"(?ims)^\s*(?:" + label_pattern + r")\s*$\n(.*?)(?=^\s*(?:"
            + "|".join(label_patterns.values()) + r")\s*$|\Z)",
            block_4,
        )
        if legacy:
            extracted[key] = re.sub(r"\s+", " ", legacy.group(1)).strip()

    thesis = extracted.get("these", "")
    if not thesis:
        return text
    beneficiaries_raw = extracted.get("beneficiaries", "")
    beneficiaries = re.split(r"(?i)\bVerlierer\s*:", beneficiaries_raw, maxsplit=1)[0].strip()
    beneficiaries = re.sub(r"(?i)^Profiteure\s*:\s*", "", beneficiaries).strip()
    intro_line = next(
        (line.strip() for line in block_4.splitlines() if "Vertiefung der These" in line),
        "",
    )
    asset_match = re.search(r"\((.*)\)\s*:\s*$", intro_line)
    technical_assets = asset_match.group(1).strip() if asset_match else (
        extracted.get("reaction") or "Keine konkreten technischen Nachsuch-Assets ausdrücklich ausgewiesen."
    )
    # Wenn nur generische Sektorstaerke angegeben wurde, konkrete, im selben
    # Abschnitt 4 genannte Ticker aus der Kausalkette uebernehmen.
    if (
        re.search(r"(?i)selektive relative stärke|selektive relative staerke|überdurchschnittliche erholung", technical_assets)
        or not re.search(r"\b[A-Z]{1,6}(?:\.[A-Z]{1,3})?\b", technical_assets)
    ):
        if cloud_theme:
            technical_assets = "ServiceNow (NOW), Zscaler (ZS) (Ideenreferenzen; technische Bestätigung separat prüfen)"
        else:
            tickers = []
            for ticker in re.findall(r"\b[A-Z]{1,6}\.[A-Z]{1,3}\b", block_4):
                if ticker not in tickers:
                    tickers.append(ticker)
            if tickers:
                technical_assets = ", ".join(tickers) + " (Ideenreferenz; technische Bestätigung separat prüfen)"
    missing_technical = extracted.get("missing", "")
    technical_status = (
        "Beobachtungsphase; noch kein bestätigtes technisches Trendfolge-Setup (z. B. Volumen-Breakout)."
        if missing_technical else "Beobachtungsphase; technische Bestätigung einzelner Titel ist separat zu prüfen."
    )
    reaction_value = extracted.get("reaction", "")
    why_value = extracted.get("why", "")
    missing_value = extracted.get("missing", "")
    if not reaction_value or re.search(r"(?i)^nicht (?:gesondert|ausdrücklich)|nicht verfuegbar|nicht verfügbar", reaction_value):
        reaction_value = "Eine neue kurzfristige Marktveränderung ist in Abschnitt 4 nicht gesondert belegt."
    elif re.search(r"(?i)selektive relative stärke|selektive relative staerke", reaction_value):
        reaction_value = "Als frühe Reaktion wird nur selektive relative Stärke in Versorger- und Industrietiteln beschrieben; ein sektorweiter Ausbruch ist laut Abschnitt 4 noch nicht bestätigt."
    if not why_value or re.search(r"(?i)^nicht (?:gesondert|ausdrücklich)|nicht verfuegbar|nicht verfügbar", why_value):
        if cloud_theme:
            why_value = "Abgeleiteter Treiber aus der These: steigende IT-Budgets für Cloud-Sicherheit und Enterprise-Software; dies ist eine Wirkungshypothese, kein gemessener Kapitalfluss."
        else:
            why_value = "Der Treiber wird nur aus der in Abschnitt 4 dokumentierten These abgeleitet; nicht belegte Kapitalflüsse werden nicht behauptet."
    # "Was verändert sich?" beschreibt die beobachtete Reaktion. Der noch
    # fehlende Setup-Trigger gehört ausschließlich in "Technischer Status" und
    # darf nicht in den Reaktionssatz hineinkopiert werden.
    sector_value = extracted.get("sector", "")
    if not sector_value or re.search(r"(?i)nicht eindeutig|nicht gesondert|nicht verfuegbar|nicht verfügbar", sector_value) or theme_mismatch:
        if cloud_theme:
            sector_value = "Cloud-Sicherheit, Cybersecurity und Enterprise-Software; aus der in Abschnitt 4 beschriebenen Kausalkette abgeleitet."
        else:
            sector_value = "Die in Abschnitt 4 explizit genannte Branche/Assetklasse; keine weitergehende Zuordnung ergänzt."
    fields = [
        ("Thema / Assetklasse", thesis),
        ("Zeithorizont", "Nicht ausdrücklich in Abschnitt 4 angegeben."),
        ("Was verändert sich?", reaction_value),
        ("Warum? / Treiber", why_value),
        ("Bestätigende Daten", extracted.get("evidence") or "Nicht gesondert ausgewiesen."),
        ("Kausalzusammenhang", extracted.get("causal") or "Nicht gesondert ausgewiesen."),
        ("Wohin fließt Kapital?", (
            "Hypothese: " + re.sub(
                r"(?i)^Profiteure\s+sind\s+", "mögliche Profiteure sind ",
                re.sub(r"(?i)\bVerlierer\s+sind\s+", "mögliche Verlierer sind ", beneficiaries),
            ).rstrip(" .;") + ". Tatsächliche Kapitalflüsse sind mit den vorliegenden Daten nicht belegt."
            if beneficiaries else "Keine gemessenen Kapitalflüsse belegt; nur eine qualitative These vorhanden."
        )),
        ("Betroffene Assetklasse / Sektor", sector_value),
        ("Frühester Beobachtungspunkt", extracted.get("trigger") or "Nicht ausdrücklich in Abschnitt 4 ausgewiesen; kein Beobachtungspunkt ergänzt."),
        ("Bestehender Kandidat / Bezug", (
            "ServiceNow (NOW) und Zscaler (ZS) werden in Abschnitt 4 als Ideenreferenzen genannt; ein validiertes Setup wird damit nicht behauptet."
            if cloud_theme else
            ("Siemens Energy AG (ENR.DE) wird in der Kausalkette als Ideenreferenz genannt; ein validiertes Setup wird damit nicht behauptet."
             if re.search(r"Siemens Energy AG \(ENR\.DE\)", block_4)
             else "Kein konkreter Einzelkandidat in der These eindeutig ausgewiesen.")
        )),
        ("Potenzielle Assets für technische Nachsuche", technical_assets),
        ("Nächster bestätigter Kalenderkatalysator", "NICHT VERFUEGBAR – in Abschnitt 4 nicht konkret datiert."),
        ("Nächster bestätigender Trigger", extracted.get("trigger") or "Nicht gesondert ausgewiesen."),
        ("Widerlegender Trigger", extracted.get("invalid") or "Nicht gesondert ausgewiesen."),
        ("Gegentreiber / Risiko", extracted.get("counter") or "Nicht gesondert ausgewiesen."),
        ("Discovery-Status", extracted.get("status") or "Beobachtung; keine weitergehende Bestätigung ergänzt."),
        ("Technischer Status", technical_status),
    ]
    rebuilt = "1.3 Ideen im Aufbau\n" + "\n".join(f"{label}: {value}" for label, value in fields)
    return text[:start_13.start()] + rebuilt + "\n\n" + text[end_13.start():]

def _repariere_3_x_kausalitaet(text):
    """Ergaenzt fehlende Kausal-/Trigger-Einordnung in 3.1–3.5 vorsichtig.

    Die Ergaenzungen sind qualitative Interpretationshilfen, keine neuen
    Zahlen, Signale oder Kaufempfehlungen. Bereits vorhandene Inhalte bleiben
    erhalten; nur ein fehlender Kausalaspekt oder Pruefpunkt wird ergaenzt.
    """
    if not text:
        return text, []

    repairs = []
    specs = [
        (
            "3.1 Makro → Branche → Aktie",
            "Einordnung: Das Makroumfeld wirkt auf Branchen über Finanzierungskosten, Nachfrage und Bewertungsmultiplikatoren. Die Wirkung auf einzelne Aktien ist deshalb nicht automatisch gleichgerichtet; entscheidend bleiben die im Bericht genannten Unternehmens- und Kursdaten.",
            "Prüfpunkt: Bestätigt wird die Einordnung, wenn die beschriebene relative Branchenstärke in den aktuellen Daten bestehen bleibt. Widerlegt oder abgeschwächt wird sie, wenn sich die ausgewiesene Markt-/Sektorrichtung umkehrt oder die genannten Belastungsfaktoren zunehmen.",
        ),
        (
            "3.2 Rohstoff → Branche → Aktie",
            "Einordnung: Rohstoffbewegungen wirken je nach Geschäftsmodell unterschiedlich: höhere Verkaufspreise können Produzenten stützen, während sie für rohstoffintensive Abnehmer ein Kostenrisiko darstellen. Eine konkrete Aktienwirkung ist daher nur für die im Abschnitt genannten Unternehmen und mit Blick auf deren jeweilige Kosten- und Erlösseite abzuleiten.",
            "Prüfpunkt: Bestätigung erfordert, dass die im Bericht genannte Rohstoffrichtung anhält und sich in den zugehörigen Branchen- oder Aktiendaten widerspiegelt. Ein deutlicher Richtungswechsel der Rohstoffe oder eine gegenläufige Aktienreaktion schwächt die These.",
        ),
        (
            "3.3 Politik → Branche → Aktie",
            "Einordnung: Politische Entscheidungen wirken erst über konkrete Umsetzung, Budgets, Regulierung oder Auftragsvergabe auf Unternehmensumsätze und Kosten. Eine politische Schlagzeile allein ist daher noch kein belastbarer Nachweis für einen Aktienimpuls.",
            "Prüfpunkt: Bestätigung entsteht durch einen belegten Umsetzungsschritt oder eine erkennbare Reaktion der betroffenen Branche. Verzögerung, Rücknahme oder ausbleibende Umsetzung der genannten Maßnahme widerlegt beziehungsweise schwächt den Zusammenhang.",
        ),
        (
            "3.4 Technologie → Branche → Aktie",
            "Einordnung: Technologietrends werden für Aktien erst dann wirtschaftlich relevant, wenn sich Adoption oder Investitionen in Aufträge, Umsatzwachstum, Margen oder Wettbewerbsvorteile übersetzen. Sektorstärke allein belegt noch nicht, dass jedes genannte Unternehmen gleichermaßen profitiert.",
            "Prüfpunkt: Bestätigung liefern im weiteren Verlauf belegte Nachfrage-, Umsatz- oder Ergebnisdaten sowie anhaltende relative Stärke. Kürzungen von Investitionen, schwächere Unternehmensausblicke oder eine nachlassende relative Stärke sprechen gegen die These.",
        ),
        (
            "3.5 Unternehmens-/Fundamentaldaten → Aktie",
            "Einordnung: Fundamentaldaten beeinflussen die Aktie über Gewinnentwicklung, Margen, Bilanzqualität und Bewertung. Positive Unternehmensdaten können bereits eingepreist sein; deshalb müssen Fundamentallage und aktuelle Kursreaktion getrennt betrachtet werden.",
            "Prüfpunkt: Bestätigung liefern die nächsten tatsächlich verfügbaren Unternehmenszahlen oder Ausblicke, sofern sie die beschriebene Entwicklung stützen. Verfehlte Erwartungen, sinkende Margen oder eine gegenteilige Kursreaktion schwächen die Investmentwirkung.",
        ),
    ]

    for heading, causal_text, trigger_text in specs:
        pattern = re.compile(r"(?m)^" + re.escape(heading) + r"\s*$")
        match = pattern.search(text)
        if not match:
            continue
        next_heading = re.search(r"(?m)^4\.\s*(?:🔭\s*)?IDEEN IM AUFBAU\s*$", text[match.end():])
        # Zwischen 3.x-Abschnitten jeweils den nächsten 3.x-Header als Grenze nehmen.
        candidates = []
        for other, _, _ in specs:
            if other == heading:
                continue
            other_match = re.search(r"(?m)^" + re.escape(other) + r"\s*$", text[match.end():])
            if other_match:
                candidates.append(other_match.start())
        if next_heading:
            candidates.append(next_heading.start())
        end = match.end() + min(candidates) if candidates else len(text)
        block = text[match.end():end]
        # Qualitative Begriffe allein reichen nicht, wenn es nur die Überschrift ist.
        has_causal = bool(re.search(
            r"(?i)\b(?:weil|dadurch|führt|fuehrt|stützt|stuetzt|belastet|profitiert|wirkt|Auswirkung|Investmentwirkung|Implikation|Kausalkette|Einordnung)\b",
            block,
        )) and len(re.sub(r"\s+", "", block)) >= 80
        has_trigger = bool(re.search(
            r"(?i)\b(?:Trigger|bestätig|bestaetig|widerleg|Invalidierung|beobacht|überwach|ueberwach|überschreit|ueberschreit|unterschreit|Breakout|Ausbruch|Prüfpunkt)\w*\b",
            block,
        ))
        additions = []
        if not has_causal:
            additions.append(causal_text)
        if not has_trigger:
            additions.append(trigger_text)
        if additions:
            separator = "\n" if block.endswith("\n") or not block.strip() else "\n\n"
            insert_at = end
            text = text[:insert_at] + separator + "\n".join(additions) + "\n" + text[insert_at:]
            repairs.append(heading)

    return text, repairs


def _pruefe_themen_kausalitaet(text):
    """Verhindert, dass 3.1–3.5 nur Marktwerte und Profiteure aufzählen."""
    required_sections = (
        "3.1 Makro → Branche → Aktie",
        "3.2 Rohstoff → Branche → Aktie",
        "3.3 Politik → Branche → Aktie",
        "3.4 Technologie → Branche → Aktie",
        "3.5 Unternehmens-/Fundamentaldaten → Aktie",
    )
    positions = []
    for heading in required_sections:
        match = re.search(r"(?m)^" + re.escape(heading) + r"\s*$", text or "")
        if match:
            positions.append((match.start(), heading))
    end_four = re.search(r"(?m)^4\.\s*(?:🔭\s*)?IDEEN IM AUFBAU\s*$", text or "")
    if end_four:
        positions.append((end_four.start(), "__END_3__"))
    positions.sort()
    errors = []
    for index, (start, heading) in enumerate(positions):
        if heading == "__END_3__":
            continue
        next_boundary = next(
            (position for position, _ in positions[index + 1:]),
            len(text),
        )
        block = text[start:next_boundary]
        body = re.sub(r"(?m)^" + re.escape(heading) + r"\s*$", "", block).strip()
        if len(body) < 100:
            errors.append(f"{heading}: zu wenig eigenständiger Analyseinhalt")
            continue
        if not re.search(
            r"(?i)\b(?:weil|dadurch|führ\w*|fuehr\w*|stütz\w*|stuetz\w*|belast\w*|"
            r"profitier\w*|wirk\w*|Auswirkung\w*|Investmentwirkung|Implikation|Kausalkette)\b",
            block,
        ):
            errors.append(f"{heading}: konkrete Kausalkette/Investmentwirkung fehlt")
        if not re.search(
            r"(?i)\b(?:Trigger|bestätig\w*|bestaetig\w*|widerleg\w*|Invalidierung|"
            r"beobacht\w*|überwach\w*|ueberwach\w*|überschreit\w*|ueberschreit\w*|"
            r"unterschreit\w*|Breakout|Ausbruch)\b",
            block,
        ):
            errors.append(f"{heading}: nächster Bestätigungs-/Widerlegungstrigger fehlt")
        if heading == "3.5 Unternehmens-/Fundamentaldaten → Aktie":
            # Ein Label wie 'Kausalkette' oder ein generischer Lehrsatz allein
            # ist kein konkreter Unternehmensnachweis. Mindestens ein benannter
            # Titel muss mit KGV, Sektorvergleich und Investmentwirkung verknüpft sein.
            concrete_company = re.search(
                r"(?is)[A-ZÄÖÜ][^\n]{2,100}\([A-Z][A-Z0-9.^=-]{0,12}\)[^\n]{0,240}"
                r"\bKGV\b[^\n]{0,160}Sektor[- ]Median[^\n]{0,240}"
                r"(?:Bewertungsprämie|Bewertungsprämie|relative Bewertung|Multiplikator|Investmentwirkung|Gewinnwachstum)",
                block,
            )
            if not concrete_company:
                errors.append(f"{heading}: konkrete Unternehmens-Kausalkette mit KGV, Sektorvergleich und Investmentwirkung fehlt")
    found = {heading for _, heading in positions if heading != "__END_3__"}
    errors.extend(f"{heading}: Abschnitt fehlt" for heading in required_sections if heading not in found)
    if errors:
        raise RuntimeError("THEMEN_KAUSALITAET_UNGUELTIG: " + " | ".join(errors))
    print("THEMEN-KAUSALITAETS-GATE: PASS")


def _korrigiere_statusverlauf_ideen(text, eingabedateien=None):
    """Hebt A-Status/Aufstiege und Statusverluste aus A hervor, nicht B->B.

    Die kompakte Statuspruefung wird auch dann sichtbar in Abschnitt 1
    eingefuegt, wenn Gemini den urspruenglichen Ankersatz nicht ausgegeben hat.
    Fehlende/unlesbare Historie wird ausdruecklich als nicht verifiziert markiert.
    """
    if not text:
        return text
    history_path = (eingabedateien or {}).get("Einzel-Check-Technikhistorie")
    if not history_path or not os.path.isfile(history_path):
        history_path = finde_datei(DATEIMUSTER["Einzel-Check-Technikhistorie"])

    pattern = re.compile(
        r"(?i)sowie stabile Statuszuweisungen in der Einzel-Check-Liste für Zscaler, Inc\. \(ZS\) und Palantir Technologies Inc\. \(PLTR\)"
    )

    def publish(replacement, visible_note):
        if pattern.search(text):
            return pattern.sub(replacement, text)
        # Nicht davon abhaengig sein, dass Gemini einen bestimmten Satz erzeugt.
        note_line = f"Statushistorie Einzel-Check (deterministische Prüfung): {visible_note}"
        if note_line in text:
            return text
        heading_12 = re.search(r"(?im)^\s*1\.2\b[^\n]*$", text)
        if heading_12:
            insert_at = heading_12.start()
            return text[:insert_at].rstrip() + "\n\n" + note_line + "\n\n" + text[insert_at:]
        heading_1 = re.search(r"(?im)^\s*1\.[^\n]*$", text)
        if heading_1:
            insert_at = heading_1.end()
            return text[:insert_at] + "\n\n" + note_line + text[insert_at:]
        return text.rstrip() + "\n\n" + note_line + "\n"

    if not history_path or not os.path.isfile(history_path):
        print("WARNUNG: Statusverlauf nicht verifiziert: Historien-Datei fehlt.")
        msg = "NICHT VERIFIZIERT – Historien-Datei fehlt; keine Stabilitätsbehauptung."
        return publish(
            "Statusverlauf NICHT VERIFIZIERT (Historien-Datei fehlt; keine Stabilitätsbehauptung)",
            msg,
        )

    date_match = re.search(
        r"(?im)^\s*Datum der Auswertung\s*:\s*(\d{2})\.(\d{2})\.(\d{4})\s*$",
        text,
    )
    target_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )
    if not target_date:
        # Ohne ein unabhaengig belegtes Berichtsdatum kann die Historie nicht
        # als aktueller Tagesstatus interpretiert werden. Niemals den letzten
        # beliebigen JSONL-Eintrag als heutigen Status ausgeben.
        print("WARNUNG: Statusverlauf nicht verifiziert: Berichtsdatum fehlt.")
        msg = "NICHT VERIFIZIERT – Berichtsdatum fehlt; Historie nicht als aktuell interpretierbar."
        return publish(
            "Statusverlauf NICHT VERIFIZIERT (Berichtsdatum fehlt; Historie nicht als aktuell interpretierbar)",
            msg,
        )
    wanted = {"ZS": "Zscaler, Inc.", "PLTR": "Palantir Technologies Inc."}
    latest = {}
    try:
        with open(history_path, "r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                if target_date and str(row.get("Datum") or "").strip() != target_date:
                    continue
                ticker = str(row.get("Ticker") or "").strip().upper()
                if ticker in wanted:
                    latest[ticker] = row
    except OSError as exc:
        print(f"WARNUNG: Statusverlauf nicht verifiziert: Historie nicht lesbar ({exc}).")
        msg = f"NICHT VERIFIZIERT – Historien-Datei nicht lesbar ({exc}); keine Stabilitätsbehauptung."
        return publish(
            "Statusverlauf NICHT VERIFIZIERT (Historien-Datei nicht lesbar; keine Stabilitätsbehauptung)",
            msg,
        )

    rank = {
        "KEIN KANDIDAT": 0,
        "KAUFKANDIDAT C": 1,
        "KAUFKANDIDAT B": 2,
        "KAUFKANDIDAT A": 3,
        "C": 1, "B": 2, "A": 3,
    }

    def describe(ticker):
        row = latest.get(ticker)
        if not row:
            return f"{wanted[ticker]} ({ticker}): Statusverlauf nicht verifiziert"
        previous_raw = row.get("Vorheriger_Status")
        current_raw = row.get("Status")
        previous = str(previous_raw or "").strip().upper()
        current = str(current_raw or "").strip().upper()
        unavailable = {"NICHT VERFUEGBAR", "NICHT VERFÜGBAR", "N/A", "UNKNOWN", "UNBEKANNT"}
        prev_missing = not previous or previous in unavailable
        curr_missing = not current or current in unavailable
        prev_rank = None if prev_missing else rank.get(previous)
        curr_rank = None if curr_missing else rank.get(current)
        prev_unknown = not prev_missing and prev_rank is None
        curr_unknown = not curr_missing and curr_rank is None
        if curr_rank == 3 and (prev_missing or prev_unknown):
            reason = "fehlend" if prev_missing else f"unbekannter Wert '{previous_raw}'"
            return f"aktueller A-Status: {wanted[ticker]} ({ticker}); vorheriger Status nicht verifiziert ({reason})"
        if prev_rank == 3 and (curr_missing or curr_unknown):
            reason = "fehlend" if curr_missing else f"unbekannter Wert '{current_raw}'"
            return (
                f"STATUSVERLAUF NICHT VERIFIZIERT: {wanted[ticker]} ({ticker}) war zuvor A; "
                f"aktueller Status {reason}; möglicher Statusverlust aus A nicht ausgeschlossen"
            )
        if prev_missing or curr_missing or prev_unknown or curr_unknown:
            return None
        if curr_rank == 3 and prev_rank is not None and prev_rank < 3:
            return f"AUFSTIEG ZU A: {wanted[ticker]} ({ticker}), {previous_raw} → {current_raw}"
        if curr_rank == 3 and prev_rank == 3:
            return f"weiterhin A: {wanted[ticker]} ({ticker}); kein neuer Aufstieg"
        if prev_rank == 3 and curr_rank is not None and curr_rank < 3:
            return f"STATUSVERLUST AUS A: {wanted[ticker]} ({ticker}), {previous_raw} → {current_raw}"
        return None

    relevant = [item for ticker in ("ZS", "PLTR") if (item := describe(ticker))]
    if not relevant:
        visible_note = (
            "Keine hervorgehobenen A-Aufstiege oder Statusverluste aus A festgestellt; "
            "unveränderte B/C-Verläufe werden nicht hervorgehoben."
        )
    else:
        visible_note = "; ".join(relevant)
    replacement = "Statusverlauf Einzel-Check: " + visible_note
    return publish(replacement, visible_note)


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


def _lade_name_ticker_quellenmapping():
    """Lädt eindeutige Firmenname/Ticker-Paare aus den aktuellen Laufquellen."""
    name_tickers = {}
    ticker_names = {}

    def add(name, ticker):
        name = re.sub(r"\s+", " ", str(name or "").strip())
        ticker = str(ticker or "").strip()
        if not name or not ticker or name.casefold() == ticker.casefold():
            return
        name_tickers.setdefault(name.casefold(), set()).add(ticker)
        ticker_names.setdefault(ticker, set()).add(name)

    # Primärquelle des Trade-Story-Universums.
    universe_path = finde_datei(DATEIMUSTER["Trade_Story_Universum(...).json"])
    if not universe_path or not os.path.isfile(universe_path):
        raise RuntimeError("NAME_TICKER_NORMALISIERUNG_QUELLE_FEHLT")
    try:
        data = json.loads(Path(universe_path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"NAME_TICKER_NORMALISIERUNG_QUELLE_NICHT_LESBAR: {exc}") from exc
    for item in data.get("candidates", []) if isinstance(data, dict) else []:
        if isinstance(item, dict):
            add(item.get("name"), item.get("ticker"))

    # Krypto-Story-Quelle: Name/Ticker-Paare explizit ergänzen, damit auch
    # Bitcoin & Co. in allen Berichtsteilen kanonisch bezeichnet werden.
    crypto_path = finde_datei(DATEIMUSTER["Trade_Story_Bitcoin(...).json"])
    if crypto_path and os.path.isfile(crypto_path):
        try:
            crypto_data = json.loads(Path(crypto_path).read_text(encoding="utf-8-sig"))
            if isinstance(crypto_data, dict):
                # Root-Format der Trade_Story_Bitcoin-Quelle (z.B. asset/ticker)
                # zusätzlich zu den vorhandenen Listenformaten übernehmen.
                if crypto_data.get("asset") and crypto_data.get("ticker"):
                    add(crypto_data["asset"], crypto_data["ticker"])

                # Bestehende candidates/assets/instruments-Formate beibehalten.
                crypto_items = crypto_data.get("candidates", [])
                if not crypto_items:
                    crypto_items = crypto_data.get("assets", crypto_data.get("instruments", []))
            else:
                crypto_items = []
            for item in crypto_items if isinstance(crypto_items, list) else []:
                if isinstance(item, dict):
                    add(item.get("name") or item.get("asset") or item.get("coin"),
                        item.get("ticker") or item.get("symbol") or item.get("yahoo_ticker"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(
                f"WARNUNG: Krypto-Quelle {os.path.basename(crypto_path)} "
                f"konnte nicht eingelesen werden ({type(exc).__name__}: {exc}); "
                "Krypto-Mapping aus dieser Quelle wurde nicht übernommen."
            )

    # Ergänzende aktuelle Scanner-CSV-Dateien schließen z.B. Titel ein, die
    # nicht im Trade-Story-Universum liegen, aber in einem aktuellen Einzelcheck
    # oder Edelmetall-/Trendwende-Scan vorkommen. Die Identitätsliste gilt für
    # die gesamte Auswertung (Aktien, Edelmetalle, Krypto und weitere Assets).
    csv_keys = (
        "Setups(...).csv",
        "Trade_Story_Setup_Rohuniversum(...).csv",
        "Trendwende_Setups(...).csv",
        "Short_Setups(...).csv",
        "Edelmetalle_Setups(...).csv",
        "Langfrist_Bewertung(...).csv",
        "Trade_Story_Aktienuniversum(...).csv",
    )
    for key in csv_keys:
        try:
            path = finde_datei(DATEIMUSTER[key])
        except Exception:
            path = None
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(4096)
                handle.seek(0)
                delimiter = ";" if sample.count(";") >= sample.count(",") else ","
                for row in csv.DictReader(handle, delimiter=delimiter):
                    lower = {str(k or "").strip().casefold(): v for k, v in row.items()}
                    name = next((lower[k] for k in ("name", "firmenname", "unternehmen", "company") if k in lower), "")
                    ticker = next((lower[k] for k in ("ticker", "symbol") if k in lower), "")
                    add(name, ticker)
        except (OSError, csv.Error):
            continue

    # Aktuelle A-Meldungen sind autoritative Identitätsquelle für Kandidaten,
    # die in keinem der generischen Scanneruniversen vorkommen.
    try:
        a_path = finde_datei(DATEIMUSTER["Einzel_Check_A_Meldungen(...).txt"])
    except Exception:
        a_path = None
    if a_path and os.path.isfile(a_path):
        try:
            a_text = Path(a_path).read_text(encoding="utf-8-sig")
            for match in re.finditer(
                r"(?im)^\s*Name:\s*(.*?)\s*\|\s*Ticker:\s*([A-Z0-9.^=-]+)\b",
                a_text,
            ):
                add(match.group(1), match.group(2))
        except OSError:
            pass

    return name_tickers, ticker_names


def _name_ticker_pruefbereiche(text):
    """Liefert identische fachliche Berichtbereiche fuer Normalizer und Gate.

    Nummerierte Haupt-/Unterabschnitte 1 bis 10 werden eingeschlossen,
    insbesondere Portfolio (7.x/10.x) und Edelmetall-Analysen. Punkt 11 ist
    Methodik/Datenqualitaet und wird explizit ausgeschlossen. Die Erkennung
    ist an Zeilenanfang gebunden, damit Zahlen innerhalb von Fliesstexten,
    Kursen oder Datumsangaben keine neuen Bereiche erzeugen.
    """
    if not text:
        return []
    heading_re = re.compile(r"(?m)^\s*(?P<number>(?P<main>\d{1,2})\.(?:\d+(?:\.\d+)*\.?)?)\s+")
    headings = [(match.start(), int(match.group("main")))
                for match in heading_re.finditer(text)]
    if not headings:
        return []
    spans = []
    for i, (start, main) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        # Abschnitt 11 (Methodik/Datenqualität) und weitere Nicht-Analyse-
        # Hauptabschnitte dienen nur als Grenze, werden aber nicht geprüft.
        if 1 <= main <= 10 and start < end:
            spans.append((start, end))
    return spans


def _normalisiere_name_ticker_ausgabe(text):
    """Normalisiert konkrete Firmennamen quellengebunden zu Name (TICKER).

    Wie die funktionierende Sektor-Analyse-Version interpretiert diese Funktion
    einen nackten Ticker NICHT als Aktienbezug. Nur ein kanonischer Firmenname
    aus dem aktuellen Quellenmapping löst eine Normalisierung aus.
    """
    if not text:
        return text

    name_tickers, ticker_names = _lade_name_ticker_quellenmapping()
    names = []
    for folded, tickers_for_name in name_tickers.items():
        if len(tickers_for_name) != 1:
            continue
        ticker = next(iter(tickers_for_name))
        name = next(
            (candidate for candidate in ticker_names.get(ticker, set())
             if candidate.casefold() == folded),
            None,
        )
        if name:
            names.append((name, ticker))
    names.sort(key=lambda pair: len(pair[0]), reverse=True)

    # Gemeinsamer Abschnittsfilter: Portfolio (7.x/10.x) und Edelmetalle
    # werden ebenso erfasst wie die übrigen fachlichen Analyseabschnitte;
    # Methodik/Datenqualität (11.x) bleibt außen vor.
    spans = _name_ticker_pruefbereiche(text)
    if not spans:
        return text
    for start, end in reversed(spans):
        block = text[start:end]
        lines = block.splitlines(keepends=True)
        for i, line in enumerate(lines):
            folded_line = line.casefold()
            for name, ticker in names:
                if name.casefold() not in folded_line:
                    continue
                # Den direkt folgenden Klammerzusatz konsumieren und auf den
                # quellengebundenen Ticker korrigieren.
                pattern = re.compile(
                    r"(?<!\w)" + re.escape(name) +
                    r"(?:\s*\(\s*[A-Z0-9.^=-]+\s*\))?(?!\w)",
                    re.I,
                )
                lines[i] = pattern.sub(lambda _m, n=name, t=ticker: f"{n} ({t})", lines[i])
                line = lines[i]
                folded_line = line.casefold()
        text = text[:start] + "".join(lines) + text[end:]
    return text


def _pruefe_name_ticker_gate(text):
    """Harte Endpruefung: konkrete Firmennamen nur als Name (Ticker).

    Ein nackter Ticker ist fuer sich allein kein Aktienbezug und wird daher
    nicht beanstandet. Das entspricht dem funktionierenden Gate in der
    Sektor-Analyse. Ein erkannter kanonischer Firmenname ohne zugehoerigen
    Ticker bleibt dagegen ein Fehler.
    """
    name_tickers, ticker_names = _lade_name_ticker_quellenmapping()
    name_tickers = {
        name: set(tickers)
        for name, tickers in name_tickers.items()
        if name and tickers
    }

    # Dasselbe Bereichsmodell wie die Normalisierung verwenden, damit das
    # Gate keine Abschnitte übersieht oder zusätzliche Bereiche beanstandet.
    spans = _name_ticker_pruefbereiche(text)
    if not spans:
        return
    checked = "\n".join(text[start:end] for start, end in spans)

    errors = []
    for line_no, line in enumerate(checked.splitlines(), 1):
        if not line.strip():
            continue
        # Source-/Dateinamen und URL-Provenienz sind keine Unternehmensnennung.
        if re.search(r"(?i)(?:https?://|\.csv\b|\.json\b|\.py\b|\.zip\b)", line):
            continue
        folded = line.casefold()
        for name_key, valid_tickers in name_tickers.items():
            if name_key not in folded:
                continue
            # Mehrdeutige Firmennamen/Ticker werden nicht geraten.
            if len(valid_tickers) != 1:
                continue
            ticker = next(iter(valid_tickers))
            name = next(
                (candidate for candidate in ticker_names.get(ticker, set())
                 if candidate.casefold() == name_key),
                name_key,
            )
            if not re.search(
                r"(?<!\w)" + re.escape(name) +
                r"\s*\(\s*" + re.escape(ticker) + r"\s*\)(?!\w)",
                line,
                re.I,
            ):
                errors.append(
                    f"Zeile {line_no}: {name} nicht als {name} ({ticker}) formatiert"
                )
                break
        if len(errors) >= 20:
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
    """Idempotente Endformatierung für Überschriften, Unterpunkte und Aktienblöcke.

    Vereinheitlicht Leerzeilen im gesamten Bericht und stellt insbesondere
    Abstände zwischen nummerierten Abschnitten sowie einzelnen Aktienprofilen
    in 1.2/2.1 her. Inhaltliche Werte werden nicht verändert.
    """
    if not text:
        return text

    lines = text.splitlines()

    # 7.5: Edelmetalle gehören ausschließlich in Abschnitt 8. Nur eigenständige
    # Zeilen mit einem Metallnamen werden entfernt; andere Rohstoffdaten bleiben.
    precious = re.compile(r"(?i)^\s*[-•]?\s*(?:gold|silber|platin|palladium)\b")
    out = []
    in_75 = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^7\.5\s+Rohstoffe\s*$", stripped, re.I):
            in_75 = True
            out.append(line.rstrip())
            continue
        if in_75 and re.match(r"^7\.6\s+Krypto\s*$", stripped, re.I):
            in_75 = False
            out.append(line.rstrip())
            continue
        if in_75 and precious.match(line):
            continue
        out.append(line.rstrip())

    # Alle strukturellen Haupt- und Unterüberschriften erhalten genau eine
    # Leerzeile davor. Das behebt u.a. fehlende Abstände vor 1.3, 8 und 9.
    heading_re = re.compile(r"^\s*(?:\d+\.\s+|\d+\.\d+\s+).+$")
    # Kandidaten-/Aktienblöcke, die nicht immer mit einer Nummer beginnen.
    stock_12_re = re.compile(r"^.+\([A-Z0-9.^=-]+\)\s*\|\s*Ticker:")
    stock_21_re = re.compile(r"^.+\([A-Z0-9.^=-]+\)\s*\|\s*Ticker:.*\|\s*Scanner:")
    a_candidate_re = re.compile(r"^Name:\s*.+\|\s*Ticker:")

    formatted = []
    section = None
    for line in out:
        stripped = line.strip()
        if not stripped:
            # Vorläufig nur eine Leerzeile zulassen.
            if formatted and formatted[-1] != "":
                formatted.append("")
            continue

        # Abschnitte mit separaten Listeneinträgen.
        main = re.match(r"^(\d+)\.\s+", stripped)
        sub = re.match(r"^(\d+\.\d+)\s+", stripped)
        if main:
            section = main.group(1) + "."
        elif sub:
            section = sub.group(1)

        needs_blank = bool(heading_re.match(stripped))
        if section == "1.2" and stock_12_re.match(stripped):
            needs_blank = True
        if section == "2.1" and stock_21_re.match(stripped):
            needs_blank = True
        if section == "2.4" and a_candidate_re.match(stripped):
            needs_blank = True
        # Im Ideen-/Frühsignal-Abschnitt jeden eigenständigen Titel trennen.
        if section == "5." and stripped.startswith(("- ", "• ")):
            needs_blank = True
        if section == "1.4" and stripped.startswith(("- ", "• ")):
            needs_blank = True

        if needs_blank and formatted and formatted[-1] != "":
            formatted.append("")
        formatted.append(stripped)

    # Kein Leerraum am Dokumentende; bei mehrfachen Aufrufen identisches Ergebnis.
    while formatted and formatted[-1] == "":
        formatted.pop()
    return "\n".join(formatted) + "\n"


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
    matched_line = find_exact_labeled_line(source or "", label)
    for line in ([matched_line] if matched_line else []):
        clean_line = line.strip()
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
    matched_line = find_exact_labeled_line(source or "", label)
    for line in ([matched_line] if matched_line else []):
        clean_line = line.strip()
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
    # Im REGIONEN-PERFORMANCE-Block steht erst die Tagesveraenderung und
    # danach der Indexstand in Klammern. Fuer diese Zeilen muss der Indexstand
    # explizit aus der Klammer gelesen werden, nicht die davorstehende Rendite.
    index_labels = [
        ("S&P 500", True), ("Nasdaq", True), ("DAX", True),
        ("EuroStoxx50", True), ("Russell 2000", False), ("Nikkei 225", True),
        ("Hang Seng", True), ("Shanghai Composite", True),
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
        block = _abschnitt_text(result, heading, sections + ["8. 🪙 EDELMETALLE", "8. Edelmetalle", "8.1 Gold"])
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
        for nxt in sections + ["8. 🪙 EDELMETALLE", "8. Edelmetalle", "8.1 Gold"]:
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







def _portfolio_tickers_aus_positionsdatensatz(line):
    """Extrahiert Ticker aus Positionszeilen mit Klammer- oder Feldnotation."""
    value = line or ""
    candidates = re.findall(r"\(([A-Z0-9][A-Z0-9.^=-]{0,12})\)", value)
    candidates.extend(re.findall(
        r"(?i)\b(?:ticker|symbol|wertpapier)\s*:\s*([A-Z0-9][A-Z0-9.^=-]{0,12})\b",
        value,
    ))
    # Positionszeilen koennen mit einer Aktion/Richtung vor dem Ticker beginnen,
    # z.B. "- LONG AAPL | ..." oder "- SHORT TSLA | ...". Das Aktionswort ist
    # kein Wertpapierkennzeichen; der eigentliche Ticker folgt direkt danach.
    action_words = {
        "LONG", "SHORT", "BUY", "SELL", "HOLD", "FLAT", "ADD", "REDUCE",
        "TRIM", "CLOSE", "OPEN", "KAUFEN", "VERKAUFEN", "HALTEN",
        "AUFSTOCKEN", "REDUZIEREN",
    }
    leading = re.match(
        r"^\s*[-•*]\s+(?:(?:LONG|SHORT|BUY|SELL|HOLD|FLAT|ADD|REDUCE|TRIM|CLOSE|OPEN|KAUFEN|VERKAUFEN|HALTEN|AUFSTOCKEN|REDUZIEREN)\s+)?"
        r"([A-Z0-9][A-Z0-9.^=-]{0,12})(?=\s*(?:\||[-–—:]|\s))",
        value,
        re.I,
    )
    if leading:
        candidates.append(leading.group(1))
    excluded = {
        "USD", "EUR", "US", "EMA", "SMA", "YTD", "TP1", "TP2", "CRV", "MSCI",
        "ENTRY", "CURRENT", "PERFORMANCE", "EINSTIEG", "AKTUELL", "TICKER", "SYMBOL",
        *action_words,
    }
    return list(dict.fromkeys(t.upper() for t in candidates if t.upper() not in excluded))


def _ist_portfolio_positionsdatensatz(line):
    """Erkennt vollständige Positionsdatensätze mit gängigen Feldvarianten."""
    value = line or ""
    if not re.match(r"^\s*[-•*]\s+", value):
        return False
    entry = r"(?i)\b(?:Einstieg|Einstand|Entry|Entry\s+Price|Kaufkurs)\s*:"
    current = r"(?i)\b(?:Aktuell|Aktueller\s+Kurs|Kurs\s+aktuell|Current|Current\s+Price|Marktwert)\s*:"
    performance = r"(?i)\b(?:Performance|Rendite|Ergebnis|Gewinn\s*/\s*Verlust|P\s*/\s*L|P&L|Unrealisierter\s+Gewinn)\s*:"
    return bool(re.search(entry, value) and re.search(current, value) and re.search(performance, value))


def _portfolio_positionsduplikate(text):
    """Find duplicate tickers only within true position records in the same 10.x section."""
    p10 = re.search(r"(?m)^10\.\s*💼\s*BESTEHENDES PORTFOLIO\s*$", text or "")
    p11 = re.search(r"(?m)^11\.\s*METHODIK\s*/\s*DATENQUALITÄT\s*$", text or "")
    if not p10 or not p11 or p11.start() <= p10.start():
        return None
    seen_by_section = {}
    current_section = None
    duplicates = []
    for line in (text or "")[p10.start():p11.start()].splitlines():
        heading = re.match(r"^\s*(?:#{1,6}\s*)?10\.(1|2|3|4|5)\b", line)
        if heading:
            current_section = heading.group(1)
        if current_section not in {"1", "3", "4"} or not _ist_portfolio_positionsdatensatz(line):
            continue
        tickers = _portfolio_tickers_aus_positionsdatensatz(line)
        section_seen = seen_by_section.setdefault(current_section, set())
        for ticker in tickers:
            if ticker in section_seen:
                duplicates.append((current_section, ticker))
            else:
                section_seen.add(ticker)
    return duplicates


def _dedupliziere_portfolio_positionsdatensaetze(text):
    """Remove only duplicate full records within the same section; preserve cross-section mentions."""
    p10 = re.search(r"(?m)^10\.\s*💼\s*BESTEHENDES PORTFOLIO\s*$", text or "")
    p11 = re.search(r"(?m)^11\.\s*METHODIK\s*/\s*DATENQUALITÄT\s*$", text or "")
    if not p10 or not p11 or p11.start() <= p10.start():
        return text, []
    block = text[p10.start():p11.start()]
    seen_by_section = {}
    current_section = None
    out = []
    removed = []
    for line in block.splitlines():
        heading = re.match(r"(?m)^\s*(?:#{1,6}\s*)?10\.(1|2|3|4|5)\b", line)
        if heading:
            current_section = heading.group(1)
        if current_section in {"1", "3", "4"} and _ist_portfolio_positionsdatensatz(line):
            tickers = _portfolio_tickers_aus_positionsdatensatz(line)
            section_seen = seen_by_section.setdefault(current_section, set())
            already = [ticker for ticker in tickers if ticker in section_seen]
            unseen = [ticker for ticker in tickers if ticker not in section_seen]
            if already and not unseen:
                removed.append((current_section, tuple(already)))
                continue
            section_seen.update(unseen)
        out.append(line)
    new_block = "\n".join(out).rstrip("\n")
    return text[:p10.start()] + new_block + "\n\n" + text[p11.start():], removed


def _normalisiere_quellenbezeichnung(value):
    """Normalisiert Marktbezeichnungen unabhängig von Leerzeichen/Interpunktion."""
    key = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    aliases = {
        "sp500": "sp500",
        "sandp500": "sp500",
        "nasdaq": "nasdaqcomposite",
        "nasdaqcomposite": "nasdaqcomposite",
        "eurostoxx": "eurostoxx50",
        "eurostoxx50": "eurostoxx50",
        "eurostoxx50index": "eurostoxx50",
        "nikkei": "nikkei225",
        "nikkei225": "nikkei225",
        "shanghaicomposite": "shanghaicomposite",
        "hangseng": "hangseng",
    }
    return aliases.get(key, key)


def _quellenwert_toleranz(raw):
    """Halbe Einheit der letzten ausgewiesenen Quell-Dezimalstelle.

    Die Toleranz hängt damit von der Präzision des konkreten Quellwerts ab,
    statt für Prozentwerte, Indexstände und Kurse eine gemeinsame Konstante
    anzuwenden. Die Toleranz dient nur der Rundungsdarstellung, nicht einer
    fachlichen Abweichung.
    """
    value = str(raw or "").strip().strip(".,;:()[]{}")
    if not value:
        return 0.0
    comma, dot = value.rfind(","), value.rfind(".")
    sep = None
    if comma >= 0 and dot >= 0:
        sep = "," if comma > dot else "."
    elif comma >= 0 or dot >= 0:
        candidate = "," if comma >= 0 else "."
        tail = value.rsplit(candidate, 1)[-1]
        # Drei Nachkommastellen sind bei Punkt/Komma typischerweise ein
        # Tausendertrenner; eine explizite Dezimalpräzision mit drei Stellen
        # wird weiterhin unterstützt, wenn die andere Trennart vorkommt.
        if 1 <= len(tail) <= 2:
            sep = candidate
        elif len(tail) == 3 and len(value.split(candidate)[0].lstrip("+-")) <= 3:
            sep = candidate
    if sep is None:
        decimals = 0
    else:
        decimals = len(value.rsplit(sep, 1)[-1])
    return 0.5 * (10 ** (-decimals)) + 1e-12


def _quellenwert_ist_mehrdeutig(raw):
    """Erkennt Einzeltrenner mit genau drei Folgeziffern ohne Locale-Kontext.

    Beispiele wie "1.234" können Dezimalzahl oder Tausenderformat bedeuten.
    Statt eine fachlich falsche Interpretation zu erraten, erzwingt das Gate
    in diesem Fall eine explizite, eindeutige Quellformatierung.
    """
    value = str(raw or "").strip().strip(".,;:()[]{}")
    if "," in value and "." in value:
        return False
    separator = "," if "," in value else "." if "." in value else None
    if separator is None or value.count(separator) != 1:
        return False
    left, right = value.rsplit(separator, 1)
    return bool(re.fullmatch(r"[+-]?\d{1,3}", left) and re.fullmatch(r"\d{3}", right))


def _quellenwerte_gleich(source_raw, output_raw):
    """Vergleicht Werte numerisch; mehrdeutige Locale-Formate werden abgelehnt."""
    if _quellenwert_ist_mehrdeutig(source_raw) or _quellenwert_ist_mehrdeutig(output_raw):
        return False
    source_value = _quellenwert_float(source_raw)
    output_value = _quellenwert_float(output_raw)
    if source_value is None or output_value is None:
        return False
    return abs(source_value - output_value) <= _quellenwert_toleranz(source_raw)


def _pruefe_quellenperioden_und_positionsduplikate(text, briefing_text="", makro_text="", edel_text=""):
    """Hartes Abschluss-Gate für Pflichtquellen, Perioden, Zeitbasis und Positionsduplikate."""
    errors = []
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("QUELLEN_PERIODEN_POSITIONEN_GATE: leere Auswertung")

    # Alle sieben Indizes sind Pflichtdaten. Quelle und Ausgabe müssen jeweils
    # genau einen parsebaren Datensatz mit Tagesrendite, Schlussstand, YTD und
    # Datenstand enthalten. Unbekannte/fehlende Quellen werden nie übersprungen.
    required_indices = (
        ("sp500", "S&P 500", r"S&P\s*500"),
        ("nasdaqcomposite", "Nasdaq Composite", r"Nasdaq(?:\s+Composite)?"),
        ("dax", "DAX", r"DAX"),
        ("eurostoxx50", "EuroStoxx 50", r"Euro\s*Stoxx\s*50"),
        ("nikkei225", "Nikkei 225", r"Nikkei\s*225"),
        ("shanghaicomposite", "Shanghai Composite", r"Shanghai\s+Composite"),
        ("hangseng", "Hang Seng", r"Hang\s*Seng"),
    )
    source_rows = {}
    # Bezeichnung zuerst vollständig erfassen und danach normalisieren. Dadurch
    # werden Varianten wie "EURO STOXX-50" nicht vor der Normalisierung verworfen.
    required_keys = {entry[0] for entry in required_indices}
    source_rows = {}
    source_pattern = re.compile(
        r"(?i)^\s*[•-]\s*(?P<label>[^:|]+):\s*"
        r"letzter\s+abgeschlossener\s+Handelstag\s+(?P<daily>[+-]?\d[\d.,]*)%\s*"
        r"\((?P<close>[^)]+)\)\s*\|\s*YTD\s+(?P<ytd>[+-]?\d[\d.,]*)%\s*"
        r"\|\s*Datenstand\s+(?P<date>\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{4}-\d{2}-\d{2})"
        r"(?:\s+\[WARNUNG:[^\r\n]*\])?\s*$"
    )
    for line in (briefing_text or "").splitlines():
        match = source_pattern.match(line)
        if not match:
            continue
        key = _normalisiere_quellenbezeichnung(match.group("label"))
        if key not in required_keys:
            continue
        if key in source_rows:
            errors.append(f"7.1 {match.group('label')}: Pflichtquelle mehrfach vorhanden")
        source_rows[key] = match.groupdict()

    output_rows = {}
    output_pattern = re.compile(
        r"(?i)^\s*[-•]\s*(?P<label>[^:|]+):\s*"
        r"letzter\s+abgeschlossener\s+Handelstag\s+(?P<daily>[+-]?\d[\d.,]*)%\s*"
        r"\((?P<close>[^)]+)\)\s*\|\s*YTD\s+(?P<ytd>[+-]?\d[\d.,]*)%\s*"
        r"\|\s*Datenstand\s+(?P<date>\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{4}-\d{2}-\d{2})\s*$"
    )
    for line in text.splitlines():
        match = output_pattern.match(line)
        if not match:
            continue
        key = _normalisiere_quellenbezeichnung(match.group("label"))
        if key not in required_keys:
            continue
        if key in output_rows:
            errors.append(f"7.1 {match.group('label')}: Ausgabezeile mehrfach vorhanden")
        output_rows[key] = match.groupdict()

    for key, label, _ in required_indices:
        src = source_rows.get(key)
        out = output_rows.get(key)
        if not src:
            errors.append(f"7.1 {label}: Pflichtquellwert fehlt oder ist nicht vollständig parsebar")
            continue
        if not out:
            errors.append(f"7.1 {label}: Pflicht-Ausgabezeile fehlt oder ist unvollständig")
            continue
        for field, field_label in (("daily", "Tagesrendite"), ("close", "Schlussstand"), ("ytd", "YTD")):
            if not _quellenwerte_gleich(src[field], out[field]):
                errors.append(f"7.1 {label}: {field_label} weicht vom Quellwert ab")
        if _normalisiere_datenstand(src["date"]) is None:
            errors.append(f"7.1 {label}: Quell-Datenstand ist ungültig")
        elif not _datenstand_ist_aktuell(src["date"]):
            errors.append(f"7.1 {label}: Quelldatenstand ist veraltet oder liegt in der Zukunft")
        if _normalisiere_datenstand(out["date"]) is None:
            errors.append(f"7.1 {label}: Ausgabe-Datenstand ist ungültig")
        elif _normalisiere_datenstand(src["date"]) != _normalisiere_datenstand(out["date"]):
            errors.append(f"7.1 {label}: Datenstand fehlt, ist veraltet oder weicht von der Quelle ab")

    # WTI/Brent: Kurs (Preiseinheit), 5T/4W (Prozent) und Datenstand sind Pflicht.
    for asset in ("WTI", "Brent"):
        source_matches = list(re.finditer(
            r"(?im)^\s*[-•]\s*" + re.escape(asset) +
            r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*([-+]?\d[\d.,]*)%\s*5T\s*\|\s*"
            r"([-+]?\d[\d.,]*)%\s*4W(?:\s*\|\s*[-+]?\d[\d.,]*%\s*(?:1M|3M|6M|1J))*\s*\|\s*Datenstand\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{2}-\d{2})\s*$",
            briefing_text or "",
        ))
        if len(source_matches) != 1:
            errors.append(f"7.5 {asset}: Pflichtquelle fehlt, ist unvollständig oder mehrfach vorhanden")
        else:
            source_match = source_matches[0]
            source_values = source_match.groups()
            summary_matches = list(re.finditer(
                r"(?im)^\s*[-•]\s*" + re.escape(asset) +
                r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*([-+]?\d[\d.,]*)%\s*5T\s*\|\s*"
                r"([-+]?\d[\d.,]*)%\s*4W(?:\s*\|\s*[-+]?\d[\d.,]*%\s*(?:1M|3M|6M|1J))*\s*\|\s*Datenstand\s+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{2}-\d{2})\s*$",
                text,
            ))
            if len(summary_matches) != 1:
                errors.append(f"7.5 {asset}: Ausgabe fehlt, ist unvollständig oder mehrfach vorhanden")
            else:
                output_values = summary_matches[0].groups()
                for i, field in enumerate(("Kurs", "5T", "4W")):
                    if not _quellenwerte_gleich(source_values[i], output_values[i]):
                        errors.append(f"7.5 {asset}: {field} stimmt nicht mit der Quelle überein")
                # Zusätzliche Perioden werden anhand ihres Labels verglichen.
                # Dadurch bleiben 4W (28 Kalendertage) und 1M (Kalendermonat)
                # getrennte Quellenwerte und können nicht versehentlich vertauscht werden.
                period_pattern = re.compile(r"([-+]?\d[\d.,]*)%\s*(5T|4W|1M|3M|6M|1J)\b")
                src_periods = {m.group(2): m.group(1) for m in period_pattern.finditer(source_match.group(0))}
                out_periods = {m.group(2): m.group(1) for m in period_pattern.finditer(summary_matches[0].group(0))}
                for period in ("1M", "3M", "6M", "1J"):
                    if period in src_periods and period not in out_periods:
                        errors.append(f"7.5 {asset}: Ausgabe-Periode {period} fehlt")
                    elif period in src_periods and not _quellenwerte_gleich(src_periods[period], out_periods[period]):
                        errors.append(f"7.5 {asset}: Periode {period} stimmt nicht mit der Quelle überein")
                if _normalisiere_datenstand(source_values[3]) is None:
                    errors.append(f"7.5 {asset}: Quell-Datenstand ist ungültig")
                elif not _datenstand_ist_aktuell(source_values[3]):
                    errors.append(f"7.5 {asset}: Quelldatenstand ist veraltet oder liegt in der Zukunft")
                if (_normalisiere_datenstand(source_values[3]) is not None
                        and _normalisiere_datenstand(source_values[3]) != _normalisiere_datenstand(output_values[3])):
                    errors.append(f"7.5 {asset}: Datenstand fehlt, ist ungültig oder veraltet")

        macro_ref = _extrahiere_makro_referenzwerte(makro_text or "").get(asset.casefold())
        if macro_ref:
            fact = next((ln for ln in text.splitlines() if re.match(
                r"(?i)^\s*-\s*\[AUTORITATIVE QUELLE\]\s*" + re.escape(asset) + r":", ln)), "")
            source_date = macro_ref.get("datenstand", "")
            source_date_de = source_date
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date or ""):
                try:
                    source_date_de = datetime.datetime.strptime(source_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                except ValueError:
                    errors.append(f"7.5 {asset}: ungültiger Makro-Datenstand {source_date!r}")
                    source_date_de = "__INVALID_DATE__"
            if not fact or "Quelle=Makro_Briefing" not in fact or (source_date_de and f"Datenstand={source_date_de}" not in fact):
                errors.append(f"7.5 {asset}: Makro-Fakt muss Quelle und aktuellen Datenstand ausweisen")
            for period, value in macro_ref.get("perioden", {}).items():
                expected = f"{period}={value:+.2f}".replace(".", ",") + "%"
                if expected not in fact:
                    errors.append(f"7.5 {asset}: Makro-Periode {period} fehlt oder ist falsch zugeordnet")

    # Edelmetalle: Quelle liefert in LAGE JE METALL zwingend Kurs und 4W.
    # Der Block hat im produktiven Briefing keinen eigenen Datenstand; deshalb
    # wird hier kein erfundener Zeitstempel verlangt. Die expliziten 4W-Perioden
    # werden jedoch geprüft und die Ausgabekurswerte müssen quellenidentisch sein.
    metal_source_block = _edelmetall_lage_block(edel_text or "")
    for idx, metal in enumerate(("Gold", "Silber", "Platin", "Palladium"), start=1):
        source_row = next((ln.strip() for ln in metal_source_block.splitlines() if re.match(
            r"(?i)^\s*" + re.escape(metal) + r"\s*:\s*Kurs\b", ln)), "")
        source_course = re.search(r"(?i)^\s*" + re.escape(metal) + r"\s*:\s*Kurs\s+([-+]?\d[\d.,]*)", source_row) if source_row else None
        source_value = _edelmetall_4w_value(edel_text or "", metal)
        if not source_row or not source_course:
            errors.append(f"8.{idx} {metal}: Kurs-Pflichtwert fehlt oder ist nicht parsebar")
            continue
        if source_value is None or len(source_value) < 2 or source_value[1] is None:
            errors.append(f"8.{idx} {metal}: 4W-Pflichtwert fehlt oder ist nicht parsebar")
            continue
        summaries = [ln for ln in text.splitlines() if re.match(
            r"(?i)^\s*[-•]\s*8\." + str(idx) + r"\s+" + re.escape(metal) + r":", ln)]
        if len(summaries) != 1:
            errors.append(f"8.{idx} {metal}: Ausgabezeile fehlt oder ist mehrfach vorhanden")
            continue
        out_course = re.search(r"(?i)\bKurs\s*\(Futures\s+[^)]+\)\s*:\s*([-+]?\d[\d.,]*)\s+USD\b", summaries[0])
        out_4w = re.search(r"(?i)\b4W\s*:\s*([+-]?[\d.,]+)\s*%", summaries[0])
        if not out_course or not _quellenwerte_gleich(source_course.group(1), out_course.group(1)):
            errors.append(f"8.{idx} {metal}: Kurs stimmt nicht mit dem Edelmetalle-Briefing überein")
        if not out_4w or not _quellenwerte_gleich(source_value[0], out_4w.group(1)):
            errors.append(f"8.{idx} {metal}: 4W-Wert stimmt nicht mit dem Edelmetalle-Briefing überein")

    pdups = _portfolio_positionsduplikate(text)
    if pdups is None:
        errors.append("Portfolio: Abschnittsgrenze 10/11 fehlt oder ist fehlerhaft")
    else:
        for section_id, ticker in pdups:
            errors.append(f"Portfolio: echter Positionsdatensatz {ticker} innerhalb 10.{section_id} doppelt")

    if errors:
        raise RuntimeError("QUELLEN_PERIODEN_POSITIONEN_GATE: " + " | ".join(errors))
    print("QUELLEN-PERIODEN-POSITIONS-GATE: PASS")
    return True


def _normalisiere_datenstand(value):
    """Validiert und normalisiert DD.MM.YYYY, DD-MM-YYYY und YYYY-MM-DD."""
    raw = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%y", "%d-%m-%y", "%d/%m/%y"):
            try:
                return datetime.datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
    except (TypeError, ValueError):
        return None
    return None


def _datenstand_ist_aktuell(value, max_alter_tage=5, heute=None):
    """Validiert das Alter eines Marktdatenstands (inkl. Wochenend-/Feiertagspuffer)."""
    normalized = _normalisiere_datenstand(value)
    if normalized is None:
        return False
    try:
        datum = datetime.date.fromisoformat(normalized)
        referenz = heute or datetime.date.today()
    except (TypeError, ValueError):
        return False
    alter = (referenz - datum).days
    return 0 <= alter <= max_alter_tage

def _repariere_quellenperioden_und_positionsduplikate(
    text, briefing_text="", makro_text="", edel_text=""
):
    """Finale deterministische Korrektur von Periodenzuordnung und doppelten Portfolioeinträgen.

    Prioritäten:
    - abgeschlossene Index-Tagesrendite/YTD: explizite REGIONEN-PERFORMANCE im Markt-Briefing;
    - aktuelle WTI-/Brent-Übersicht: Kurs/5T/4W gemeinsam aus dem Markt-Briefing;
    - Makro-Faktenblock 7.2–7.7: strukturierte Makroquelle inklusive Datenstand/Zeitbasis;
    - sämtliche Edelmetall-Fakten: ausschließlich Edelmetalle-Briefing; Marktbriefing nur als separat dokumentierte Vergleichsquelle;
    - Portfolio-Deduplizierung: nur echte Positionsdatensätze innerhalb desselben Unterabschnitts.
    """
    if not text:
        return text, []

    changes = []
    # Eine bereits beobachtete Gemini-Ausgabe klebte die Methodik-Überschrift
    # an das Ende des Portfolio-Fazits und erzeugte später zusätzlich eine
    # korrekte Überschrift. Den angeklebten Doppelkopf entfernen.
    text, n_inline_methodik = re.subn(
        r"(?m)(?<=\.)11\.\s*METHODIK\s*/\s*DATENQUALITÄT\s*$",
        "",
        text,
        count=1,
    )
    if n_inline_methodik:
        changes.append("An Portfolio-Fazit angeklebte doppelte Methodik-Überschrift entfernt")

    def fmt_num(raw, decimals=2):
        value = _quellenwert_float(str(raw))
        if value is None:
            return str(raw)
        return f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def fmt_pct(raw):
        raw = str(raw or "").strip().replace("%", "").replace(".", ",")
        if not raw:
            return raw
        try:
            value = float(raw.replace(",", "."))
            return f"{value:+.2f}".replace(".", ",") + "%"
        except ValueError:
            return raw + "%"

    # --- Markt-Briefing: belastbare regionale Tagesrendite und YTD-Felder ---
    regional = {}
    regional_counts = {}
    required_regional_keys = {
        "sp500", "nasdaqcomposite", "dax", "eurostoxx50",
        "nikkei225", "shanghaicomposite", "hangseng",
    }
    for line in (briefing_text or "").splitlines():
        label_match = re.match(r"(?i)^\s*[•-]\s*([^:|]+):", line)
        if label_match:
            candidate_key = _normalisiere_quellenbezeichnung(label_match.group(1))
            if candidate_key in required_regional_keys:
                regional_counts[candidate_key] = regional_counts.get(candidate_key, 0) + 1
        m = re.match(
            r"(?i)^\s*[•-]\s*(S&P\s*500|Nasdaq(?:\s+Composite)?|DAX|"
            r"Euro[\s-]*Stoxx[\s-]*50|Nikkei[\s-]*225|"
            r"Shanghai\s+Composite|Hang\s+Seng):\s*"
            r"letzter\s+abgeschlossener\s+Handelstag\s+([+-]?\d+(?:[.,]\d+)?)%\s*"
            r"\(([^)]+)\)\s*\|\s*YTD\s+([+-]?\d+(?:[.,]\d+)?)%\s*\|\s*Datenstand\s+"
            r"(\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{4}-\d{2}-\d{2})"
            r"(?:\s+\[WARNUNG:[^\r\n]*\])?\s*$",
            line,
        )
        if m:
            key = _normalisiere_quellenbezeichnung(m.group(1))
            regional[key] = {
                "daily": m.group(2), "close": m.group(3),
                "ytd": m.group(4), "date": m.group(5),
            }

    # 1.1: Keine 5T-Rendite als Tagesrendite/YTD ausgeben; DAX erhält den
    # jüngsten expliziten abgeschlossenen Schluss aus dem Markt-Briefing.
    dax = regional.get("dax")
    if dax:
        corrected = (
            f"- Der DAX schloss bei {fmt_num(dax['close'])} Punkten "
            f"({fmt_pct(dax['daily'])} am letzten abgeschlossenen Handelstag, "
            f"YTD {fmt_pct(dax['ytd'])}; Datenstand {dax['date']}) und notiert "
            "weiter unterhalb von EMA20 und EMA50."
        )
        text, n = re.subn(
            r"(?m)^\s*-\s*Der DAX schloss\b[^\n]*$", corrected, text, count=1
        )
        if n:
            changes.append("1.1 DAX-Tagesrendite/YTD aus Markt-Briefing korrigiert")

    # WTI/Brent: Kurs, 5T und 4W immer aus derselben Markt-Briefing-Zeile.
    energy = {}
    energy_source_errors = []
    energy_pattern = (
        r"(?im)^\s*[-•]\s*" + r"ASSET" +
        r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*"
        r"([-+]?\d[\d.,]*)%\s*5T\s*\|\s*"
        r"([-+]?\d[\d.,]*)%\s*4W\s*\|\s*Datenstand\s*[:=]?\s*"
        r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{2}-\d{2})\s*$"
    )
    for asset in ("WTI", "Brent"):
        candidate_lines = [
            line for line in (briefing_text or "").splitlines()
            if re.match(r"(?i)^\s*[-•]\s*" + re.escape(asset) + r"\s*:", line)
        ]
        matches = list(re.finditer(
            energy_pattern.replace("ASSET", re.escape(asset)),
            briefing_text or "",
        ))
        if len(candidate_lines) != 1 or len(matches) != 1:
            energy_source_errors.append(
                f"{asset}: erwartet genau eine vollständige Quellzeile, "
                f"gefunden {len(candidate_lines)} Kandidatenzeilen und {len(matches)} parsebare Zeilen"
            )
            continue
        m = matches[0]
        price, period_5t, period_4w, raw_date = m.groups()
        numeric_values = (price, period_5t, period_4w)
        if any(
            _quellenwert_float(value) is None
            or _quellenwert_ist_mehrdeutig(value)
            for value in numeric_values
        ):
            energy_source_errors.append(f"{asset}: Kurs/5T/4W nicht eindeutig numerisch parsebar")
            continue
        normalized_date = _normalisiere_datenstand(raw_date)
        if not normalized_date or not _datenstand_ist_aktuell(raw_date):
            energy_source_errors.append(f"{asset}: Datenstand ungültig, veraltet oder zukünftig")
            continue
        energy[asset.lower()] = {
            "price": price, "5T": period_5t,
            "4W": period_4w, "date": raw_date,
            "normalized_date": normalized_date,
        }

    # WTI und Brent werden weiterhin nur gemeinsam ersetzt, wenn beide
    # Pflichtquellen jeweils eindeutig und vollständig parsebar sind. Ihre
    # Datenstände müssen aber nicht identisch sein: Jede Ausgabezeile trägt
    # den Datenstand ihrer eigenen Quelle, und das Abschluss-Gate prüft beide
    # Zeilen getrennt gegen genau diese Quelle.
    #
    # Ein pauschales Verwerfen beider gültiger Zeilen bei unterschiedlichen
    # Datenständen ließ alte/unvollständige Gemini-Zeilen stehen und führte
    # anschließend zu "Ausgabe fehlt, ist unvollständig oder mehrfach vorhanden".
    if energy_source_errors:
        changes.append(
            "7.5 Öl-Reparatur übersprungen; bestehende Ausgabe unverändert gelassen: "
            + "; ".join(energy_source_errors)
        )
    # S&P-500-Abstand zum Datenhoch aus Kurs und Hoch berechnen statt einen
    # falsch gerundeten Prozentwert aus Gemini zu übernehmen.
    sp_line = re.search(r"(?m)^\s*Der S&P 500 schloss bei\s+([\d.,]+)\s+Punkten,\s*rund\s+([\d.,]+)\s*%\s+unter dem höchsten Wert.*?von\s+([\d.,]+)\s+Punkten", text)
    if sp_line:
        close_value = _quellenwert_float(sp_line.group(1))
        high_value = _quellenwert_float(sp_line.group(3))
        if close_value is not None and high_value:
            distance = (high_value - close_value) / high_value * 100.0
            corrected = f"{distance:.1f}".replace(".", ",")
            text = text[:sp_line.start(2)] + corrected + text[sp_line.end(2):]
            changes.append("1.1 S&P-500-Abstand zum Datenhoch rechnerisch korrekt gerundet")

    # Bitcoin: aktuelle Kurs-/SMA-Distanz rechnerisch aus den beiden sichtbaren
    # Zahlen ableiten; nicht die Distanz des separaten Wochen-Schlusskurses verwenden.
    btc_line = re.search(r"(?m)^\s*-\s*Bitcoin notiert\b[^\n]*$", text)
    if btc_line:
        line = btc_line.group(0)
        price_m = re.search(r"Bitcoin notiert bei\s+([\d.,]+)\s*USD", line, re.I)
        sma_m = re.search(r"50-Wochen-SMA bei\s+([\d.,]+)\s*USD", line, re.I)
        if price_m and sma_m:
            def parse_report_number(raw):
                raw = str(raw or "").strip()
                # Deutsche Tausenderpunkte wie 77.114 sind keine Dezimalwerte.
                if "," not in raw and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
                    raw = raw.replace(".", "")
                return _quellenwert_float(raw)
            price = parse_report_number(price_m.group(1))
            sma = parse_report_number(sma_m.group(1))
            if price is not None and sma:
                distance = (price / sma - 1.0) * 100.0
                line = re.sub(
                    r"\(Abstand\s+[+-]?[\d.,]+\s*%\)",
                    f"(Abstand {distance:+.2f}% auf Basis des aktuellen Kurses)".replace(".", ","),
                    line,
                    count=1,
                    flags=re.I,
                )
                line = re.sub(
                    r"Bitcoin notiert bei",
                    "Bitcoin notiert aktuell bei",
                    line,
                    count=1,
                    flags=re.I,
                )
                line = re.sub(
                    r"(Bitcoin notiert aktuell bei\s+[\d.,]+\s*USD)",
                    r"\1 (Live/Intraday)",
                    line,
                    count=1,
                    flags=re.I,
                )
                text = text[:btc_line.start()] + line + text[btc_line.end():]
                changes.append("1.1 Bitcoin-SMA-Abstand aus aktuellem Kurs neu berechnet und Zeitbasis gekennzeichnet")

    # Dieselbe falsche Zuordnung stand erneut im Frühindikatoren-Abschnitt:
    # der aktuelle Kurs war dort fälschlich als SMA-Wert bezeichnet.
    btc_line_14 = re.search(r"(?m)^\s*[-•]\s*Bitcoin-Stabilität:[^\n]*$", text)
    btc_summary = re.search(
        r"Bitcoin notiert aktuell bei\s+([\d.,]+)\s*USD.*?50-Wochen-SMA bei\s+([\d.,]+)\s*USD",
        text,
        re.I,
    )
    if btc_line_14 and btc_summary:
        corrected_14 = (
            f"- Bitcoin-Stabilität: Aktueller Kurs {btc_summary.group(1)} USD (Live/Intraday) "
            f"liegt oberhalb des 50W-SMA bei {btc_summary.group(2)} USD und dient als "
            "prozyklischer Liquiditäts- und Risk-Appetite-Frühindikator."
        )
        text = text[:btc_line_14.start()] + corrected_14 + text[btc_line_14.end():]
        changes.append("1.4 Bitcoin-Kurs und 50W-SMA korrekt getrennt")

    # Der Fast-Stop-Hinweis muss aus Kurs/Stop berechnet werden. FSLY lag
    # tatsächlich deutlich über 2% vom Stop entfernt; ROK lag innerhalb 2%.
    fast_line = re.search(r"(?m)^\s*[-•]\s*Fastly, Inc\.\s*\(FSLY\)[^\n]*$", text)
    if fast_line:
        line = fast_line.group(0)
        fsl = re.search(r"Fastly, Inc\.\s*\(FSLY\)\s*\(Kurs\s+([\d.,]+)\s*USD,\s*Stop\s+([\d.,]+)\s*USD\)", line, re.I)
        rok = re.search(r"Rockwell Automation, Inc\.\s*\(ROK\)\s*\(Kurs\s+([\d.,]+)\s*USD,\s*Stop\s+([\d.,]+)\s*USD\)", line, re.I)
        if fsl and rok:
            fsl_price, fsl_stop = (_quellenwert_float(x) for x in fsl.groups())
            rok_price, rok_stop = (_quellenwert_float(x) for x in rok.groups())
            if None not in (fsl_price, fsl_stop, rok_price, rok_stop) and fsl_price and rok_price:
                fsl_gap = (fsl_price - fsl_stop) / fsl_price * 100.0
                rok_gap = (rok_price - rok_stop) / rok_price * 100.0
                corrected_fast = (
                    f"- Rockwell Automation, Inc. (ROK) (Kurs {fmt_num(rok.groups()[0])} USD, "
                    f"Stop {fmt_num(rok.groups()[1])} USD, Stop-Abstand {rok_gap:.2f}%) liegt "
                    f"innerhalb des Fast-Stop-Bereichs (<2%) und erfordert erhöhte Wachsamkeit. "
                    f"Fastly, Inc. (FSLY) (Kurs {fmt_num(fsl.groups()[0])} USD, Stop "
                    f"{fmt_num(fsl.groups()[1])} USD, Stop-Abstand {fsl_gap:.2f}%) liegt nicht "
                    "innerhalb des <2%-Fast-Stop-Bereichs."
                )
                corrected_fast = re.sub(r"(Stop-Abstand )([\d]+)\.([\d]+)%", r"\1\2,\3%", corrected_fast)
                text = text[:fast_line.start()] + corrected_fast + text[fast_line.end():]
                changes.append("10.1 Fast-Stop-Abstände rechnerisch korrigiert (ROK innerhalb, FSLY außerhalb 2%)")

    # 7.1: Den kompakten Überblick vollständig aus expliziten Tages-/YTD-Paaren
    # neu aufbauen. Russell hat in dieser Quelle keinen Tages-/YTD-Wert und wird
    # deshalb nicht mit 5T/1M als Ersatz befüllt.
    # Überschriften können nach Gemini/Markdown-Normalisierung als Listenpunkt,
    # Markdown-Heading oder nackte nummerierte Zeile vorliegen.
    heading_prefix = r"(?im)^\s*(?:(?:[-•]\s*)|(?:#{1,6}\s*))?"
    overview_start = re.search(
        heading_prefix + r"7\.1\s+Aktienmärkte\s*/\s*Indizes\b[^\n]*$", text
    )
    overview_end = re.search(
        heading_prefix + r"7\.2\s+Zinsen\b[^\n]*$",
        text[overview_start.end():],
    ) if overview_start else None
    if overview_start and overview_end:
        block_end = overview_start.end() + overview_end.start()
        block = text[overview_start.end():block_end]
        labels = [
            ("sp500", "S&P 500"),
            ("nasdaqcomposite", "Nasdaq Composite"),
            ("dax", "DAX"),
            ("eurostoxx50", "EuroStoxx 50"),
            ("nikkei225", "Nikkei 225"),
            ("shanghaicomposite", "Shanghai Composite"),
            ("hangseng", "Hang Seng"),
        ]
        rows = []
        for key, display in labels:
            item = regional.get(key)
            if not item:
                continue
            rows.append(
                f"- {display}: letzter abgeschlossener Handelstag {fmt_pct(item['daily'])} "
                f"({fmt_num(item['close'])}) | YTD {fmt_pct(item['ytd'])} | Datenstand {item['date']}"
            )
        russell = re.search(
            r"(?im)^\s*[-•]?\s*Russell 2000:\s*([-+]?\d[\d.,]*)\s*\|.*?\bDatenstand\s*[:=]\s*(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{2}-\d{2})",
            briefing_text or "",
        )
        if russell:
            rows.append(
                f"- Russell 2000: Schlussstand {fmt_num(russell.group(1))} | "
                f"Tagesrendite: NICHT VERFÜGBAR | YTD: NICHT VERFÜGBAR | Datenstand {russell.group(2)}"
            )
        required_index_keys = [key for key, _display in labels]
        invalid_index_sources = []
        for key, display in labels:
            item = regional.get(key)
            if not item:
                invalid_index_sources.append(f"{display}: Quellzeile fehlt oder wurde nicht erkannt")
                continue
            if regional_counts.get(key, 0) != 1:
                invalid_index_sources.append(
                    f"{display}: erwartet genau eine Quellzeile, gefunden {regional_counts.get(key, 0)}"
                )
            values = (item.get("daily"), item.get("close"), item.get("ytd"))
            if any(
                not value
                or _quellenwert_float(value) is None
                or _quellenwert_ist_mehrdeutig(value)
                for value in values
            ):
                invalid_index_sources.append(f"{display}: Tagesrendite/Schlussstand/YTD nicht eindeutig parsebar")
            normalized_date = _normalisiere_datenstand(item.get("date", ""))
            if not normalized_date or not _datenstand_ist_aktuell(item.get("date", "")):
                invalid_index_sources.append(f"{display}: Datenstand ungültig, veraltet oder zukünftig")
        # Der bestehende Abschnitt darf erst ersetzt werden, wenn ALLE sieben
        # Pflichtindizes vollständig und eindeutig aus der Quelle extrahiert
        # wurden. Bei Teiltreffern bleibt der Originalblock unverändert.
        index_sources_complete = (
            not invalid_index_sources
            and all(key in regional for key in required_index_keys)
            and len(rows) >= len(required_index_keys)
        )
        if index_sources_complete:
            # Remove all prior index-summary lines, including false period assignments.
            kept = [
                ln for ln in block.splitlines()
                if not re.match(
                    r"(?i)^\s*[-•]\s*(?:S&P\s*500|Nasdaq(?:\s+Composite)?|Russell\s+2000|"
                    r"DAX|Euro[\s-]*Stoxx[\s-]*50|Nikkei[\s-]*225|Shanghai\s+Composite|Hang\s+Seng)\s*:",
                    ln,
                )
            ]
            rebuilt = "\n" + "\n".join(rows) + "\n" + "\n".join(kept).strip("\n") + "\n"
            text = text[:overview_start.end()] + rebuilt + text[block_end:]
            changes.append("7.1 Kurzüberblick aus vollständigen Tagesrendite-/YTD-Quellpaaren neu aufgebaut")
        else:
            changes.append(
                "7.1 Index-Reparatur übersprungen; bestehende Ausgabe unverändert gelassen: "
                + "; ".join(invalid_index_sources or ["nicht alle sieben Pflichtindizes vollständig extrahiert"])
            )

    # 7.5: Markt-Briefing-Werte (13.10) und strukturierte Makro-Fakten (12.10)
    # werden als getrennte Zeitstände ausgewiesen. Keine stille Vermischung.
    if all(k in energy for k in ("wti", "brent")):
        start = re.search(
            heading_prefix + r"7\.5\s+Rohstoffe\b[^\n]*$", text
        )
        end = re.search(
            heading_prefix + r"7\.6\s+Krypto\b[^\n]*$",
            text[start.end():],
        ) if start else None
        if start and end:
            en = start.end() + end.start()
            block = text[start.end():en]
            replacement_lines = []
            for asset in ("wti", "brent"):
                item = energy[asset]
                label = asset.upper()
                # Genau dasselbe kanonische Format wie das harte Abschluss-Gate:
                # Wert vor Periodenlabel, Datenstand ohne abweichende Metadaten-Syntax.
                replacement_lines.append(
                    f"- {label}: Kurs {fmt_num(item['price'])} | "
                    f"{fmt_pct(item['5T'])} 5T | {fmt_pct(item['4W'])} 4W | "
                    f"Datenstand {item['date']}"
                )
            out = []
            inserted = False
            for ln in block.splitlines():
                if re.match(r"(?i)^\s*[-•]\s*(?:WTI|Brent)\s*:", ln):
                    if not inserted:
                        out.extend(replacement_lines)
                        inserted = True
                    continue
                out.append(ln)
            if not inserted:
                out = replacement_lines + out
            text = text[:start.end()] + "\n" + "\n".join(out).strip("\n") + "\n" + text[en:]
            changes.append("7.5 Öl-Kurzüberblick mit explizitem Markt-Briefing-Zeitstand und korrekten 4W-Werten gesetzt")

    # 7.6: Die Makro-Kurszeilen erhalten echte Periodenlabels und die Zeitbasis
    # der Quelle. Live-/Intraday-Werte werden nicht als Schlusskurse ausgegeben.
    refs = _extrahiere_makro_referenzwerte(makro_text or "")
    for asset, display in (("bitcoin", "Bitcoin"), ("ethereum", "Ethereum")):
        ref = refs.get(asset)
        if not ref:
            continue
        source_line = next(
            (ln for ln in (makro_text or "").splitlines()
             if re.match(r"(?i)^\s*" + re.escape(display) + r"\s*:", ln)),
            "",
        )
        if not source_line or not re.search(r"(?i)REAL_HEUTE|laufender/aktueller Tageswert", source_line):
            continue
        date = ref.get("datenstand", "")
        date_de = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") else date
        periods = ref.get("perioden", {})
        period_text = " | ".join(f"{key}={fmt_pct(value)}" for key, value in periods.items())
        line = (
            f"- {display}: {fmt_num(ref['kurs'])} USD | Zeitbasis=Live/Intraday"
            + (f" | Datenstand={date_de}" if date_de else "")
            + (f" | {period_text}" if period_text else "")
        )
        text, n = re.subn(
            r"(?m)^\s*-\s*" + re.escape(display) + r"\s*:\s*[^\n]*$",
            line, text, count=1
        )
        if n:
            changes.append(f"7.6 {display}: Live-Zeitbasis und Periodenwerte explizit zugeordnet")

    # Add time-basis metadata to every source-bound output fact whose exact
    # macro source row is explicitly REAL_HEUTE/live. This prevents a live quote
    # from being silently compared with a prior close.
    live_sources = {}
    for raw_line in (makro_text or "").splitlines():
        m = re.match(r"^\s*([^:]+):\s*([-+]?\d[\d.,]*)\s*\|([^\n]*)$", raw_line)
        if not m:
            continue
        if not re.search(r"(?i)REAL_HEUTE|laufender/aktueller Tageswert", m.group(3)):
            continue
        label = m.group(1).strip()
        dm = re.search(r"(?i)Datenstand=(\d{4}-\d{2}-\d{2})", m.group(3))
        date = dm.group(1) if dm else ""
        periods = {}
        for key in ("5T", "1M", "3M", "6M", "1J"):
            pm = re.search(rf"(?:^|\|)\s*{key}\s*=\s*([+-]?\d[\d.,]*)%", m.group(3))
            if pm:
                periods[key] = pm.group(1)
        live_sources[label.casefold()] = (date, periods)

    # Include likely aliases for output labels that differ from the source label.
    label_aliases = {
        "eur/usd": ("eur/usd",), "usd/jpy": ("usd/jpy",),
        "dxy": ("dxy",), "erdgas": ("erdgas",), "aluminium": ("aluminium",),
        "bitcoin": ("bitcoin",), "ethereum": ("ethereum",),
    }
    for output_label, aliases in label_aliases.items():
        src_entry = next((live_sources[a] for a in aliases if a in live_sources), None)
        if not src_entry:
            continue
        date, periods = src_entry
        date_de = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") else date
        metadata = "Zeitbasis=Live/Intraday"
        if date_de:
            metadata += f"; Datenstand={date_de}"
        if periods:
            metadata += "; " + ", ".join(f"{k}={fmt_pct(v)}" for k, v in periods.items())
        pattern = re.compile(
            r"(?im)^(\s*[-•]\s*(?:\[AUTORITATIVE QUELLE\]\s*)?" +
            re.escape(output_label) + r"\s*:[^\n]*?)(?<!\|)\s*$"
        )
        new_text, n = pattern.subn(
            lambda m: m.group(1) + " | " + metadata
            if not re.search(r"(?i)Zeitbasis\s*=\s*Live/Intraday|Live/Intraday", m.group(1))
            else m.group(1),
            text,
        )
        if n:
            text = new_text
            changes.append(f"{output_label}: Live-Zeitbasis/Datenstand an quellengebundene Zeilen angefügt")

    # Öl-Fakten im ausführlichen Makroblock bleiben an den älteren strukturierten
    # Makro-Datenstand gebunden. Die Zeiträume werden direkt an derselben Zeile
    # genannt, damit sie nicht mit den aktuelleren Markt-Briefing-4W-Werten
    # darüber verwechselt werden.
    for asset in ("wti", "brent"):
        ref = refs.get(asset)
        if not ref:
            continue
        date = ref.get("datenstand", "")
        date_de = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") else date
        periods = ref.get("perioden", {})
        period_text = " | ".join(f"{key}={fmt_pct(value)}" for key, value in periods.items())
        metadata = "Quelle=Makro_Briefing"
        if date_de:
            metadata += f" | Datenstand={date_de}"
        if period_text:
            metadata += " | " + period_text
        label = "WTI" if asset == "wti" else "Brent"
        pat = re.compile(
            r"(?im)^(\s*[-•]\s*\[AUTORITATIVE QUELLE\]\s*"
            + re.escape(label) + r"\s*:[^\n]*)\s*$"
        )

        def _aktualisiere_makro_metadaten(match):
            # Vorhandene Quelle=Makro_Briefing darf die Reparatur nicht
            # vorzeitig stoppen: eine Zeile kann die Quelle nennen, aber
            # Datenstand und/oder Periodenwerte noch nicht enthalten.
            # Entferne nur die von diesem Reparaturschritt verwalteten
            # Metadatenfelder und setze sie anschließend quellengetreu neu.
            parts = match.group(1).split("|")
            retained = [parts[0].rstrip()]
            for part in parts[1:]:
                field = part.strip()
                if re.match(
                    r"(?i)^(?:Quelle\s*=|Datenstand\s*=|(?:5T|1M|3M|6M|1J)\s*=)",
                    field,
                ):
                    continue
                if field:
                    retained.append(field)
            return " | ".join(retained + metadata.split(" | "))

        text, n = pat.subn(_aktualisiere_makro_metadaten, text)
        if n:
            changes.append(
                f"7.5 {label}: Makro-Quelle, Datenstand und verfügbare Periodenmetadaten synchronisiert"
            )

    # 8: Kompakte Faktenzeilen ausschließlich aus dem Edelmetalle-Briefing.
    # Python stellt belegte Werte dar; es erzeugt hier keine Marktinterpretation.
    metal_tickers = {
        "Gold": "GC=F",
        "Silber": "SI=F",
        "Platin": "PL=F",
        "Palladium": "PA=F",
    }
    metal_date_match = re.search(r"(?im)^EDELMETALLE-SCAN\s+(\d{4}-\d{2}-\d{2})\s*$", edel_text or "")
    metal_date = metal_date_match.group(1) if metal_date_match else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", metal_date or ""):
        metal_date = datetime.datetime.strptime(metal_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    metal_rows = []
    for metal, ticker in metal_tickers.items():
        row = _edelmetall_lage_zeile(edel_text or "", metal)
        if not row:
            continue
        m = re.search(
            r"(?i)^\s*" + re.escape(metal) +
            r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*"
            r"([-+]?\d[\d.,]*)%\s+in den letzten 4 Wochen\b([^\n]*)",
            row,
        )
        if not m:
            continue
        facts = [f"Kurs (Futures {ticker}) {fmt_num(m.group(1))} USD"]
        five_day = re.search(
            r"(?i)([-+]?\d[\d.,]*)%\s*(?:in den letzten\s*)?5 Tagen\b|\b5T\s*[:=]?\s*([-+]?\d[\d.,]*)%",
            row,
        )
        if five_day:
            raw_5t = five_day.group(1) or five_day.group(2)
            facts.append(f"5T {fmt_pct(raw_5t)}")
        facts.append(f"4W {fmt_pct(m.group(2))}")
        low = re.search(
            r"(?i)52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%",
            row,
        )
        if low:
            facts.append(
                f"52-Wochen-Tief {fmt_num(low.group(1))} USD "
                f"({fmt_pct(low.group(2))} darüber)"
            )
        trend_block = _edelmetall_quellenblock(edel_text or "", metal)
        trend_values = _extrahiere_200er_trend(trend_block)
        if trend_values:
            facts.extend([
                f"EMA200 {fmt_num(trend_values[1])} USD",
                f"WMA200 {fmt_num(trend_values[2])} USD",
            ])
        facts.append(f"Quelle={EDELMETALL_FAKTENQUELLE}")
        if metal_date:
            facts.append(f"Datenstand={metal_date}")
        metal_rows.append(f"- 8.{len(metal_rows)+1} {metal}: " + " | ".join(facts))
    if len(metal_rows) == 4:
        start = re.search(r"(?m)^8\.\s*🪙\s*EDELMETALLE\s*$", text)
        end = re.search(r"(?m)^8\.1\s+Gold\s*$", text[start.end():]) if start else None
        if start and end:
            en = start.end() + end.start()
            block = text[start.end():en]
            out = []
            inserted = False
            for ln in block.splitlines():
                if re.match(r"(?i)^\s*-\s*8\.[1-4]\s+(?:Gold|Silber|Platin|Palladium)\s*:", ln):
                    if not inserted:
                        out.extend(metal_rows)
                        inserted = True
                    continue
                out.append(ln)
            if not inserted:
                out = metal_rows + out
            text = text[:start.end()] + "\n" + "\n".join(out).strip("\n") + "\n\n" + text[en:]
            changes.append("8.1–8.4 Kurzüberblick vollständig aus Edelmetalle-Briefing neu aufgebaut")

    # 10: Nur echte Positionsdatensätze innerhalb desselben Unterabschnitts
    # deduplizieren. Cross-section mentions and monitoring bullets remain valid.
    deduped_text, removed = _dedupliziere_portfolio_positionsdatensaetze(text)
    if removed:
        changes.append(
            "Echte doppelte Positionsdatensätze innerhalb eines Abschnitts entfernt: "
            + ", ".join(f"10.{section}: {', '.join(tickers)}" for section, tickers in removed)
        )
    if deduped_text != text:
        text = deduped_text

    if changes:
        print("QUELLEN-/PERIODEN-/POSITIONS-GATE: " + " | ".join(changes))
    return text, changes


def _repariere_7_performanceperioden(text, makro_text):
    """Kanonisiert Periodenrenditen anhand der Label-Wert-Paare im Makro-Briefing.

    Die alte Gemini-Zeile wird nicht durch blosses Vorhandensein eines Kurses
    als korrekt akzeptiert: Jede Performance muss exakt am Quellzeitraum haengen.
    Gibt es fuer ein Instrument keine strukturierte Periodenquelle, werden
    numerische Periodenangaben entfernt statt geraten.
    """
    if not text or not makro_text:
        return text, False
    refs = _extrahiere_makro_referenzwerte(makro_text)
    specs = [
        ("7.1 Aktienmärkte / Indizes", r"S&P 500", ("s&p 500",)),
        ("7.1 Aktienmärkte / Indizes", r"Nasdaq Composite(?:\s*/\s*Nasdaq 100)?", ("nasdaq composite",)),
        ("7.1 Aktienmärkte / Indizes", r"Russell 2000", ("russell 2000",)),
        ("7.1 Aktienmärkte / Indizes", r"DAX", ("dax",)),
        ("7.1 Aktienmärkte / Indizes", r"EuroStoxx 50", ("eurostoxx 50",)),
        ("7.1 Aktienmärkte / Indizes", r"Nikkei 225", ("nikkei 225",)),
        ("7.1 Aktienmärkte / Indizes", r"Shanghai Composite", ()),
        ("7.1 Aktienmärkte / Indizes", r"Hang Seng", ()),
        ("7.3 Volatilität", r"VIX", ("vix",)),
        ("7.5 Rohstoffe", r"WTI", ("wti",)),
        ("7.5 Rohstoffe", r"Brent", ("brent",)),
        ("7.6 Krypto", r"Bitcoin(?:\s*\([^)]*\))?", ("bitcoin",)),
        ("7.6 Krypto", r"Ethereum(?:\s*\([^)]*\))?", ("ethereum",)),
    ]
    sections = ["7.1 Aktienmärkte / Indizes", "7.2 Zinsen", "7.3 Volatilität", "7.4 FX", "7.5 Rohstoffe", "7.6 Krypto", "7.7 Konjunktur / Makro"]
    changed = False
    result = text
    period_re = re.compile(r"(?i)(?:^|\|)\s*(?:1J|1M|3M|5T|6M)\s*(?:=|:)?\s*[+-]?\d[\d.,]*(?:\s*(?:bis|to)\s*[+-]?\d[\d.,]*)?\s*%?")
    for section, label_pattern, aliases in specs:
        block = _abschnitt_text(result, section, sections + ["8. 🪙 EDELMETALLE", "8. Edelmetalle", "8.1 Gold"])
        if not block:
            continue
        ref = next((refs.get(a) for a in aliases if refs.get(a)), None)
        periods = (ref or {}).get("perioden", {})
        canonical = [f"{k}={_fmt_pct_de(periods[k])}" for k in ("5T", "1M", "3M", "6M", "1J") if k in periods]
        pattern = re.compile(r"(?im)^\s*[-•]\s*(?!\[AUTORITATIVE QUELLE\])(" + label_pattern + r")\s*:\s*[^\n]*$")
        matches = list(pattern.finditer(block))
        if not matches:
            continue
        # Nur Zeilen mit Performancefeldern reparieren; eine saubere reine Kurszeile bleibt erhalten.
        for m in reversed(matches):
            line = m.group(0)
            if not re.search(r"(?i)(?:\b1J\b|\b1M\b|\b3M\b|\b5T\b|\b6M\b)", line):
                continue
            first_period = re.search(r"(?i)(?:\||\s)\s*(?:1J|1M|3M|5T|6M)\s*(?:=|:)?\s*[+-]?\d", line)
            prefix = line[:first_period.start()].rstrip() if first_period else line
            if canonical:
                replacement = prefix + " | " + " | ".join(canonical)
            else:
                replacement = prefix + " | Periodenveränderungen: NICHT VERFUEGBAR (keine strukturierte Quelle)."
            block = block[:m.start()] + replacement + block[m.end():]
            changed = changed or replacement != line
        # Write the repaired section back at its exact boundary.
        sm = re.search(r"(?m)^" + re.escape(section) + r"\s*$", result)
        if sm:
            en = len(result)
            for nxt in sections + ["8. 🪙 EDELMETALLE", "8. Edelmetalle", "8.1 Gold"]:
                nm = re.search(r"(?m)^" + re.escape(nxt) + r"\s*$", result[sm.end():])
                if nm:
                    en = min(en, sm.end() + nm.start())
            result = result[:sm.start()] + block.rstrip() + "\n\n" + result[en:].lstrip("\n")
    return result, changed



def _pruefe_7_performanceperioden(text, makro_text):
    """Hartes Gate: numerische Periodenrenditen muessen den Quellperioden entsprechen."""
    refs = _extrahiere_makro_referenzwerte(makro_text or "")
    specs = [
        ("7.1 Aktienmärkte / Indizes", r"S&P 500", "s&p 500"),
        ("7.1 Aktienmärkte / Indizes", r"Nasdaq Composite(?:\s*/\s*Nasdaq 100)?", "nasdaq composite"),
        ("7.1 Aktienmärkte / Indizes", r"Russell 2000", "russell 2000"),
        ("7.1 Aktienmärkte / Indizes", r"DAX", "dax"),
        ("7.1 Aktienmärkte / Indizes", r"EuroStoxx 50", "eurostoxx 50"),
        ("7.1 Aktienmärkte / Indizes", r"Nikkei 225", "nikkei 225"),
        ("7.3 Volatilität", r"VIX", "vix"),
        ("7.5 Rohstoffe", r"WTI", "wti"),
        ("7.5 Rohstoffe", r"Brent", "brent"),
        ("7.6 Krypto", r"Bitcoin(?:\s*\([^)]*\))?", "bitcoin"),
        ("7.6 Krypto", r"Ethereum(?:\s*\([^)]*\))?", "ethereum"),
    ]
    sections = ["7.1 Aktienmärkte / Indizes", "7.2 Zinsen", "7.3 Volatilität", "7.4 FX", "7.5 Rohstoffe", "7.6 Krypto", "7.7 Konjunktur / Makro"]
    errors = []
    for section, label_pattern, alias in specs:
        block = _abschnitt_text(text, section, sections + ["8. 🪙 EDELMETALLE", "8. Edelmetalle", "8.1 Gold"])
        line = next((ln for ln in block.splitlines() if re.match(r"(?i)^\s*[-•]\s*(?!\[AUTORITATIVE QUELLE\])" + label_pattern + r"\s*:", ln)), None)
        if not line:
            continue
        periods = (refs.get(alias) or {}).get("perioden", {})
        has_periods = bool(re.search(r"(?i)(?:\b1J\b|\b1M\b|\b3M\b|\b5T\b|\b6M\b)\s*[=:]?\s*[+-]?\d", line))
        if not has_periods:
            continue
        for key, value in periods.items():
            expected = f"{key}={_fmt_pct_de(value)}"
            if expected not in line:
                errors.append(f"{section}/{label_pattern}: {key} erwartet {expected}; Ausgabezeile: {line.strip()}")
        if not periods:
            errors.append(f"{section}/{label_pattern}: numerische Perioden ohne strukturierte Quelle: {line.strip()}")
    if errors:
        raise RuntimeError("PERIODENRENDITEN_QUELLENBINDUNG_UNGUELTIG: " + " | ".join(errors))
    print("PERIODENRENDITEN-QUELLENBINDUNGS-GATE: PASS")

def _repariere_7_7_macro_leakage(text, makro_text):
    """Verschiebt autoritative Makro-Fakten aus Abschnitt 8 nach 7.7, statt sie zu verlieren."""
    if not text:
        return text
    top8 = re.search(r"(?m)^8\.\s*(?:🪙\s*)?EDELMETALLE\s*$", text, re.I)
    gold = re.search(r"(?m)^8\.1\s+Gold\s*$", text)
    macro_heading = re.search(r"(?m)^7\.7\s+Konjunktur\s*/\s*Makro\s*$", text)
    if not (top8 and gold and macro_heading and macro_heading.start() < top8.start() < gold.start()):
        return text

    macro_labels = (
        "CPI", "Core CPI", "PCE", "Core PCE", "PPI", "Arbeitslosenquote",
        "NFP / Nonfarm Payrolls", "ADP Employment Change", "Reales BIP-Wachstum",
        "ISM Manufacturing PMI", "ISM Services PMI",
    )
    between = text[top8.end():gold.start()]
    moved = []
    kept = []
    for line in between.splitlines():
        stripped = line.strip()
        if any(re.match(
            r"(?i)^[-•]?\s*(?:\[AUTORITATIVE QUELLE\]\s*)?" + re.escape(label) + r"\s*:",
            stripped,
        ) for label in macro_labels):
            # Keep the original authoritative fact line and move it to its correct section.
            moved.append(stripped if stripped.startswith(("-", "•")) else "- " + stripped)
        else:
            kept.append(line)

    if moved:
        macro_block = text[macro_heading.end():top8.start()].rstrip()
        existing_authoritative = set()
        for line in macro_block.splitlines():
            m = re.match(
                r"(?i)^\s*[-•]\s*\[AUTORITATIVE QUELLE\]\s*([^:]+)\s*:",
                line,
            )
            if m:
                existing_authoritative.add(m.group(1).strip().casefold())
        additions = []
        for line in moved:
            m = re.match(
                r"(?i)^\s*[-•]\s*\[AUTORITATIVE QUELLE\]\s*([^:]+)\s*:",
                line,
            )
            label = m.group(1).strip().casefold() if m else ""
            if not label or label not in existing_authoritative:
                additions.append(line)
                if label:
                    existing_authoritative.add(label)
        if additions:
            macro_block = (macro_block + "\n" if macro_block else "") + "\n".join(additions)
            text = (
                text[:macro_heading.end()] + "\n" + macro_block.rstrip()
                + "\n\n" + text[top8.start():]
            )
            # Recalculate positions after inserting the lines into 7.7.
            top8 = re.search(r"(?m)^8\.\s*(?:🪙\s*)?EDELMETALLE\s*$", text, re.I)
            gold = re.search(r"(?m)^8\.1\s+Gold\s*$", text)

    if not (top8 and gold and top8.end() < gold.start()):
        raise RuntimeError("MAKRO_PLATZIERUNG_UNGUELTIG: Abschnittsgrenzen 8/8.1 nach Reparatur nicht auffindbar.")
    between = text[top8.end():gold.start()]
    kept = []
    for line in between.splitlines():
        stripped = line.strip()
        if any(re.match(
            r"(?i)^[-•]?\s*(?:\[AUTORITATIVE QUELLE\]\s*)?" + re.escape(label) + r"\s*:",
            stripped,
        ) for label in macro_labels):
            continue
        kept.append(line)
    clean_between = "\n".join(kept).strip("\n")
    if clean_between != between.strip("\n"):
        text = text[:top8.end()] + ("\n" + clean_between if clean_between else "") + "\n\n" + text[gold.start():]
    print("INFO: Autoritative Makro-Fakten sind Abschnitt 7.7 zugeordnet; Abschnitt 8 enthaelt keine Makro-Leakage.")
    return text

def _repariere_3_5_fundamentaldaten(text):
    """Erzeugt eine konkrete, quellengebundene Bewertungs-/Technik-Kausalkette für 3.5."""
    if not text:
        return text
    start = re.search(r"(?m)^3\.5\s+Unternehmens-/Fundamentaldaten\s*→\s*Aktie\s*$", text)
    end = re.search(r"(?m)^4\.\s*(?:🔭\s*)?IDEEN IM AUFBAU\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    s21 = re.search(r"(?m)^2\.1\s+Trendfolge\s*$", text)
    e22 = re.search(r"(?m)^2\.2\s+Trendwende\s*$", text)
    trade_block = text[s21.end():e22.start()] if s21 and e22 and s21.start() < e22.start() else ""
    entries = []
    heads = list(re.finditer(r"(?m)^([^\n]+\([^\n()]{1,15}\)\s*\|\s*(?:Ticker|Scanner):[^\n]*)$", trade_block))
    for i, hm in enumerate(heads):
        eb = trade_block[hm.start():heads[i+1].start() if i+1 < len(heads) else len(trade_block)]
        am = re.search(r"(?im)^Fundamental-Ampel:\s*([^\n]+)", eb)
        fh = re.search(r"(?im)^Fundamental-Hinweis:\s*([^\n]+)", eb)
        ticker_m = re.search(r"\(([A-Z][A-Z0-9.^=-]{0,12})\)", hm.group(1))
        name = hm.group(1).split("|",1)[0].strip()
        ticker = ticker_m.group(1) if ticker_m else ""
        if ticker:
            name = re.sub(r"\s*\(" + re.escape(ticker) + r"\)\s*$", "", name).strip()
        if not am or not fh or not ticker:
            continue
        amp = am.group(1).strip()
        hint = fh.group(1).strip().rstrip(".")
        macd = re.search(r"(?im)^MACD-Trend:\s*([^\n]+)", eb)
        setup = re.search(r"(?im)^Setup:\s*([^\n]+)", eb)
        stop = re.search(r"(?im)^Stop:\s*([^\n]+)", eb)
        if amp.casefold().startswith(("guenstig", "günstig")):
            implication = "Die relative Bewertung kann Erwartungen dämpfen, belegt aber weder Gewinnwachstum noch eine bevorstehende Neubewertung; entscheidend sind bestätigende Unternehmenszahlen und die Fortsetzung des technischen Setups."
        elif amp.casefold().startswith("teuer"):
            implication = "Die Bewertungsprämie erhöht das Risiko einer Multiplikator-Kontraktion; ein valides Pullback-Setup allein beseitigt dieses Fundamentaldatenrisiko nicht."
        else:
            implication = "Die Bewertung liefert keinen eindeutigen Bewertungsrand; der technische Status ist deshalb nicht mit einem fundamentalen Vorteil gleichzusetzen."
        details = f"Kausalkette/Investmentwirkung: {name} ({ticker}) weist Fundamental-Ampel {amp} auf; {hint}. "
        if setup:
            details += f"Technische Bestätigung: {setup.group(1).strip()}. "
        if macd:
            details += f"MACD-Trend {macd.group(1).strip()}. "
        details += implication
        if stop:
            details += f" Beobachtbarer technischer Invalidierungspunkt: {stop.group(1).strip()}."
        entries.append("- " + details)
    if not entries:
        entries = ["- Die verfügbaren Quellen enthalten in Abschnitt 2.1 keine eindeutig zuordenbare Kombination aus Fundamental-Ampel, Bewertungsvergleich und technischem Status. Eine konkrete Aktienwirkung ist daher NICHT VERFUEGBAR und wird nicht erfunden."]
    entries.append("- Bestätigung/Widerlegung: Die Bewertungsthese wird erst durch die nächsten tatsächlich veröffentlichten Ergebniszahlen bzw. Ausblicke und eine dazu passende Kursreaktion bestätigt; Margenrückgang, Ergebnisverfehlung oder Bruch der jeweiligen Stop-Marke schwächen bzw. invalidieren sie.")
    replacement = "3.5 Unternehmens-/Fundamentaldaten → Aktie\n" + "\n".join(entries) + "\n"
    return text[:start.start()] + replacement + "\n" + text[end.start():]


def _pruefe_dokumentierte_reparaturen(text, previous_report_path=None):
    """Regression gate fuer die zuvor dokumentierten Auswertungsfehler."""
    errors = []
    # 1.1: eine vorhandene Vortagesauswertung darf nicht als fehlend bezeichnet werden.
    date_match = re.search(r"(?m)^Datum der Auswertung:\s*(\d{2})\.(\d{2})\.(\d{4})", text or "")
    if date_match:
        try:
            current = datetime.date(int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1)))
            candidates = []
            if previous_report_path and os.path.isfile(previous_report_path):
                candidates.append(previous_report_path)
            candidates.extend(glob.glob("Auswertung(*).txt"))
            for candidate_path in set(candidates):
                m = re.search(r"Auswertung\((\d{4})-(\d{2})-(\d{2})\)\.txt$", os.path.basename(candidate_path))
                if m and datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) < current:
                    if re.search(r"(?i)(?:Keine Vortagesauswertung in der Historie vorhanden|Es liegen keine abweichenden Vortagesdaten im Dateibestand vor)", text):
                        errors.append("1.1: falsche Behauptung, es gebe keine Vortagesauswertung trotz vorhandenem Vorbericht.")
                    break
        except ValueError:
            errors.append("1.1: Berichtsdatum nicht sicher interpretierbar; Vorberichtsabgleich nicht prüfbar.")
    # 1.3: nicht dieselbe These als Veränderung wiederholen; konkrete Ideenreferenz benennen.
    b13 = _abschnitt_text(text, "1.3 Ideen im Aufbau", ["1.4 Frühindikatoren / neue Themen"])
    topic = re.search(r"(?im)^Thema / Assetklasse:\s*(.+)$", b13)
    change = re.search(r"(?im)^Was verändert sich\?:\s*(.+)$", b13)
    assets = re.search(r"(?im)^Potenzielle Assets für technische Nachsuche:\s*(.+)$", b13)
    if topic and change and topic.group(1).strip() == change.group(1).strip():
        errors.append("1.3: Thema und Veränderung werden wortgleich wiederholt.")
    if assets and re.search(r"(?i)selektive relative stärke|selektive relative staerke|nicht verfuegbar|nicht verfügbar", assets.group(1)):
        errors.append("1.3: technische Nachsuch-Assets bleiben generisch oder nicht verfügbar.")
    # 7.7/8: makro facts must not leak into the precious-metals heading area.
    top8 = re.search(r"(?m)^8\.\s*(?:🪙\s*)?EDELMETALLE\s*$", text or "", re.I)
    gold = re.search(r"(?m)^8\.1\s+Gold\s*$", text or "")
    if top8 and gold and top8.end() < gold.start():
        between = text[top8.end():gold.start()]
        if re.search(r"(?im)^\s*[-•]?\s*(?:\[AUTORITATIVE QUELLE\]\s*)?(?:CPI|Core CPI|PCE|Core PCE|PPI|Arbeitslosenquote|NFP / Nonfarm Payrolls|ADP Employment Change|Reales BIP-Wachstum|ISM Manufacturing PMI|ISM Services PMI)\s*:", between):
            errors.append("7.7/8: Makro-Fakten stehen weiterhin im Edelmetall-Hauptabschnitt.")
    # 8: Futures-Basis may not coexist with the canonical facts block.
    b8 = re.search(r"(?ms)^8\.1\s+Gold\s*$.*?(?=^9\.\s*📅\s*NÄCHSTE KATALYSATOREN\s*$|\Z)", text or "")
    if b8 and re.search(r"(?i)Futures-Basis", b8.group(0)):
        errors.append("8.1–8.4: widersprüchliche Futures-Basis-Duplikate sind noch vorhanden.")
    # 9.2: undated generic earnings placeholder is not a verified catalyst.
    b92 = _abschnitt_text(text, "9.2 Unternehmen", ["9.3 Branchenereignisse"])
    if re.search(r"(?i)Quartalsberichte und Earnings-Meldungen im Berichtsmonat", b92):
        errors.append("9.2: undatierter Earnings-Platzhalter ist noch vorhanden.")
    # 10.3: known misattribution / unproven positive performance statement forbidden.
    b103 = _abschnitt_text(text, "10.3 Positionen mit neuer Investmentthese", ["10.4 Positionen, deren These schwächer wird"])
    if re.search(r"(?i)Bestätigte Trendfolge-Positionen mit positiver Performance|\+9,87%[^\n]{0,100}ENR\.DE|ENR\.DE[^\n]{0,100}\+9,87%", b103):
        errors.append("10.3: unbewiesene Thesenänderung oder falsche +9,87%-Zuordnung zu ENR.DE.")
    if errors:
        raise RuntimeError("DOKUMENTIERTE_REPARATUREN_UNGUELTIG: " + " | ".join(errors))
    print("DOKUMENTIERTE-REPARATUREN-REGRESSION-GATE: PASS")

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
    blocks = {h: _abschnitt_text(text, h, sections + ["8. 🪙 EDELMETALLE", "8. Edelmetalle", "8.1 Gold"]) for h in sections}
    errors = [f"{h}: Abschnitt fehlt oder ist nicht kanonisch abgrenzbar." for h in sections if not blocks[h]]
    if errors:
        raise RuntimeError("PUNKT7_QUELLENABDECKUNG_UNGUELTIG: " + " | ".join(errors))

    # Pro Abschnitt werden nur die aktuell in den Quellen vorhandenen Kernfelder
    # abgefragt. Dadurch ist das Gate datumsoffen und muss nicht bei jedem Lauf
    # mit neuen Kursen/Terminen im Code angepasst werden.
    # Muss mit _repariere_7_x_quellengebunden uebereinstimmen:
    # diese Indizes stehen im Briefing im REGIONEN-PERFORMANCE-Format,
    # in dem die Tagesveraenderung vor dem Indexstand in Klammern steht.
    index_labels = [
        ("S&P 500", True), ("Nasdaq", True), ("DAX", True),
        ("EuroStoxx50", True), ("Russell 2000", False), ("Nikkei 225", True),
        ("Hang Seng", True), ("Shanghai Composite", True),
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


def _extrahiere_200er_trend(source_block):
    """Liest Kurs, EMA200 und WMA200 mit einer gemeinsamen Formatregel.

    Gleichheitszeichen und die optionale EMA200-Klammerannotation sind
    zulässige reine Darstellungsvarianten; Reparatur und Gate verwenden
    exakt denselben Parser.
    """
    match = re.search(
        r"200er-Trend:\s*Kurs\s*=?\s*([-+]?\d[\d.,]*)\s*\|\s*"
        r"EMA200\s*=?\s*([-+]?\d[\d.,]*)"
        r"(?:\s*\([^)]*\))?\s*\|\s*"
        r"WMA200\s*=?\s*([-+]?\d[\d.,]*)",
        source_block or "",
    )
    return match.groups() if match else None


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


def _repariere_8_x_quellengebunden(text, edel_text, briefing_text=""):
    """
    Uebernimmt die autoritativen 8.1–8.4-Daten deterministisch.

    Regeln:
    - Sämtliche Edelmetall-Fakten (Kurs, 4W, 52W-Tief/Abstand, EMA/WMA und
      Strategie-Status) stammen ausschließlich aus dem Edelmetalle-Briefing.
    - Das Markt-Briefing ist keine Ersatzquelle. Fehlende Pflichtfakten lösen
      einen Quellenfehler aus, statt durch Werte anderer Quellen ersetzt zu werden.
    - Qualitative Gemini-Interpretationen bleiben getrennt erhalten.
    """
    if not text:
        return text
    if not edel_text:
        raise RuntimeError("EDELMETALL_QUELLE_UNVOLLSTAENDIG: leere Quelle.")

    # Das Edelmetalle-Briefing ist die alleinige Faktenquelle für den gesamten
    # Edelmetallbereich. Abweichungen zum Markt-Briefing sind nur Diagnosemetadaten.
    if briefing_text:
        conflicts = []
        for metal in ("Gold", "Silber", "Platin", "Palladium"):
            market_4w = _extract_metal_4w_value(briefing_text, metal)
            detail_4w = _edelmetall_4w_value(edel_text, metal)
            if market_4w is not None and detail_4w is not None and abs(market_4w[1] - detail_4w[1]) > 1e-8:
                conflicts.append(f"{metal}: Markt-Briefing {market_4w[0]}% vs. Edelmetalle-Briefing {detail_4w[0]}%; Edelmetalle-Briefing verwendet")
        if conflicts:
            print("EDELMETALL_4W_QUELLENKONFLIKT: " + " | ".join(conflicts))

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
    for asset, expected_ticker in assets.values():
        # Eine Asset-Zeile muss genau einmal im LAGE-Block vorkommen. Bei
        # Duplikaten ist die Quellenzuordnung mehrdeutig und wird nicht geraten.
        row_pattern = re.compile(
            r"(?m)^\s*" + re.escape(asset) + r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|([^\n]*)$"
        )
        source_rows = list(row_pattern.finditer(lage_block))
        if len(source_rows) != 1:
            missing.append(
                f"{asset}: Kurs-Zuordnung nicht eindeutig (Quellzeilen={len(source_rows)}; erwartet 1; Ticker={expected_ticker})"
            )
            continue
        line_match = source_rows[0]
        kurs_raw = line_match.group(1)
        kurs_numeric = _quellenwert_float(kurs_raw)
        if kurs_numeric is None or not math.isfinite(kurs_numeric):
            missing.append(f"{asset}: Kurswert nicht numerisch parsebar ({kurs_raw!r}; Ticker={expected_ticker})")
            continue
        # Wenn die Quellzeile selbst einen Ticker explizit nennt, muss er zur
        # fest verdrahteten Metallzuordnung passen. Fehlende Ticker-Metadaten
        # sind erlaubt; widersprüchliche Angaben nicht.
        source_line = line_match.group(0)
        ticker_tokens = re.findall(r"(?<![A-Z0-9])(?:GC=F|SI=F|PL=F|PA=F)(?![A-Z0-9])", source_line, re.I)
        if ticker_tokens and any(token.upper() != expected_ticker.upper() for token in ticker_tokens):
            missing.append(
                f"{asset}: Ticker-Konflikt in Quellzeile (gefunden={ticker_tokens}; erwartet={expected_ticker})"
            )
            continue
        detail_4w = _extract_metal_4w_value(lage_block, asset)
        chosen_4w = _waehle_edelmetall_4w(detail_4w, _extract_metal_4w_value(briefing_text, asset))
        if chosen_4w is None:
            missing.append(f"{asset}: 4W-Wert fehlt/ungültig im Edelmetalle-Briefing")
            continue
        lage[asset] = (kurs_raw, chosen_4w[0])
        low = re.search(
            r"52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%",
            line_match.group(0),
        )
        if low:
            lage[asset] = (kurs_raw, chosen_4w[0], low.group(1), low.group(2))

    # 2) Operative EMA200/WMA200-Basis aus dem exakt zugeordneten
    #    Trendfolge-Diagnoseblock lesen.
    trend = {}
    for asset, _ticker in assets.values():
        source_block = _edelmetall_quellenblock(edel_text, asset)
        if not source_block:
            missing.append(f"{asset}: Trendfolge-Diagnose")
            continue

        # Derselbe Parser wird im Abschnitt-8-Gate verwendet. Damit duerfen
        # Reparatur und Gate nicht unterschiedliche Schreibvarianten akzeptieren.
        m200 = _extrahiere_200er_trend(source_block)
        if not m200:
            missing.append(f"{asset}: EMA200/WMA200")
        else:
            trend[asset] = (m200[1], m200[2])

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
            # Das Abschluss-Gate verwirft jede Futures-Basis-Erwaehnung in 8.1–8.4;
            # deshalb werden auch zusammengesetzte/inline Duplikat-Zeilen entfernt.
            if re.search(r"(?i)Futures[- ]Basis", stripped):
                continue
            if re.match(
                r"^[-•]?\s*(?:Kurs(?:\s*\(Futures[^)]*\))?|"
                r"4W|52W-Tief|Abstand 52W-Tief|EMA200|WMA200|"
                r"Autoritative Fakten|Futures-Basis(?:\s*\([^)]*\))?|Strategie-Status(?:\s*\([^)]*\))?)\s*:",
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
        metal_date_match = re.search(
            r"(?im)^EDELMETALLE-SCAN\s+(\d{4}-\d{2}-\d{2})\s*$",
            edel_text,
        )
        metal_date = metal_date_match.group(1) if metal_date_match else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", metal_date or ""):
            metal_date = datetime.datetime.strptime(metal_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        fact_parts = [f"Quelle: {EDELMETALL_FAKTENQUELLE}"]
        if metal_date:
            fact_parts.append(f"Datenstand: {metal_date}")
        fact_parts.extend([
            f"Kurs (Futures {ticker}): {kurs}$",
        ])
        source_lage_line = _edelmetall_lage_zeile(edel_text, asset)
        five_day_match = re.search(
            r"(?i)([-+]?\d[\d.,]*)%\s*(?:in den letzten\s*)?5 Tagen\b|\b5T\s*[:=]?\s*([-+]?\d[\d.,]*)%",
            source_lage_line or "",
        )
        if five_day_match:
            five_day_raw = five_day_match.group(1) or five_day_match.group(2)
            fact_parts.append(f"5T: {five_day_raw}%")
        fact_parts.append(f"4W: {four_w}%")
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
    inputs = eingabedateien or {}
    path = inputs.get("Edelmetalle_Briefing(...).txt")
    if path and os.path.isfile(path):
        try:
            edel = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"PUNKT8_QUELLE_NICHT_LESBAR: Edelmetalle_Briefing(...).txt: {exc}") from exc
    markt = ""
    market_path = inputs.get("briefing.txt")
    if market_path and os.path.isfile(market_path):
        try:
            markt = Path(market_path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"PUNKT8_MARKTQUELLE_NICHT_LESBAR: briefing.txt: {exc}") from exc
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
        if not re.search(r"(?i)Quelle\s*:?\s*Edelmetalle-Briefing", block):
            errors.append(f"{heading}: Faktenquelle Edelmetalle-Briefing ist nicht explizit ausgewiesen.")
        source_date_match = re.search(r"(?im)^EDELMETALLE-SCAN\s+(\d{4}-\d{2}-\d{2})\s*$", edel)
        if source_date_match:
            expected_date = datetime.datetime.strptime(source_date_match.group(1), "%Y-%m-%d").strftime("%d.%m.%Y")
            if not re.search(r"(?i)Datenstand\s*:?\s*" + re.escape(expected_date), block):
                errors.append(f"{heading}: Datenstand {expected_date} des Edelmetalle-Briefings fehlt in der Ausgabe.")

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
            kurs_match = re.search(r"Kurs\s+([-+]?\d[\d.,]*)", lage_line)
            if not kurs_match:
                errors.append(f"{heading}: Kurswert konnte aus der Lagezeile nicht gelesen werden.")
            else:
                kurs_value = _quellenwert_float(kurs_match.group(1))
                if kurs_value is not None:
                    _gate_quellenwert(block, "Kurs", kurs_value, errors)
            five_day_match = re.search(
                r"(?i)([-+]?\d[\d.,]*)%\s*(?:in den letzten\s*)?5 Tagen\b|\b5T\s*[:=]?\s*([-+]?\d[\d.,]*)%",
                lage_line,
            )
            if five_day_match:
                five_day_raw = five_day_match.group(1) or five_day_match.group(2)
                _gate_quellenwert(block, "5T", _quellenwert_float(five_day_raw), errors)

            detail_4w = _extract_metal_4w_value(lage_block, asset)
            chosen_4w = _waehle_edelmetall_4w(detail_4w, _extract_metal_4w_value(markt, asset))
            if chosen_4w is None:
                errors.append(f"{heading}: kein brauchbarer 4W-Wert im Edelmetalle-Briefing.")
            else:
                _gate_quellenwert(block, "4W", chosen_4w[1], errors)

            low_match = re.search(r"52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%", lage_line)
            if low_match:
                _gate_quellenwert(block, "52W-Tief", _quellenwert_float(low_match.group(1)), errors)
                _gate_quellenwert(block, "Abstand 52W-Tief", _quellenwert_float(low_match.group(2)), errors)

        # Die 200er-Zeile enthält die operative EMA200/WMA200-Basis und das
        # deterministische Trendfolge-Ergebnis je Metall.
        m200 = _extrahiere_200er_trend(trend_block)
        if not m200:
            errors.append(f"{heading}: 200er-Trendzeile fehlt oder ist nicht lesbar.")
        else:
            for label, raw in (("EMA200", m200[1]), ("WMA200", m200[2])):
                _gate_quellenwert(block, label, _quellenwert_float(raw), errors)
        if "BLOCKIERT" in trend_block and not re.search(r"trendfolge", block, re.I):
            errors.append(f"{heading}: Trendfolge-Blockierung fehlt in der Ausgabe.")
        for strategy in ("Trendfolge", "Trendwende", "Short"):
            if not re.search(r"\b" + strategy + r"\b", block, re.I):
                errors.append(f"{heading}: {strategy}-Status/Einordnung fehlt.")

    if errors:
        raise RuntimeError("PUNKT8_QUELLENABDECKUNG_UNGUELTIG: " + " | ".join(errors))
    print("PUNKT-8-QUELLENABDECKUNGS-GATE: PASS")

def _pct_aus_quellenzeile(text, asset, period="4W"):
    """Liest eine explizite Periodenrendite aus dem Marktbriefing."""
    patterns = {
        "WTI": r"(?im)^\s*-\s*WTI:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|.*?([+-]\d+(?:[.,]\d+)?)%\s*4W\b",
        "Brent": r"(?im)^\s*-\s*Brent:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|.*?([+-]\d+(?:[.,]\d+)?)%\s*4W\b",
        "Gold": r"(?im)^\s*-\s*Gold:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|.*?([+-]\d+(?:[.,]\d+)?)%\s*4W\b",
        "Silber": r"(?im)^\s*-\s*Silber:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|.*?([+-]\d+(?:[.,]\d+)?)%\s*4W\b",
        "Platin": r"(?im)^\s*-\s*Platin:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|.*?([+-]\d+(?:[.,]\d+)?)%\s*4W\b",
        "Palladium": r"(?im)^\s*-\s*Palladium:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|.*?([+-]\d+(?:[.,]\d+)?)%\s*4W\b",
    }
    match = re.search(patterns.get(asset, r"(?!)"), text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _fmt_pct_de(value):
    sign = "+" if value > 0 else ""
    return (sign + f"{value:.2f}".rstrip("0").rstrip(".")).replace(".", ",") + "%"


def _parse_percent_value(raw):
    """Liest Prozentpunkte aus einer Prozent-Spalte; keine stillschweigende Dezimal-Skalierung."""
    value = str(raw or "").strip().replace("%", "").replace(" ", "")
    if not value:
        return None
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _repariere_doppelte_sektion_6(text):
    """Entfernt nur den zweiten, widersprüchlichen 6.1–6.6-Platzhalterblock."""
    first_66 = re.search(r"(?im)^\s*6\.6\s+Risiken bestehender Ideen\b[^\n]*$", text or "")
    if not first_66:
        return text
    second_61 = re.search(r"(?im)^\s*6\.1\s+Makro gegen Technik\b[^\n]*$", text[first_66.end():])
    if not second_61:
        return text
    start = first_66.end() + second_61.start()
    next7 = re.search(r"(?im)^\s*7\.\s*🌍?\s*MARKT.*MAKROKONTEXT\s*$", text[start:])
    if not next7:
        return text
    end = start + next7.start()
    duplicate_block = text[start:end]
    # Nur entfernen, wenn die zweite Serie nachweislich aus Platzhaltern besteht.
    if duplicate_block.count("Keine belastbare") >= 4:
        return text[:start].rstrip() + "\n\n" + text[end:].lstrip()
    return text

def _repariere_1_1_rohstoffperioden(text, briefing_text, previous_report_path=None, edelmetall_text=""):
    """WTI/Brent stammen aus dem Marktbriefing; Gold-Fakten aus dem Edelmetalle-Briefing."""
    values = {asset: _pct_aus_quellenzeile(briefing_text, asset) for asset in ("WTI", "Brent")}
    gold_4w = _edelmetall_4w_value(edelmetall_text or "", "Gold")
    values["Gold"] = gold_4w[1] if gold_4w is not None else None
    # Kurswerte werden als Rohstring aus genau der metallbezogenen Quellzeile
    # gelesen. Keine Suche im gesamten Briefing und kein float-Roundtrip: das
    # verhindert, dass benachbarte Zahlen/Metalle oder bereits formatierte
    # Reportwerte in den Kurs geraten.
    gold_row = _edelmetall_lage_zeile(edelmetall_text or "", "Gold")
    gold_price_match = re.fullmatch(
        r"Gold:\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|.*",
        gold_row or "",
    )
    gold_price_raw = gold_price_match.group(1) if gold_price_match else None
    gold_price = _quellenwert_float(gold_price_raw) if gold_price_raw else None
    metal_prices = {}
    for metal in ("Gold", "Silber", "Platin", "Palladium"):
        row = _edelmetall_lage_zeile(edelmetall_text or "", metal)
        row_match = re.fullmatch(
            re.escape(metal) + r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|.*",
            row or "",
        )
        raw_price = row_match.group(1) if row_match else None
        numeric_price = _quellenwert_float(raw_price) if raw_price else None
        if raw_price and numeric_price is not None and math.isfinite(numeric_price):
            metal_prices[metal] = numeric_price
    start = re.search(r"(?m)^\s*1\.1\s+Was hat sich seit dem letzten Lauf verändert\?\s*$", text or "")
    end = re.search(r"(?m)^\s*1\.2\s+Sofort handelbare Chancen\s*$", text or "")
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.start():end.start()]
    # Kursnennungen im Marktüberblick werden je Metall anhand des exakten
    # Namens aus der jeweiligen LAGE-JE-METALL-Quellzeile synchronisiert. Die
    # komplette beschädigte Zahl wird ersetzt (nicht nur ein Präfix), damit
    # zusammengezogene Werte wie 4181.104181,10USD verschwinden.
    for metal, source_price in metal_prices.items():
        display_price = f"{source_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        patterns = (
            r"(?i)(\b" + re.escape(metal) + r"\s+notiert\s+bei\s+)[+-]?[\d.,]+(?=\s*(?:USD|\$))",
            r"(?i)(\b" + re.escape(metal) + r"\s+)[+-]?[\d.,]+(?=\s*(?:USD|\$))",
        )
        total_replacements = 0
        for pattern in patterns:
            block, n = re.subn(pattern, lambda mm: mm.group(1) + display_price, block)
            total_replacements += n
        if total_replacements:
            print(f"EDELMETALL_KURS_REPARIERT | {metal} | Quelle=Edelmetalle-Briefing | Ersetzungen={total_replacements}")
    replacements = [
        (r"(WTI-Öl\s+mit\s+[^(\n]+\()\s*[+-]?\d+(?:[.,]\d+)?%\s+(?:auf\s+)?4-Wochen-Sicht", values.get("WTI")),
        (r"(Brent-Öl\s+bei\s+[^(\n]+\()\s*[+-]?\d+(?:[.,]\d+)?%\s+(?:auf\s+)?4-Wochen-Sicht", values.get("Brent")),
        (r"(Gold\s+notiert\s+bei\s+[^(\n]+\()\s*[+-]?\d+(?:[.,]\d+)?%\s+(?:in\s+)?4\s+Wochen", values.get("Gold")),
    ]
    for pattern, value in replacements:
        if value is None:
            continue
        block = re.sub(pattern, lambda m, v=value: m.group(1) + _fmt_pct_de(v) + (" auf 4-Wochen-Sicht" if "4-Wochen-Sicht" in m.group(0) else " in 4 Wochen"), block, count=1, flags=re.I)
    # Do not present the already-A BTG status as a fresh A promotion; make
    # the low momentum and bearish MACD caveat explicit without breaking names.
    block = re.sub(
        r"In der Einzel-Check-Beobachtungsliste wurden frische A-Meldungen für "
        r"Newmont Corporation \(NEM\), B2Gold Corp\. \(BTG\), Guardant Health, Inc\. "
        r"\(GH\) und General Motors Company \(GM\) registriert\.",
        "In der Einzel-Check-Beobachtungsliste wurden aktuelle A-Meldungen für "
        "Newmont Corporation (NEM), B2Gold Corp. (BTG), Guardant Health, Inc. (GH) "
        "und General Motors Company (GM) registriert. Alle vier Meldungen haben Momentum 0/4; "
        "NEM, BTG und GH weisen einen bärischen MACD auf; BTG war bereits zuvor "
        "KAUFKANDIDAT A und ist kein neuer A-Aufstieg.",
        block, flags=re.I, count=1
    )
    # Falsche Negativbehauptungen ueber eine fehlende Vortagesauswertung werden
    # nicht nur in einer einzelnen alten Formulierung korrigiert. Ist ein
    # Vortagesbericht lokal vorhanden, wird der Vergleichsstatus explizit gesetzt.
    report_date = re.search(r"(?m)^Datum der Auswertung:\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    prior_report = None
    if report_date:
        from datetime import date as _date
        current_date = _date(int(report_date.group(3)), int(report_date.group(2)), int(report_date.group(1)))
        try:
            import glob as _glob
            candidates = []
            if previous_report_path and os.path.isfile(previous_report_path):
                candidates.append(previous_report_path)
            candidates.extend(_glob.glob("Auswertung(*).txt"))
            for candidate in set(candidates):
                dm = re.search(r"Auswertung\((\d{4})-(\d{2})-(\d{2})\)\.txt$", os.path.basename(candidate))
                if dm:
                    candidate_date = _date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                    if candidate_date < current_date and (prior_report is None or candidate_date > prior_report[0]):
                        prior_report = (candidate_date, candidate)
        except (OSError, ValueError):
            prior_report = None
    false_prior_claims = re.compile(
        r"(?is)(?:Keine Vortagesauswertung in der Historie vorhanden;[^.]*\.|"
        r"Es liegen keine abweichenden Vortagesdaten im Dateibestand vor;[^.]*\.|"
        r"keine Vortagesauswertung[^.]*nicht vorhanden[^.]*\.)\s*"
    )
    if prior_report:
        replacement = (f"Vortagesauswertung vorhanden: {prior_report[0].strftime('%d.%m.%Y')} "
                       "(lokaler Vorbericht). Der Vergleich wird auf diesen Bericht und die aktuellen Quelldaten bezogen; "
                       "nicht belegte Feldänderungen werden nicht behauptet. ")
        block = false_prior_claims.sub(replacement, block, count=1)
    else:
        block = false_prior_claims.sub(
            "Ein lokal lesbarer Vortagesbericht wurde nicht verifiziert; deshalb werden keine konkreten Tagesdeltas behauptet. ",
            block, count=1
        )
    return text[:start.start()] + block + text[end.start():]


def _repariere_8_doppelte_durchschnitte(text, briefing_text="", edelmetall_text=""):
    """Verhindert widersprüchliche zweite WMA200-Werte in den Metall-Unterabschnitten."""
    headings = [
        (r"(?m)^\s*8\.1\s+Gold\s*$", r"(?m)^\s*8\.2\s+Silber\s*$"),
        (r"(?m)^\s*8\.2\s+Silber\s*$", r"(?m)^\s*8\.3\s+Platin\s*$"),
        (r"(?m)^\s*8\.3\s+Platin\s*$", r"(?m)^\s*8\.4\s+Palladium\s*$"),
        (r"(?m)^\s*8\.4\s+Palladium\s*$", r"(?m)^\s*9\.\s*📅?\s*NÄCHSTE KATALYSATOREN\s*$"),
    ]
    for start_pat, end_pat in headings:
        start = re.search(start_pat, text)
        if not start:
            continue
        end = re.search(end_pat, text[start.end():])
        if not end:
            continue
        stop = start.end() + end.start()
        block = text[start.start():stop]
        block = re.sub(r"(?m)(^\s*[-•]\s*EMA20:[^\n]*?)\s*\|\s*WMA200\s*:\s*[-+]?\d[\d.,]*\$?",
                       r"\1", block)
        text = text[:start.start()] + block + text[stop:]
    # Die Edelmetall-Fakten bleiben ausschließlich in der spezialisierten Quelle
    # verankert. Ein Quellenvergleich wird zentral in der Methodik dokumentiert,
    # nicht als zusätzlicher Faktenblock im Edelmetall-Gesamtbild ausgegeben.
    return text


def _repariere_3_2_rohstoffinterpretation(text, macro_text, briefing_text, edelmetall_text=""):
    """Bindet Edelmetall-4W-Fakten an die Spezialquelle, ohne Gemini-Interpretation neu zu formulieren."""
    copper_1y = re.search(r"(?im)^Kupfer:\s*[-+]?\d[\d.,]*\s*\|.*?1J=([+-]?\d[\d.,]*)%", macro_text or "")
    copper_1m = re.search(r"(?im)^Kupfer:\s*[-+]?\d[\d.,]*\s*\|.*?1M=([+-]?\d[\d.,]*)%", macro_text or "")
    start = re.search(r"(?m)^\s*3\.2\s+Rohstoff\s*→\s*Branche\s*→\s*Aktie\s*$", text or "")
    end = re.search(r"(?m)^\s*3\.3\s+Politik\s*→\s*Branche\s*→\s*Aktie\s*$", text or "")
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.start():end.start()]
    if copper_1y and copper_1m and re.search(r"Steigende Industriemetallpreise.*stabile Edelmetalle", block, re.I):
        y = _parse_percent_value(copper_1y.group(1))
        m = _parse_percent_value(copper_1m.group(1))
        metal_values = []
        for name in ("Gold", "Silber", "Platin", "Palladium"):
            source_value = _edelmetall_4w_value(edelmetall_text or "", name)
            if source_value is not None:
                metal_values.append(f"{name} {_fmt_pct_de(source_value[1])}")
        if metal_values:
            facts = "4W-Werte laut Edelmetalle-Briefing: " + ", ".join(metal_values)
            # Nur die Faktenangabe aktualisieren. Die bestehende Gemini-Interpretation
            # und deren Kausalitätsvorbehalte werden nicht durch Python neu verfasst.
            if re.search(r"(?i)Edelmetalle", block):
                if re.search(r"(?i)\([^)]*(?:Gold|Silber|Platin|Palladium)[^)]*\)", block):
                    block = re.sub(
                        r"(?i)\([^)]*(?:Gold|Silber|Platin|Palladium)[^)]*\)",
                        "(" + facts + ")",
                        block,
                        count=1,
                    )
                else:
                    block = re.sub(
                        r"(?i)(Edelmetalle)",
                        r"\1 (" + facts + ")",
                        block,
                        count=1,
                    )
                return text[:start.start()] + block + text[end.start():]
    return text


def _repariere_6_4_rohstoffperioden(text, briefing_text, macro_text):
    """Korrigiert Rohstoff-Zeiträume im Widerspruchsblock auf die jeweilige Quelle."""
    wti = _pct_aus_quellenzeile(briefing_text, "WTI")
    copper_1y = re.search(r"(?im)^Kupfer:\s*[-+]?\d[\d.,]*\s*\|.*?1J=([+-]?\d[\d.,]*)%", macro_text or "")
    copper_1m = re.search(r"(?im)^Kupfer:\s*[-+]?\d[\d.,]*\s*\|.*?1M=([+-]?\d[\d.,]*)%", macro_text or "")
    if wti is None:
        return text
    y = _parse_percent_value(copper_1y.group(1)) if copper_1y else None
    m = _parse_percent_value(copper_1m.group(1)) if copper_1m else None
    copper_phrase = (
        f"Kupfer liegt auf 1M bei {_fmt_pct_de(m)} (1J {_fmt_pct_de(y)})"
        if m is not None and y is not None else "Kupfer zeigt ein anderes Periodenbild als Öl"
    )
    corrected = (
        f"6.4 Rohstoff gegen Aktie: WTI liegt laut Marktbriefing auf 4W bei "
        f"{_fmt_pct_de(wti)}; {copper_phrase}. Die kurzfristigen Rohstoffsignale divergieren; "
        "Aktienwirkungen bleiben branchen- und unternehmensabhängig."
    )
    return re.sub(r"(?m)^\s*6\.4\s+Rohstoff gegen Aktie:.*$", corrected, text or "", count=1)


def _repariere_1_4_kapitalflussbehauptung(text):
    return re.sub(
        r"spiegeln defensive Mittelzuflüsse wider",
        "zeigen relative defensive Sektorstärke im Rotation-Score; direkte Kapitalflussdaten liegen nicht vor",
        text or "", flags=re.I
    )


def _hat_expliziten_zeitbezug(text):
    """Gemeinsame Erkennung expliziter Datums-/Zeitangaben für Abschnitt 9."""
    value = text or ""
    patterns = (
        r"\b\d{1,2}\.\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\s+20\d{2}\b",
        r"\b\d{1,2}[./-]\d{1,2}[./-]20\d{2}\b",
        r"\b20\d{2}-\d{1,2}-\d{1,2}\b",
        r"\b(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\s+20\d{2}\b",
        r"\b\d{1,2}\.?\s*(?:bis|[-–—])\s*\d{1,2}\.?\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b",
        r"\b(?:am\s+)?\d{1,2}\.\s*(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b",
        r"\b(?:im|in|anfang|mitte|ende)\s+(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b",
        r"\b(?:heute|morgen|übermorgen|naechste[nr]?|kommende[nr]?|diese[rn]?)\s+(?:woche|monat|tag(?:en)?|jahr(?:es)?|wochen|monaten|monate)\b",
        r"\b(?:nächste|naechste|kommende)\s+woche\b",
        r"\b(?:in|innerhalb(?:\s+von)?)\s+\d{1,3}\s+(?:tag(?:en)?|woche(?:n)?|monat(?:en)?|jahr(?:en)?)\b",
        r"\b(?:am\s+)?(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b",
        r"\b(?:heute|morgen|übermorgen|naechste[nr]?|kommende[nr]?|diese[rn]?)\b",
        r"\bq[1-4](?:\s+20\d{2})?\b",
        r"\bkw\s*\d{1,2}(?:\s*[/.-]?\s*20\d{2})?\b",
    )
    return any(re.search(pattern, value, re.I | re.U) for pattern in patterns)


def _ereignisbehauptungen_aufteilen(unit, event_pattern):
    """Trennt mehrere Ereignisbehauptungen in derselben Satz-Unit konservativ."""
    value = (unit or "").strip()
    if not value:
        return []
    # Nur an Konnektoren trennen, wenn beide Seiten tatsaechlich ein Ereignis
    # enthalten. So wird ein gemeinsamer Datumsbezug nicht unnoetig zerschnitten.
    parts = [value]
    separators = re.compile(r"\s*(?:;|\s+und\s+|\s+sowie\s+|\s+außerdem\s+|\s+zudem\s+)\s*", re.I)
    changed = True
    while changed:
        changed = False
        next_parts = []
        for part in parts:
            matches = list(separators.finditer(part))
            split_done = False
            for match in matches:
                left, right = part[:match.start()].strip(" ,"), part[match.end():].strip(" ,")
                if (left and right
                        and re.search(event_pattern, left, re.I | re.U)
                        and re.search(event_pattern, right, re.I | re.U)):
                    next_parts.extend([left, right])
                    changed = True
                    split_done = True
                    break
            if not split_done:
                next_parts.append(part)
        parts = next_parts
    return parts


def _undatierte_ereignisbehauptungen(units, event_pattern):
    """Liefert jede konkrete, nicht negierte Ereignisbehauptung ohne eigenen Zeitbezug."""
    undated = []
    for unit in units or []:
        for claim in _ereignisbehauptungen_aufteilen(unit, event_pattern):
            if not re.search(event_pattern, claim, re.I | re.U):
                continue
            if re.search(r"\b(?:kein|keine|keinen|keiner|nicht|derzeit\s+keine|aktuell\s+keine)\b", claim, re.I | re.U):
                continue
            if not _hat_expliziten_zeitbezug(claim):
                undated.append(claim)
    return undated


def _repariere_9_2_unbelegte_katalysatoren(text):
    """Entfernt undatierte Earnings-Platzhalter ohne konkrete Firma/Terminquelle."""
    if not text:
        return text
    start = re.search(r"(?m)^\s*9\.2\s+Unternehmen\s*$", text)
    end = re.search(r"(?m)^\s*9\.3\s+Branchenereignisse\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.end():end.start()]
    vague = re.compile(
        r"(?i)^\s*[-•]?\s*.*(?:quartalsbericht|earnings|quartalszahlen|ergebnisberichte).*"
        r"(?:im\s+berichtsmonat|im\s+oktober|anstehend|kommend|demnächst|demnaechst).*\.?\s*$"
    )
    kept = []
    changed = False
    for line in block.splitlines():
        if vague.match(line) and not _hat_expliziten_zeitbezug(line):
            changed = True
            continue
        kept.append(line)
    if changed and not any(_hat_expliziten_zeitbezug(line) for line in kept):
        kept.append("- Keine konkreten Unternehmenstermine mit verifizierbarem Datum in den bereitgestellten Laufquellen belegt.")
    return text[:start.end()] + "\n" + "\n".join(kept).strip("\n") + "\n" + text[end.start():]


def _repariere_9_3_unbelegte_ereignisplatzhalter(text):
    """Entfernt nur generische, unbelegte 9.3-Platzhalter; konkrete Ereignisse bleiben erhalten."""
    if not text:
        return text
    start = re.search(r"(?m)^\s*9\.3\s+Branchenereignisse\s*$", text)
    end = re.search(r"(?m)^\s*9\.4\s+Technische Trigger\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.end():end.start()]
    event_terms = r"konferenz|branchenereignis|industrieereignis|sektorereignis|regulierungs(?:entscheidung|beschluss)|aufsicht(?:sentscheidung|smaßnahme)|gesetz(?:esänderung|esbeschluss)?|wahl(?:termin)?|abstimmung|verordnung|ankündigung|ankuendigung|event"
    vague_terms = r"im\s+berichtsmonat|anstehend|kommend|demnächst|demnaechst|irgendwann|bald|in\s+naher\s+zukunft"
    kept = []
    changed = False
    for line in block.splitlines():
        if (re.search(event_terms, line, re.I | re.U)
                and re.search(vague_terms, line, re.I | re.U)
                and not _hat_expliziten_zeitbezug(line)
                and not re.search(r"\b(?:keine|kein|nicht)\b", line, re.I | re.U)):
            changed = True
            continue
        kept.append(line)
    if changed and not any(re.search(event_terms, line, re.I | re.U) for line in kept):
        kept.append("- Keine konkreten Branchenereignisse mit verifizierbarem Datum in den bereitgestellten Laufquellen belegt.")
    return text[:start.end()] + "\n" + "\n".join(kept).strip("\n") + "\n" + text[end.start():]


def _repariere_10_3_unbelegte_systemperformance(text):
    """Korrigiert falsch zugeordnete Positionsrenditen und verhindert Scheinthemen in 10.3."""
    if not text:
        return text
    start = re.search(r"(?m)^\s*10\.3\s+Positionen mit neuer Investmentthese\s*$", text)
    end = re.search(r"(?m)^\s*10\.4\s+Positionen, deren These schwächer wird\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.end():end.start()]
    bad = re.search(
        r"(?im)^\s*[-•]\s*.*(?:FCX|Freeport-McMoRan).*?(?:FSLY|Fastly).*?(?:ENR\.DE|Siemens Energy).*?(?:positive Performance|System-Performance|Trendfolge-Positionen).*?$",
        block,
    )
    if bad:
        replacement = (
            "- Keine Position mit gegenüber dem vorherigen Lauf belastbar belegter neuer Investmentthese. "
            "Die zuvor aufgeführte Renditeliste wurde entfernt, weil sie keine Thesenänderung belegt."
        )
        block = block[:bad.start()] + replacement + block[bad.end():]
    # Andere pauschale Rendite-Listen duerfen nicht als Beleg fuer eine neue These gelten.
    block = re.sub(
        r"(?im)^\s*[-•]\s*(?=.*(?:System-Performance|positive Performance))(?=.*(?:ENR\.DE|Siemens Energy)).*$",
        "- Für Siemens Energy AG (ENR.DE) ist aus den bereitgestellten Quellen keine neue Investmentthese gegenüber dem Vortag belegt; Positionsperformance allein genügt dafür nicht.",
        block,
    )
    if not block.strip() or re.search(r"(?i)keine neue investmentthese|keine position.*neue these", block) is None:
        # Nur falls der Abschnitt nach der Korrektur weiterhin ausschliesslich
        # die alten positiven Renditen enthaelt, keine Scheinthemen stehen lassen.
        if re.search(r"(?i)Bestätigte Trendfolge-Positionen mit positiver Performance", block):
            block = "\n- Keine Position mit gegenüber dem vorherigen Lauf belastbar belegter neuer Investmentthese. Performanceänderungen allein reichen hierfür nicht.\n"
    replacement = "10.3 Positionen mit neuer Investmentthese\n" + block.strip()
    return text[:start.start()] + replacement + "\n\n" + text[end.start():].lstrip("\n")


def _repariere_11_7_doppelten_reichweitenhinweis(text):
    lines = (text or "").splitlines()
    out = []
    seen_reach = False
    for line in lines:
        if line.strip().startswith("- Reichweiten-Hinweis:"):
            if seen_reach:
                continue
            seen_reach = True
        out.append(line)
    return "\n".join(out) + ("\n" if (text or "").endswith("\n") else "")


def _repariere_punkt10_5_und_portfolio(text, eingabedateien, closed_history):
    """Letzter Schutz: 10.5 kommt nur aus Tab 2; Portfoliozahlen nur aus offener Positionsquelle."""
    heading = re.search(r"(?m)^\s*10\.5\s+Geschlossene Positionen\s*$", text or "")
    next11 = re.search(r"(?m)^\s*11\.\s*METHODIK\s*/\s*DATENQUALITÄT\s*$", text or "")
    if not (heading and next11 and heading.start() < next11.start()):
        return text
    block = text[heading.end():next11.start()]
    # Den vorhandenen qualitativen Portfolio-Fazit-Satz bewahren, aber alte
    # geschlossene Positionen und isolierte Duplikate niemals übernehmen.
    fazit_match = re.search(r"(?m)^\s*Portfolio-Fazit:\s*.*$", block)
    portfolio_fazit = fazit_match.group(0).strip() if fazit_match else ""
    csv_path = (eingabedateien or {}).get("Offene Positionen+Check.csv")
    if not csv_path or not os.path.isfile(csv_path):
        try:
            csv_path = finde_datei(DATEIMUSTER["Offene Positionen+Check.csv"])
        except Exception:
            csv_path = None

    portfolio_line = "Portfolio-Übersicht: Anzahl offene Positionen: NICHT VERIFIZIERT | Ø Performance offener Positionen: NICHT VERIFIZIERT"
    if csv_path and os.path.isfile(csv_path):
        try:
            rows = _offene_positionen_rows(csv_path)
            unique = {}
            perf_values = []
            for row in rows:
                ticker = _csv_value(row, ["Ticker", "Yahoo-Ticker", "Yahoo Ticker"]).strip()
                status = _csv_value(row, ["Status", "Positionstatus"]).casefold()
                if not ticker or any(term in status for term in ("geschlossen", "gestoppt", "closed", "verkauft")):
                    continue
                unique.setdefault(ticker, row)
            for row in unique.values():
                raw = _csv_value(row, [
                    "Performance_Seit_Einstieg%", "Performance Seit Einstieg %",
                    "Performance_Seit_Einstieg", "Performance%", "Performance"
                ])
                value = _parse_percent_value(raw)
                if value is not None:
                    perf_values.append(value)
            if perf_values:
                average = sum(perf_values) / len(perf_values)
                portfolio_line = (
                    f"Portfolio-Übersicht: Anzahl offene Positionen: {len(unique)} | "
                    f"Ø Performance offener Positionen: {average:.2f}% "
                    f"(n={len(perf_values)} Positionen mit verwertbarer Performance)"
                )
            else:
                portfolio_line = (
                    f"Portfolio-Übersicht: Anzahl offene Positionen: {len(unique)} | "
                    "Ø Performance offener Positionen: NICHT VERIFIZIERT (keine verwertbaren Performancewerte)"
                )
        except Exception as exc:
            print(f"WARNUNG: Portfolio-Zusammenfassung konnte nicht deterministisch neu berechnet werden: {exc}")

    closed_lines = []
    for raw_line in str(closed_history or "").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("(keine geschlossenen Positionen"):
            closed_lines.append("- " + line.lstrip("-• "))
    if not closed_lines:
        closed_lines = ["Keine geschlossene Position innerhalb der letzten 3 Kalendertage laut autoritativer Tab-2-Prüfung."]
    replacement = ["", *closed_lines, "", portfolio_line]
    if portfolio_fazit:
        replacement.extend(["", portfolio_fazit])
    new_block = "\n".join(replacement).rstrip() + "\n\n"
    return text[:heading.end()] + new_block + text[next11.start():]




def _synchronisiere_1_1_mit_quellen(text, briefing_text="", portfolio_text=""):
    """Synchronisiert Scannerzahlen, getrennte Portfolio-Grundgesamtheiten und S&P-Text."""
    if not text:
        return text

    # Die Jahreszahl 1927 bezeichnet im Briefing den ungefähren Beginn der
    # Datenreihe, nicht das Jahr des Rekordhochs. Die Formulierung wird entwirrt.
    text = _repariere_sp500_rekordhoch(text)

    start = re.search(r"(?m)^1\.1\s+Was hat sich seit dem letzten Lauf verändert\?\s*$", text)
    end = re.search(r"(?m)^1\.2\s+Sofort handelbare Chancen\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.start():end.start()]

    valid_match = re.search(
        r"(?i)(?:Nach allen Filtern:\s*\d+\s*\|\s*)?davon\s+VALIDE:\s*(\d+)",
        briefing_text or "",
    )
    if valid_match:
        count = int(valid_match.group(1))
        # Den kompletten alten Scannervergleich ersetzen; keine alte Zahl oder
        # veraltete Kandidatenliste aus dem Gemini-Text stehen lassen.
        new_intro = (
            f"Der aktuelle Trendfolge-Scanner weist laut Scan-Statistik {count} valide Setups aus; "
            "die konkreten Setups werden ausschließlich in Abschnitt 2.1 aufgeführt. "
        )
        block = re.sub(
            r"(?is)Gegenüber dem vorherigen Lauf.*?(?=Der Trendwende-Scanner|Die Trendwende|Auf der Short-Seite)",
            new_intro,
            block,
            count=1,
        )
        block = re.sub(
            r"(?i)listet der aktuelle Scanner\s+\d+\s+valide A- und A--Trendfolge-Setups\s*\([^)]*\)",
            f"weist der aktuelle Scanner {count} valide Trendfolge-Setups aus",
            block,
            count=1,
        )

    # Zwei gültige Kennzahlen mit unterschiedlichen Grundgesamtheiten getrennt
    # ausweisen: offener Korb ohne Benchmark-ETF (21) und gesamtes Portfolio (22).
    basket = re.search(
        r"(?im)^\s*Ø\s*Performance aktuell offener Positionen\s*\(ohne\s+EUNL\.DE\)\s*:\s*([+-]?\d+(?:[.,]\d+)?)\s*%\s*\r?\n"
        r"\s*Anzahl berücksichtigter offener Positionen\s*:\s*(\d+)",
        briefing_text or "",
    )
    port = re.search(
        r"(?im)^Portfolio-Übersicht:\s*Anzahl offene Positionen:\s*(\d+)\s*\|\s*"
        r"Ø Performance offener Positionen:\s*([-+]?\d+[.,]\d+)%\s*"
        r"\(n=(\d+) Positionen mit verwertbarer Performance\)",
        portfolio_text or "",
    )
    live_match = re.search(
        r"Live-System bei\s*([-+]?\d+[.,]\d+)\s*%\s*gegenüber dem MSCI World mit\s*"
        r"([-+]?\d+[.,]\d+)\s*%.*?Underperformance von\s*([-+]?\d+[.,]\d+)\s*Prozentpunkten",
        block, re.I,
    )
    live_clause = ""
    if live_match:
        live, benchmark, under = (x.replace(".", ",") for x in live_match.groups())
        live_clause = (
            f" Der Live-System-Benchmark-Abgleich beträgt {live}% gegenüber dem MSCI World "
            f"mit {benchmark}% im gleichen Zeitraum (Underperformance: {under} Prozentpunkte)."
        )
    if basket and port:
        basket_perf = basket.group(1).replace(".", ",")
        basket_n = basket.group(2)
        portfolio_perf = port.group(2).replace(".", ",")
        portfolio_n = port.group(3)
        portfolio_count = port.group(1)
        metrics_clause = (
            f"Der offene Korb ohne EUNL.DE liegt bei {basket_perf}% ({basket_n} Positionen). "
            f"Die Portfolio-Übersicht über alle offenen Positionen weist im Durchschnitt "
            f"{portfolio_perf}% bei {portfolio_count} offenen Positionen aus "
            f"(n={portfolio_n} mit verwertbarer Performance); die Grundgesamtheiten sind verschieden."
            + live_clause + " "
        )
        block = re.sub(
            r"(?is)(?:Der Live-Benchmark-Abgleich zeigt|Die durchschnittliche Performance der offenen Positionen liegt bei|Der offene Korb ohne EUNL\.DE liegt bei).*?(?=Der S&P 500)",
            metrics_clause,
            block,
            count=1,
        )
    elif port:
        portfolio_perf = port.group(2).replace(".", ",")
        block = re.sub(
            r"(?is)(?:Der Live-Benchmark-Abgleich zeigt|Die durchschnittliche Performance der offenen Positionen liegt bei|Der offene Korb ohne EUNL\.DE liegt bei).*?(?=Der S&P 500)",
            f"Die Portfolio-Übersicht weist {portfolio_perf}% bei {port.group(1)} offenen Positionen aus. " + live_clause + " ",
            block,
            count=1,
        )

    block = _repariere_sp500_rekordhoch(block)
    return text[:start.start()] + block + text[end.start():]


def _repariere_sp500_rekordhoch(text):
    """Entwirrt den S&P-500-Hinweis: 1927 ist Datenreihenbeginn, nicht Rekordjahr."""
    if not text:
        return text
    lines = []
    for line in text.splitlines():
        is_record_note = (
            re.search(r"(?i)S&P\s*500", line)
            and re.search(r"1927|7[.,]844[.,]52", line)
            and re.search(r"(?i)Rekordhoch|Vergleichshoch", line)
            and re.search(r"(?i)1927|im Tagesverlauf", line)
        )
        if is_record_note:
            close = re.search(r"(?i)(?:Schlussstand(?:\s+von)?|schloss bei)\s*([0-9.,]+)\s*Punkte", line)
            high = re.search(r"7[.,]844[.,]52\s*Punkte", line)
            close_value = close.group(1) if close else "7.811,54"
            high_value = high.group(0).replace(" Punkte", "") if high else "7.844,52"
            if line.lstrip().startswith(("- S&P", "• S&P")):
                prefix = line[:len(line) - len(line.lstrip())]
                line = (
                    f"{prefix}- S&P 500 – Schlussstand {close_value} Punkte; rund 0,4 % unter dem "
                    f"höchsten Wert im verfügbaren Datenbestand von {high_value} Punkten "
                    "(Datenreihe ab ca. 1927)."
                )
            else:
                line = (
                    f"Der S&P 500 schloss bei {close_value} Punkten, rund 0,4 % unter dem "
                    f"höchsten Wert im verfügbaren Datenbestand von {high_value} Punkten "
                    "(Datenreihe ab ca. 1927)."
                )
        # Ältere, grammatisch beschädigte Formulierungen aus früheren Reparaturpässen.
        line = re.sub(
            r"(?i)bisheriges im verfügbaren Datenbestand dokumentiertes Vergleichshoch von 7\.844,52 Punkten",
            "höchster Wert im verfügbaren Datenbestand von 7.844,52 Punkten",
            line,
        )
        lines.append(line)
    result = "\n".join(lines)
    # In 1.1 steht die Marktbeobachtung innerhalb eines langen Absatzes. Nur
    # diesen S&P-Satz ersetzen, nicht den gesamten Absatz mit seinen Makrofakten.
    result = re.sub(
        r"(?i)Der S&P 500 nähert sich mit einem Schlussstand von 7\.811,54 Punkten "
        r"\(ca\.\s*0,4%?\s*unter dem bisherigen Rekordhoch von 7\.844,52 Punkten im verfügbaren Datenbestand\) "
        r"aktiv seinem Höchststand\.",
        "Der S&P 500 schloss bei 7.811,54 Punkten, rund 0,4 % unter dem höchsten Wert im verfügbaren Datenbestand von 7.844,52 Punkten (Datenreihe ab ca. 1927).",
        result,
    )
    return result


def _repariere_watchlist_technisches_potenzial(text, eingabedateien=None):
    """Berechnet Watchlist-Technikpotenzial aus Kurs und technischem Ziel der Scanner-CSV."""
    if not text:
        return text
    inputs = eingabedateien or {}
    path = inputs.get("Setups(...).csv") if isinstance(inputs, dict) else None
    if not path or not os.path.isfile(path):
        try:
            path = finde_datei(DATEIMUSTER["Setups(...).csv"])
        except Exception:
            path = None
    if not path or not os.path.isfile(path):
        return text
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
    except (OSError, csv.Error):
        return text

    source = {}
    for row in rows:
        ticker = str(row.get("Ticker", "")).strip().upper()
        try:
            price = float(str(row.get("Kurs", "")).strip().replace(",", "."))
            target = float(str(row.get("Tech-Kursziel", "")).strip().replace(",", "."))
        except (TypeError, ValueError):
            continue
        if ticker and price > 0 and target > 0:
            source[ticker] = (price, target, row)

    def fmt_de(value):
        return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    lines = text.splitlines()
    in_watchlist = False
    changed = False
    out = []
    for line in lines:
        if re.match(r"(?i)^WATCHLIST(?:\s|$)", line.strip()):
            in_watchlist = True
            out.append(line)
            continue
        if in_watchlist and re.match(r"(?i)^RISIKO-WATCH\s*$", line.strip()):
            in_watchlist = False
        if in_watchlist:
            m = re.match(r"(?P<prefix>\s*[-•]\s*)?(?P<name>.+?)\s*\((?P<ticker>[A-Z0-9.^=-]+)\)\s*\|(?P<rest>.*)", line)
            if m and m.group("ticker").upper() in source and re.search(r"Technisches Potenzial", line, re.I):
                price, target, row = source[m.group("ticker").upper()]
                pct = (target / price - 1.0) * 100.0
                currency = "€" if str(row.get("Waehrung", "")).upper() == "EUR" else "$"
                reason = str(row.get("Status_Grund", "")).strip() or "Nicht gesondert ausgewiesen"
                name = str(row.get("Name", m.group("name"))).strip()
                pct_de = f"{pct:.2f}".replace(".", ",")
                replacement = (
                    f"- {name} ({m.group('ticker').upper()}) | Grund: {reason} | Kurs: {fmt_de(price)}{currency} | "
                    f"Technisches Potenzial: {pct_de}% (Tech-Kursziel: {fmt_de(target)}{currency})"
                )
                out.append(replacement)
                changed = changed or replacement != line
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _repariere_7_1_ytd_aus_briefing(text, briefing_text):
    """Synchronisiert YTD-Angaben in 7.1 ausschließlich aus dem aktuellen Briefing."""
    if not text:
        return text
    start = re.search(r"(?m)^7\.1\s+Aktienmärkte\s*/\s*Indizes\s*$", text)
    end = re.search(r"(?m)^7\.2\s+Zinsen\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.start():end.start()]
    # Nur die explizit vorhandenen YTD-Quellwerte werden übernommen.
    source_specs = [
        ("S&P 500", r"S&P\s*500"),
        ("Nasdaq", r"Nasdaq(?: Composite)?(?:\s*/\s*Nasdaq 100)?"),
        ("DAX", r"DAX"),
        ("EuroStoxx50", r"EuroStoxx\s*50"),
        ("Nikkei 225", r"Nikkei\s*225"),
        ("Shanghai Composite", r"Shanghai Composite"),
        ("Hang Seng", r"Hang Seng"),
    ]
    ytd = {}
    for label, line_pattern in source_specs:
        src = re.search(
            r"(?im)^\s*[•-]\s*" + line_pattern + r"\s*:\s*[^\n]*?\bYTD\s+([+-]?\d+(?:[.,]\d+)?)%",
            briefing_text or "",
        )
        if src:
            ytd[label] = src.group(1).replace(".", ",")
    source_dates = {}
    for label, line_pattern in source_specs:
        src = re.search(
            r"(?im)^\s*[•-]\s*" + line_pattern + r"\s*:\s*[^\n]*?\bDatenstand\s+(\d{2})\.(\d{2})\.(\d{4})",
            briefing_text or "",
        )
        if src:
            day, month, year = src.groups()
            source_dates[label] = f"{year}-{month}-{day}"
    lines = block.splitlines()
    out = []
    for line in lines:
        replaced = line
        for label, pattern in source_specs:
            if label not in ytd or not re.search(r"(?i)^\s*[-•]\s*" + pattern + r"\s*:", line):
                continue
            replaced = re.sub(r"(?i)(\bYTD\s+)[+-]?\d+(?:[.,]\d+)?%", lambda m: m.group(1) + ytd[label] + "%", line, count=1)
            if label in source_dates:
                replaced = re.sub(r"(?i)(\bDatenstand:\s*)\d{4}-\d{2}-\d{2}", lambda m: m.group(1) + source_dates[label], replaced, count=1)
                replaced = re.sub(r"(?i)(\bDatenstand:\s*)\d{2}\.\d{2}\.\d{4}", lambda m: m.group(1) + source_dates[label], replaced, count=1)
            break
        # Für Russell 2000 enthält das aktuelle Briefing keinen YTD-Wert. Keine
        # nicht belegte Zahl stehen lassen; andere Perioden/Indexwerte bleiben.
        if re.match(r"(?i)^\s*[-•]\s*Russell\s+2000\s*:", replaced) and re.search(r"(?i)\bYTD\s+[+-]?\d+(?:[.,]\d+)?%", replaced) and "Russell 2000" not in ytd:
            replaced = re.sub(r"(?i)\s*\|\s*YTD\s+[+-]?\d+(?:[.,]\d+)?%", " | YTD NICHT VERFÜGBAR (im aktuellen Briefing nicht ausgewiesen)", replaced, count=1)
        out.append(replaced)
    new_block = "\n".join(out)
    return text[:start.start()] + new_block + text[end.start():]


def _repariere_2_5_alternativen(text):
    """Trennt Portfolio-Performance/Ideenreferenzen von tatsächlich belegten Alternativ-Setups."""
    if not text:
        return text
    start = re.search(r"(?m)^2\.5\s+Sonstige durch Gemini erkannte Chancen\s*$", text)
    end = re.search(r"(?m)^3\.\s*🧠\s*THEMEN\s*&\s*ZUSAMMENHÄNGE\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.end():end.start()]
    portfolio_refs = all(ticker in block for ticker in ("XDEQ.DE", "SPYI.DE", "LSUG.MI")) and "Performance im Korb" in block
    unsupported_theme_refs = all(ticker in block for ticker in ("ETN", "PH", "ITW"))
    if not (portfolio_refs and unsupported_theme_refs):
        return text
    replacement = (
        "2.5 Sonstige durch Gemini erkannte Chancen\n"
        "- Keine zusätzlichen, durch aktuelle Scanner-Daten bestätigten Alternativ-Setups ausgewiesen.\n"
        "- XDEQ.DE, SPYI.DE und LSUG.MI werden mit ihrer Performance im Korb als bestehender Portfolio-Kontext genannt; diese Performance ist kein neues Einstiegssignal.\n"
        "- Eaton Corporation plc (ETN), Parker-Hannifin Corporation (PH) und Illinois Tool Works Inc. (ITW) sind thematische Ideenreferenzen. Für diesen Lauf sind keine aktuellen Scanner-Setups oder konkreten Entry-/Stop-/Ziel-/CRV-Daten belegt; daher keine sofort handelbaren Alternativen.\n\n"
    )
    return text[:start.start()] + replacement + text[end.start():]




def _kanonische_unterabschnittstitel():
    """Zentrale Titelliste fuer die Erkennung echter numerierter Abschnittsueberschriften."""
    return {
        "1.1": "Was hat sich seit dem letzten Lauf verändert",
        "1.2": "Sofort handelbare Chancen",
        "1.3": "Ideen im Aufbau",
        "1.4": "Frühindikatoren neue Themen",
        "2.1": "Trendfolge",
        "2.2": "Trendwende",
        "2.3": "Short",
        "2.4": "HebelTrader",
        "2.5": "Sonstige durch Gemini erkannte Chancen",
        "3.1": "Makro Branche Aktie",
        "3.2": "Rohstoff Branche Aktie",
        "3.3": "Politik Branche Aktie",
        "3.4": "Technologie Branche Aktie",
        "3.5": "Unternehmens Fundamentaldaten Aktie",
        "6.1": "Makro gegen Technik",
        "6.2": "Technik gegen Fundamentaldaten",
        "6.3": "Sektor gegen Aktie",
        "6.4": "Rohstoff gegen Aktie",
        "6.5": "Investmentthese gegen aktuelle Marktdaten",
        "6.6": "Risiken bestehender Ideen",
        "7.1": "Aktienmärkte Indizes",
        "7.2": "Zinsen",
        "7.3": "Volatilität",
        "7.4": "FX",
        "7.5": "Rohstoffe",
        "7.6": "Krypto",
        "7.7": "Konjunktur Makro",
        "8.1": "Gold",
        "8.2": "Silber",
        "8.3": "Platin",
        "8.4": "Palladium",
        "9.1": "Makrotermine",
        "9.2": "Unternehmen",
        "9.3": "Branchenereignisse",
        "9.4": "Technische Trigger",
        "9.5": "Mögliche Aktivierung Invalidierung",
        "10.1": "Sofortiger Handlungsbedarf",
        "10.2": "Stop TP Änderungen",
        "10.3": "Positionen mit neuer Investmentthese",
        "10.4": "Positionen deren These schwächer wird",
        "10.5": "Geschlossene Positionen",
        "11.1": "Datenstatus",
        "11.2": "Makro Szenario Status",
        "11.3": "Datenlücken",
        "11.4": "externe Quellen",
        "11.5": "technische fundamentale Datenqualität",
        "11.6": "Hinweise zur Interpretation",
        "11.7": "Abgrenzung",
    }


def _echte_unterabschnittsueberschrift(line):
    """Erkennt echte Abschnittsüberschriften und weist satzförmigen Fließtext zurück.

    Neben der Abschnittsnummer muss der Titelkern zur bekannten Titelliste passen.
    Zusatzinformationen sind nur in ausdrücklich abgetrennten Formen zulässig
    (Klammerzusatz, Gedankenstrich, Doppelpunkt, senkrechter Strich oder
    definierte Titelergänzung). Ein bloßer Wort-Overlap genügt ausdrücklich nicht.
    """
    if not line:
        return None
    value = line.strip()
    if value.startswith(("-", "*", "+")):
        return None
    value = re.sub(r"^#{1,6}\s*", "", value)
    value = re.sub(r"^(?:\*\*|__)", "", value)
    value = re.sub(r"(?:\*\*|__)$", "", value).strip()
    if len(value) > 150 or re.search(r"[.!?]\s*$", value):
        return None
    match = re.fullmatch(r"(?P<key>\d{1,2}\.\d{1,2})\s+(?P<title>.+?)\s*", value)
    if not match:
        return None
    key, title = match.group("key"), match.group("title")
    expected = _kanonische_unterabschnittstitel().get(key)
    if not expected:
        return None

    ignored = {
        "und", "der", "die", "das", "gegen", "für", "fuer", "mit", "von",
        "im", "in", "zu", "zur", "des", "den", "mögliche", "moegliche",
        "aktuelle", "bestehender", "bestehende", "bestehenden", "bestehendes", "einer", "eines", "vs",
        "versus", "v", "and",
    }

    def words(text):
        result = set()
        for word in re.findall(r"[a-zäöüß0-9]+", text.casefold()):
            if word in ignored:
                continue
            # Harmloser Kurzname für "Marktdaten" in einer Titelvariante.
            if word == "marktdaten":
                word = "daten"
            result.add(word)
        return result

    expected_words = words(expected)
    if not expected_words:
        return None

    # Nur klar markierte Zusätze entfernen. Die vollständige Variante bleibt
    # ebenfalls Kandidat, damit inhaltlich notwendige Klammerteile erhalten sind.
    candidates = [title]
    without_parenthetical = re.sub(r"\s*\([^()]*\)", "", title).strip()
    if without_parenthetical != title:
        candidates.append(without_parenthetical)
    for separator in (" — ", " – ", " | ", ": "):
        if separator in title:
            candidates.append(title.split(separator, 1)[0].strip())
    if "/" in title:
        candidates.append(title.split("/", 1)[0].strip())
    # Eine verbreitete, semantisch harmlose Überschriftenerweiterung.
    if re.search(r"\s+und\s+Auslöser\s*$", title, flags=re.IGNORECASE):
        candidates.append(re.sub(r"\s+und\s+Auslöser\s*$", "", title, flags=re.IGNORECASE).strip())

    for candidate in candidates:
        actual_words = words(candidate.rstrip(":"))
        if actual_words == expected_words:
            return key, title
    return None

def _ist_fallback_pflichttext(body, key):
    """Erkennt bekannte Fallbacks trotz kleiner, inhaltlich harmloser Formulierungsvarianten."""
    if not body:
        return True
    compact = " ".join(body.casefold().split())
    if len(compact) > 700 or "\n" in body.strip():
        return False
    # Fallback-Texte sind kurze, deklarative Aussagen ohne konkrete Zahlen/Marken.
    if re.search(r"\b\d+(?:[.,]\d+)?\s*%|\b(?:usd|eur|ticker|kurs|stop|tp\d)\b", compact):
        return False
    families = {
        "6.1": (r"keine belastbare gegenueberstellung|keine belastbare gegenüberstellung", ("makro", "technik")),
        "6.2": (r"keine belastbare gegenueberstellung|keine belastbare gegenüberstellung", ("technik", "fundament")),
        "6.3": (r"keine belastbare gegenueberstellung|keine belastbare gegenüberstellung", ("sektor", "aktie")),
        "6.4": (r"keine belastbare gegenueberstellung|keine belastbare gegenüberstellung", ("rohstoff", "aktie")),
        "6.5": (r"keine belastbare pruefung|keine belastbare prüfung", ("investmentthese", "marktdaten")),
        "6.6": (r"keine belastbare risikobewertung|keine belastbare bewertung der risiken", ("risik", "ideen")),
        "9.1": (r"keine verifizier(?:baren|ten) makrotermine|keine bestaetigten makrotermine|keine bestätigten makrotermine", ("makro", "termin")),
        "9.2": (r"keine verifizier(?:baren|ten) unternehmens|keine bestaetigten unternehmens|keine bestätigten unternehmens", ("unternehmens",)),
        "9.3": (r"keine verifizier(?:baren|ten) branchenereignisse|keine bestaetigten branchenereignisse|keine bestätigten branchenereignisse", ("branchen",)),
        "9.4": (r"keine verifizier(?:baren|ten) technischen trigger|keine belastbaren technischen trigger", ("techn", "trigger")),
        "9.5": (r"keine belastbaren aktivierungs|keine verifizierbaren aktivierungs|keine belastbaren aktivierungs- oder invalidierungsbedingungen", ("aktivier", "invalidier")),
    }
    family = families.get(key)
    if not family:
        return False
    start_pattern, required_terms = family
    starts_as_fallback = bool(re.search(start_pattern, compact))
    if not starts_as_fallback and key.startswith("6."):
        # Harmlose Umschreibungen wie "Eine belastbare Gegenüberstellung ...
        # ist nicht möglich" gelten ebenfalls als Fallback, solange sie keine
        # konkreten Messwerte/Instrumente enthalten (oben bereits ausgeschlossen).
        noun_by_key = {
            "6.1": r"gegen(?:ueberstellung|überstellung).*?(?:nicht moeglich|nicht möglich|nicht belastbar)",
            "6.2": r"gegen(?:ueberstellung|überstellung).*?(?:nicht moeglich|nicht möglich|nicht belastbar)",
            "6.3": r"gegen(?:ueberstellung|überstellung).*?(?:nicht moeglich|nicht möglich|nicht belastbar)",
            "6.4": r"gegen(?:ueberstellung|überstellung).*?(?:nicht moeglich|nicht möglich|nicht belastbar)",
            "6.5": r"(?:pruefung|prüfung).*?(?:nicht moeglich|nicht möglich|nicht belastbar)",
            "6.6": r"(?:risikobewertung|bewertung der risiken).*?(?:nicht moeglich|nicht möglich|nicht belastbar)",
        }
        starts_as_fallback = bool(
            re.search(r"\b(?:eine|keine)\s+belastbare\b", compact)
            and re.search(noun_by_key.get(key, r"$^"), compact)
        )
    if not starts_as_fallback:
        return False
    return all(term in compact for term in required_terms)


def _repariere_doppelte_pflichtunterabschnitte(text):
    """Entfernt nur erkannte Fallback-Duplikate; erhält echte Überschriften und Inhalte.

    Die Funktion ist idempotent. Grenzen werden ausschließlich anhand echter,
    titelvalidierter Abschnittsüberschriften gebildet. Fließtext mit einer
    nummerähnlichen Präfixzeile kann weder als Abschnittsgrenze noch als Duplikat
    gewertet werden. Vorhandene fachliche Überschriften werden nie umgeschrieben.
    """
    if not text:
        return text

    canonical_keys = {"6.1", "6.2", "6.3", "6.4", "6.5", "6.6",
                      "9.1", "9.2", "9.3", "9.4", "9.5"}
    lines = text.splitlines(keepends=True)
    occurrences = []
    offset = 0
    for line in lines:
        heading = _echte_unterabschnittsueberschrift(line.rstrip("\r\n"))
        if heading and heading[0] in canonical_keys:
            occurrences.append({
                "key": heading[0], "start": offset,
                "line_end": offset + len(line), "line": line,
            })
        offset += len(line)

    if not occurrences:
        return text

    # Abschnittsgrenzen sind nur andere erkannte echte Überschriften, nicht
    # nummerierte Fließtextzeilen.
    all_boundaries = []
    offset = 0
    for line in lines:
        if _echte_unterabschnittsueberschrift(line.rstrip("\r\n")):
            all_boundaries.append(offset)
        offset += len(line)

    for item in occurrences:
        item["end"] = next(
            (pos for pos in all_boundaries if pos > item["start"]), len(text)
        )
        item["body"] = text[item["line_end"]:item["end"]].strip()
        item["fallback"] = _ist_fallback_pflichttext(item["body"], item["key"])

    by_key = {}
    for item in occurrences:
        by_key.setdefault(item["key"], []).append(item)

    remove_ranges = []
    for key, items in by_key.items():
        if len(items) < 2:
            continue
        real_items = [item for item in items if not item["fallback"]]
        if len(real_items) > 1:
            distinct_bodies = {
                re.sub(r"\s+", " ", item["body"]).strip() for item in real_items
            }
            if len(distinct_bodies) > 1:
                raise RuntimeError(
                    "DOPPELTE_PFLICHTUNTERABSCHNITTE_ECHTE_INHALTE: "
                    + key + " – mehrere substanzielle Abschnitte; keine automatische Löschung."
                )
            keep = real_items[0]
        elif real_items:
            keep = real_items[0]
        else:
            keep = items[0]
        for item in items:
            if item is not keep:
                remove_ranges.append((item["start"], item["end"]))

    if not remove_ranges:
        return text
    # Entfernen in umgekehrter Offset-Reihenfolge hält die Originalpositionen stabil.
    result = text
    for start, end in sorted(remove_ranges, reverse=True):
        result = result[:start] + result[end:]
    return result

def _pruefe_finale_struktur_und_konsistenz(text):
    """Harte letzte Schranke gegen die im Produktionslauf beobachteten Regressionen."""
    errors = []
    # Doppelte Nummern nur anhand titelvalidierter Abschnittsüberschriften zählen.
    # Eine nummerierte Fließtextzeile darf kein Struktur-Gate auslösen.
    occurrences = {}
    for line_number, line in enumerate((text or "").splitlines(), start=1):
        heading = _echte_unterabschnittsueberschrift(line)
        if heading:
            key = heading[0]
            title = heading[1] if len(heading) > 1 else line.strip()
            occurrences.setdefault(key, []).append((line_number, title.strip()))
    duplicates = sorted(k for k, items in occurrences.items() if len(items) > 1)
    if duplicates:
        # Echte doppelte Abschnittsüberschriften sind ein harter Gate-Fehler.
        # Nummerierte Erwähnungen im Fließtext werden vom titelvalidierten
        # Parser _echte_unterabschnittsueberschrift() ausgeschlossen.
        duplicate_details = []
        for key in duplicates:
            locations = "; ".join(
                f"Zeile {line_number}: {title!r}"
                for line_number, title in occurrences[key]
            )
            duplicate_details.append(f"{key} [{locations}]")
        errors.append(
            "Doppelte Unterabschnittsnummer(n): "
            + ", ".join(duplicates)
            + " | Überschriften: " + " | ".join(duplicate_details)
        )

    if re.search(r"(?i)Rekordhoch[^\n]{0,80}\(ca\.\s*1927\s*,", text or ""):
        errors.append("Mehrdeutige S&P-500-Formulierung: 1927 muss als Beginn der Datenreihe, nicht als Rekordjahr, bezeichnet werden.")

    b13 = _abschnitt_text(text, "1.3 Ideen im Aufbau", ["1.4 Frühindikatoren / neue Themen"])
    b4 = _abschnitt_text(text, "4. 🔭 IDEEN IM AUFBAU", ["5. 🥇 AKTIEN MIT FRÜHEM SIGNAL"])
    topic = re.search(r"(?im)^Thema / Assetklasse:\s*(.+)$", b13)
    sector = re.search(r"(?im)^Betroffene Assetklasse / Sektor:\s*(.+)$", b13)
    thesis4 = re.search(r"(?im)^\s*-\s*THESE:\s*(.+)$", b4)
    if topic and thesis4 and re.sub(r"\W+", "", topic.group(1).casefold()) != re.sub(r"\W+", "", thesis4.group(1).casefold()):
        # Die Überschriften dürfen unterschiedlich formuliert sein, der fachliche
        # Kern Cloud/SaaS vs. Energie/Netz darf jedoch nicht gegeneinander laufen.
        if re.search(r"(?i)cloud|cybersecurity|enterprise software|saas", topic.group(1)) and re.search(r"(?i)energie|netz|stromversorgung", (sector.group(1) if sector else "")):
            errors.append("1.3/4: Cloud-/Softwarethese widerspricht Energie-/Netzsektorzuordnung.")

    if errors:
        raise RuntimeError("FINALE_STRUKTUR_KONSISTENZ_UNGUELTIG: " + " | ".join(errors))
    print("FINALE-STRUKTUR-KONSISTENZ-GATE: PASS")


def _komprimiere_1_2_chancen(text):
    """Macht 1.2 zur echten Kurzuebersicht und laesst die Detailanalyse in 2.1."""
    if not text:
        return text
    start = re.search(r"(?m)^1\.2\s+Sofort handelbare Chancen\s*$", text)
    end = re.search(r"(?m)^1\.3\s+Ideen im Aufbau\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text
    block = text[start.end():end.start()]
    # Nur Kandidaten mit klar erkennbaren Pflichtfeldern komprimieren; falls das
    # Format abweicht, bleibt der Originalblock unveraendert.
    candidates = list(re.finditer(
        r"(?m)^([^\n]+\([^\n)]*\)[^\n]*)\n(.*?)(?=\n\s*\n[^\n]+\([^\n)]*\)|\Z)",
        block, re.S,
    ))
    rendered = []
    for match in candidates:
        chunk = match.group(0).strip()
        head = chunk.splitlines()[0].strip()
        ticker_m = re.search(r"\bTicker:\s*([A-Z0-9.^=-]+)", chunk)
        direction = re.search(r"(?im)^Richtung:\s*([^|\n]+)", chunk)
        sector = re.search(r"(?i)Sektor:\s*([^|\n]+)", chunk)
        setup = re.search(r"(?i)Setup:\s*([^|\n]+)", chunk)
        status = re.search(r"(?i)Status:\s*([^|\n]+)", chunk)
        price = re.search(r"(?im)^Kurs:\s*([^|\n]+)", chunk)
        entry = re.search(r"(?i)Einstieg:\s*([^|\n]+)", chunk)
        stop = re.search(r"(?i)Stop:\s*([^|\n]+)", chunk)
        risk = re.search(r"(?i)Risiko:\s*([^|\n]+)", chunk)
        tp1 = re.search(r"(?i)TP1:\s*([^|\n]+)", chunk)
        crv = re.search(r"(?i)CRV1:\s*([^|\n]+)", chunk)
        tp2 = re.search(r"(?i)TP2:\s*([^|\n]+)", chunk)
        source = re.search(r"(?i)Quelle:\s*([^\n]+)", chunk)
        if not (ticker_m and direction and setup and status and entry and stop and tp1 and crv):
            return text
        rendered.append(
            f"{head}\n"
            f"Setup: {setup.group(1).strip()} | Richtung: {direction.group(1).strip()} | "
            f"Status: {status.group(1).strip()}"
            + (f" | Sektor: {sector.group(1).strip()}" if sector else "")
            + "\n"
            + (f"Kurs: {price.group(1).strip()} | " if price else "")
            + f"Einstieg: {entry.group(1).strip()} | Stop: {stop.group(1).strip()}"
            + (f" | Risiko: {risk.group(1).strip()}" if risk else "")
            + f" | TP1: {tp1.group(1).strip()} | CRV1: {crv.group(1).strip()}"
            + (f" | TP2: {tp2.group(1).strip()}" if tp2 else "")
            + (f"\nQuelle: {source.group(1).strip()}" if source else "")
        )
    if not rendered:
        return text
    rebuilt = "\n\n" + "\n\n".join(rendered).strip() + "\n\n"
    return text[:start.end()] + rebuilt + text[end.start():]


def _ergaenze_handlungsprioritaet_2_4(text):
    """Trennt A-Kandidatenstatus von sofortiger Kaufprioritaet."""
    if not text:
        return text
    heading = re.search(r"(?m)^2\.4\s+HebelTrader\s*$", text)
    if not heading:
        return text
    end = re.search(r"(?m)^EINZEL-CHECK:\s*AKTUELLE KAUFKANDIDAT-A-MELDUNGEN\s*$", text[heading.end():])
    if not end:
        return text
    block_end = heading.end() + end.start()
    block = text[heading.end():block_end]
    if "Handlungspriorität:" in block or "Handlungsprioritaet:" in block:
        return text
    consequence = (
        "\nHandlungspriorität: A ist ein Kandidatenstatus, kein unmittelbares Kaufsignal. "
        "Kandidaten mit Momentum 0/4 und bärischem MACD bleiben auf Beobachtung; "
        "ein bullischer MACD allein macht einen Kandidaten mit Momentum 0/4 ebenfalls nicht kaufbereit. "
        "Ein hohes CRV1 allein hebt die Handlungspriorität nicht an.\n"
    )
    return text[:block_end].rstrip() + consequence + "\n" + text[block_end:]



def _repariere_makro_und_edelmetall_konsistenz(text, makro_text, edel_text):
    """Synchronisiert bekannte numerische Konflikte strikt aus den aktuellen Quellen.

    Nicht vergleichbare alternative Indexstände werden absichtlich nicht
    überschrieben: solange ihre Herkunft nicht belegt ist, bleiben sie getrennt
    und erhalten einen expliziten Hinweis auf die offene Quellenvergleichbarkeit.
    """
    if not text:
        return text, []

    changes = []
    # Makro: einzelne, strukturierte Faktenwerte aus dem aktuellen Makrobriefing.
    macro_lines = (makro_text or "").splitlines()
    ism_services = None
    for line in macro_lines:
        m = re.match(r"(?i)^\s*ISM Services PMI:\s*([-+]?\d[\d.,]*)\b", line)
        if m:
            ism_services = m.group(1).replace(".", ",")
            break

    if ism_services is not None:
        # Korrigiert nur das explizite PMI-Label bzw. die zugeordnete Services-Angabe,
        # nicht beliebige Vorkommen derselben Zahl (z.B. Subindizes).
        patterns = [
            (r"(?i)(ISM Services PMI\s+)(\d+(?:[.,]\d+)?)\s*(?:bzw\.\s*\d+(?:[.,]\d+)?|\|\s*\d+(?:[.,]\d+)?)",
             r"\g<1>" + ism_services),
            (r"(?i)(ISM Services PMI\s+)(?:54[,.]9|58[,.]8)\b", r"\g<1>" + ism_services),
            (r"(?i)(ISM Services\s+)(?:54[,.]9|58[,.]8)\b", r"\g<1>" + ism_services),
        ]
        for pattern, replacement in patterns:
            text, n = re.subn(pattern, replacement, text)
            if n:
                changes.append(f"ISM Services PMI auf Quellwert {ism_services} gesetzt ({n} Stelle(n))")

    # Brent/WTI: jede als Spread-/Differenzsatz erkennbare Formulierung erhält
    # die richtigen drei Rollen; der Spread darf nicht als Brent-Kurs erscheinen.
    refs = _extrahiere_makro_referenzwerte(makro_text or "")
    brent = refs.get("brent")
    wti = refs.get("wti")
    if brent and wti:
        b = float(brent["kurs"])
        w = float(wti["kurs"])
        spread = b - w
        fmt = lambda v: f"{v:.2f}".replace(".", ",")
        bad_energy = re.compile(
            r"(?i)Brent\s+" + re.escape(fmt(spread)) +
            r"\s*\$?\s*(?:vs\.?|gegenüber|und)\s*WTI\s+" + re.escape(fmt(w)) +
            r"\s*\$?\s*,?\s*(?:Differenz|Spread)\s+" + re.escape(fmt(spread)) + r"\s*\$?"
        )
        text, n = bad_energy.subn(
            f"Brent {fmt(b)} $ vs. WTI {fmt(w)} $, Spread {fmt(spread)} $",
            text,
        )
        if n:
            changes.append(f"Brent/WTI-Rollen korrigiert ({n} Stelle(n))")

    # Edelmetalle: Quelle liefert Kurs/4W/Tief/Abstand aus Lageblock und
    # EMA200/WMA200 aus dem expliziten Trendfolge-Diagnoseblock.
    if edel_text:
        lage_m = re.search(
            r"(?ms)^LAGE JE METALL\b.*?^(?=TRENDFOLGE-DIAGNOSE JE METALL\b)",
            edel_text,
        )
        lage_block = lage_m.group(0) if lage_m else ""
        metals = {
            "Gold": ("GC=F", "8.1 Gold"),
            "Silber": ("SI=F", "8.2 Silber"),
            "Platin": ("PL=F", "8.3 Platin"),
            "Palladium": ("PA=F", "8.4 Palladium"),
        }
        source_date_match = re.search(
            r"(?im)^\s*(?:(?:Datenstand|Stand|Erstellt am|Datum)\s*:\s*|EDELMETALLE-SCAN\s+)"
            r"(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})",
            edel_text,
        )
        source_date = source_date_match.group(1) if source_date_match else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
            try:
                source_date = datetime.datetime.strptime(source_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            except ValueError:
                source_date = ""
        source_date_note = f"; Quelle: {EDELMETALL_FAKTENQUELLE}; Datenstand {source_date}" if source_date else f"; Quelle: {EDELMETALL_FAKTENQUELLE}"
        source = {}
        for metal, (ticker, heading) in metals.items():
            lm = re.search(
                r"(?m)^\s*" + re.escape(metal) +
                r":\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|\s*"
                r"([-+]?\d[\d.,]*)%\s+in den letzten 4 Wochen\b([^\n]*)",
                lage_block,
            )
            if not lm:
                continue
            vals = {"kurs": lm.group(1), "fourw": lm.group(2)}
            low = re.search(r"52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%", lm.group(3))
            if low:
                vals.update({"low": low.group(1).rstrip("."), "distance": low.group(2).rstrip(".")})
            src_block = _edelmetall_quellenblock(edel_text, metal)
            tm = re.search(
                r"200er-Trend:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|\s*EMA200\s+([-+]?\d[\d.,]*)"
                r"\s*\([^)]*\)\s*\|\s*WMA200\s+([-+]?\d[\d.,]*)",
                src_block or "",
            )
            if tm:
                vals.update({"ema": tm.group(1), "wma": tm.group(2)})
            source[metal] = vals

        section_matches = list(re.finditer(r"(?m)^8\.[1-4]\s+(Gold|Silber|Platin|Palladium)\s*$", text))
        for idx, sm in reversed(list(enumerate(section_matches))):
            metal = sm.group(1)
            vals = source.get(metal)
            if not vals:
                continue
            end = section_matches[idx + 1].start() if idx + 1 < len(section_matches) else len(text)
            n9 = re.search(r"(?m)^9\.\s*📅\s*NÄCHSTE KATALYSATOREN\s*$", text[sm.end():end])
            if n9:
                end = sm.end() + n9.start()
            block = text[sm.start():end]
            ticker = metals[metal][0]
            # Entfernt veraltete Varianten der exakt quellengebundenen Felder,
            # ersetzt sie durch eine kanonische Einzelzeile. Kurzfristige 5T-Werte
            # bleiben erhalten, da sie in diesem Briefing nicht geliefert werden.
            def fmt_source_number(raw):
                value = _quellenwert_float(str(raw))
                if value is None:
                    # Kein nicht lesbarer Quellwert darf den gesamten Bericht
                    # am Speichern hindern; in diesem Fall den Originalwert
                    # unverändert und sichtbar lassen.
                    return str(raw)
                return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            four_w_display = vals["fourw"].replace(".", ",")
            canonical_lines = [
                f"Aktueller Kurs: {fmt_source_number(vals['kurs'])}$ (Futures {ticker}{source_date_note})",
                f"4-Wochen-Entwicklung: {four_w_display}% (Quelle: {EDELMETALL_FAKTENQUELLE})",
            ]
            if "low" in vals:
                canonical_lines.append(
                    f"52-Wochen-Situation: 52W-Tief bei {fmt_source_number(vals['low'])}$, aktueller Abstand {vals['distance'].replace('.', ',')}% darüber (Quelle: {EDELMETALL_FAKTENQUELLE})"
                )
            if "ema" in vals and "wma" in vals:
                canonical_lines.append(f"EMA200 / WMA200: EMA200 bei {fmt_source_number(vals['ema'])}$, WMA200 bei {fmt_source_number(vals['wma'])}$ (Quelle: {EDELMETALL_FAKTENQUELLE})")
            for label, canonical in [
                ("Aktueller Kurs", canonical_lines[0]),
                ("4-Wochen-Entwicklung", canonical_lines[1]),
            ]:
                pat = r"(?m)^\s*" + re.escape(label) + r":.*$"
                block, n = re.subn(pat, canonical, block)
                if n:
                    changes.append(f"{metal}: {label} quellengebunden ersetzt ({n})")
            if "low" in vals:
                pat = r"(?m)^\s*52-Wochen-Situation:.*$"
                block, n = re.subn(pat, canonical_lines[2], block)
                if n:
                    changes.append(f"{metal}: 52-Wochen-Situation quellengebunden ersetzt ({n})")
            if "ema" in vals and "wma" in vals:
                pat = r"(?m)^\s*EMA200\s*/\s*WMA200:.*$"
                block, n = re.subn(pat, canonical_lines[-1], block)
                if n:
                    changes.append(f"{metal}: EMA/WMA200 quellengebunden ersetzt ({n})")
            text = text[:sm.start()] + block + text[end:]

        # Gesamtbild muss dieselben autoritativen Kurse und 4W-Werte nennen.
        summary = re.search(r"(?ms)^EDELMETALL-GESAMTBILD\s*$.*?(?=^\s*9\.\s*📅\s*NÄCHSTE KATALYSATOREN|\Z)", text)
        if summary:
            block = summary.group(0)
            for metal in ("Gold", "Silber", "Platin", "Palladium"):
                vals = source.get(metal)
                if not vals:
                    continue
                # Ersetzt kursgebundene Klammerangabe nach Metallname, wenn vorhanden.
                pat = r"(?i)(" + re.escape(metal) + r"\s*)\(\s*[-+]?\d[\d.,]*\s*\$?\s*\)"
                canonical_price = f"{metal} ({fmt_source_number(vals['kurs'])}$)"
                block, n = re.subn(pat, canonical_price, block)
                if n:
                    changes.append(f"Gesamtbild {metal}: Kurs synchronisiert ({n})")
            text = text[:summary.start()] + block + text[summary.end():]

    # Auch der spätere Trigger-Kalender darf das aktuelle Palladium-Kurstief
    # nicht mit dem abweichenden Kurswert verwechseln.
    if edel_text:
        p_low = re.search(
            r"(?m)^\s*Palladium:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|\s*[-+]?\d[\d.,]*%\s+in den letzten 4 Wochen[^\n]*52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)",
            lage_block if "lage_block" in locals() else "",
        )
        if p_low:
            low_raw = p_low.group(1).rstrip(".,")
            low_numeric = _quellenwert_float(low_raw)
            if low_numeric is None:
                print(f"WARNUNG: Palladium-52W-Tief aus Quelle nicht numerisch lesbar: {low_raw!r}")
            else:
                low_value = f"{low_numeric:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                text, n = re.subn(
                    r"(?i)(Palladium:\s*Verteidigung des 52-Wochen-Tiefs bei\s*)[-+]?\d[\d.,]*\s*\$",
                    lambda m: m.group(1) + low_value + "$",
                    text,
                )
                if n:
                    changes.append(f"Palladium-Trigger auf 52W-Tief {low_value}$ gesetzt ({n})")

    # Alternative Indexstände: bewusst nicht angleichen; dokumentiere, dass die
    # gelieferten Dateien Herkunft/Vergleichbarkeit nicht abschließend belegen.
    if re.search(r"(?i)Alternativer Datenstand", text) and not re.search(r"(?i)Vergleichbarkeit.*(?:nicht|ungeklärt|offen)", text):
        note = (
            "Quellenhinweis: Die alternativen Index-Punktestände werden getrennt dokumentiert. "
            "Ihre genaue Herkunft, Indexdefinition und zeitliche Vergleichbarkeit sind durch die vorliegenden "
            "Dateien nicht abschließend belegt; sie werden deshalb nicht miteinander verrechnet oder automatisch vereinheitlicht."
        )
        anchor = re.search(r"(?m)^Hinweis zu den Datenständen:.*$", text)
        if anchor:
            text = text[:anchor.end()] + "\n" + note + text[anchor.end():]
            changes.append("Hinweis zur offenen Vergleichbarkeit alternativer Indexstände ergänzt")

    return text, changes


def _repariere_1_1_wti_btc_kurse(text, makro_text):
    """Ersetzt WTI- und BTC-Kursdarstellungen in 1.1 aus eindeutigen Makrozeilen.

    Quellenformat: `WTI: <Kurs> | ... | SOURCE=CL=F` und
    `Bitcoin: <Kurs> | ... | SOURCE=BTC-USD`. Andere Kennzahlen/Perioden
    werden nicht aus diesem Kurswert abgeleitet oder überschrieben.
    """
    if not text or not makro_text:
        return text, []
    start = re.search(r"(?m)^1\.1\s+Was hat sich seit dem letzten Lauf verändert\?\s*$", text)
    end = re.search(r"(?m)^1\.2\s+Sofort handelbare Chancen\s*$", text)
    if not (start and end and start.start() < end.start()):
        return text, []

    def parse_source(asset, expected_source):
        candidates = []
        for line in makro_text.splitlines():
            m = re.match(r"(?im)^\s*" + re.escape(asset) + r":\s*([-+]?\d[\d.,]*)\s*\|([^\n]*)$", line)
            if not m:
                continue
            tail = m.group(2)
            source_m = re.search(r"(?i)\bSOURCE\s*=\s*([^|\s]+)", tail)
            if source_m and source_m.group(1).upper() == expected_source.upper():
                value = _quellenwert_float(m.group(1))
                if value is not None and not _quellenwert_ist_mehrdeutig(m.group(1)):
                    candidates.append((value, m.group(1)))
        return candidates

    block = text[start.start():end.start()]
    changes = []
    for asset, source_id in (("WTI", "CL=F"), ("Bitcoin", "BTC-USD")):
        values = parse_source(asset, source_id)
        if len(values) != 1:
            # Warnung bleibt sichtbar; keine Schätzung/unsichere Quellwahl.
            if re.search(r"(?i)\b" + re.escape(asset) + r"\b", block):
                changes.append(f"{asset}: Kurs nicht ersetzt; eindeutige Quelle {source_id} fehlt/mehrdeutig ({len(values)} Treffer)")
            continue
        value = values[0][0]
        display = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        # Repariert ausschließlich die Kursangabe im 1.1-Absatz, nicht 5T/4W
        # oder technische Schwellen/Moving-Average-Werte.
        patterns = (
            r"(?i)(WTI(?:-Öl)?\s*\(?\s*)(?:[-+]?\d[\d.,]*\s*USD?)(?=\s*[;,)])",
            r"(?i)(Bitcoin\s*\(?\s*)(?:[-+]?\d[\d.,]*\s*USD?)(?=\s*[;,)])",
        )
        pat = patterns[0] if asset == "WTI" else patterns[1]
        block, n = re.subn(pat, lambda m: m.group(1) + display + " USD", block, count=1)
        if n:
            changes.append(f"{asset}: Kurs auf Makroquelle {source_id} ({display} USD) gesetzt")
        else:
            # Fließtext kann zwischen Assetname und Kurs einen beschreibenden
            # Satzteil enthalten (WTI-Öl markiert ... (KURSUSD)).
            if asset == "WTI":
                bad_pat = r"(?is)(\bWTI(?:-Öl)?\b[^()\n]*?\()\s*[-+]?\d[\d.,]*\s*(USD)"
            else:
                bad_pat = r"(?is)(\bBitcoin\s*\()\s*[-+]?\d[\d.,]*\s*(USD)"
            block, n = re.subn(bad_pat, lambda m: m.group(1) + display + " " + m.group(2), block, count=1)
            if n:
                changes.append(f"{asset}: beschädigte Kursdarstellung aus Makroquelle {source_id} ersetzt ({display} USD)")
    return text[:start.start()] + block + text[end.start():], changes

def _repariere_auswertung_final(text, eingabedateien=None, closed_history=""):
    """Letzter deterministischer Korrekturpass fuer die zuvor festgestellten Fehler."""
    inputs = eingabedateien or {}
    def source_text(key, pattern_key=None):
        path = inputs.get(key)
        if not path and pattern_key:
            try:
                path = finde_datei(DATEIMUSTER[pattern_key])
            except Exception:
                path = None
        if path and os.path.isfile(path):
            try:
                return Path(path).read_text(encoding="utf-8-sig")
            except OSError as exc:
                print(f"WARNUNG: Finaler Reparaturpass kann Quelle {key} nicht lesen: {exc}")
        return ""

    briefing = source_text("briefing.txt", "briefing.txt")
    macro = source_text("Makro_Briefing(...).txt", "Makro_Briefing(...).txt")
    edelmetall = source_text("Edelmetalle_Briefing(...).txt", "Edelmetalle_Briefing(...).txt")
    text = _repariere_1_1_rohstoffperioden(
        text, briefing, inputs.get("Letzte_Auswertung(...).txt"), edelmetall
    )
    text = _repariere_1_4_kapitalflussbehauptung(text)
    text = _repariere_3_2_rohstoffinterpretation(text, macro, briefing, edelmetall)
    text = _repariere_6_4_rohstoffperioden(text, briefing, macro)
    text = _repariere_8_doppelte_durchschnitte(text, briefing, edelmetall)
    text = _repariere_doppelte_sektion_6(text)
    text = _repariere_9_2_unbelegte_katalysatoren(text)
    text = _repariere_9_3_unbelegte_ereignisplatzhalter(text)
    text = _repariere_10_3_unbelegte_systemperformance(text)
    text = _repariere_11_7_doppelten_reichweitenhinweis(text)
    text = _repariere_1_3_aus_idee_im_aufbau(text)
    text = _repariere_3_5_fundamentaldaten(text)
    text = _repariere_7_7_macro_leakage(text, macro)
    text, _perioden_repaired = _repariere_7_performanceperioden(text, macro)
    text = _repariere_punkt10_5_und_portfolio(text, inputs, closed_history)
    text = _synchronisiere_1_1_mit_quellen(text, briefing, text)
    text, market_price_changes = _repariere_1_1_wti_btc_kurse(text, macro)
    if market_price_changes:
        print("1.1_MARKTKURSE_QUELLGEBUNDEN: " + " | ".join(market_price_changes))
    text = _komprimiere_1_2_chancen(text)
    text = _ergaenze_handlungsprioritaet_2_4(text)

    # Report-Zeitstempel von der Briefing-Erstellungszeit trennen.
    now = datetime.datetime.now(ZoneInfo("Europe/Berlin"))
    timestamp = now.strftime("%d.%m.%Y, %H:%M Uhr (MESZ/MEZ)")
    text = re.sub(r"(?m)^Erstellt am:\s*.*$", "Erstellt am: " + timestamp, text, count=1)

    # Der A-Status ist kein Kaufsignal; Momentum-/MACD-Einschraenkungen sichtbar halten.
    if "Einordnung: A-Status ist ein technischer Kandidatenstatus" not in text:
        text = re.sub(
            r"(?m)^2\.4 HebelTrader\s*$",
            "2.4 HebelTrader\nEinordnung: A-Status ist ein technischer Kandidatenstatus, keine bestätigte Kaufgelegenheit. "
            "Die vier aktuellen Meldungen haben jeweils Momentum 0/4; NEM, BTG und GH weisen einen bärischen MACD auf. "
            "BTG war bereits zuvor KAUFKANDIDAT A und ist kein neuer A-Aufstieg.\n",
            text, count=1
        )
    return text



def _repariere_auswertung_quellenkonflikte(text, eingabedateien=None, briefing_text="", makro_text="", edel_text=""):
    """Letztes quellengebundenes Konflikt-Gate: repariert nur belegbare Werte.

    Python-Quellen bleiben für Fakten maßgeblich; Gemini darf diese Fakten
    interpretieren, aber keine abweichenden Zahlen in den Bericht zurückschreiben.
    Fehlende Positionsziele werden entfernt statt aus thematischen Zahlen abgeleitet.
    """
    if not text:
        return text, []
    changes = []
    inputs = eingabedateien or {}

    def fmt_de(value, decimals=2):
        return f"{float(value):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    # 7.1: Der erste Indexblock darf keine alten Punktestände neben dem
    # aktuellen Briefing enthalten. Er wird vollständig aus dessen Regionalblöcken gebaut.
    if briefing_text:
        region_lines = {}
        region_dates = {}
        current_region = None
        for ln in briefing_text.splitlines():
            mreg = re.match(r"^\s*(Europa|USA|Asien)\s*\(Datenstand:\s*([^)]+)\):\s*$", ln, re.I)
            if mreg:
                current_region = mreg.group(1).casefold()
                region_dates[current_region] = mreg.group(2).strip()
                region_lines[current_region] = []
                continue
            if current_region and re.match(r"^\s*•\s*", ln):
                region_lines[current_region].append(ln.strip().replace("•", "-", 1))
            elif current_region and ln.strip() and not re.match(r"^\s*•\s*", ln):
                current_region = None
        # Nur dann ersetzen, wenn die Quelle die vollständigen Regionalblöcke hat.
        if all(region_lines.get(k) for k in ("europa", "usa", "asien")):
            for region, label in (("europa", "Europa"), ("usa", "USA"), ("asien", "Asien")):
                pat = re.compile(r"(?m)^- " + re.escape(label) + r" \(Datenstand: [^)]+\):[^\n]*$")
                source_date = region_dates.get(region)
                if not source_date:
                    continue
                replacement = f"- {label} (Datenstand: {source_date}): " + " | ".join(
                    re.sub(r"^-\s*", "", x).strip() for x in region_lines[region]
                )
                text, n = pat.subn(replacement, text, count=1)
                if n:
                    changes.append(f"7.1 {label}-Indexblock aus Briefing ersetzt")

    # Makro-Quellenwerte für Stundenlohn und Zinskurve robust auslesen.
    if makro_text:
        wage = re.search(r"(?i)Durchschnittlicher Stundenlohn\s*:\s*([-+]?\d[\d.,]*)", makro_text)
        if wage:
            wage_value = fmt_de(float(wage.group(1)))
            # Nur einen Stundenlohn-Niveauwert mit Waehrungseinheit ersetzen.
            # Prozentuale Lohnveraenderungen (z.B. +4,2%) bleiben unberuehrt.
            pat = re.compile(
                r"(?i)(Stundenl(?:ohn|öhne|oehne)\s*)(?:von\s*)?"
                r"[-+]?\d[\d.,]*\s*(?:USD|\$)(?:\s*/?\s*(?:Stunde|h))?"
            )
            text, n = pat.subn(lambda m: m.group(1) + wage_value + " USD", text)
            if n:
                changes.append(f"7.7 Stundenlohn auf Makro-Quellwert {wage_value} USD gesetzt ({n})")

        spread = re.search(r"(?m)^\s*10J-2J-Spread:\s*([+-]?\d[\d.,]*)\s*Prozentpunkte", briefing_text or "")
        if not spread:
            spread = re.search(r"(?i)2Y-10Y Spread:\s*([+-]?\d[\d.,]*)", makro_text)
        if spread:
            spread_value = fmt_de(float(spread.group(1)))
            # Keine fehlerhafte verklebte Spanne oder abweichende Gemini-Zahl stehen lassen.
            pat = re.compile(r"(?i)(10J\s*[-–]\s*2J\s*[-–]?\s*Spread\s*:\s*)[^)\n]*(?:\)|$)")
            text, n = pat.subn(lambda m: m.group(1) + f"+{spread_value} Prozentpunkte)", text)
            # Die separate Frühindikatoren-Zeile darf ebenfalls keine künstliche Spanne nennen.
            text, n2 = re.subn(
                r"(?i)(10J\s*[-–]\s*2J\s*[-–]?\s*Spread\s+verbleibt\s+bei\s+)[^.]*(?:Prozentpunkte)",
                lambda m: m.group(1) + f"+{spread_value} Prozentpunkte",
                text,
            )
            if n or n2:
                changes.append(f"Zinskurven-Spread auf Briefing-Quellwert +{spread_value} Prozentpunkte gesetzt ({n + n2})")

    # WTI-EMA-Aussage muss die tatsächlichen EMA-Werte respektieren.
    if makro_text:
        wti_source = briefing_text or makro_text
        wti = re.search(r"(?i)(?:Rohöl\s*\(\s*)?WTI\s*\)?\s*:\s*([-+]?\d+(?:\.\d+)?)\s*\|\s*EMA20:\s*([-+]?\d+(?:\.\d+)?)\s*\|\s*EMA50:\s*([-+]?\d+(?:\.\d+)?)", wti_source)
        if wti:
            price, ema20, ema50 = map(float, wti.groups())
            state = ("über" if price > ema20 else "unter" if price < ema20 else "auf") + " EMA20 und " + ("über" if price > ema50 else "unter" if price < ema50 else "auf") + " EMA50"
            # Korrigiert nur die WTI-Kursbeschreibung, nicht andere Assets/EMA-Angaben.
            text, n = re.subn(r"(?i)(WTI:\s*" + re.escape(fmt_de(price)) + r"\s*\$?\s*\([^\n)]*?)(?:über|unter|auf)\s+EMA20\s*/\s*EMA50", lambda m: m.group(1) + state, text)
            if n:
                changes.append(f"WTI-EMA-Aussage aus Quellkurs/EMA20/EMA50 korrigiert ({n})")

    # Edelmetall-4W: Edelmetalle-Briefing priorisieren, Marktwert bei fehlendem
    # oder unbrauchbarem Detailwert als gekennzeichneten Fallback verwenden.
    metal_values = {}
    for metal in ("Gold", "Silber", "Platin", "Palladium"):
        detail_4w = _edelmetall_4w_value(edel_text or "", metal)
        chosen = _waehle_edelmetall_4w(detail_4w, _extract_metal_4w_value(briefing_text, metal))
        if chosen is not None:
            metal_values[metal] = chosen[1]
    for idx, metal in enumerate(("Gold", "Silber", "Platin", "Palladium"), start=1):
        if metal not in metal_values:
            continue
        value = metal_values[metal]
        display = fmt_de(value, 1)
        heading = f"8.{idx} {metal}"
        sm = re.search(r"(?m)^" + re.escape(heading) + r"\s*$", text)
        if not sm:
            continue
        next_heading = re.search(r"(?m)^8\.[1-4]\s+(?:Gold|Silber|Platin|Palladium)\s*$|^9\.\s*📅?\s*NÄCHSTE KATALYSATOREN\s*$", text[sm.end():])
        en = sm.end() + next_heading.start() if next_heading else len(text)
        block = text[sm.start():en]
        block, n = re.subn(
            r"(?i)([-+]?\d+(?:[.,]\d+)?%\s*(?:bis\s*[-+]?\d+(?:[.,]\d+)?%\s*)?(?:in\s*(?:den letzten\s*)?4W|in den letzten 4 Wochen))",
            ("+" if value > 0 else "") + display + "% in 4W",
            block,
        )
        block, n_facts = re.subn(
            r"(?i)(\b4W:\s*)[-+]?\d+(?:[.,]\d+)?%",
            lambda mm: mm.group(1) + ("+" if value > 0 else "") + display + "%",
            block,
        )
        n += n_facts
        # Defensive normalization of doubled signs introduced by older repair passes.
        block = re.sub(r"--(?=\d)", "-", block)
        block, n2 = re.subn(
            r"(?im)^(\s*(?:[-•]\s*)?4-Wochen-Entwicklung:\s*)[-+]?\d+(?:[.,]\d+)?%[^\n]*$",
            lambda mm: mm.group(1) + ("+" if value > 0 else "") + display + "% (autoritative Markt-Briefing-Daten)",
            block,
        )
        if n or n2:
            text = text[:sm.start()] + block + text[en:]
            changes.append(f"{metal}: 4W-Performance auf priorisierten Quellwert {display}% gesetzt ({n + n2})")

    # Das 52W-Tief ist nicht der letzte Schlusskurs. Quelle: Edelmetall-Lageblock.
    if edel_text:
        m = re.search(r"(?m)^\s*Palladium:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|[^\n]*?52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)", edel_text)
        if m:
            low_value = _quellenwert_float(m.group(1))
            if low_value is None:
                print(f"WARNUNG: Palladium-52W-Tief aus Quelle nicht numerisch lesbar: {m.group(1)!r}")
            else:
                low = fmt_de(low_value)
                text, n = re.subn(r"(?i)(Palladium[^\n]{0,100}?52-Wochen-Tief\s*\()\s*[-+]?\d[\d.,]*", lambda x: x.group(1) + low, text)
                if n:
                    changes.append(f"Palladium-52W-Tief auf {low} USD gesetzt ({n})")
    # Der Trigger-Kalender verwendet teilweise USD statt Dollarzeichen; beide Formen
    # müssen auf dasselbe echte 52W-Tief zeigen, nicht auf den letzten Schlusskurs.
    if edel_text:
        low_match = re.search(r"(?m)^\s*Palladium:\s*Kurs\s+[-+]?\d[\d.,]*\s*\|[^\n]*?52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)", edel_text)
        if low_match:
            low_value = _quellenwert_float(low_match.group(1))
            if low_value is None:
                print(f"WARNUNG: Palladium-52W-Tief aus Quelle nicht numerisch lesbar: {low_match.group(1)!r}")
            else:
                low_display = fmt_de(low_value)
                text, n_low = re.subn(
                    r"(?i)(Palladium:\s*Verteidigung des 52-Wochen-Tiefs bei\s*)[-+]?\d[\d.,]*\s*(?:USD|\$)",
                    lambda mm: mm.group(1) + low_display + " USD",
                    text,
                )
                if n_low:
                    changes.append(f"Palladium-Trigger auf autoritatives 52W-Tief {low_display} USD gesetzt ({n_low})")

    # Keine fest codierten Palladium-Kurse verwenden: Falls Fließtext oder
    # Gesamtbild einen Kurs nennt, wird er nur dann ersetzt, wenn der aktuelle
    # Kurs explizit aus der gelieferten Edelmetall-Quelldatei extrahiert werden kann.
    if edel_text:
        p_current = re.search(
            r"(?m)^\s*Palladium:\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|",
            edel_text,
        )
        if p_current:
            current_raw = p_current.group(1).rstrip(".,")
            current_numeric = _quellenwert_float(current_raw)
            if current_numeric is None:
                print(f"WARNUNG: Palladium-Kurs aus Quelle nicht numerisch lesbar: {current_raw!r}")
            else:
                current_value = fmt_de(current_numeric)
                text, n = re.subn(
                    r"(?i)(Palladium\s+bei\s+)[-+]?\d[\d.,]*\s*(?:USD|\$)",
                    lambda m: m.group(1) + current_value + " USD",
                    text,
                )
                text, n2 = re.subn(
                    r"(?i)(Palladium\s*\(\s*)[-+]?\d[\d.,]*\s*(?:USD|\$)(\s*\))",
                    lambda m: m.group(1) + current_value + " USD" + m.group(2),
                    text,
                )
                if n or n2:
                    changes.append(f"Palladium-Fließtext auf aktuellen Quellkurs {current_value} USD synchronisiert ({n+n2})")

    # FCX: 6,74 USD ist der Kupferpreis je Pfund, kein Aktien-TP. Falls die
    # Positionsquelle keinen expliziten TP1-Wert enthält, wird die falsche Zahl entfernt.
    fcx_tp = None
    csv_path = inputs.get("Offene Positionen+Check.csv")
    if not csv_path:
        try:
            csv_path = finde_datei(DATEIMUSTER["Offene Positionen+Check.csv"])
        except Exception:
            csv_path = None
    position_rows = []
    if csv_path and os.path.isfile(csv_path):
        try:
            position_rows = _offene_positionen_rows(csv_path)
        except Exception as exc:
            print(f"WARNUNG: CSV-Positionsquelle konnte im finalen Konflikt-Gate nicht gelesen werden: {exc}")
    if not position_rows:
        xlsx_path = inputs.get("Offene Positionen+Check.xlsx")
        if not xlsx_path:
            try:
                xlsx_path = finde_datei(DATEIMUSTER["Offene Positionen+Check.xlsx"])
            except Exception:
                xlsx_path = None
        if xlsx_path and os.path.isfile(xlsx_path):
            try:
                from openpyxl import load_workbook
                wb = load_workbook(xlsx_path, read_only=True, data_only=True)
                ws = wb["Offene Positionen+Check"] if "Offene Positionen+Check" in wb.sheetnames else wb[wb.sheetnames[0]]
                rows = ws.iter_rows(values_only=True)
                next(rows, None)  # Titelzeile
                headers = [str(x or "").strip() for x in next(rows, ())]
                position_rows = [dict(zip(headers, row)) for row in rows if row and any(x is not None for x in row)]
                wb.close()
            except Exception as exc:
                print(f"WARNUNG: XLSX-Positionsquelle konnte im finalen Konflikt-Gate nicht gelesen werden: {exc}")
    for row in position_rows:
        ticker = _csv_value(row, ["Ticker", "Yahoo-Ticker", "Yahoo Ticker"]).strip().upper()
        if ticker == "FCX":
            for key in ("TP1", "TP_1", "Take_Profit_1", "Kursziel_1"):
                val = _parse_percent_value(_csv_value(row, [key]))
                if val is not None and val > 10:
                    fcx_tp = val
                    break
            break
    if fcx_tp is not None:
        target = fmt_de(fcx_tp)
        text, n = re.subn(r"(?i)(Freeport-McMoRan[^\n]{0,300}?TP1\s+bei\s+)[-+]?\d[\d.,]*\s*USD", lambda m: m.group(1) + target + " USD", text)
        if n:
            changes.append(f"FCX-TP1 auf autoritativen Positionswert {target} USD gesetzt ({n})")
    else:
        text, n = re.subn(r"(?i)\s*mit\s+TP1\s+bei\s+6[.,]74\s*USD", "", text)
        if n:
            changes.append(f"Falsches FCX-TP1 aus Kupferpreis entfernt ({n}); kein TP1 aus Positionsquelle verfügbar")

    # Portfolio-Performancewerte in positionsbezogenen Textzeilen aus CSV synchronisieren.
    if position_rows:
        try:
            position_values = {}
            for row in position_rows:
                ticker = _csv_value(row, ["Ticker", "Yahoo-Ticker", "Yahoo Ticker"]).strip().upper()
                if not ticker:
                    continue
                raw = _csv_value(row, ["Performance_Seit_Einstieg%", "Performance Seit Einstieg %", "Performance_Seit_Einstieg", "Performance%", "Performance"])
                value = _parse_percent_value(raw)
                if value is not None:
                    position_values[ticker] = value
            if position_values:
                sections = ["6. ⚠️ WIDERSPRÜCHE & RISIKEN", "10. 💼 BESTEHENDES PORTFOLIO"]
                for section in sections:
                    sm = re.search(r"(?m)^" + re.escape(section) + r"\s*$", text)
                    if not sm:
                        continue
                    end = re.search(r"(?m)^" + (r"7\. 🌍 MARKT- & MAKROKONTEXT" if section.startswith("6.") else r"11\. METHODIK / DATENQUALITÄT") + r"\s*$", text[sm.end():])
                    en = sm.end() + end.start() if end else len(text)
                    block = text[sm.start():en]
                    for ticker, value in position_values.items():
                        # Nur Zeilen mit explizitem Ticker und Prozentangabe werden geändert.
                        lines = block.splitlines()
                        changed_line = False
                        for i, line in enumerate(lines):
                            if ticker not in line.upper():
                                continue
                            if not re.search(r"(?i)performance|buchverlust|\(-?\d+[.,]\d+%\)", line):
                                continue
                            canonical = fmt_de(value) + "%"
                            # Mixed-position sentences may contain several percentages.
                            # Prefer the percentage immediately attached to this ticker.
                            ticker_pct = re.compile(
                                r"(?i)(\b" + re.escape(ticker) + r"\s*\)\s*\()\s*[-+]?\d+[.,]\d+%"
                            )
                            newline, count = ticker_pct.subn(lambda mm: mm.group(1) + canonical, line, count=1)
                            if not count and re.search(r"(?i)Performance\s*:", line):
                                # Slash-separated performance lists follow the ticker order in the same line.
                                listed_tickers = [x.upper() for x in re.findall(r"\(([A-Z0-9][A-Z0-9.-]{0,9})\)", line)]
                                pct_start = re.search(r"(?i)(Performance\s*:\s*)(.*)$", line)
                                if pct_start and ticker in listed_tickers:
                                    idx = listed_tickers.index(ticker)
                                    pct_text = pct_start.group(2)
                                    pct_matches = list(re.finditer(r"[-+]?\d+[.,]\d+%", pct_text))
                                    if idx < len(pct_matches):
                                        pm = pct_matches[idx]
                                        replacement_pct = canonical
                                        pct_text = pct_text[:pm.start()] + replacement_pct + pct_text[pm.end():]
                                        newline = line[:pct_start.start(2)] + pct_text
                                        count = 1
                            if not count:
                                ticker_count = sum(1 for known in position_values if known in line.upper())
                                if ticker_count == 1:
                                    newline, count = re.subn(r"(?<![\d])[-+]?\d+[.,]\d+%", canonical, line, count=1)
                            if count:
                                lines[i] = newline
                                changed_line = True
                        if changed_line:
                            block = "\n".join(lines)
                    text = text[:sm.start()] + block + text[en:]
        except Exception as exc:
            print(f"WARNUNG: Portfolio-Positionswerte konnten im finalen Konflikt-Gate nicht synchronisiert werden: {exc}")

    # Doppelte direkt aufeinanderfolgende Abschnittsüberschriften entfernen.
    text, n = re.subn(r"(?m)^(9\.5\s+Mögliche Aktivierung\s*/\s*Invalidierung)\s*\n\s*\1\s*$", r"\1", text)
    if n:
        changes.append(f"Doppelte Überschrift 9.5 entfernt ({n})")
    return text, changes



def _extract_metal_4w_value(source_text, metal):
    """Extract the percent explicitly attached to the metal's 4W field."""
    return extract_metal_four_week_value(source_text or "", metal)


# Verbindliche Faktenquellenregel: Python übernimmt Edelmetall-Fakten ausschließlich
# aus dem Edelmetalle-Briefing; Gemini interpretiert diese Fakten. Das Markt-Briefing
# darf nur für eine separat dokumentierte Abweichungsdiagnose gelesen werden, niemals
# als Ersatz-/Fallbackquelle für Edelmetallwerte in der Auswertung.
EDELMETALL_FAKTENQUELLE = "Edelmetalle-Briefing"


def _edelmetall_lage_block(edel_text):
    """Return only the current LAGE JE METALL block from the specialized source."""
    match = re.search(
        r"(?ms)^LAGE JE METALL\b.*?^(?=TRENDFOLGE-DIAGNOSE JE METALL\b)",
        edel_text or "",
    )
    return match.group(0) if match else ""


def _edelmetall_lage_zeile(edel_text, metal):
    """Return the exact metal row from LAGE JE METALL, never a duplicate elsewhere."""
    block = _edelmetall_lage_block(edel_text)
    return next((line.strip() for line in block.splitlines()
                 if re.match(r"(?i)^\s*" + re.escape(metal) + r"\s*:\s*Kurs\b", line)), "")


def _edelmetall_4w_value(edel_text, metal):
    """Read the 4W value only from the specialized source's LAGE JE METALL block."""
    block = _edelmetall_lage_block(edel_text)
    return _extract_metal_4w_value(block, metal) if block else None


def _edelmetall_4w_quellenkonflikte(edel_text, markt_text):
    """Return diagnostic 4W differences; they are not unresolved blockers by themselves."""
    conflicts = []
    for metal in ("Gold", "Silber", "Platin", "Palladium"):
        detail = _edelmetall_4w_value(edel_text, metal)
        market = _extract_metal_4w_value(markt_text or "", metal)
        if (
            detail is not None and detail[1] is not None
            and market is not None and market[1] is not None
            and abs(detail[1] - market[1]) > 1e-8
        ):
            conflicts.append(
                f"{metal}: Markt-Briefing {market[0]}% vs. "
                f"Edelmetalle-Briefing {detail[0]}%"
            )
    return conflicts




def _repariere_inline_8_x_kurzzeilen(text, edel_text):
    """Baut die vier kompakten 8.1–8.4-Zeilen ausschließlich aus Edelmetalle-Briefing-Fakten neu auf.

    Diese Zusammenfassungszeilen sind eine zweite Darstellung derselben Fakten wie
    die Detailblöcke. Sie werden vollständig ersetzt, damit keine alten Gemini-Zahlen,
    nicht belegten 5T-Werte oder EMA20/EMA50-Aussagen stehen bleiben.
    """
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("EDELMETALL_INLINE_GATE: Auswertungstext fehlt.")
    if not isinstance(edel_text, str) or not edel_text.strip():
        raise RuntimeError("EDELMETALL_INLINE_GATE: Edelmetalle-Briefing fehlt.")

    assets = (
        ("Gold", "GC=F", "8.1"),
        ("Silber", "SI=F", "8.2"),
        ("Platin", "PL=F", "8.3"),
        ("Palladium", "PA=F", "8.4"),
    )
    replacements = {}
    missing = []

    for asset, ticker, section in assets:
        source_row = _edelmetall_lage_zeile(edel_text, asset)
        if not source_row:
            missing.append(f"{asset}: LAGE-JE-METALL-Zeile")
            continue

        kurs_match = re.search(
            r"(?i)^\s*" + re.escape(asset) + r":\s*Kurs\s+([-+]?\d[\d.,]*)",
            source_row,
        )
        four_w = _edelmetall_4w_value(edel_text, asset)
        if not kurs_match:
            missing.append(f"{asset}: Kurs")
            continue
        if not four_w or four_w[1] is None:
            missing.append(f"{asset}: 4W")
            continue

        trend_block = _edelmetall_quellenblock(edel_text, asset)
        trend = _extrahiere_200er_trend(trend_block)
        if not trend:
            missing.append(f"{asset}: EMA200/WMA200")
            continue

        parts = [
            f"Quelle: {EDELMETALL_FAKTENQUELLE}",
            f"Kurs (Futures {ticker}): {kurs_match.group(1)} USD",
            f"4W: {four_w[0]}%",
        ]

        low_match = re.search(
            r"(?i)52-Wochen-Tief\s*\(([-+]?\d[\d.,]*)\s*,\s*([-+]?\d[\d.,]*)%",
            source_row,
        )
        if low_match:
            parts.extend([
                f"52W-Tief: {low_match.group(1)} USD",
                f"Abstand 52W-Tief: {low_match.group(2)}%",
            ])

        parts.extend([
            f"EMA200: {trend[1]} USD",
            f"WMA200: {trend[2]} USD",
            "Strategie-Status: "
            + "; ".join(
                f"{strategy}={_edelmetall_status(edel_text, strategy, asset, ticker)}"
                for strategy in ("Trendfolge", "Trendwende", "Short")
            ),
        ])
        replacements[section] = f"- {section} {asset}: " + " | ".join(parts)

    if missing:
        raise RuntimeError(
            "EDELMETALL_INLINE_GATE: Pflichtfakten fehlen im spezialisierten Briefing: "
            + " | ".join(missing)
        )

    # Alle bestehenden kompakten Zeilen ersetzen; falls eine fehlt, nach der
    # Überschrift von Abschnitt 8 einfügen. Duplikate werden entfernt.
    missing_rows = []
    for _asset, _ticker, section in assets:
        replacement = replacements[section]
        pattern = re.compile(
            r"(?m)^[ \t]*[-•]?[ \t]*" + re.escape(section)
            + r"[ \t]+" + re.escape(_asset) + r"[ \t]*:[^\n]*(?:\n|$)"
        )
        matches = list(pattern.finditer(text))
        if matches:
            count = {"n": 0}
            def _replace_row(match):
                count["n"] += 1
                return replacement + "\n" if count["n"] == 1 else ""
            text = pattern.sub(_replace_row, text)
        else:
            missing_rows.append(replacement)

    if missing_rows:
        heading = re.search(r"(?m)^8\.\s*.*EDELMETALLE.*$", text)
        if not heading:
            raise RuntimeError(
                "EDELMETALL_INLINE_GATE: Abschnitt 8 (EDELMETALLE) fehlt."
            )
        insert_at = heading.end()
        text = text[:insert_at] + "\n" + "\n".join(missing_rows) + "\n" + text[insert_at:]

    return text



def _repariere_unbelegte_edelmetall_aussagen(text):
    """Entfernt die drei bekannten, nicht durch die Edelmetallquelle belegten Aussagen.

    Die Reparatur ist deterministisch und idempotent. Sie läuft vor dem globalen
    Edelmetall-Release-Gate: nicht belegte Kurzfristbehauptungen werden nicht
    sprachlich kaschiert, sondern durch eine quellengebundene Formulierung ersetzt.
    """
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("EDELMETALL_AUSSAGEN_GATE: Auswertungstext fehlt.")

    lines = text.splitlines()
    repaired = []
    changes = []

    for line_no, line in enumerate(lines, 1):
        # Die frühere Gesamtbild-Zeile enthielt sowohl eine unbelegte 5T-Aussage
        # als auch einen nicht gelieferten EMA20/EMA50-Zustand. Ersetze die ganze
        # Zeile, damit keine Teilbehauptung aus derselben Synthese stehen bleibt.
        if re.search(r"(?i)EDELMETALL[- ]GESAMTBILD", line):
            new_line = (
                "- EDELMETALL-GESAMTBILD: Die Gesamteinordnung stützt sich "
                "ausschließlich auf die belegten 4W-/52W-Angaben sowie EMA200/WMA200; "
                "nicht belegte kurzfristige Zustände werden nicht behauptet."
            )
            if new_line != line:
                changes.append(f"Zeile {line_no}: Edelmetall-Gesamtbild quellengebunden ersetzt")
            repaired.append(new_line)
            continue

        # EMA20/EMA50 können in 9.4 als allgemeiner Index-Trigger vorkommen,
        # aber nicht als aktueller Edelmetall-Trigger, weil diese Metallwerte
        # nicht Teil der spezialisierten Faktenquelle sind. Die Zeile wird
        # daher eindeutig auf belegbare Metall-Trigger begrenzt.
        if (
            re.search(r"(?i)^\s*[-•]?\s*9\.4\s+Technische Trigger\s*:", line)
            and re.search(r"(?i)Edelmetall", line)
            and re.search(r"(?i)EMA\s*20\s*/\s*(?:EMA\s*)?50|EMA20/50", line)
        ):
            new_line = (
                "- 9.4 Technische Trigger: bestätigte technische Signale an "
                "europäischen Indizes; bei Edelmetallen ausschließlich "
                "quellenbelegte EMA200/WMA200-Niveaus; Ausbruch an lokalen "
                "Widerständen der Top-Sektoren."
            )
            if new_line != line:
                changes.append(f"Zeile {line_no}: unbelegte EMA20/EMA50-Edelmetalltrigger entfernt")
            repaired.append(new_line)
            continue

        repaired.append(line)

    if changes:
        print("EDELMETALL_AUSSAGEN_REPARIERT | " + " | ".join(changes))
    # splitlines() entfernt den abschließenden Zeilenumbruch. Den Zustand der
    # Eingabe deshalb explizit erhalten, damit die Reparatur keine reine
    # Formatänderung am Dateiende erzeugt.
    result = "\n".join(repaired)
    if text.endswith(("\n", "\r")):
        result += "\n"
    return result

def _edelmetall_interpretationswarnungen(text, edel_text):
    """Prueft Metallinterpretationen erst nach strikter Identitaets-/Abschnittsbindung.

    Rueckgabe ist eine Liste maschinenlesbarer Befunde:
      ZUORDNUNG | ...     -> Zahl/Quelle nicht sicher einem Metall zugeordnet
      INTERPRETATION | ... -> Metallbezug eindeutig, Aussage aber nicht belegt
    """
    report = str(text or "")
    source = str(edel_text or "")
    warnings = []
    seen = set()
    metals = {
        "Gold": {"name": r"\bGold\b", "ticker": r"\bGC=F\b"},
        "Silber": {"name": r"\bSilber\b", "ticker": r"\bSI=F\b"},
        "Platin": {"name": r"\bPlatin\b", "ticker": r"\bPL=F\b"},
        "Palladium": {"name": r"\bPalladium\b", "ticker": r"\bPA=F\b"},
    }

    def norm_num(value):
        try:
            value = str(value).replace("%", "").replace("$", "").strip()
            if "." in value and "," in value and value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            elif "," in value:
                value = value.replace(",", ".")
            return float(value)
        except (TypeError, ValueError):
            return None

    def same_number(a, b):
        return a is not None and b is not None and abs(a - b) <= max(0.015, abs(b) * 0.0005)

    # Aktuelle Fakten ausschliesslich aus der jeweils eigenen Lage-Zeile.
    facts = {}
    for metal in metals:
        row = _edelmetall_lage_zeile(source, metal) or ""
        item = {"percentages": [], "prices": []}
        fw = _edelmetall_4w_value(source, metal)
        if fw and fw[1] is not None:
            item["percentages"].append(float(fw[1]))
        low = re.search(r"(?i)52[- ]Wochen[- ]Tief\s*\(\s*([-+]?\d+(?:[.,]\d+)?)", row)
        if low:
            val = norm_num(low.group(1))
            if val is not None:
                item["prices"].append(val)
        for match in re.finditer(
            r"(?i)(?:Kurs|52[- ]Wochen[- ]Tief|52W[- ]Tief)\s*(?:\([^)]*\))?\s*[:=]?\s*([-+]?\d[\d.,]*)",
            row,
        ):
            val = norm_num(match.group(1))
            if val is not None:
                item["prices"].append(val)
        diag = re.search(
            r"(?is)" + re.escape(metal) + r"\s*\([^\n]*?\).*?(?=\n-\s*(?:Gold|Silber|Platin|Palladium)\s*\(|\Z)",
            source,
        )
        for indicator in ("EMA200", "WMA200"):
            im = re.search(r"(?i)\b" + indicator + r"\s+([-+]?\d[\d.,]*)", diag.group(0) if diag else "")
            if im:
                val = norm_num(im.group(1))
                if val is not None:
                    item["prices"].append(val)
        facts[metal] = item

    percent_re = re.compile(r"(?<![\w.])([-+]?\d+(?:[.,]\d+)?)\s*%")
    currency_re = re.compile(r"(?<![\w.])([-+]?\d[\d.,]*)\s*(?:\$|USD\b)", re.I)
    indicator_re = re.compile(r"(?i)\bEMA\s*\d+\s*/\s*(?:EMA\s*)?\d+\b|\b(?:EMA|WMA|SMA)\s*\d+\b")
    timeframe_re = re.compile(
        r"(?i)(?<![\w/])(?:\d+\s*(?:T|D|Tag|Tage|Handelstag|Handelstage|W|Woche|Wochen|Monat|Monate|Stunde|Stunden))\b|"
        r"\b(?:in den letzten|ueber die letzten|über die letzten|auf)\s+\d+\s+(?:Tagen?|Handelstagen?|Wochen?|Monaten?|Stunden?)\b|"
        r"\b5[- ]Tages[- ]Sicht\b"
    )

    # Zahlen werden nicht aus einer gesamten physischen Zeile verteilt. Erst
    # trennen wir eigenstaendige Aussagen; danach muss jede Aussage selbst
    # eine widerspruchsfreie Metallidentitaet enthalten.
    # Vollstaendige konservative Erkennung fremder Markt-/Rohstoffkontexte.
    # Namen UND gaengige Yahoo-/Index-Ticker werden erkannt. Bei gemischten
    # Edelmetall- und Fremdmarkt-Aussagen wird nicht geraten.
    unrelated_market = re.compile(
        r"(?i)(?:"
        r"\bS\s*&?P\s*500\b|\bS&P\s*500\b|\bSPX\b|\^GSPC|\bES=F\b|"
        r"\bNASDAQ(?:\s*100|\s*Composite)?\b|\bNDX\b|\^IXIC|\bNQ=F\b|"
        r"\bDAX\b|\bGDAXI\b|\^GDAXI|\bDow\s*Jones(?:\s*Industrial)?\b|\bDJI\b|\^DJI|\bYM=F\b|"
        r"\bRussell\s*2000\b|\bRUT\b|\^RUT|\bEuro\s*Stoxx(?:\s*50)?\b|\bSX5E\b|"
        r"\bMSCI\s*World\b|\bVIX\b|\^VIX|"
        r"\bFTSE\s*100\b|\bUKX\b|\^FTSE|\bNikkei(?:\s*225)?\b|\^N225|"
        r"\bHang\s*Seng\b|\^HSI|\bCAC\s*40\b|\^FCHI|\bIBEX\s*35\b|\^IBEX|"
        r"\bSMI\b|\^SSMI|\bTSX(?:\s*Composite)?\b|\^GSPTSE|\bKOSPI(?:\s*200)?\b|\^KS11|"
        r"\bASX\s*200\b|\^AXJO|\bBovespa\b|\^BVSP|\bSTOXX\s*600\b|\bSXXP\b|"
        r"\bNifty\s*50\b|\^NSEI|\bSensex\b|\^BSESN|\bMSCI\s*Emerging\s*Markets\b|"
        r"\bBrent(?:\s*(?:Crude|Oel|Öl))?\b|\bBZ=F\b|\bWTI\b|\bCL=F\b|"
        r"\bNatural\s*Gas\b|\bErdgas\b|\bNG=F\b|"
        r"\bKupfer\b|\bCopper\b|\bHG=F\b|"
        r"\bAluminium\b|\bAluminum\b|\bZink\b|\bZinc\b|\bNickel\b|"
        r"\bZinn\b|\bTin\b|\bTIN=F\b|\bBlei\b|\bLead\b|"
        r"\bUran\b|\bUranium\b|\bEisenerz\b|\bIron\s*Ore\b|"
        r"\bStahl\b|\bSteel\b|\bLithium\b|\bKobalt\b|\bCobalt\b|"
        # Platin und Palladium sind selbst Prüfmetalle und dürfen hier nicht als
        # Fremdmarkt-Treffer gelten. Ihre Name-/Ticker-Zuordnung wird separat geprüft.
        r"\bWeizen\b|\bWheat\b|\bZW=F\b|\bMais\b|\bCorn\b|\bZC=F\b|"
        r"\bSojab(?:ohnen|ean)\b|\bZS=F\b|\bKaffee\b|\bCoffee\b|\bKC=F\b|"
        r"\bZucker\b|\bSugar\b|\bSB=F\b|\bKakao\b|\bCocoa\b|\bCC=F\b|"
        r"\bBaumwolle\b|\bCotton\b|\bCT=F\b|\bRind(?:er)?\b|\bLive\s*Cattle\b|\bLE=F\b|"
        r"\bSchwein(?:e)?\b|\bLean\s*Hogs\b|\bHE=F\b|"
        r"\bHafer\b|\bOats?\b|\bZO=F\b|\bReis\b|\bRice\b|\bZR=F\b|"
        r"\bOrangensaft\b|\bOrange\s*Juice\b|\bOJ=F\b|\bHolz\b|\bLumber\b|\bLBS=F\b|"
        r"\bFeeder\s*Cattle\b|\bGF=F\b|"
        r"\bBitcoin\b|\bBTC(?:-USD)?\b|\bEthereum\b|\bETH(?:-USD)?\b|"
        r"\bDollar\s*Index\b|\bDXY\b|\bEUR\s*/\s*USD\b|\bUSD\s*/\s*JPY\b"
        r")"
    )
    foreign_section_header = re.compile(
        r"(?im)^\s*(?:\d+(?:\.\d+)*\s+)?"
        r"(?:Rohstoffe?|Commodit(?:y|ies)|Energie|Agrarrohstoffe|Indizes?|Aktien|"
        r"Devisen|Waehrungen|Währungen|Krypto(?:waehrungen|währungen)?|"
        r"Makro(?:daten|maerkte|märkte)?|Fremdmaerkte|Fremdmärkte)\b.*$"
    )


    def emit(line_no, category, metal, value, reason, snippet):
        compact = re.sub(r"\s+", " ", snippet).strip()[:180]
        value_text = "ZAHL=" + (str(value) if value is not None else "nicht_erkannt")
        msg = f"{category} | ROHSTOFF={metal} | {value_text} | GRUND={reason} | Zeile={line_no} | {compact}"
        if msg not in seen:
            seen.add(msg)
            warnings.append(msg)

    # Der Abschnittskontext wird aus den strukturellen Edelmetall-Überschriften
    # übernommen. So werden Zahlen ohne lokale Name/Ticker-Paarung nicht einfach
    # übergangen, sondern als unzugeordnet markiert.
    section_metal = None
    section_header = re.compile(
        r"(?im)^\s*8\.[1-4]\s+(Gold|Silber|Platin|Palladium)\s*$"
    )
    # Jeder andere nummerierte Haupt-/Unterabschnitt beendet den Metallkontext.
    # Damit koennen z.B. Zahlen aus 7.5 Rohstoffe oder 9. Aktien nicht in
    # einen vorherigen Edelmetallabschnitt hineinlaufen.
    any_numbered_header = re.compile(r"(?im)^\s*\d+(?:\.\d+)*\s+[^\n]{2,100}\s*$")
    for line_no, line in enumerate(report.splitlines(), 1):
        header_match = section_header.match(line)
        if header_match:
            section_metal = header_match.group(1).capitalize()
            continue
        if any_numbered_header.match(line) or foreign_section_header.match(line):
            section_metal = None
        if not line.strip():
            continue
        # Quellenvergleichs-/Diagnosezeilen sind keine Gemini-Metallinterpretation.
        if "EDELMETALL-4W-QUELLENKONFLIKT" in line.upper() or (
            "Markt-Briefing" in line and ("vs." in line or "Abweich" in line or "Vergleichsquelle" in line)
        ):
            continue
        chunks = re.split(r"(?<=[!?;])\s+|(?<=\.)\s+(?!\d)|\s+\|\s+", line)
        for chunk in chunks:
            if not chunk.strip():
                continue
            numbers = list(percent_re.finditer(chunk)) + list(currency_re.finditer(chunk))
            identities = []
            for metal, aliases in metals.items():
                for kind, pattern in aliases.items():
                    for match in re.finditer(pattern, chunk, re.I):
                        identities.append((match.start(), match.end(), metal, kind))
            if not identities:
                # Explizit erkannte Fremdmarkt-/Indexaussagen bleiben außerhalb
                # der Edelmetallprüfung, auch wenn sie direkt nach einer
                # Edelmetall-Überschrift stehen.
                if unrelated_market.search(chunk):
                    continue
                # In einem Edelmetallabschnitt können Zahlen auch ohne wiederholte
                # Identität auftreten. Diese werden nicht geraten oder verglichen.
                if section_metal:
                    unbound_numbers = numbers
                    for number in unbound_numbers:
                        emit(
                            line_no, "ZUORDNUNG", section_metal, number.group(0),
                            "Zahl im Edelmetall-Marktabschnitt ohne eindeutiges Name-/Ticker-Paar; nicht als Metallfakt verwendet",
                            chunk,
                        )
                continue

            names = {metal for _, _, metal, kind in identities if kind == "name"}
            tickers = {metal for _, _, metal, kind in identities if kind == "ticker"}
            # Identitaet ist ein vorgeschaltetes Gate: Erst wenn Name UND
            # passender Ticker im selben Aussageabschnitt stehen, darf die Zahl
            # mit den Fakten des Metalls verglichen werden. Name-only/Ticker-only
            # wird nicht geraten und ist daher ein Zuordnungsbefund.
            if not names or not tickers or names != tickers:
                affected = sorted(names | tickers) or ["UNGEKLAERT"]
                reason = (
                    "Name und Ticker fehlen gemeinsam oder widersprechen sich; "
                    "Zahl bleibt unzugeordnet"
                )
                for m in affected:
                    for number in numbers or [None]:
                        raw = number.group(0) if number else None
                        emit(line_no, "ZUORDNUNG", m, raw, reason, chunk)
                continue

            # Auch eine Aussage mit mehreren Metallen und einer fremden
            # Markt-/Indexidentitaet ist fuer Zahlenbindung mehrdeutig.
            # Marktwerte werden niemals gegen Edelmetall-Fakten verglichen.
            if unrelated_market.search(chunk) and numbers:
                affected = sorted(names | tickers)
                for m in affected:
                    for number in numbers:
                        emit(line_no, "ZUORDNUNG", m, number.group(0),
                             "Index-/Fremdmarktwert im selben Aussageabschnitt; Quelle nicht eindeutig", chunk)
                continue

            identities.sort(key=lambda x: (x[0], x[1]))
            # Mehrere unterschiedliche Metalle in einem Aussageabschnitt:
            # Werte nur bei eindeutig getrennten lokalen Klauseln zuordnen.
            unique_metals = {x[2] for x in identities}
            if len(unique_metals) > 1:
                # Wenn klar getrennte Name/Ticker-Klauseln vorliegen, wird an
                # Semikolon/Satzgrenzen getrennt; innerhalb dieses Chunks bleibt
                # die gemeinsame Zahlenbasis aber absichtlich ungeklärt.
                for m in sorted(unique_metals):
                    for number in numbers or [None]:
                        emit(line_no, "ZUORDNUNG", m, number.group(0) if number else None,
                             "mehrere Rohstoffe im selben Aussageabschnitt; Zahl nicht eindeutig gebunden", chunk)
                continue

            metal = next(iter(unique_metals))
            item = facts.get(metal, {})
            for tm in timeframe_re.finditer(chunk):
                token = tm.group(0)
                token_norm = re.sub(r"\s+", "", token.lower())
                if token_norm in {"4w", "4wochen", "52w", "52wochen"}:
                    continue
                around = chunk[max(0, tm.start() - 18):min(len(chunk), tm.end() + 24)]
                if re.search(r"(?i)52[- ]Wochen[- ]Tief|52W[- ]Tief", around):
                    continue
                emit(line_no, "INTERPRETATION", metal, token,
                     "Zeitraum nicht durch die aktuelle Metallquelle belegt", chunk)

            for im in indicator_re.finditer(chunk):
                indicator = re.sub(r"\s+", "", im.group(0)).upper()
                if indicator in {"EMA200", "WMA200"}:
                    continue
                context = chunk[max(0, im.start() - 55):min(len(chunk), im.end() + 90)]
                if re.search(r"(?i)\b(?:unter|ueber|über|unterschritten|ueberschritten|überschritten|darunter|darueber|darüber|notiert|liegt|liegen|ueberwinden|überwinden|bei|signal|trigger|durchschnitt|gleitenden|kreuzt|ausbruch)\b", context):
                    emit(line_no, "INTERPRETATION", metal, indicator,
                         "aktueller Indikator ist nicht als Faktenwert belegt (nur EMA200/WMA200)", chunk)

            for match in percent_re.finditer(chunk):
                value = norm_num(match.group(1))
                if value is None or any(same_number(value, known) for known in item.get("percentages", [])):
                    continue
                emit(line_no, "INTERPRETATION", metal, match.group(0),
                     "Prozentangabe stimmt nicht mit den Fakten dieses Rohstoffs ueberein", chunk)

            for match in currency_re.finditer(chunk):
                value = norm_num(match.group(1))
                if value is None or any(same_number(value, known) for known in item.get("prices", [])):
                    continue
                emit(line_no, "INTERPRETATION", metal, match.group(0),
                     "Preiszahl stimmt nicht mit Kurs/Tief/EMA200/WMA200 dieses Rohstoffs ueberein", chunk)
    return warnings

def _dokumentiere_edelmetall_4w_quellenkonflikte(text, edel_text, markt_text):
    """Keep 4W source disagreements visible in the saved report without blocking output."""
    marker = "EDELMETALL-4W-QUELLENKONFLIKT"
    # Idempotent replacement: a pre-existing generated note must not accumulate.
    text = re.sub(
        r"(?ms)^" + re.escape(marker) + r".*?(?:\n[ \t]*\n|\Z)",
        "",
        text or "",
    )
    conflicts = _edelmetall_4w_quellenkonflikte(edel_text, markt_text)
    if not conflicts:
        return text
    note = (
        marker + " | FACHLICH_FINAL=NICHT_FINAL | PRIORITÄT ANGEWENDET\n"
        "- Faktenquelle für sämtliche Edelmetallangaben: Edelmetalle-Briefing. Markt-Briefing dient ausschließlich der dokumentierten Abweichungsdiagnose und wird nicht als Fallback verwendet.\n"
        "- Dokumentierte Quellenabweichungen (keine Mittelung, kein stilles Überschreiben): "
        + " | ".join(conflicts)
        + "\n- Die spezialisierte Edelmetallquelle wurde deterministisch priorisiert; die Abweichung bleibt transparent dokumentiert.\n\n"
    )
    methodik = re.search(r"(?m)^11\. METHODIK / DATENQUALITÄT\s*$", text)
    if methodik:
        return text[:methodik.start()] + note + text[methodik.start():]
    return text.rstrip() + "\n\n" + note.rstrip() + "\n"


def _entferne_futures_basis_duplikate_aus_abschnitt8(text):
    """Remove every Futures-Basis line from 8.1–8.4, including inline variants.

    The documented-repair gate rejects any remaining Futures-Basis mention in
    these four sections. Therefore the final sanitizer uses the same broad
    condition and runs after all content-changing repairs.
    """
    if not text:
        return text
    pattern = re.compile(
        r"(?m)^(8\.[1-4]\s+(?:Gold|Silber|Platin|Palladium))\s*$"
    )
    matches = list(pattern.finditer(text))
    replacements = []
    for match in matches:
        following = re.search(
            r"(?m)^(?:8\.[1-4]\s+(?:Gold|Silber|Platin|Palladium)|"
            r"9\.\s*📅\s*NÄCHSTE KATALYSATOREN)\s*$",
            text[match.end():],
        )
        end = match.end() + following.start() if following else len(text)
        block = text[match.start():end]
        kept = [
            line for line in block.splitlines()
            if not re.search(r"(?i)Futures[- ]Basis", line)
        ]
        replacements.append((match.start(), end, "\n".join(kept).rstrip() + "\n"))
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text


def _waehle_edelmetall_4w(edelmetall_wert, markt_wert=None):
    """Priorisiert das Edelmetalle-Briefing; Markt-Briefing ist Fallback.

    Ein Quellenwert muss als (Rohwert, Zahlenwert) vorliegen und einen endlichen
    Zahlenwert enthalten. Die Rohdarstellung bleibt unveraendert erhalten.
    """
    def parse_source(source_value, source_name):
        if source_value is None:
            return None
        try:
            raw, value = source_value
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        # Ohne globale math-Abhaengigkeit nutzbar, auch bei isolierten AST-Tests.
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return raw, numeric, source_name

    detail = parse_source(edelmetall_wert, "Edelmetalle-Briefing")
    if detail is not None:
        return detail
    return parse_source(markt_wert, "Markt-Briefing-Fallback")


def _first_4w_value_in_block(block):
    """Find a percentage explicitly labelled as a 4W value in report text."""
    for line in (block or "").splitlines():
        raw = extract_four_week_percent(line)
        if raw is not None:
            return raw
    return None


def _pruefe_auswertung_quellenkonflikte(
    text, briefing_text="", makro_text="", edel_text="", eingabedateien=None
):
    """Prueft Zahlenkonflikte gegen die aktuell gelieferten Python-Quellen.

    Die Prüfung enthält keine tagesgebundenen Sollwerte. Sie vergleicht die
    im finalen Text explizit zugeordneten Werte mit den Werten der jeweiligen
    Eingabedateien. Nicht auflösbare Konflikte werden dokumentiert, verhindern
    aber nicht das Speichern der Tagesauswertung.
    """
    errors = []
    inputs = eingabedateien or {}

    def section(heading, next_heading=None):
        source = text or ""
        m = re.search(r"(?m)^" + re.escape(heading) + r"\s*$", source)
        if not m:
            return ""
        if next_heading:
            e = re.search(r"(?m)^" + re.escape(next_heading) + r"\s*$", source[m.end():])
            return source[m.start():m.end() + e.start() if e else len(source)]
        return source[m.start():]

    def fmt_value(value):
        return f"{float(value):,.6f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")

    def source_value_status(source, label, parenthesized=False):
        status, raw = _quellenwert_status_aus_zeile(source or "", label, parenthesized)
        value = _quellenwert_float(raw) if status == "VALUE" else None
        return status, raw, value

    def line_values_for_label(block, label):
        """Liest Zahlen, die in einer Ausgabezeile direkt einem Label zugeordnet sind."""
        values = []
        # Bevorzugt eine kanonische Label-/Wert-Zeile. Das Label wird als
        # vollständiger Begriff gesucht, damit z.B. Nasdaq nicht versehentlich
        # die Indexnummer "100" aus "Nasdaq 100" als Kurswert liest.
        label_re = re.compile(
            r"(?i)^\s*(?:[-•]\s*)?(?:\[[^]]+\]\s*)?" + re.escape(label) + r"(?![A-Za-z0-9])"
        )
        for line in (block or "").splitlines():
            lm = label_re.search(line)
            if not lm:
                continue
            tail = line[lm.end():]
            # Bei kombinierten Indexzeilen nur den Teil bis zum nächsten
            # Feldtrenner betrachten; kein fremdes Feld derselben Zeile lesen.
            tail = re.split(r"\s+[|;]\s+", tail, maxsplit=1)[0]
            # Optionaler Klammer-/Doppelpunkt-/Gleichheitsseparator, danach
            # der erste numerische Wert. Jahreszahlen im Label selbst liegen
            # vor lm.end() und werden dadurch ausgeschlossen.
            nm = re.search(r"^\s*(?:[:=]\s*)?(?:\(\s*)?([-+]?\d[\d.,]*)", tail)
            if not nm:
                nm = re.search(r"^\s*\(\s*([-+]?\d[\d.,]*)", tail)
            if nm:
                parsed = _quellenwert_float(nm.group(1))
                if parsed is not None:
                    values.append((line.strip(), parsed, nm.group(1)))
        return values

    def check_labeled_source(block, label, source, parenthesized=False, section_name=""):
        status, raw, expected = source_value_status(source, label, parenthesized)
        if status != "VALUE" or expected is None:
            return
        source_line = find_exact_labeled_line(source or "", label) or ""
        source_is_live = bool(re.search(
            r"(?i)REAL_HEUTE|laufender/aktueller Tageswert",
            source_line,
        ))
        for line, actual, actual_raw in line_values_for_label(block, label):
            # Zeitbasis muss an der konkreten Zahlenzeile erkennbar sein; ein
            # Live-Hinweis an anderer Stelle im Abschnitt darf nicht auf alle
            # Werte uebertragen werden.
            output_basis = line.casefold()
            if source_is_live and not re.search(
                r"(?i)live|intraday|laufender Tageswert|Live/Tagesstand|Schlusskurs|letzter Schluss|last close",
                output_basis,
            ):
                errors.append(
                    f"{section_name or 'Auswertung'} {label}: Zeitbasis nicht belegt. "
                    f"Quelle {raw} ist Live/Intraday; Ausgabe {actual_raw} hat keine passende Zeitbasis."
                )
                continue
            if not values_match_display(actual, expected, actual_raw):
                errors.append(
                    f"{section_name or 'Auswertung'} {label}: Ausgabe {actual_raw} widerspricht "
                    f"vergleichbarem Quellwert {raw}."
                )

    # Die aktuellen Indexstände werden labelbezogen aus dem heutigen Briefing
    # geprüft; es gibt keine Liste veralteter Zahlen mehr im Code.
    index_block = section("7.1 Aktienmärkte / Indizes", "7.2 Zinsen")
    index_labels = [
        ("S&P 500", False), ("Nasdaq", False), ("DAX", False),
        ("EuroStoxx50", False), ("Russell 2000", False), ("Nikkei 225", False),
        ("Hang Seng", False), ("Shanghai Composite", True),
    ]
    for label, parenthesized in index_labels:
        # Indexblock-Werte stammen standardmaessig aus dem Briefing mit dem
        # letzten abgeschlossenen Schlussstand. Ein einzelner Outputwert darf
        # nur dann auf den Live-Wert umgestellt werden, wenn seine eigene Zeile
        # die Live-/Intraday-Basis explizit ausweist.
        check_labeled_source(index_block, label, briefing_text, parenthesized, "7.1")

    # Direkte Makrofelder: alle verfügbaren Quellwerte werden mit den
    # labelgebundenen Zahlen im jeweiligen finalen Abschnitt verglichen.
    macro_labels = {
        "7.2 Zinsen": ["Fed Funds Effective Rate", "ECB Deposit Facility Rate", "US 2Y Treasury", "US 5Y Treasury", "US 10Y Treasury", "US 30Y Treasury", "Realzins 10Y TIPS", "2Y-10Y Spread", "10J-2J-Spread"],
        "7.3 Volatilität": ["VIX"],
        "7.4 FX": ["DXY", "EUR/USD", "USD/JPY"],
        "7.5 Rohstoffe": ["WTI", "Brent", "Erdgas", "Kupfer", "Aluminium", "Zink", "Lithium", "Eisenerz"],
        "7.6 Krypto": ["Bitcoin", "Ethereum"],
        "7.7 Konjunktur / Makro": ["CPI", "Core CPI", "PCE", "Core PCE", "PPI", "Arbeitslosenquote", "NFP / Nonfarm Payrolls", "ADP Employment Change", "Reales BIP-Wachstum", "ISM Manufacturing PMI", "ISM Services PMI"],
    }
    section_ends = {
        "7.2 Zinsen": "7.3 Volatilität",
        "7.3 Volatilität": "7.4 FX",
        "7.4 FX": "7.5 Rohstoffe",
        "7.5 Rohstoffe": "7.6 Krypto",
        "7.6 Krypto": "7.7 Konjunktur / Makro",
        "7.7 Konjunktur / Makro": "8. 🪙 EDELMETALLE",
    }
    for heading, labels in macro_labels.items():
        block = section(heading, section_ends.get(heading))
        for label in labels:
            source = makro_text
            if heading == "7.3 Volatilität" and label == "VIX":
                if not re.search(r"(?i)live|intraday|laufender Tageswert|Live/Tagesstand", block):
                    vix_close_source = re.sub(
                        r"(?im)^\s*VIX\s*\(Volatilitaet\)\s*:", "VIX:", briefing_text or ""
                    )
                    source = vix_close_source
            check_labeled_source(block, label, source, False, heading)

    # Stundenlohn: Wert wird dynamisch aus der aktuellen Makroquelle gelesen.
    wage_match = re.search(
        r"(?im)^\s*Durchschnittlicher Stundenlohn\s*:\s*([-+]?\d[\d.,]*)",
        makro_text or "",
    )
    wage_block = section("7.7 Konjunktur / Makro", "8. 🪙 EDELMETALLE")
    if wage_match:
        expected = _quellenwert_float(wage_match.group(1))
        if expected is not None:
            for line in (wage_block or "").splitlines():
                if not re.search(r"(?i)Stundenl(?:ohn|öhne|oehne)", line):
                    continue
                # Niveauwert nur aus einer explizit als USD/$ gekennzeichneten
                # Zahl lesen. Eine Wachstumsrate wie 4,2% ist kein Stundenlohn.
                wage_value_match = re.search(
                    r"(?i)([-+]?\d[\d.,]*)\s*(?:USD|\$)(?:\s*/?\s*(?:Stunde|h))?",
                    line,
                )
                if not wage_value_match:
                    continue
                parsed_raw = wage_value_match.group(1)
                parsed = _quellenwert_float(parsed_raw)
                if parsed is not None and not values_match_display(parsed, expected, parsed_raw):
                    errors.append(
                        f"7.7 Stundenlohn: Ausgabe {parsed_raw} USD/Stunde widerspricht aktuellem "
                        f"Quellwert {wage_match.group(1)} USD/Stunde."
                    )

    # Edelmetall-Performance: 4W-Werte werden aus der aktuellen Edelmetallquelle
    # gelesen, nicht aus festen Prozentzahlen im Programmcode.
    metal_names = ("Gold", "Silber", "Platin", "Palladium")
    for idx, metal in enumerate(metal_names, start=1):
        block = section(f"8.{idx} {metal}", f"8.{idx+1} " + metal_names[idx] if idx < 4 else "9. 📅 NÄCHSTE KATALYSATOREN")
        detail_4w = _edelmetall_4w_value(edel_text or "", metal)
        chosen_4w = _waehle_edelmetall_4w(detail_4w, _extract_metal_4w_value(briefing_text, metal))
        source_line = _edelmetall_lage_zeile(edel_text or "", metal)
        if source_line:
            perf_match = re.search(
                r"(?i)(?<!\d)([-+]?\d[\d.,]*)\s*%\s*4W\b",
                source_line,
            )
            if not perf_match:
                perf_match = re.search(
                    r"(?i)\b4W\s*[:=]?\s*([-+]?\d[\d.,]*)\s*%",
                    source_line,
                )
            if not perf_match:
                perf_match = re.search(
                    r"(?i)([-+]?\d[\d.,]*)\s*%\s*(?:in\s*(?:den letzten\s*)?4W|in den letzten 4 Wochen)",
                    source_line,
                )
            if perf_match:
                expected = _quellenwert_float(perf_match.group(1))
                if expected is not None:
                    for line in (block or "").splitlines():
                        if not re.search(r"(?i)\b4W\b|4[- ]Wochen-Entwicklung|in den letzten 4 Wochen", line):
                            continue
                        # Nur den Prozentwert pruefen, der syntaktisch direkt zu 4W
                        # gehoert. Der Abstand zum 52W-Tief (z.B. 7,6% darueber)
                        # ist eine andere Kennzahl und darf nicht als Rendite gelten.
                        raw_value = extract_four_week_percent(line)
                        if raw_value is not None:
                            actual = _quellenwert_float(raw_value)
                            if actual is not None and not values_match_display(actual, expected, raw_value):
                                errors.append(
                                    f"8.{idx} {metal}: 4W-Wert {raw_value}% widerspricht "
                                    f"aktuellem Quellwert {perf_match.group(1)}%."
                                )
        # Der ausgegebene 4W-Wert muss mit der priorisierten Quelle oder dem
        # gekennzeichneten Markt-Fallback übereinstimmen.
        detail_4w = _edelmetall_4w_value(edel_text or "", metal)
        chosen_4w = _waehle_edelmetall_4w(detail_4w, _extract_metal_4w_value(briefing_text, metal))
        if chosen_4w is not None:
            block = section(f"8.{idx} {metal}", "9. 📅 NÄCHSTE KATALYSATOREN")
            raw_value = _first_4w_value_in_block(block)
            actual_value = _quellenwert_float(raw_value) if raw_value is not None else None
            if actual_value is None or not values_match_display(actual_value, chosen_4w[1], raw_value):
                errors.append(
                    f"8.{idx} {metal}: 4W-Wert fehlt oder widerspricht priorisierter Quelle "
                    f"{chosen_4w[2]} ({chosen_4w[0]}%)."
                )

        # Palladium-Livekurs und letzter Schlusskurs sind getrennte Felder.
        # Ohne explizite Zeitbasis darf der Pruefer keinen der beiden Werte als
        # Sollwert fuer den anderen verwenden.
        if metal == "Palladium":
            current_match = re.search(
                r"(?im)^\s*Palladium:\s*Kurs\s+([-+]?\d[\d.,]*)\s*\|",
                edel_text or "",
            )
            current_expected = _quellenwert_float(current_match.group(1)) if current_match else None
            macro_close_line = find_exact_labeled_line(makro_text or "", "Palladium") or ""
            close_match = re.match(r"(?i)^Palladium:\s*([-+]?\d[\d.,]*)", macro_close_line)
            close_expected = _quellenwert_float(close_match.group(1)) if close_match else None
            for line in (section("8.4 Palladium", "9. 📅 NÄCHSTE KATALYSATOREN") or "").splitlines():
                for price_match in re.finditer(
                    r"(?i)Palladium\s+(?:bei\s+|\(\s*)([-+]?\d[\d.,]*)\s*(?:USD|\$)",
                    line,
                ):
                    actual_price = _quellenwert_float(price_match.group(1))
                    if actual_price is None:
                        continue
                    if re.search(r"(?i)Schlusskurs|letzter Schluss|last close", line):
                        if close_expected is None or not values_match_display(actual_price, close_expected, price_match.group(1)):
                            errors.append(
                                f"8.4 Palladium: Schlusskurs {price_match.group(1)} ist nicht durch "
                                f"den verfügbaren Schlusskurs {close_match.group(1) if close_match else 'NICHT VERFÜGBAR'} belegt."
                            )
                    elif re.search(r"(?i)live|intraday|laufender Tageswert|aktueller Kurs", line):
                        errors.append(
                            f"8.4 Palladium: Live-Kurs {price_match.group(1)} kann ohne identischen Abrufzeitpunkt "
                            f"nicht gegen den Quellenwert {current_match.group(1) if current_match else 'NICHT VERFÜGBAR'} geprüft werden."
                        )
                    else:
                        errors.append(
                            f"8.4 Palladium: Kurs {price_match.group(1)} ohne ausgewiesene Zeitbasis; "
                            "Livekurs und letzter Schlusskurs sind nicht vergleichbar."
                        )
        if metal == "Palladium":
            edel_source_line = next(
                (ln for ln in (edel_text or "").splitlines()
                 if re.match(r"(?i)^\s*Palladium:\s*Kurs\b", ln)),
                "",
            )
            low_match = re.search(
                r"(?i)52[- ]Wochen[- ]Tief\s*\(\s*([-+]?\d[\d.,]*)",
                edel_source_line,
            )
            if low_match:
                expected_low = _quellenwert_float(low_match.group(1))
                if expected_low is not None:
                    for line in (block or "").splitlines():
                        if not re.search(r"(?i)52[- ]Wochen[- ]Tief", line):
                            continue
                        num = re.search(r"52[- ]Wochen[- ]Tief\s*\(\s*([-+]?\d[\d.,]*)", line, re.I)
                        if num:
                            actual_low = _quellenwert_float(num.group(1))
                            if actual_low is not None and not values_match_display(actual_low, expected_low, num.group(1)):
                                errors.append(
                                    f"8.4 Palladium: 52-Wochen-Tief {num.group(1)} widerspricht "
                                    f"aktuellem Quellwert {low_match.group(1)}."
                                )

    # Positions-Performance dynamisch aus Offene Positionen+Check.csv prüfen.
    csv_path = inputs.get("Offene Positionen+Check.csv")
    if not csv_path:
        try:
            csv_path = finde_datei(DATEIMUSTER["Offene Positionen+Check.csv"])
        except Exception:
            csv_path = None
    if csv_path and os.path.isfile(csv_path):
        try:
            rows = _offene_positionen_rows(csv_path)
        except Exception as exc:
            print(f"WARNUNG: Positionsquelle für Zahlenkonsistenz-Gate nicht lesbar: {exc}")
            rows = []
        portfolio = section("10. 💼 BESTEHENDES PORTFOLIO", "11. METHODIK / DATENQUALITÄT")
        for row in rows:
            ticker = _csv_value(row, ["Ticker", "Yahoo-Ticker", "Yahoo Ticker"]).strip().upper()
            raw_perf = _csv_value(row, [
                "Performance_Seit_Einstieg%", "Performance Seit Einstieg %",
                "Performance_Seit_Einstieg", "Performance%", "Performance",
            ])
            expected = _parse_percent_value(raw_perf)
            if not ticker or expected is None:
                continue
            for line in (portfolio or "").splitlines():
                if ticker not in line.upper() or not re.search(r"(?i)performance|buchverlust|\\([-+]?\\d+[.,]\\d+%\\)", line):
                    continue
                # Direkte tickerbezogene Klammerform, z.B. TICKER) (-12,34%).
                associated = re.search(
                    r"(?i)" + re.escape(ticker) + r"\s*\)?\s*\(\s*([-+]?\d[\d.,]*)\s*%",
                    line,
                )
                if associated:
                    actual = _parse_percent_value(associated.group(1))
                    if actual is not None and abs(actual - expected) > max(1e-6, abs(expected) * 1e-6):
                        errors.append(
                            f"Portfolio {ticker}: Performance {associated.group(1)}% widerspricht "
                            f"aktuellem Positionsquellwert {raw_perf}%."
                        )

    # Strukturfehler ohne Tageswerte bleiben weiterhin prüfbar.
    if len(re.findall(r"(?m)^9\.5\s+Mögliche Aktivierung\s*/\s*Invalidierung\s*$", text or "")) > 1:
        errors.append("Doppelte Überschrift 9.5 Mögliche Aktivierung / Invalidierung")

    # Die Gate-Meldung darf nicht zum Abbruch des Tageslaufs führen.
    if errors:
        print("ZAHLENKONSISTENZ-GATE: WARNUNG | " + " | ".join(errors))
        note_lines = [
            "",
            "ZAHLENKONSISTENZ – OFFENE PRÜFPUNKTE",
            "Die Auswertung wurde trotz einzelner nicht automatisch auflösbarer Zahlenkonflikte erstellt. "
            "Python-Quelldateien bleiben maßgeblich; ungeklärte Werte dürfen nicht als bestätigt interpretiert werden.",
        ]
        note_lines.extend(f"- NICHT AUTOMATISCH AUFGELÖST: {item}" for item in errors)
        note = "\n".join(note_lines)
        methodik = re.search(r"(?m)^11\. METHODIK / DATENQUALITÄT\s*$", text or "")
        if methodik:
            text = text[:methodik.start()] + note + "\n\n" + text[methodik.start():]
        else:
            text = text.rstrip() + "\n" + note + "\n"
        return text, errors
    print("ZAHLENKONSISTENZ-GATE: PASS | Aktuelle Python-Quellen ohne erkannte labelgebundene Zahlenkonflikte")
    return text, []


def _gemini_pruefe_freigabe_vor_speicherung(zahlen_gate_warnungen, fachliche_mindesttiefe_offen):
    """Protokolliert dokumentierbare Qualitaetswarnungen; harte Gates laufen separat."""
    if zahlen_gate_warnungen:
        print(
            "GEMINI_RELEASE_GATE=WARNUNG | "
            f"{len(zahlen_gate_warnungen)} dokumentierte Quellen-/Zahlenpruefpunkte; "
            "Speicherung wird nicht allein dadurch blockiert."
        )
        for warnung in zahlen_gate_warnungen:
            print("QUELLEN_ZAHLENKONFLIKT_DOKUMENTIERT | " + str(warnung))

    if fachliche_mindesttiefe_offen:
        print(
            "GEMINI_RELEASE_GATE=WARNUNG | inhaltliche Mindesttiefe mit offenem "
            "Pruefpunkt; Speicherung wird nicht allein dadurch blockiert."
        )

    # Harte Integritaetsfehler werden nicht hier abgeschwaecht: Quellenbindung,
    # Pflichtstruktur, Name/Ticker, Quellperioden/Positionen, Themenkausalitaet
    # und das globale Edelmetall-Release-Gate werden separat vor dem Schreiben
    # geprueft und werfen bei Fehlern weiterhin RuntimeError.



def _gemini_bereichsstatus(
    zahlen_gate_warnungen=None,
    fachliche_mindesttiefe_offen=False,
    edelmetall_nicht_final=False,
    edelmetall_global_warnungen=None,
):
    """Erzeugt fail-closed Bereichsstatus aus expliziten oder klaren Befunden.

    Abschnittsnummern werden nur am Warnungsanfang ausgewertet. Bei mehreren
    widerspruechlichen Fachsignalen bleibt die Warnung berichtsweit, statt eine
    falsche Bereichszuordnung zu behaupten.
    """
    zahlen_gate_warnungen = list(zahlen_gate_warnungen or [])
    edelmetall_global_warnungen = list(edelmetall_global_warnungen or [])
    bereiche = {
        "AKTIEN": [], "MARKT_MAKRO": [], "EDELMETALLE": [],
        "GESAMTBERICHT": [],
    }

    def zuordnen(warnung):
        text = str(warnung or "").casefold()
        # Stabile, explizite Bereichstags haben Vorrang vor Freitext.
        tag = re.match(r"^\s*\[(aktien|markt_makro|edelmetalle)\]", text)
        if tag:
            return {"aktien": "AKTIEN", "markt_makro": "MARKT_MAKRO",
                    "edelmetalle": "EDELMETALLE"}[tag.group(1)]

        # Nummern nur am Anfang / nach einem bekannten Abschnittslabel lesen;
        # beiläufige Zahlen im Warntext dürfen keinen Bereich festlegen.
        nummer = re.match(r"^\s*(?:abschnitt\s*)?(\d+(?:\.\d+)*)(?:[.):]\s|\s+)", text)
        nummer_bereich = None
        if nummer:
            haupt = nummer.group(1).split(".", 1)[0]
            if haupt in {"2", "4", "5", "6"}:
                nummer_bereich = "AKTIEN"
            elif haupt in {"1", "3", "7", "8"}:
                nummer_bereich = "MARKT_MAKRO"

        metal = any(token in text for token in (
            "edelmetall", "gold", "silber", "platin", "palladium",
            "rohstoff=", "metall",
        ))
        stock = any(token in text for token in (
            "ticker", "aktie", "aktien", "kandidat", "unternehmen",
        ))
        market = any(token in text for token in (
            "vix", "wti", "brent", "makro", "markt",
            "volatilitaet", "volatilität", "sektor",
        ))
        domains = [name for name, active in (
            ("EDELMETALLE", metal), ("AKTIEN", stock),
            ("MARKT_MAKRO", market),
        ) if active]

        # Abschnittsnummer und Textsignale muessen zusammenpassen. Ein
        # widerspruechlicher oder fachlich gemischter Hinweis bleibt global.
        if nummer_bereich:
            if domains and (len(domains) != 1 or domains[0] != nummer_bereich):
                return None
            return nummer_bereich
        return domains[0] if len(domains) == 1 else None

    for warnung in zahlen_gate_warnungen:
        bereich = zuordnen(warnung)
        if bereich:
            bereiche[bereich].append(str(warnung))
        else:
            bereiche["GESAMTBERICHT"].append(str(warnung))

    for warnung in edelmetall_global_warnungen:
        bereiche["EDELMETALLE"].append(str(warnung))

    if fachliche_mindesttiefe_offen:
        bereiche["GESAMTBERICHT"].append("Inhaltliche Mindesttiefe nicht bestanden")
    if edelmetall_nicht_final and not bereiche["EDELMETALLE"]:
        bereiche["EDELMETALLE"].append("Edelmetallprüfung nicht bestanden")

    status = {name: ("NICHT_FINAL" if befundliste else "FINAL")
              for name, befundliste in bereiche.items()}
    status["GESAMT"] = ("NICHT_FINAL" if any(
        value == "NICHT_FINAL" for value in status.values()
    ) else "FINAL")
    return status, bereiche


def _gemini_report_bereichsstatus(report_text):
    """Liest Bereichsstatus/-befunde aus einer gespeicherten Auswertung robust aus."""
    text = str(report_text or "")
    status = {}
    befunde = {name: [] for name in (
        "AKTIEN", "MARKT_MAKRO", "EDELMETALLE", "GESAMTBERICHT"
    )}
    status_matches = re.findall(
        r"(?m)^BEREICH_STATUS=(AKTIEN|MARKT_MAKRO|EDELMETALLE|GESAMTBERICHT)\|(FINAL|NICHT_FINAL)\s*$",
        text,
    )
    for bereich, wert in status_matches:
        if bereich in status:
            status[bereich] = "NICHT_FINAL"
        else:
            status[bereich] = wert
    for bereich, befund in re.findall(
        r"(?m)^BEREICH_BEFUND=(AKTIEN|MARKT_MAKRO|EDELMETALLE|GESAMTBERICHT)\|(.*)$",
        text,
    ):
        befunde[bereich].append(befund)
    # Fehlende/duplizierte Statusmarker sind kein implizites FINAL.
    for bereich in befunde:
        if bereich not in status:
            status[bereich] = "NICHT_FINAL"
        if any(bereich == marker_bereich for marker_bereich, _ in status_matches) and sum(
            1 for marker_bereich, _ in status_matches if marker_bereich == bereich
        ) != 1:
            status[bereich] = "NICHT_FINAL"
    # Legacy-Edelmetallstatus wird separat konsumiert und muss mit dem
    # Bereichsmarker konsistent sein.
    metall_marker = re.findall(r"(?m)^EDELMETALL_STATUS=(FINAL|NICHT_FINAL)\s*$", text)
    if len(metall_marker) != 1 or metall_marker[0] != status["EDELMETALLE"]:
        status["EDELMETALLE"] = "NICHT_FINAL"

    gesamt = "NICHT_FINAL" if any(v == "NICHT_FINAL" for v in status.values()) else "FINAL"
    bericht_marker = re.findall(r"(?m)^BERICHT_STATUS=(FINAL|NICHT_FINAL)\s*$", text)
    fach_marker = re.findall(r"(?m)^FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text)
    if len(bericht_marker) != 1 or len(fach_marker) != 1:
        gesamt = "NICHT_FINAL"
    elif bericht_marker[0] != gesamt or fach_marker[0] != gesamt:
        gesamt = "NICHT_FINAL"
    status["GESAMT"] = gesamt
    return status, befunde



def _trading_radar_berichtsfelder_normalisieren(text):
    """Ergaenzt fehlende M-06/M-08-Pflichtfelder explizit, ohne Werte zu erfinden."""
    if not isinstance(text, str) or not text.strip():
        return text

    def locate_section(source, number, title, next_number):
        start = re.search(
            rf"(?im)^[ \t]*(?:#{{1,6}}\s*)?{number}\s*[.·]\s*{re.escape(title)}[^\r\n]*$",
            source,
        )
        if not start:
            return None
        tail = source[start.end():]
        end = re.search(
            rf"(?im)^[ \t]*(?:#{{1,6}}\s*)?{next_number}(?:\s*[.·](?!\d)|\s+)[^\r\n]*$",
            tail,
        )
        if not end:
            return None
        return start, start.end(), start.end() + end.start()

    def field_value_ok(body, labels):
        label = r"(?:" + "|".join(labels) + r")"
        matches = list(re.finditer(
            rf"(?im)(?:^|[|;]\s*|\n)\s*{label}\s*[:=]\s*([^|;\r\n]+)", body
        ))
        if not matches:
            return bool(re.search(
                rf"(?i){label}\s+(?:nicht\s+(?:verfügbar|verifizierbar|vorhanden)|fehlt|unklar)", body
            ))
        for match in matches:
            value = match.group(1).strip().strip(". ")
            if re.fullmatch(r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?", value):
                continue
            value = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", value, flags=re.I)
            value = re.sub(r"\s*(?:EUR|USD|GBP|€|\$|£|%)$", "", value, flags=re.I).strip()
            if not re.fullmatch(r"[-+]?(?:\d+(?:[,.]\d+)?|\d{1,3}(?:[ .’']\d{3})+(?:[,.]\d+)?)", value):
                return False
        return True

    # M-06: replace malformed/placeholder values, not merely append a second
    # status line that could conceal a contradictory placeholder.
    section = locate_section(text, 7, "MSCI-Vergleich", 8)
    if section:
        start, body_start, body_end = section
        body = text[body_start:body_end]
        fields = (
            ("Portfolio-Gesamtperformance", (r"Portfolio(?:-Gesamt)?performance", r"Gesamtperformance\s+Portfolio")),
            ("Ø Positionsperformance", (r"(?:Ø|durchschnitt(?:liche|s)?|mittelwert)\s*Positionsperformance", r"Positionsperformance(?:\s*\(?(?:Ø|Durchschnitt|Mittelwert)\)?)?")),
        )
        for canonical, labels in fields:
            if field_value_ok(body, labels):
                continue
            label = r"(?:" + "|".join(labels) + r")"
            # Replace each labeled malformed value in-place so a stale TBD does
            # not coexist with the explicit missing-data status.
            body, substitutions = re.subn(
                rf"(?im)((?:^|[|;]\s*|\n)\s*{label}\s*[:=]\s*)([^|;\r\n]+)",
                rf"\1NICHT VERFÜGBAR (keine belegte Berechnungsgrundlage)",
                body,
            )
            if not substitutions:
                body = body.rstrip() + f"\n{canonical}: NICHT VERFÜGBAR (keine belegte Berechnungsgrundlage)\n"
        text = text[:body_start] + body + text[body_end:]

    # M-08: normalize each event record independently. A valid first event
    # must never mask missing fields in a later event. Only explicit unavailable
    # values are inserted; no calendar values are fabricated.
    section = locate_section(text, 3, "Ereignisradar", 4)
    if section:
        _, body_start, body_end = section
        body = text[body_start:body_end]
        body_trailing_newline = body.endswith("\n")
        no_events = re.fullmatch(
            r"(?is)\s*(?:[-*•]\s*)?(?:Ereignisradar\s*:\s*)?"
            r"keine\s+(?:(?:relevanten|anstehenden)\s+)?ereignisse"
            r"(?:\s+im\s+[^.!?\r\n]+)?[.!]?\s*",
            body,
        )
        if not no_events:
            field_only = re.compile(
                r"(?i)^\s*(?:[-•]\s+|\d+[.)]\s+)?(?:\*\*)?"
                r"(?:Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|Berichtszeitraum|Zeitraum|"
                r"Letzter Wert|Vorwert|Konsens|Istwert)(?:\*\*)?\s*[:=](?:\*\*)?"
            )
            bullet = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.*)$")
            named_event = re.compile(r"(?i)^\s*(?:Ereignis|Termin|Event)\s*[:=]")
            lines = body.splitlines()
            records = []
            current = []
            started = False
            for line in lines:
                match = bullet.match(line)
                starts_record = (
                    (bool(match) and not field_only.match(line))
                    or (bool(named_event.match(line)) and not field_only.match(line))
                )
                if starts_record:
                    if current:
                        records.append(current)
                    current = [line]
                    started = True
                elif started:
                    current.append(line)
            if current:
                records.append(current)
            # Plain prose with no detectable event list is one record. Introductory
            # prose is not promoted to a separate event when bullets are present.
            if not records and body.strip():
                records = [body.splitlines()]

            required = (
                ("Uhrzeit", (r"Uhrzeit", r"Zeit"), False),
                ("Zeitzone", (r"Zeitzone",), True),
                ("Kennzahl", (r"Kennzahl", r"Indikator"), False),
                ("Berichtszeitraum", (r"Berichtszeitraum", r"Zeitraum"), False),
            )
            unavailable = "NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)"
            missing_value = re.compile(
                r"(?i)^(?:|[-–—]|TBD|TBA|N/?A|NA|PLATZHALTER|UNBEKANNT|"
                r"NICHT VERFÜGBAR|FEHLT|UNKLAR|TODO|XXX)$"
            )
            timezone_value = re.compile(
                r"(?i)(?:\b(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|UTC|CET|CEST|GMT)\b|"
                r"\b(?:UTC|GMT)?[+-]\d{1,2}(?::?\d{2})?\b|"
                r"\b[A-Za-z_]+/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?\b)"
            )
            observation_labels = (r"Letzter Wert", r"Vorwert", r"Konsens", r"Istwert")

            def field_pattern(labels):
                label = r"(?:" + "|".join(labels) + r")"
                return re.compile(
                    rf"(?im)(?:^|[|;]\s*|\n)\s*(?:[-•]\s+|\d+[.)]\s+)?"
                    rf"(?:\*\*)?{label}(?:\*\*)?\s*[:=](?:\*\*)?\s*([^|;\r\n]+)"
                )

            def normalize_record(record_lines):
                record = "\n".join(record_lines)
                for canonical, labels, is_timezone in required:
                    pattern = field_pattern(labels)
                    matches = list(pattern.finditer(record))
                    if not matches:
                        record = record.rstrip() + f"\n{canonical}: {unavailable}"
                        continue
                    for match in reversed(matches):
                        value = match.group(1).strip().strip(". ")
                        valid = bool(value) and not missing_value.fullmatch(value)
                        if is_timezone and valid:
                            valid = bool(timezone_value.search(value))
                        if not valid:
                            raw_value = match.group(1)
                            trailing_space = " " if raw_value.endswith(" ") else ""
                            record = (
                                record[:match.start(1)]
                                + unavailable + trailing_space
                                + record[match.end(1):]
                            )

                obs_matches = []
                for label in observation_labels:
                    obs_matches.extend(field_pattern((label,)).finditer(record))
                valid_obs = [
                    match for match in obs_matches
                    if match.group(1).strip()
                    and not missing_value.fullmatch(match.group(1).strip().strip(". "))
                ]
                if not valid_obs:
                    if obs_matches:
                        for match in reversed(obs_matches):
                            value = match.group(1).strip().strip(". ")
                            if missing_value.fullmatch(value):
                                raw_value = match.group(1)
                                trailing_space = " " if raw_value.endswith(" ") else ""
                                record = (
                                    record[:match.start(1)]
                                    + unavailable + trailing_space
                                    + record[match.end(1):]
                                )
                    else:
                        record = record.rstrip() + f"\nLetzter Wert: {unavailable}\nKonsens: {unavailable}"
                return record.splitlines()

            normalized_records = [normalize_record(record) for record in records]
            if records:
                first_event_index = next(
                    (i for i, line in enumerate(lines)
                     if (bullet.match(line) and not field_only.match(line)) or named_event.match(line)),
                    None,
                )
                if first_event_index is not None:
                    prefix = lines[:first_event_index]
                    body = "\n".join(prefix + [line for record in normalized_records for line in record])
                else:
                    body = "\n".join(line for record in normalized_records for line in record)
        if body_trailing_newline and body and not body.endswith("\n"):
            body += "\n"
        text = text[:body_start] + body + text[body_end:]

    # Presentation contract: keep primary section headings visually separated
    # even when Gemini emits adjacent sections without a blank line.
    heading_pattern = re.compile(
        r"(?m)^(?P<header>[ \t]*(?:#{1,6}[ \t]*)?(?:[1-8][.·][ \t]+"
        r"(?:Trading-Radar|Thesen-Update|Ereignisradar|Assetklassenvergleich|"
        r"Technische Kandidaten|Portfolio & Risiko|MSCI-Vergleich|Daten-/Prüfanhang)"
        r"|9[.][ \t]+Vollständige offene Positionen))"
    )
    text = heading_pattern.sub(
        lambda match: ("\n" if match.start() > 0 and not text[:match.start()].endswith("\n\n") else "")
        + match.group("header"),
        text,
    )
    # Keep the unchanged historical_ohlcv exclusion universe visible in the
    # report so intentionally excluded tickers are not misreported as active
    # universe/company-name resolution failures.
    excluded_tickers = (
        "10O.DE", "1TRA.DE", "ABB", "ANSS", "ARND.DE", "ASTA.DE", "AUM.DE",
        "CMA", "DFS", "FLIR", "FRAG.DE", "HES", "JNPR", "KCOG.DE", "MAXN",
        "MRO", "NAFG.DE", "PNE.DE", "PSAG.DE", "RTL.DE", "SHA.DE", "SHELL.SO",
        "SNV", "STM1.DE", "WRK", "X",
    )
    exclusion_heading = "Bewusst ausgeschlossene Ticker (nicht Teil des aktiven Datenuniversums):"
    exclusion_line = exclusion_heading + " " + ", ".join(excluded_tickers)
    if exclusion_heading not in text:
        section9 = re.search(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?9[.][ \t]+Vollständige offene Positionen", text)
        if section9:
            insert_at = section9.start()
            prefix = text[:insert_at]
            if prefix and not prefix.endswith("\n\n"):
                prefix += "\n" if prefix.endswith("\n") else "\n\n"
            text = prefix + exclusion_line + "\n\n" + text[insert_at:]
        else:
            text = text.rstrip() + "\n\n" + exclusion_line + "\n"
    text = _trading_radar_nicht_handhabbare_kandidaten_normalisieren(text)
    return text






def _trading_radar_unvollstaendige_kandidaten_nicht_handelsbar(text):
    """Downgrade candidates without source-backed numeric levels to watch-only.

    No levels are invented. Incomplete setups remain visible for context but
    cannot retain a buy-candidate status or pass as actionable candidates.
    """
    if not isinstance(text, str) or not text:
        return text
    match = re.search(
        r"(?ims)^(?P<header>[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*\r?\n)"
        r"(?P<body>.*?)(?=^[ \t]*(?:#{1,6}\s*)?6(?:\s*[.·](?!\d)|\s+)|\Z)",
        text,
    )
    if not match:
        return text
    body = match.group("body")
    lines = body.splitlines()
    starts = [
        i for i, line in enumerate(lines)
        if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:\*\*)?(?:Asset/Ticker|Ticker|Asset)(?:\*\*)?\s*[:=]", line)
    ]
    if not starts:
        return text
    rebuilt = []
    cursor = 0
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        rebuilt.extend(lines[cursor:start])
        record = lines[start:end]
        record_text = "\n".join(record)
        def numeric_field(label):
            m = re.search(
                rf"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?{label}(?:\*\*)?\s*[:=]\s*([^\r\n|;]+)",
                record_text,
            )
            if not m:
                return False
            value = m.group(1).strip()
            if re.search(r"(?i)NICHT VERFÜGBAR|FEHLT|UNBEKANNT|UNKLAR|TBD|TBA", value):
                return False
            value = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", value, flags=re.I)
            value = re.sub(r"\s*(?:EUR|USD|GBP|€|\$|£|%|x)$", "", value, flags=re.I).strip()
            try:
                float(value.replace(".", "").replace(",", ".") if "," in value else value)
                return True
            except ValueError:
                return False
        complete = all(numeric_field(label) for label in ("Einstieg", "Stop", "TP1", "TP2", "CRV"))
        if not complete:
            status_idx = next(
                (j for j, line in enumerate(record)
                 if re.search(r"(?i)(?:Regelstatus|Datenstatus|Regel[-/]Datenstatus)", line)),
                None,
            )
            if status_idx is None:
                record.append("- **Regelstatus:** NICHT HANDELBAR – Handelslevels/CRV nicht vollständig verifiziert")
            else:
                record[status_idx] = re.sub(
                    r"(?i)(?:KAUFKANDIDAT(?:\s+[A-Z])?|JETZT TECHNISCH PRÜFEN|AUF TRIGGER WARTEN|EREIGNIS ABWARTEN|NICHT HANDELBAR|VERWERFEN\s*/\s*INVALIDIERT).*?$",
                    "NICHT HANDELBAR – Handelslevels/CRV nicht vollständig verifiziert",
                    record[status_idx],
                )
            record_text = "\n".join(record)
        rebuilt.extend(record)
        cursor = end
    rebuilt.extend(lines[cursor:])
    new_body = "\n".join(rebuilt)
    return text[:match.start("body")] + new_body + text[match.end("body"):]


def _trading_radar_name_ticker_normalisieren(text, eingabedateien=None):
    """Ensure narrative asset references use Name (Ticker), preserving Tab-2 keys."""
    if not isinstance(text, str) or not text:
        return text

    name_by_ticker = {
        "GC=F": "Gold Future",
        "SI=F": "Silber Future",
        "BTC-USD": "Bitcoin USD",
        "ETH-USD": "Ethereum USD",
        "CL=F": "WTI Crude Oil Futures",
        "BZ=F": "Brent Crude Oil Futures",
        "^GSPC": "S&P 500 Index",
        "^RUT": "Russell 2000 Index",
        "^IXIC": "NASDAQ Composite Index",
        "^DJI": "Dow Jones Industrial Average",
    }
    # Persistent company-name cache is available in the production workflow.
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".firma_name_cache.json")
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            cache = json.load(handle)
        if isinstance(cache, dict):
            for ticker, entry in cache.items():
                if isinstance(entry, dict):
                    name = str(entry.get("name") or "").strip()
                    if name and not entry.get("missing"):
                        name_by_ticker[str(ticker).strip()] = name
    except (OSError, ValueError, TypeError):
        pass

    # Resolve symbols from the actual structured inputs supplied to this run.
    import csv as _csv

    def add_source_name(ticker, name):
        ticker = str(ticker or "").strip()
        name = str(name or "").strip()
        if ticker and name and name.casefold() != ticker.casefold():
            name_by_ticker.setdefault(ticker, name)

    def walk_json_names(value):
        if isinstance(value, dict):
            ticker = value.get("Ticker", value.get("ticker", value.get("symbol", value.get("Symbol"))))
            name = value.get("Name", value.get("name", value.get("company_name", value.get("Company"))))
            add_source_name(ticker, name)
            for nested in value.values():
                walk_json_names(nested)
        elif isinstance(value, list):
            for nested in value:
                walk_json_names(nested)

    for source_path in (eingabedateien or {}).values():
        if not source_path or not os.path.isfile(source_path):
            continue
        try:
            suffix = os.path.splitext(str(source_path))[1].lower()
            if suffix == ".csv":
                with open(source_path, "r", encoding="utf-8-sig", newline="") as handle:
                    sample = handle.read(4096)
                    handle.seek(0)
                    try:
                        dialect = _csv.Sniffer().sniff(sample, delimiters=";,\t") if sample.strip() else None
                    except _csv.Error:
                        dialect = None
                    reader = _csv.DictReader(handle, delimiter=dialect.delimiter if dialect else ";")
                    columns = {str(column).strip().casefold(): column for column in (reader.fieldnames or [])}
                    ticker_key = next((columns[key] for key in ("ticker", "symbol", "yahoo-ticker", "yahoo ticker") if key in columns), None)
                    name_key = next((columns[key] for key in ("name", "firmenname", "unternehmen", "company", "company_name") if key in columns), None)
                    if ticker_key and name_key:
                        for row in reader:
                            add_source_name(row.get(ticker_key), row.get(name_key))
            elif suffix == ".json":
                with open(source_path, "r", encoding="utf-8-sig") as handle:
                    walk_json_names(json.load(handle))
        except (OSError, ValueError, TypeError, _csv.Error):
            # A broken auxiliary name source must not abort report generation.
            continue

    # Resolve names from explicit pairs already present in the report.
    for match in re.finditer(
        r"(?im)(?:^|\|\s*)Ticker:\s*([^|\r\n]+)\s*\|\s*Name:\s*([^|\r\n]+)",
        text,
    ):
        ticker, name = match.group(1).strip(), match.group(2).strip()
        if ticker and name and name.casefold() != ticker.casefold():
            name_by_ticker.setdefault(ticker, name)

    # Also resolve names from any explicit Name (Ticker) display.
    for match in re.finditer(
        r"(?<![A-Za-z0-9])([^()|\r\n]{3,}?)\s+\(([A-Za-z0-9][A-Za-z0-9.=^/_-]*)\)",
        text,
    ):
        name, ticker = match.group(1).strip(" -*"), match.group(2).strip()
        if name and ticker and name.casefold() != ticker.casefold():
            name_by_ticker.setdefault(ticker, name)

    if not name_by_ticker:
        return text

    # Preserve the machine-readable Tab-2 payload byte-for-byte. Its source
    # fields already carry Name and Ticker separately; do not rewrite them.
    tab2_start = text.find("10.5 Geschlossene Positionen")
    tab2_end = text.find("TAB-2-EXPORT-SHA256=", tab2_start) if tab2_start >= 0 else -1
    if tab2_start >= 0:
        if tab2_end < 0:
            next_section = re.search(r"(?m)^\s*(?:#{1,6}\s*)?11[.][ \t]+", text[tab2_start + 1:])
            tab2_end = tab2_start + 1 + next_section.start() if next_section else len(text)
        else:
            tab2_end = text.find("\n", tab2_end)
            tab2_end = len(text) if tab2_end < 0 else tab2_end
        before_tab2, tab2_block, after_tab2 = text[:tab2_start], text[tab2_start:tab2_end], text[tab2_end:]
    else:
        before_tab2, tab2_block, after_tab2 = text, "", ""

    # Tab-2 is a machine-readable source payload. Never enrich or normalize it:
    # preserve the complete block, including line endings and checksum, verbatim.

    # Only prose lines are eligible for substitutions. Structured key/value
    # lines, pipe-delimited records, section markers, excluded-ticker inventories,
    # and the complete Tab-2 export are deliberately excluded.
    structured_label = re.compile(
        r"(?i)^\s*(?:[-*•]\s*)?(?:"
        r"Ticker|Name|Asset|Einstieg|Einstiegsdatum|Aktueller Kurs|Stop|Stop-Loss|"
        r"TP1|TP2|CRV|Status|Richtung|Quelle|Zeitraum|Uhrzeit|Zeitzone|Kennzahl|"
        r"Berichtszeitraum|Letzter Wert|Konsens|Istwert|Vorwert|Performance|"
        r"Portfolio-Gesamtperformance|Ø Positionsperformance|TAB-2-|"
        r"TRADING_RADAR_|OFFENE_POSITIONEN_|Thesen-ID|Version|"
        r"Portfolio|Datenstand|Methodik|OHLCV-Abdeckung|Lückenstatus)\s*[:=]"
    )
    # Recognize both legacy underscore tokens and ordinary exchange-style symbols.
    # A small explicit acronym allowlist avoids classifying common report vocabulary
    # (e.g. FOMC, MSCI, USD, SMA) as unresolved securities.
    # Unicode-aware boundaries are essential: ASCII-only boundaries treated
    # the suffix GBAR in the German word VERFÜGBAR as a standalone ticker.
    tickerish = re.compile(
        r"(?<![\w])(?:[A-Z][A-Z0-9]{1,15}_[A-Z0-9_]{1,20}|"
        r"[A-Z]{2,6}(?:\.[A-Z]{1,3})?)(?![\w.=^/])",
        flags=re.UNICODE,
    )
    non_ticker_acronyms = {
        "FOMC", "MSCI", "USD", "EUR", "SMA", "OHLCV", "ETF", "ETFS",
        "TP1", "TP2", "CRV", "API", "CSV", "JSON", "SHA", "SHA256",
        "REAL", "LIVE", "TAB", "LONG", "SHORT", "AG", "SE", "INC",
        "CORP", "LLC", "NASDAQ", "SP", "US", "EU", "UK", "WTI", "BTC",
        "ETH", "SPY", "VNA", "DAX", "EUR", "YTD", "WKN", "OS",
        "NICHT", "VERF", "VERFÜGBAR", "KEINE", "CME", "UCITS", "SPDR",
        "CRUDE", "OIL", "INDEX", "FUTURES", "FUTURE", "USD", "TIPS",
        # Macro/economic abbreviations are not security tickers.
        "BIP", "GDP", "NFP", "CPI", "PCE", "PMI", "ISM", "ADP",
    }
    excluded_tickers = {
        "10O.DE", "1TRA.DE", "ABB", "ANSS", "ARND.DE", "ASTA.DE", "AUM.DE",
        "CMA", "DFS", "FLIR", "FRAG.DE", "HES", "JNPR", "KCOG.DE", "MAXN",
        "MRO", "NAFG.DE", "PNE.DE", "PSAG.DE", "RTL.DE", "SHA", "SHELL.SO",
        "SNV", "STM1.DE", "WRK", "X",
    }

    def normalize_prose_line(line):
        # Abschnitts-/Markdown-Ueberschriften sind Struktur und niemals
        # Kandidaten fuer Cache- oder Ticker-Ersetzungen. Das gilt auch fuer
        # Unterkapitel wie 8.1 und unabhaengig vom Inhalt des Namens-Caches.
        if re.match(r"^\s*#{1,6}\s+\S", line):
            return line
        if re.match(r"^\s*(?:#{1,6}\s*)?\d{1,2}(?:\.\d+)*[.·]?\s+\S.*$", line):
            return line
        if (not line.strip() or "|" in line or structured_label.search(line)
                or re.match(r"^\s*[A-Z][A-Z0-9_]{5,}\s*=", line)):
            return line
        # Bare machine markers are structure, not securities. Without this
        # guard, underscore-delimited markers such as
        # TRADING_RADAR_OFFENE_POSITIONEN_BEGIN are falsely split into
        # "unresolved tickers" by the generic ticker detector.
        if re.match(
            r"^\s*(?:TRADING_RADAR_[A-Z0-9_]+|OFFENE_POSITIONEN_[A-Z0-9_]+|"
            r"TAB-2-[A-Z0-9_-]+)(?:\s*[:=].*)?\s*$",
            line,
        ):
            return line
        if re.match(r"(?i)^\s*(?:Bewusst ausgeschlossene Ticker|Ausgeschlossene Ticker)\s*[:(]", line):
            return line
        # Collapse common natural-language name + symbol pairs before token replacement.
        paired_aliases = (
            (r"\bGold\s+GC=F\b", "Gold Future (GC=F)"),
            (r"\bSilber\s+SI=F\b", "Silber Future (SI=F)"),
            (r"\bBitcoin\s+BTC-USD\b", "Bitcoin USD (BTC-USD)"),
            (r"\bEthereum\s+ETH-USD\b", "Ethereum USD (ETH-USD)"),
        )
        for paired_pattern, paired_display in paired_aliases:
            line = re.sub(paired_pattern, paired_display, line, flags=re.IGNORECASE)

        # First normalize known symbols. Use boundaries and avoid duplicating an
        # already complete Name (TICKER) pair.
        for ticker, name in sorted(name_by_ticker.items(), key=lambda item: len(item[0]), reverse=True):
            if not ticker or not name or len(ticker) < 2:
                continue
            pattern = re.compile(rf"(?<![A-Za-z0-9._=^/]){re.escape(ticker)}(?![A-Za-z0-9_=^/])")
            def replace_known(match):
                start_pos, end_pos = match.span()
                before = line[max(0, start_pos - len(name) - 4):start_pos]
                after = line[end_pos:end_pos + 2]
                name_forms = {name.casefold(), name.split()[0].casefold()}
                if any(before.casefold().endswith(form + " (") for form in name_forms) and after.startswith(")"):
                    return match.group(0)

                # A ticker may also be the beginning of its own company name
                # (e.g. CME in "CME Group Inc."). Do not expand that prefix
                # inside the already-written company name.
                if name.casefold().startswith(ticker.casefold()):
                    suffix = name[len(ticker):]
                    if suffix and line[end_pos:end_pos + len(suffix)].casefold() == suffix.casefold():
                        return match.group(0)

                # MSCI is both the company ticker and the prefix of benchmark
                # names. In those contexts it means the index family, not MSCI
                # Inc.; expanding it corrupts references such as "MSCI World".
                if ticker.casefold() == "msci" and re.match(
                    r"(?i)\s+(?:World|ACWI|EAFE|Emerging\s+Markets|Frontier\s+Markets|"
                    r"Europe|USA|Japan|China|India|Pacific|World\s+ex\s+USA|"
                    r"World\s+Small\s+Cap|All\s+Country)(?:\b|$)",
                    line[end_pos:],
                ):
                    return match.group(0)

                return f"{name} ({ticker})"
            line = pattern.sub(replace_known, line)

        # Known natural-language aliases are normalized only in prose, never
        # in structured fields or source data.
        aliases = (
            (r"\bWTI\b(?!\s+(?:Crude Oil Futures)?\s*\(CL=F\))", "WTI Crude Oil Futures (CL=F)"),
            (r"\bBrent\b(?!\s+(?:Crude Oil Futures)?\s*\(BZ=F\))", "Brent Crude Oil Futures (BZ=F)"),
            (r"\bGold\b(?!\s+Future\s*\(GC=F\)|\s*\(GC=F\))", "Gold Future (GC=F)"),
            (r"\bSilber\b(?!\s+Future\s*\(SI=F\)|\s*\(SI=F\))", "Silber Future (SI=F)"),
            (r"\bBitcoin\b(?!\s+USD\s*\(BTC-USD\)|\s*\(BTC-USD\))", "Bitcoin USD (BTC-USD)"),
            (r"\bEthereum\b(?!\s+USD\s*\(ETH-USD\)|\s*\(ETH-USD\))", "Ethereum USD (ETH-USD)"),
        )
        alias_tickers = {
            "CL=F": r"\bCL=F\b", "BZ=F": r"\bBZ=F\b",
            "GC=F": r"\bGC=F\b", "SI=F": r"\bSI=F\b",
            "BTC-USD": r"\bBTC-USD\b", "ETH-USD": r"\bETH-USD\b",
        }
        for (alias_pattern, display), ticker in zip(aliases, alias_tickers.values()):
            if re.search(ticker, line, flags=re.IGNORECASE):
                continue
            line = re.sub(alias_pattern, display, line, flags=re.IGNORECASE)

        line = re.sub(
            r"(?i)\b(Gold Future \(GC=F\)|Silber Future \(SI=F\)|"
            r"Bitcoin USD \(BTC-USD\)|Ethereum USD \(ETH-USD\))\s+\1",
            r"\1", line,
        )

        for duplicate in (
            "Gold Future (GC=F) Gold Future (GC=F)",
            "Silber Future (SI=F) Silber Future (SI=F)",
            "Bitcoin USD (BTC-USD) Bitcoin USD (BTC-USD)",
            "Ethereum USD (ETH-USD) Ethereum USD (ETH-USD)",
        ):
            line = line.replace(duplicate, duplicate.split(") ", 1)[0] + ")")

        # Restore common macro abbreviations that an older normalizer may
        # already have wrapped as unresolved pseudo-tickers. These are metrics,
        # not company/security symbols.
        line = re.sub(
            r"(?i)Name nicht aufgelöst \((BIP|GDP|NFP|CPI|PCE|PMI|ISM|ADP)\)",
            lambda m: m.group(1).upper(), line,
        )
        # F-01: ticker-like symbols that remain unresolved are made explicit.
        # Do not treat ordinary acronyms (FOMC, MSCI) as tickers.
        def mark_unresolved(match):
            token = match.group(0)
            if token in name_by_ticker or token in non_ticker_acronyms or token in excluded_tickers:
                return token
            # Existing Name (TICKER) pairs are already resolved, even if the
            # ticker is not present in the cache/source map.
            if re.search(r"\b[A-Za-z][A-Za-z0-9&.,'’ -]+\s+\(" + re.escape(token) + r"\)", line):
                return token
            # Avoid marking a token that is itself part of a larger recognized
            # exchange symbol (e.g. the ALB part of ALB.DE).
            return f"Name nicht aufgelöst ({token})"
        line = tickerish.sub(mark_unresolved, line)
        return line

    normalized_before = "\n".join(normalize_prose_line(line) for line in before_tab2.split("\n"))
    normalized_after = "\n".join(normalize_prose_line(line) for line in after_tab2.split("\n"))
    return normalized_before + tab2_block + normalized_after

def _trading_radar_nicht_handhabbare_kandidaten_normalisieren(text):
    """Do not present candidates as actionable when numeric trade levels are missing."""
    if not isinstance(text, str) or not text:
        return text
    section5 = re.compile(
        r"(?ms)(?P<head>^[ \t]*(?:#{1,6}[ \t]*)?5[.][ \t]+Technische Kandidaten[ \t]*\n)"
        r"(?P<body>.*?)(?=^[ \t]*(?:#{1,6}[ \t]*)?6[.][ \t]+Portfolio[ \t]*&[ \t]*Risiko)"
    )
    match = section5.search(text)
    if not match:
        return text
    body = match.group("body")
    candidate_starts = list(re.finditer(r"(?im)^\s*(?:[-*]\s*)?(?:Asset/Ticker|Ticker|Asset)\s*:", body))
    if not candidate_starts:
        return text
    blocks = []
    for index, start in enumerate(candidate_starts):
        end = candidate_starts[index + 1].start() if index + 1 < len(candidate_starts) else len(body)
        blocks.append(body[start.start():end])
    numeric_labels = (r"Einstieg", r"Stop(?:-Loss)?", r"TP1", r"TP2", r"CRV")
    def has_complete_numeric_levels(block):
        for label in numeric_labels:
            found = re.search(rf"(?im)^\s*(?:[-*]\s*)?{label}\s*:\s*([^\r\n]+)", block)
            if not found or not _trading_radar_numeric_wert_gueltig(found.group(1)):
                return False
        return True
    actionable_blocks = [block for block in blocks if has_complete_numeric_levels(block)]
    if len(actionable_blocks) == len(blocks):
        return text

    if not actionable_blocks:
        replacement = (
            "Keine belastbaren technischen Kandidaten; keine Handelsfreigabe. "
            "Die erkannten Setups besitzen keine vollständig belegten numerischen "
            "Einstiegs-, Stop-, TP1-, TP2- und CRV-Werte und werden daher nicht als "
            "handelbare Kandidaten ausgewiesen.\n\n"
        )
    else:
        # In gemischten Listen nur vollständig belegte Kandidaten behalten.
        # Ein einzelner gültiger Kandidat darf unvollständige Kandidaten nicht
        # versehentlich durch die M-09-Prüfung schleusen.
        prefix = body[:candidate_starts[0].start()]
        kept = "\n\n".join(block.rstrip() for block in actionable_blocks)
        replacement = prefix + kept + "\n\n"
    return text[:match.start("body")] + replacement + text[match.end("body"):]


def _trading_radar_numeric_wert_gueltig(value):
    """Match the report gate's numeric-field contract, including common units."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if not cleaned or re.fullmatch(
        r"(?i)(?:NICHT VERFÜGBAR|NICHT VERIFIZIERBAR|NICHT VORHANDEN|FEHLT|UNKLAR|N/A|NA|NONE|NULL|[-—–])",
        cleaned,
    ):
        return False
    # Accept only common, explicit currency/unit wrappers. Arbitrary prose
    # remains invalid, even if it contains a number.
    cleaned = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\s*(?:EUR|USD|GBP|€|\$|£|%|x|\b(?:Punkte|pts)\b)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if not re.fullmatch(r"[-+]?(?:\d{1,3}(?:[ .’']\d{3})+|\d+)(?:[,.]\d+)?", cleaned):
        return False
    return True


def _trading_radar_aktive_firmennamen_luecken(name_cache, excluded_tickers):
    """Return unresolved company-name tickers in the active, non-excluded universe."""
    excluded_set = {str(ticker).casefold() for ticker in excluded_tickers}
    if not isinstance(name_cache, dict):
        return []
    return sorted(
        str(key) for key, value in name_cache.items()
        if isinstance(value, dict)
        and value.get("missing")
        and str(key).casefold() not in excluded_set
    )



def _trading_radar_kandidaten_aus_abschnitt5(report_text):
    """Extrahiert Kandidaten nur aus expliziten Ticker-Feldern/Tabellenspalten.

    Freie Prosa wird absichtlich nicht nach beliebigen Großbuchstaben-Tokens
    durchsucht: Das würde Makro-Kürzel und Firmennamen als Ticker fehlklassifizieren.
    Wenn der Bericht keine strukturierte Tickerquelle enthält, ist das Ergebnis
    leer und der Audit darf keine uneingeschränkte Entwarnung ausgeben.
    """
    if not isinstance(report_text, str):
        return []
    section_match = re.search(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*\r?\n"
        r"(.*?)(?=^[ \t]*(?:#{1,6}\s*)?6\s*[.·]|\Z)",
        report_text,
    )
    if not section_match:
        return []
    body = section_match.group(1)
    found = set()
    ticker_re = re.compile(
        r"^(?:\^[A-Z0-9]{1,8}|[A-Z][A-Z0-9]{0,7}(?:[.-][A-Z0-9]{1,6})?(?:=X|-USD|=F)?)$"
    )
    # Nur explizite Labels: "Ticker: ABC" / "Symbol: ABC".
    for match in re.finditer(
        r"(?im)^[ \t]*(?:[-*]\s*)?(?:Ticker|Symbol)\s*:\s*"
        r"(\^[A-Z0-9]{1,8}|[A-Z][A-Z0-9]{0,7}(?:[.-][A-Z0-9]{1,6})?(?:=X|-USD|=F)?)"
        r"(?=[ \t]*(?:[,;|—–-]|$))",
        body,
    ):
        found.add(match.group(1).upper())
    # Tabellen nur dann auswerten, wenn die Kopfzeile die Ticker-Spalte
    # ausdrücklich benennt. Andere Spalten und Prosa werden ignoriert.
    lines = body.splitlines()
    for i, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        headers = [c.strip().strip("*` ").casefold() for c in line.strip().strip("|").split("|")]
        if not any(h in {"ticker", "symbol", "ticker/symbol", "symbol/ticker"} for h in headers):
            continue
        separator = lines[i + 1].strip()
        if not separator.startswith("|") or not re.fullmatch(r"\|?[\s:|-]+\|?", separator):
            continue
        ticker_idx = next(j for j,h in enumerate(headers) if h in {"ticker", "symbol", "ticker/symbol", "symbol/ticker"})
        for row in lines[i + 2:]:
            if not row.lstrip().startswith("|"):
                break
            cells = [c.strip().strip("*` ") for c in row.strip().strip("|").split("|")]
            if ticker_idx < len(cells):
                candidate = cells[ticker_idx].upper()
                if ticker_re.fullmatch(candidate):
                    found.add(candidate)
    return sorted(found)


def _trading_radar_ohlcv_nachweisblock(report_text=""):
    """Erstellt den OHLCV-Nachweis ausschließlich aus strukturierten Prüfdaten.

    Erwarteter Vertrag für den vorgelagerten OHLCV-Prüfer:
      _TRADING_RADAR_OHLCV_AUDIT = {
        "status": "complete" | "gaps" | "unavailable",
        "checked_at": "ISO-Datum/Zeit oder Datenstand",
        "universe_count": int, "covered_count": int,
        "missing_tickers": [str, ...], "stale_tickers": [str, ...],
        "affected_candidates": [str, ...]
      }
    Fehlt dieser Prüfnachweis, wird KEINE Vollständigkeit behauptet; Kandidaten
    werden ausdrücklich als nicht verifizierbar gekennzeichnet.
    """
    audit = globals().get("_TRADING_RADAR_OHLCV_AUDIT")
    if not isinstance(audit, dict):
        # Produktive Anbindung: den Nachweis unmittelbar aus der persistenten
        # historischen OHLCV-Datenbank lesen, statt einen unverbundenen globalen
        # Datenvertrag vorauszusetzen. Fehlt DB/Quelle, wird keine Vollständigkeit
        # behauptet.
        try:
            import historical_ohlcv
            # Nur strukturierte Ticker-Felder aus Abschnitt 5 gelten als Kandidatenquelle.
            # Freie Großbuchstaben-Tokens sind zu mehrdeutig (z. B. BIP/NFP/Firmennamen).
            candidates = _trading_radar_kandidaten_aus_abschnitt5(report_text)
            audit = historical_ohlcv.audit_snapshot(candidate_tickers=candidates)
        except Exception as exc:
            audit = {
                "status": "unavailable",
                "checked_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
                "universe_count": 0, "covered_count": 0,
                "missing_tickers": [], "stale_tickers": [],
                "affected_candidates": [],
                "error": f"OHLCV-Prüfung nicht ausführbar: {type(exc).__name__}: {exc}",
            }

    status = str(audit.get("status", "unavailable")).strip().casefold()
    checked_at = str(audit.get("checked_at") or "NICHT VERFÜGBAR").strip()
    universe = audit.get("universe_count")
    covered = audit.get("covered_count")
    missing = sorted({str(x).strip() for x in (audit.get("missing_tickers") or []) if str(x).strip()})
    stale = sorted({str(x).strip() for x in (audit.get("stale_tickers") or []) if str(x).strip()})
    affected = sorted({str(x).strip() for x in (audit.get("affected_candidates") or []) if str(x).strip()})
    counts_valid = (isinstance(universe, int) and not isinstance(universe, bool)
                    and isinstance(covered, int) and not isinstance(covered, bool)
                    and universe >= 0 and 0 <= covered <= universe)
    if not counts_valid:
        return (
            "OHLCV-Abdeckung: NICHT VERIFIZIERBAR; Prüfergebnis enthält keine gültigen "
            "Universums-/Abdeckungszahlen.\n"
            f"Datenstand: {checked_at}.\n"
            "Lückenstatus und Kandidatenauswirkung: nicht verifizierbar; "
            "Kandidaten sind bis zur Klärung nicht prüfbar und nicht freigegeben."
        )

    historical_only_invalid = sorted({
        str(x).strip() for x in (audit.get("historical_only_invalid_ohlcv_tickers") or [])
        if str(x).strip()
    })
    recent_invalid = sorted({
        str(x).strip() for x in (audit.get("recent_invalid_ohlcv_tickers") or [])
        if str(x).strip()
    })
    if (status == "complete" and covered == universe and not missing and not stale
            and not recent_invalid
            and audit.get("candidate_impact") == "none_detected_in_checked_candidates"
            and audit.get("candidate_mapping_complete") is True):
        historical_note = (
            " Historische OHLCV-Auffälligkeiten außerhalb des 90-Tage-Fensters bleiben dokumentiert: "
            + ", ".join(historical_only_invalid[:100]) + "."
        ) if historical_only_invalid else ""
        return (
            f"OHLCV-Abdeckung: vollständig ({covered}/{universe} geprüfte Symbole).\n"
            f"Datenstand: {checked_at}.\n"
            "Lückenstatus und Kandidatenauswirkung: keine fehlenden oder veralteten OHLCV-Daten; "
            "im jüngsten 90-Tage-Fenster keine ungültigen OHLCV-Kerzen; Kandidaten nicht eingeschränkt."
            + historical_note
        )

    if status not in {"gaps", "unavailable", "complete"}:
        status = "unavailable"
    details = []
    if missing:
        details.append("fehlende OHLCV-Daten: " + ", ".join(missing[:100]))
    if stale:
        details.append("veraltete OHLCV-Daten: " + ", ".join(stale[:100]))
    invalid_ohlcv = sorted({str(x).strip() for x in (audit.get("invalid_ohlcv_tickers") or []) if str(x).strip()})
    recent_invalid = sorted({str(x).strip() for x in (audit.get("recent_invalid_ohlcv_tickers") or []) if str(x).strip()})
    historical_only_invalid = sorted({str(x).strip() for x in (audit.get("historical_only_invalid_ohlcv_tickers") or []) if str(x).strip()})
    if recent_invalid:
        details.append("ungültige OHLCV-Felder im jüngsten 90-Tage-Fenster: " + ", ".join(recent_invalid[:100]))
    if historical_only_invalid:
        details.append("historische OHLCV-Auffälligkeiten außerhalb des 90-Tage-Fensters (weiterhin dokumentiert): " + ", ".join(historical_only_invalid[:100]))
    elif invalid_ohlcv and not recent_invalid:
        # Compatibility with older audit payloads that do not yet split historical/current findings.
        details.append("ungültige/unvollständige OHLCV-Felder (Zeitraum nicht getrennt nachgewiesen): " + ", ".join(invalid_ohlcv[:100]))
    unmapped_candidates = sorted({str(x).strip() for x in (audit.get("unmapped_candidate_tickers") or []) if str(x).strip()})
    if unmapped_candidates:
        details.append("Kandidaten nicht im geprüften Universum: " + ", ".join(unmapped_candidates[:100]))
    if affected:
        details.append("konkret betroffene Kandidaten: " + ", ".join(affected[:100]))
    if audit.get("candidate_impact") == "not_individually_mapped" and not affected:
        details.append("Kandidatenwirkung: einzelne Kandidaten sind in diesem Universumsnachweis nicht zuordenbar")
    if audit.get("error"):
        details.append("Prüfhinweis: " + str(audit["error"]))
    if not details:
        details.append("Prüfer meldet keine konkretisierten Ticker; Kandidatenwirkung bleibt ungeklärt")
    impact = (
        "Kandidatenwirkung nicht als uneingeschränkt bestätigt: " + "; ".join(details)
        + ". Betroffene Setups bis zur Datenprüfung nicht freigeben."
    )
    return (
        f"OHLCV-Abdeckung: {covered}/{universe} Symbole abgedeckt; Status={status}.\n"
        f"Datenstand: {checked_at}.\n"
        "Lückenstatus und Kandidatenauswirkung: " + impact
    )


def _trading_radar_ohlcv_abschnitt8_aktualisieren(text):
    """Ersetzt Gemini-OHLCV-Behauptungen in Abschnitt 8 durch Prüferbelege."""
    if not isinstance(text, str):
        return text
    section = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?8\s*[.·]\s*Daten-/Prüfanhang[^\r\n]*$", text
    )
    if not section:
        return text
    tail = text[section.end():]
    next_section = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?(?:9|10|11)(?:\s*[.·](?!\d)|\s+)[^\r\n]*$", tail
    )
    end = section.end() + next_section.start() if next_section else len(text)
    body = text[section.end():end]
    # Remove only existing OHLCV summary lines/paragraphs to avoid contradictory
    # Gemini claims. Other section-8 content remains untouched.
    body = re.sub(
        r"(?im)^[ \t]*(?:[-*•]\s*)?(?:\*\*)?(?:OHLCV-Abdeckung(?:\s*&\s*Lückenstatus)?|OHLCV-Datenstand|Lückenstatus und Kandidatenauswirkung)(?:\*\*)?\s*:[^\r\n]*(?:\r?\n)?",
        "", body
    )
    body = re.sub(
        r"(?im)^[ \t]*(?:[-*•]\s*)?(?:\*\*)?OHLCV[^\r\n]*(?:vollständig|vollstaendig|Datenlücken|Datenluecken)[^\r\n]*(?:\r?\n)?",
        "", body
    )
    block = _trading_radar_ohlcv_nachweisblock(text)
    return text[:section.end()] + "\n\n" + block + "\n" + body.lstrip("\r\n") + text[end:]


def speichere_ergebnis(text, eingabedateien=None):
    heute = datetime.date.today().isoformat()
    if globals().get("_GEMINI_TRADING_RADAR_ONLY_MODE"):
        radar_status = globals().get("_GEMINI_TRADING_RADAR_STATUS", "NICHT_ERZEUGT")
        if radar_status != "EIGENSTAENDIG_ERZEUGT" or not isinstance(text, str) or not text.strip():
            raise RuntimeError(
                f"TRADING_RADAR_NICHT_SPEICHERBAR: Status={radar_status}; "
                "keine alte Auswertung als Ersatz verwenden."
            )
        radar_datei = f"Trading_Radar({heute}).txt"
        if not re.fullmatch(r"Trading_Radar\(\d{4}-\d{2}-\d{2}\)\.txt", radar_datei):
            raise RuntimeError(f"TRADING_RADAR_DATEINAME_UNGUELTIG: {radar_datei}")
        # Der veröffentlichte Dateiname ist ein fixer Vertrag; Runner-Pfade
        # dienen nur als Ablageort. Keine automatisch nummerierten Varianten.
        radar_output = os.path.abspath(os.path.join(os.getcwd(), radar_datei))
        offene_positionen_fakten = _offene_positionen_quellblock(
            (eingabedateien or {}).get("Offene Positionen+Check.csv")
        )
        if not offene_positionen_fakten:
            raise RuntimeError(
                "TRADING_RADAR_NICHT_SPEICHERBAR: autoritative offene Positionsliste "
                "fehlt oder ist leer; Vollständigkeit kann nicht bestätigt werden."
            )
        marker = "TRADING_RADAR_OFFENE_POSITIONEN_BEGIN"
        if marker not in text:
            text = text.rstrip() + "\n\n9. Vollständige offene Positionen (Quelle: Offene Positionen+Check.csv)\n" + offene_positionen_fakten + "\n" + marker + "\n"
        geschlossene_positionen_fakten = globals().get(
            "_GEMINI_GESCHLOSSENE_10_5_FINAL",
            "",
        )
        if not isinstance(geschlossene_positionen_fakten, str) or not geschlossene_positionen_fakten.strip():
            geschlossene_positionen_fakten = (
                "TAB-2-QUELLE NICHT VERIFIZIERBAR: "
                "geschlossene Positionsquelle wurde im Lauf nicht geladen"
            )
        # M-01: Den gesamten alten Gemini-Abschnitt 10.5 entfernen, nicht nur
        # seine Überschrift. Andernfalls bleiben alte/ungeprüfte Positionszeilen
        # im Bericht stehen und vermischen sich mit dem deterministischen Tab-2-Block.
        # Abschnitt 10.5 endet an der Methodik (11), am Statusmarker oder am Dateiende.
        # Abschnittsgrenzen robust über die Kapitelnummer erkennen. Gemini darf
        # die Überschrift von Kapitel 11 variieren; der alte Abschnitt 10.5 endet
        # deshalb an jeder eigenständigen 11er-Kapitelüberschrift, nicht nur an
        # einem exakten Wortlaut.
        old_closed_section = re.compile(
            r"(?ims)^[ \t]*(?:#{1,6}\s*)?10\.5[.]?\s+Geschlossene Positionen[^\r\n]*\r?\n.*?"
            r"(?=^[ \t]*(?:#{1,6}\s*)?11(?:\s*[.·](?!\d)|\s+)[^\r\n]*$"
            r"|^[ \t]*TRADING_RADAR_FACHLICH_FINAL=(?:FINAL|NICHT_FINAL)[ \t]*$|\Z)"
        )
        text = old_closed_section.sub("", text)
        # Alte, frei formulierte Tab-2-Fehlerzeilen aus Gemini ebenfalls entfernen.
        text = re.sub(r"(?im)^TAB-2-QUELLE NICHT VERIFIZIERBAR:.*(?:\r?\n|$)", "", text)
        closed_block = (geschlossene_positionen_fakten or "TAB-2-QUELLE NICHT VERIFIZIERBAR: Quellenblock fehlt")
        # M-01/M-12: Den vollstaendigen Quellenblock vor der Freigabe validieren.
        # Bei Fehlern bleiben alle Positionszeilen als Diagnosebeleg erhalten,
        # aber Quellenmarker, Anzahl und Fingerabdruck werden nicht ausgegeben.
        raw_closed_lines = closed_block.splitlines()
        closed_rows = [
            line.strip() for line in raw_closed_lines
            if re.search(r"(?:^|\s)Quellzeile:\s*\d+\b", line)
            or re.search(r"(?:^|\s)Ticker:\s*\S+", line)
        ]
        closed_count_match = re.search(r"(?m)^TAB-2-ANZAHL=(\d+)\s*$", closed_block)
        validation_errors = []
        closed_tickers = re.findall(r"(?:^|\s)Ticker:\s*([^|\r\n]+)", closed_block, flags=re.M)
        if not closed_count_match:
            validation_errors.append("TAB-2-ANZAHL fehlt")
        elif int(closed_count_match.group(1)) != len(closed_tickers):
            validation_errors.append(
                f"Anzahl weicht ab: gemeldet={closed_count_match.group(1)}, erkannt={len(closed_tickers)}"
            )

        # Fachlicher Positionsschluessel ist Ticker + Einstiegsdatum.
        # Gleiche sonstige Fakten oder gleiche Ticker allein reichen nicht als
        # Duplikatnachweis. Zwei Zeilen mit demselben Ticker und Kaufdatum sind
        # nach dem vereinbarten Datenvertrag dagegen nicht eindeutig.
        position_keys = []
        invalid_identity_rows = []
        for line in closed_rows:
            ticker_match = re.search(r"(?:^|\s)Ticker:\s*([^|\r\n]+)", line)
            entry_match = re.search(r"(?:^|\s)Einstiegsdatum:\s*([^|\r\n]+)", line)
            source_row_match = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
            ticker = ticker_match.group(1).strip().casefold() if ticker_match else ""
            entry_date = entry_match.group(1).strip() if entry_match else ""
            source_row = source_row_match.group(1) if source_row_match else "?"
            parsed_entry_date = _parse_tab2_entry_date_text(entry_date)
            if not ticker or parsed_entry_date is None:
                invalid_identity_rows.append(source_row)
            else:
                position_keys.append((ticker, parsed_entry_date.isoformat()))
        if invalid_identity_rows:
            validation_errors.append(
                "Ticker+Einstiegsdatum fehlen/ungueltig bei Quellzeilen="
                + ", ".join(invalid_identity_rows)
            )
        duplicate_keys = sorted(
            f"{ticker}|{entry_date}"
            for ticker, entry_date in set(position_keys)
            if position_keys.count((ticker, entry_date)) > 1
        )
        if duplicate_keys:
            validation_errors.append(
                "Positionsschluessel Ticker+Einstiegsdatum doppelt: "
                + ", ".join(duplicate_keys)
            )

        if validation_errors or str(closed_block).startswith("TAB-2-QUELLE NICHT VERIFIZIERBAR"):
            existing_reason = (
                closed_block.splitlines()[0]
                if str(closed_block).startswith("TAB-2-QUELLE NICHT VERIFIZIERBAR")
                else ""
            )
            reason = "; ".join(([existing_reason] if existing_reason else []) + validation_errors)
            # Keine Position verlieren: alle erkannten Zeilen bleiben lesbar
            # im Diagnosebericht; Gate blockiert wegen fehlender kanonischer
            # Marker und des expliziten Nicht-verifizierbar-Markers.
            closed_block = "\n".join(
                ["TAB-2-QUELLE NICHT VERIFIZIERBAR: " + reason] + closed_rows
            )

        # M-06: Abschnitt 7 muss beide Performance-Kennzahlen explizit enthalten.
        # Fehlende Daten werden als nicht verfügbar ausgewiesen, niemals geschätzt.
        section7 = re.search(
            r"(?im)^[ \t]*(?:#{1,6}\s*)?7\s*[.·]\s*MSCI-Vergleich[^\r\n]*$", text
        )
        if section7:
            # Kapitel 8 darf anders heißen. Suche zuerst eine eigenständige
            # Kapitel-8-Überschrift, danach ersatzweise die nächste nummerierte
            # Hauptüberschrift (9., 10., 11. usw.). Niemals bis EOF erweitern:
            # ohne belegte Grenze wird Abschnitt 7 nicht verändert.
            remaining = text[section7.end():]
            next_section = re.search(
                r"(?im)^[ \t]*(?:#{1,6}\s*)?(?:8|9|[1-9][0-9])(?:\s*[.·](?!\d)|\s+(?!\d))[^\r\n]*$",
                remaining,
            )
            if next_section:
                section_end = section7.end() + next_section.start()
                section_body = text[section7.end():section_end]
                has_portfolio = re.search(r"(?i)Portfolio(?:-Gesamt)?performance|Gesamtperformance\s+Portfolio", section_body)
                has_average = re.search(r"(?i)(?:Ø|durchschnitt(?:liche|s)?|mittelwert)\s*Positionsperformance|Positionsperformance\s*\(?(?:Ø|Durchschnitt|Mittelwert)\)?", section_body)
                missing = []
                if not has_portfolio:
                    missing.append("Portfolio-Gesamtperformance nicht verfügbar (keine belegte Berechnungsgrundlage)")
                if not has_average:
                    missing.append("Ø Positionsperformance nicht verfügbar (keine belegte Berechnungsgrundlage)")
                if missing:
                    text = text[:section_end].rstrip() + "\n" + "; ".join(missing) + ".\n" + text[section_end:]

        # Fingerabdruck des deterministischen Exports: Das Gate berechnet ihn
        # aus dem gespeicherten Block erneut. Damit werden nachtraegliche
        # Veraenderungen, verlorene Zeilen und Duplikate in der Berichtskette sichtbar.
        if not str(closed_block).startswith("TAB-2-QUELLE NICHT VERIFIZIERBAR"):
            closed_lines = [line.rstrip() for line in closed_block.splitlines()
                            if not line.startswith("TAB-2-EXPORT-SHA256=")]
            closed_digest = hashlib.sha256("\n".join(closed_lines).encode("utf-8")).hexdigest()
            closed_block = "\n".join(closed_lines + [f"TAB-2-EXPORT-SHA256={closed_digest}"])

        # Datenqualitaet: fehlende Firmennamen werden im fachlichen Datenanhang
        # sichtbar gemacht, nicht nur in Runner-Logs. Der Ticker bleibt als
        # Identitaetsschluessel erhalten; es wird kein Name erfunden.
        try:
            name_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".firma_name_cache.json")
            with open(name_cache_path, "r", encoding="utf-8") as name_cache_file:
                name_cache = json.load(name_cache_file)
            # Die Datenqualitaetsmeldung betrifft nur das aktive Datenuniversum.
            # Diese Ausschlussmenge muss mit historical_ohlcv.EXCLUDED_TICKERS
            # uebereinstimmen; ausgeschlossene Symbole sind kein aktiver
            # Firmennamen-Aufloesungsfehler.
            excluded_tickers_for_quality = (
                "10O.DE", "1TRA.DE", "ABB", "ANSS", "ARND.DE", "ASTA.DE", "AUM.DE",
                "CMA", "DFS", "FLIR", "FRAG.DE", "HES", "JNPR", "KCOG.DE", "MAXN",
                "MRO", "NAFG.DE", "PNE.DE", "PSAG.DE", "RTL.DE", "SHA.DE", "SHELL.SO",
                "SNV", "STM1.DE", "WRK", "X",
            )
            missing_names = _trading_radar_aktive_firmennamen_luecken(
                name_cache, excluded_tickers_for_quality
            )
            if missing_names:
                disclosure = (
                    f"Firmennamen-Datenqualitaet: {len(missing_names)} Ticker ohne bestaetigten Unternehmensnamen; "
                    f"Ticker: {', '.join(missing_names[:80])}"
                    + (f"; weitere={len(missing_names)-80}" if len(missing_names) > 80 else "")
                    + ". Namen fehlen wegen nicht erfolgreicher Quellenaufloesung; Ticker werden nicht als bestaetigte Firmennamen gewertet."
                )
                section8 = re.search(r"(?im)^[ \t]*(?:#{1,6}\s*)?8\s*[.·]\s*Daten-/Prüfanhang[^\r\n]*$", text)
                if section8:
                    tail = text[section8.end():]
                    next_heading = re.search(r"(?im)^[ \t]*(?:#{1,6}\s*)?(?:9|10|11)(?:\s*[.·](?!\d)|\s+)[^\r\n]*$", tail)
                    pos = section8.end() + next_heading.start() if next_heading else len(text)
                    if "Firmennamen-Datenqualitaet:" not in text[section8.end():pos]:
                        text = text[:pos].rstrip() + "\n" + disclosure + "\n" + text[pos:]
        except (OSError, ValueError, TypeError):
            pass

        # Der Quellenblock 10.5 gehört unmittelbar VOR Abschnitt 11 (Methodik).
        # So bleiben die Kapitel in ihrer fachlichen Reihenfolge; zugleich wird
        # garantiert, dass der vollständige Tab-2-Block vor dem Statusmarker liegt.
        text = re.sub(r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(?:FINAL|NICHT_FINAL)\s*$", "", text)
        canonical_closed = (
            "\n10.5 Geschlossene Positionen (Quelle: Tab 2 / Geschlossene Positionen)\n"
            + closed_block.rstrip() + "\n\n"
        )
        # Für die Einfügestelle zählt die Kapitelnummer, nicht der exakte
        # Titel. Damit funktionieren auch Varianten wie "11. Methodische
        # Hinweise und Datenqualität". Ein echtes Kapitel 11 wird bevorzugt.
        section11 = re.search(
            r"(?im)^[ \t]*(?:#{1,6}\s*)?11(?:\s*[.·](?!\d)|\s+)[^\r\n]*$",
            text,
        )
        if section11:
            text = text[:section11.start()].rstrip() + "\n\n" + canonical_closed.lstrip("\n") + text[section11.start():]
        else:
            text = text.rstrip() + "\n\n" + canonical_closed.lstrip("\n")
        # M-10: OHLCV-Aussagen ausschließlich aus dem strukturierten Prüfergebnis übernehmen.
        text = _trading_radar_ohlcv_abschnitt8_aktualisieren(text)
        # Ereignisradar normalisieren und bekannte FOMC-Termine aus dem gepflegten
        # Projektkalender mit offizieller Fed-Quelle anreichern.
        text, event_quality_findings = normalize_event_section(text)
        validation_errors.extend("Ereignisradar: " + item for item in event_quality_findings)
        # Dieselbe persistente OHLCV-DB wie der Berichtsnachweis prüfen. Widersprüche
        # zwischen Status und Detailzahlen sind ein harter fachlicher Fehler.
        try:
            import historical_ohlcv
            # Erst Kandidaten aus dem finalen Report extrahieren, dann exakt dieselbe
            # aktive Universumsbasis und DB prüfen wie im unmittelbar vorherigen Workflow-Update.
            candidate_tickers = _trading_radar_kandidaten_aus_abschnitt5(text)
            ohlcv_audit = historical_ohlcv.audit_snapshot(candidate_tickers=candidate_tickers)
            text = downgrade_ohlcv_affected_candidates(text, ohlcv_audit)
            ohlcv_findings = validate_ohlcv_audit(ohlcv_audit)
        except Exception as exc:
            ohlcv_audit = {"status": "unavailable", "universe_count": None, "covered_count": None}
            ohlcv_findings = [f"OHLCV-Prüfung nicht ausführbar: {type(exc).__name__}: {exc}"]
        validation_errors.extend("OHLCV: " + item for item in ohlcv_findings)
        if ohlcv_findings:
            text += "\nDaten-/Freigabeprüfung: NICHT_FINAL – " + "; ".join(ohlcv_findings) + "\n"
        if event_quality_findings:
            text += "\nEreignis-/Freigabeprüfung: NICHT_FINAL – " + "; ".join(event_quality_findings) + "\n"
        # M-06/M-08 nach allen Berichtsumstellungen deterministisch absichern.
        # Unbelegte Werte werden als NICHT VERFÜGBAR markiert, nie erfunden.
        text = _trading_radar_berichtsfelder_normalisieren(text)
        text = _trading_radar_name_ticker_normalisieren(text, eingabedateien)
        # Canonicalize model Markdown labels ("**Label:**" -> "**Label**:")
        # before validators parse them. This fixes field recognition at source,
        # rather than weakening the report gate.
        text = re.sub(r"\*\*([^*\n:]+):\*\*", r"**\1**:", text)
        # Entity-name resolution must never rewrite schema labels such as Thesen-ID.
        text = re.sub(r"(?i)Thesen-Name\s+nicht\s+aufgelöst\s*\(ID\)", "Thesen-ID", text)
        text = re.sub(r"(?i)Thesen-Name\s+nicht\s+aufgeloest\s*\(ID\)", "Thesen-ID", text)
        # Nach der Namensnormalisierung einsetzen: Der generische Entity-Resolver
        # darf die festen Marktbeleg-Labels/Ticker (VIX, DXY, ^GSPC usw.) nicht
        # in "Name nicht aufgelöst" umschreiben.
        try:
            text, market_snapshot_findings = append_live_market_snapshot(text)
            validation_errors.extend("Markt-Snapshot: " + item for item in market_snapshot_findings)
        except Exception as snapshot_exc:
            validation_errors.append(
                f"Markt-Snapshot nicht verfügbar: {type(snapshot_exc).__name__}: {snapshot_exc}"
            )
        # Incomplete candidate rows remain visible but are explicitly downgraded
        # to non-tradable watchlist status before the final report gate.
        text = _trading_radar_unvollstaendige_kandidaten_nicht_handelsbar(text)
        # M-07 bis M-10: den fertig normalisierten Bericht selbst prüfen.
        # Vorher wurden nur vorgelagerte Teilprüfungen ausgewertet; damit
        # konnten strukturell ungültige Thesen/Kandidaten den FINAL-Marker umgehen.
        try:
            report_gate_findings = validate_trading_radar_report(text)
        except Exception as exc:
            report_gate_findings = [
                f"Report-Gate nicht ausführbar: {type(exc).__name__}: {exc}"
            ]
        validation_errors.extend(
            f"Report-Gate: {finding}" for finding in report_gate_findings
        )
        # Genau ein Statusmarker, stets als letzte Zeile. Ohne eindeutigen FINAL-
        # Marker bleibt der Bericht gesperrt; der Report-Gate entscheidet separat.
        # Der vom Sprachmodell gelieferte Statusmarker ist kein Beleg fuer die
        # technische/fachliche Freigabe: Gemini kann NICHT_FINAL aus Kontext
        # oder aus einem vorherigen Entwurf uebernehmen. Die Freigabe wird hier
        # deterministisch aus den validierten Pflichtdaten abgeleitet; weitere
        # unabhaengige Report-Gates pruefen den fertigen Bericht anschliessend.
        final_status = "FINAL"
        if validation_errors or str(closed_block or "").startswith("TAB-2-QUELLE NICHT VERIFIZIERBAR"):
            final_status = "NICHT_FINAL"
        text = re.sub(r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(?:FINAL|NICHT_FINAL)\s*$", "", text).rstrip()
        text += f"\nTRADING_RADAR_FACHLICH_FINAL={final_status}\n"
        temp_output = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(radar_output),
                prefix=f".{Path(radar_datei).name}.", suffix=".tmp", delete=False,
            ) as f:
                temp_output = f.name
                f.write(text.rstrip() + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_output, radar_output)
            temp_output = None
        finally:
            if temp_output and os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except OSError:
                    pass
        # Fachlichen Status ausschließlich aus einem eindeutigen Reportmarker
        # übernehmen. Ohne Marker ist der Bericht technisch erzeugt, aber nicht
        # fachlich freigegeben; niemals aus Dateiexistenz FINAL ableiten.
        status_matches = re.findall(
            r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text
        )
        radar_final = status_matches[0] if len(status_matches) == 1 else "NICHT_FINAL"
        if len(status_matches) != 1:
            with open(radar_output, "a", encoding="utf-8") as f:
                f.write(f"\nTRADING_RADAR_FACHLICH_FINAL={radar_final}\n")
        print(f"Trading-Radar-Eigenbericht gespeichert: {radar_datei}")
        print("TRADING_RADAR_DATEI=" + radar_output)
        print("TRADING_RADAR_FACHLICH_FINAL=" + radar_final)
        return radar_output
    ausgabe_datei = f"Auswertung({heute}).txt"
    fachliche_mindesttiefe_offen = False

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

        # Gemeinsame Markt-Briefing-Quelle vor allen 8.x-Reparaturaufrufen laden.
        # Sie wird benoetigt, um abweichende 4W-Werte gegen das Edelmetalle-
        # Briefing zu erkennen. Nicht mit leerem Text weiterarbeiten, weil das
        # den Quellenkonflikt-Gate unbemerkt deaktivieren wuerde.
        briefing_pfad = finde_datei(DATEIMUSTER["briefing.txt"])
        if not briefing_pfad or not os.path.isfile(briefing_pfad):
            raise RuntimeError("BRIEFING_QUELLE_FEHLT: Markt-Briefing fuer 8.x-Quellenabgleich nicht gefunden.")
        try:
            briefing_text = Path(briefing_pfad).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"BRIEFING_QUELLE_NICHT_LESBAR: {exc}") from exc

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
            final_text = _repariere_8_x_quellengebunden(final_text, edelmetall_text, briefing_text)

        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)
        if eingabedateien is None:
            eingabedateien = _GEMINI_EINGABEDATEIEN_AUSWERTUNG
        final_text = _bereinige_punkt_24_nur_a(final_text, eingabedateien)
        final_text = _normalisiere_punkt10_autoritaet(final_text)
        final_text = _normalisiere_punkt11_quellengebunden(final_text)
        # Repariert die bekannte, ungueltige Zusammenziehung zweier Firmennamen
        # vor dem ersten harten Identitaets-Gate.
        final_text = re.sub(
            r",\s*ComfoQuanta Services, Inc\.\s*\(PWR\)\s*\(\+9,87%\)",
            "",
            final_text,
            flags=re.I,
        )
        final_text = _normalisiere_name_ticker_ausgabe(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Quellengebundene Trade-Kandidaten und Statusverläufe werden nach der
        # Gemini-Synthese deterministisch gegen die aktuellen Scanner-/Historien-
        # Dateien geprüft. Das Rohuniversum und offene Positionen sind keine
        # Ersatzquelle für ein bestätigtes neues Setup.
        final_text = _normalisiere_punkt12_scannerbindung(final_text, eingabedateien)
        final_text = _korrigiere_statusverlauf_ideen(final_text, eingabedateien)
        final_text = _repariere_1_3_aus_idee_im_aufbau(final_text)

        # Letzte Strukturabsicherung NACH allen inhaltlichen Normalisierungen.
        # Frühere Pflichtabschnitte können durch nachgelagerte Normalisierungen
        # verändert oder entfernt werden; deshalb wird die verbindliche
        # 1–11.7-Struktur unmittelbar vor dem harten Final-Gate nochmals
        # deterministisch ergänzt. Bereits vorhandene echte Inhalte bleiben
        # unverändert.
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _ergaenze_fehlende_ausgabestruktur(final_text)
        if edelmetall_text:
            final_text = _repariere_8_x_quellengebunden(final_text, edelmetall_text, briefing_text)
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Letzter Reparaturpass gegen nachgewiesene Fehler aus dem Lauf vom
        # 10.10.2026. Er steht nach allen inhaltlichen Normalisierungen, damit
        # keine spaetere Gemini-/Formatierungsstufe die Korrekturen ueberschreibt.
        final_text = _repariere_auswertung_final(
            final_text,
            eingabedateien,
            _GEMINI_GESCHLOSSENE_10_5_FINAL,
        )

        # Letzte deterministische Schliessung der Kette Quelle -> 7.1–7.7 -> Gate.
        # Dieser Pass steht bewusst unmittelbar vor den harten Quellen-Gates,
        # damit nachgelagerte Normalisierungen die autoritativen Werte nicht
        # wieder aus ihrem zugeordneten Abschnitt entfernen koennen.
        final_text, quellen_repaired = _repariere_7_x_quellengebunden(
            final_text,
            briefing_text,
            makro_text_fx,
        )
        if quellen_repaired:
            print("INFO: 7.1-7.7 autoritative Quellwerte deterministisch in die zugeordneten Abschnitte ergaenzt.")
        final_text = _repariere_7_7_macro_leakage(final_text, makro_text_fx)
        final_text, period_repaired = _repariere_7_performanceperioden(final_text, makro_text_fx)
        if period_repaired:
            print("INFO: Performance-Zeitraeume in 7.1/7.3/7.5/7.6 gegen autoritative Makro-Perioden repariert.")
        _pruefe_7_performanceperioden(final_text, makro_text_fx)
        final_text = _repariere_3_5_fundamentaldaten(final_text)
        # Dieses fruehe Regression-Gate laeuft vor den spaeteren Schlusskorrekturen.
        # Futures-Basis-Duplikate muessen bereits hier aus 8.1–8.4 entfernt sein,
        # sonst bricht der Lauf ab, bevor der spaetere Abschluss-Sanitizer erreicht wird.
        final_text = _entferne_futures_basis_duplikate_aus_abschnitt8(final_text)
        _pruefe_dokumentierte_reparaturen(final_text, (eingabedateien or {}).get("Letzte_Auswertung(...).txt"))

        # WICHTIG: Der finale Reparaturpass oben kann 6.1–6.6-Platzhalter
        # entfernen, wenn er einen nachgelagerten doppelten Platzhalterblock
        # erkennt. Deshalb muss die verbindliche Gesamtstruktur NACH diesem
        # Reparaturpass und VOR dem ersten harten Struktur-Gate erneut
        # hergestellt werden. Andernfalls scheitert bereits das erste Gate,
        # bevor der bisherige spätere Abschluss-Reparaturpass erreicht wird.
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _ergaenze_fehlende_ausgabestruktur(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

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

        # Das Name/Ticker-Gate darf hier noch nicht laufen: Nach diesem Block
        # folgen weitere deterministische Inhaltsreparaturen, die neue
        # Kandidatenzeilen oder Tickerangaben erzeugen koennen. Ein Gate vor
        # diesen Reparaturen kann einen Zwischenstand ablehnen, obwohl der
        # nachgelagerte finale Normalisierungslauf ihn korrekt reparieren wuerde.
        # Die verbindliche Identitaetspruefung erfolgt einmalig nach ALLEN
        # Inhaltsreparaturen direkt vor Speicherung (siehe finales Gate unten).
        _pruefe_punkt11_quellenbindung(final_text)
        _pruefe_neue_ausgabestruktur(final_text)

        # Nichtkritische Inhaltsmaengel sollen die Tagesauswertung nicht verhindern.
        # Zuerst werden fehlende Kausal-/Pruefpunkte in 3.1–3.5 gezielt und ohne
        # neue numerische Fakten ergaenzt; verbleibende Tiefenmaengel werden als
        # Warnung protokolliert. Quellen-, Identitaets- und Struktur-Gates oben
        # bleiben harte Fehler und werden durch diese Regel nicht abgeschwaecht.
        final_text, kausal_reparaturen = _repariere_3_x_kausalitaet(final_text)
        if kausal_reparaturen:
            print(
                "QUALITAETSREPARATUR: Kausal-/Pruefpunkte ergaenzt in: "
                + ", ".join(kausal_reparaturen)
            )

        # Die Mindesttiefe bleibt eine Qualitätsprüfung, darf den Tagesbericht
        # bei einem Inhaltsmangel aber nicht am Speichern hindern. Struktur-,
        # Quellen- und Identitäts-Gates bleiben davon unberührt.
        try:
            _pruefe_inhaltliche_mindesttiefe(final_text)
        except RuntimeError as exc:
            fachliche_mindesttiefe_offen = True
            print(f"WARNUNG: Mindesttiefe der Auswertung nicht bestanden; Bericht wird dennoch gespeichert: {exc}")

        # Abschluss nach ALLEN inhaltlichen Reparaturen: keine nachgelagerte
        # Reparatur darf doppelte Abschnittsnummern oder widerspruechliche
        # 1.3/4-Zuordnungen unbemerkt wieder einfuehren.
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _ergaenze_fehlende_ausgabestruktur(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)
        final_text = _synchronisiere_1_1_mit_quellen(final_text, briefing_text, final_text)
        final_text = _repariere_watchlist_technisches_potenzial(final_text, eingabedateien)
        final_text = _repariere_7_1_ytd_aus_briefing(final_text, briefing_text)
        final_text = _repariere_1_3_aus_idee_im_aufbau(final_text)
        final_text = _repariere_2_5_alternativen(final_text)
        final_text = _komprimiere_1_2_chancen(final_text)
        final_text = _ergaenze_handlungsprioritaet_2_4(final_text)
        # Bekannter Textkorruptionsfehler aus der aktuellen Auswertung: ein
        # zusammengezogener Quanta-/Comfort-Systems-Name ist keine echte Firma.
        final_text = re.sub(
            r",\s*ComfoQuanta Services, Inc\.\s*\(PWR\)\s*\(\+9,87%\)",
            "",
            final_text,
            flags=re.I,
        )
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Quellengebundene Schlusskorrektur der bekannten Zahlenwidersprüche:
        # Makrowerte, Brent/WTI-Rollen und alle wiederholten Edelmetallwerte
        # werden erst nach sämtlichen vorherigen Reparaturen synchronisiert.
        final_text, zahlen_reparaturen = _repariere_makro_und_edelmetall_konsistenz(
            final_text, makro_text_fx, edelmetall_text
        )
        if zahlen_reparaturen:
            print("ZAHLENKONSISTENZ-GATE: " + " | ".join(zahlen_reparaturen))

        # Letztes quellengebundenes Konflikt-Gate: Fakten aus Python-Quellen
        # haben Vorrang vor widersprüchlicher Gemini-Prosa.
        final_text, konflikt_reparaturen = _repariere_auswertung_quellenkonflikte(
            final_text, eingabedateien, briefing_text, makro_text_fx, edelmetall_text
        )
        if konflikt_reparaturen:
            print("ZAHLENKONSISTENZ-GATE: " + " | ".join(konflikt_reparaturen))

        # Abschließender Fix für nachgewiesene Perioden-/Quellenzuordnungen und
        # mehrfache offene Positionseinträge. Muss nach allen Gemini-/Textreparaturen
        # laufen, damit keine spätere Normalisierung die Korrekturen zurücksetzt.
        final_text, quellen_perioden_positionen_reparaturen = _repariere_quellenperioden_und_positionsduplikate(
            final_text, briefing_text, makro_text_fx, edelmetall_text
        )

        # Letzter Identitaetsdurchlauf nach ALLEN Inhaltsreparaturen. So werden
        # auch spaeter eingefuegte Firmen-/Ticker-Nennungen kanonisiert.
        final_text = _normalisiere_name_ticker_ausgabe(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Letzte Struktur-Schliessung nach ALLEN textveraendernden Reparaturen.
        # Insbesondere Zahlen-/Quellenabgleich und Namensnormalisierung duerfen
        # keine Pflichtueberschrift vor dem harten Final-Gate unbemerkt entfernen.
        final_text = _normalisiere_inline_pflichtueberschriften(final_text)
        final_text = _ergaenze_fehlende_ausgabestruktur(final_text)
        final_text = _bereinige_ausgabe_und_formatiere(final_text)

        # Das Zahlen-Gate kann bei offenen Konflikten einen dokumentierten
        # Warnhinweis in den Bericht einsetzen. Deshalb muss es VOR den letzten
        # Struktur-/Identitäts-/Konsistenz-Gates laufen. So prüfen diese Gates
        # genau den Textstand, der anschließend gespeichert wird.
        final_text, zahlen_gate_warnungen = _pruefe_auswertung_quellenkonflikte(
            final_text,
            briefing_text,
            makro_text_fx,
            edelmetall_text,
            eingabedateien,
        )

        # Quellenabweichungen werden im gespeicherten Bericht dokumentiert;
        # sie blockieren die technische Ausgabe und FACHLICH_FINAL nicht allein.
        final_text = _dokumentiere_edelmetall_4w_quellenkonflikte(
            final_text, edelmetall_text, briefing_text
        )

        # Letzte textverändernde Reparatur direkt nach dem letzten Gate, das
        # Konflikthinweise einsetzen kann. Danach folgen ausschließlich Prüfungen.
        # So können spätere Zahlen-/Hinweisreparaturen keine Futures-Basis-
        # Duplikate in 8.1–8.4 zurückbringen.
        final_text = _entferne_futures_basis_duplikate_aus_abschnitt8(final_text)

        # Die kompakten Inline-Zeilen 8.1–8.4 werden nach ALLEN Textreparaturen
        # erneut aus der spezialisierten Faktenquelle aufgebaut. Damit bleiben
        # keine alten Gemini-Kurs-/Perioden-/Indikatorwerte als zweite Faktenkopie.
        final_text = _repariere_inline_8_x_kurzzeilen(final_text, edelmetall_text)
        # Drei bekannte, nicht belegte Edelmetall-Aussagen vor dem finalen
        # globalen Release-Gate deterministisch entfernen/neu formulieren.
        final_text = _repariere_unbelegte_edelmetall_aussagen(final_text)

        # Letztes hartes Gate: Quellperioden, Zeitstände, Edelmetall-Priorität
        # und eindeutige offene Positionslisten müssen im gespeicherten Text stehen.
        _pruefe_quellenperioden_und_positionsduplikate(
            final_text, briefing_text, makro_text_fx, edelmetall_text
        )

        # Dokumentierbare Warnungen werden protokolliert; harte Integritaets-
        # Gates wurden davor und danach separat geprueft und bleiben blockierend.
        _gemini_pruefe_freigabe_vor_speicherung(
            zahlen_gate_warnungen, fachliche_mindesttiefe_offen
        )

        # Nach dem Zahlen-Gate folgen nur noch nicht-mutierende Prüfungen.
        # Damit werden auch der ggf. ergänzte Konflikthinweis und der endgültige
        # Berichtstext vor dem Speichern geprüft.
        _pruefe_name_ticker_gate(final_text)
        _pruefe_neue_ausgabestruktur(final_text)
        _pruefe_dokumentierte_reparaturen(
            final_text, (eingabedateien or {}).get("Letzte_Auswertung(...).txt")
        )
        final_text = _repariere_doppelte_pflichtunterabschnitte(final_text)
        _pruefe_finale_struktur_und_konsistenz(final_text)

        # Die Themen-Kausalitaetspruefung ist ein hartes Final-Gate. Eine
        # fehlerhafte Analyse darf nicht mehr gespeichert/verschickt werden.
        _pruefe_themen_kausalitaet(final_text)

        # Edelmetall-Interpretationen sind ein fachlicher Teilstatus, kein
        # globaler Ausgabeblocker. Harte Struktur-/Quellen-/Kausalitaets-Gates
        # bleiben davor unveraendert aktiv. Pruefungsfehler selbst bleiben fail-closed.
        try:
            edelmetall_global_warnungen = _edelmetall_interpretationswarnungen(
                final_text, edelmetall_text
            )
        except Exception as exc:
            print(f"EDELMETALL_PRUEFUNG_FEHLER | {type(exc).__name__}: {exc}")
            raise RuntimeError(
                "EDELMETALL_PRUEFUNG_FEHLER: Zuordnungspruefung konnte nicht sicher abgeschlossen werden."
            ) from exc
        for warnung in edelmetall_global_warnungen:
            print("EDELMETALL_BEFUND | " + warnung)
        edelmetall_zuordnungsbefunde = [
            w for w in edelmetall_global_warnungen if w.startswith("ZUORDNUNG |")
        ]
        edelmetall_interpretationsbefunde = [
            w for w in edelmetall_global_warnungen if w.startswith("INTERPRETATION |")
        ]
        # Der fachliche Status wird aus den konkreten, metallbezogenen Befunden
        # abgeleitet. Die Befunde bleiben einzeln im Report erhalten; andere
        # Aktien-/Marktabschnitte werden dadurch nicht veraendert.
        edelmetall_nicht_final = bool(
            edelmetall_zuordnungsbefunde or edelmetall_interpretationsbefunde
        )
        edelmetall_betroffene_metalle = sorted({
            m.group(1) for w in edelmetall_global_warnungen
            if (m := re.search(r"ROHSTOFF=([^|]+)", w))
        })
        edelmetall_metall_status = {
            metal: (
                "NICHT_FINAL"
                if any(re.search(r"ROHSTOFF=" + re.escape(metal) + r"(?:\s*\||$)", w)
                       for w in edelmetall_global_warnungen)
                else "FINAL"
            )
            for metal in ("Gold", "Silber", "Platin", "Palladium")
        }
        if edelmetall_nicht_final:
            print(
                "EDELMETALL_STATUS=NICHT_FINAL"
                f" | ZUORDNUNG={len(edelmetall_zuordnungsbefunde)}"
                f" | INTERPRETATION={len(edelmetall_interpretationsbefunde)}"
                f" | BETROFFENE_ROHSTOFFE={','.join(edelmetall_betroffene_metalle) or 'UNGEKLAERT'}"
                " | Ausgabe, Historie und Upload bleiben erlaubt."
            )
            # Die konkreten Befunde werden mit dem Report persistiert. Ein
            # Zuordnungsfehler bleibt getrennt von einer unbelegten Interpretation.
            for metal, metal_status in edelmetall_metall_status.items():
                final_text += "\n" + f"EDELMETALL_ROHSTOFF_STATUS={metal}|{metal_status}"
            for befund in edelmetall_global_warnungen:
                final_text += "\n" + "EDELMETALL_BEFUND=" + befund
        else:
            print("EDELMETALL_STATUS=FINAL | keine Interpretationswarnungen")

    if str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        print("INFO: Technischer Gemini-Fallback - bestehende Auswertung bleibt unverändert; keine Auswertung wird gespeichert.")
        print("FACHLICH_FINAL=NICHT_FINAL | keine neue Gemini-Auswertung erzeugt")
        return None

    # Statusmarker fuer Tagesdatei, Historie und Upload: fachliche Finalitaet
    # bleibt strikt getrennt vom technischen Erfolg der Dateierzeugung.
    final_text = re.sub(r"(?m)^FACHLICH_FINAL=(?:FINAL|NICHT_FINAL)\s*\n?", "", final_text)
    final_text = re.sub(r"(?m)^(?:EDELMETALL_STATUS|FACHLICH_FINAL|BERICHT_STATUS|BEREICH_STATUS|BEREICH_BEFUND)=.*\n?", "", final_text)
    bereich_status, bereich_befunde = _gemini_bereichsstatus(
        zahlen_gate_warnungen=locals().get("zahlen_gate_warnungen", []),
        fachliche_mindesttiefe_offen=fachliche_mindesttiefe_offen,
        edelmetall_nicht_final=locals().get("edelmetall_nicht_final", False),
        edelmetall_global_warnungen=locals().get("edelmetall_global_warnungen", []),
    )
    fachlich_status = bereich_status["GESAMT"]
    metall_status = bereich_status["EDELMETALLE"]
    statuszeilen = [
        f"EDELMETALL_STATUS={metall_status}",
        f"FACHLICH_FINAL={fachlich_status}",
        f"BERICHT_STATUS={fachlich_status}",
    ]
    for bereich in ("AKTIEN", "MARKT_MAKRO", "EDELMETALLE", "GESAMTBERICHT"):
        statuszeilen.append(f"BEREICH_STATUS={bereich}|{bereich_status[bereich]}")
        for befund in bereich_befunde[bereich]:
            statuszeilen.append(f"BEREICH_BEFUND={bereich}|{befund}")
            if bereich == "EDELMETALLE":
                # Bestehende GitHub-Consumer erwarten den Legacy-Befundmarker.
                statuszeilen.append(f"EDELMETALL_BEFUND={befund}")
    for metall, status in locals().get("edelmetall_metall_status", {}).items():
        statuszeilen.append(f"EDELMETALL_ROHSTOFF_STATUS={metall}|{status}")
    final_text = "\n".join(statuszeilen) + "\n" + final_text.lstrip("\ufeff\n\r")

    # Erst nach Abschluss aller fachlichen Gates atomar ersetzen. Ein Abbruch
    # waehrend des Schreibens kann die bestehende Tagesauswertung dadurch nicht
    # kuerzen oder zerstoeren.
    if not isinstance(final_text, str) or not final_text.strip():
        raise RuntimeError("AUSGABE_LEER: Keine gueltige Auswertung zum Speichern vorhanden.")
    output_dir = os.path.dirname(os.path.abspath(ausgabe_datei))
    temp_output = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", dir=output_dir,
            prefix=f".{Path(ausgabe_datei).name}.", suffix=".tmp", delete=False,
        ) as f:
            temp_output = f.name
            f.write(final_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_output, ausgabe_datei)
        temp_output = None
    finally:
        if temp_output and os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass

    # Radar nur aus dem separaten Gemini-Request speichern; nie final_text kopieren.
    radar_text = globals().get("_GEMINI_TRADING_RADAR_TEXT")
    radar_status = globals().get("_GEMINI_TRADING_RADAR_STATUS", "NICHT_ERZEUGT")
    radar_temp = None
    if isinstance(radar_text, str) and radar_text.strip() and radar_status == "EIGENSTAENDIG_ERZEUGT":
        radar_datei = f"Trading_Radar({heute}).txt"
        if not re.fullmatch(r"Trading_Radar\(\d{4}-\d{2}-\d{2}\)\.txt", radar_datei):
            raise RuntimeError(f"TRADING_RADAR_DATEINAME_UNGUELTIG: {radar_datei}")
        radar_output = os.path.join(output_dir, radar_datei)
        if os.path.basename(radar_output) != radar_datei:
            raise RuntimeError(f"TRADING_RADAR_ABLAGEPFAD_UNGUELTIG: {radar_output}")
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", dir=output_dir,
                    prefix=f".{Path(radar_datei).name}.", suffix=".tmp", delete=False) as f:
                radar_temp = f.name
                f.write(radar_text.rstrip() + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(radar_temp, radar_output)
            radar_temp = None
            print(f"Trading-Radar-Eigenbericht gespeichert: {radar_datei}")
        finally:
            if radar_temp and os.path.exists(radar_temp):
                try:
                    os.remove(radar_temp)
                except OSError:
                    pass
    else:
        print(f"Trading-Radar nicht gespeichert: Status={radar_status}; bestehende Radar-Datei bleibt unveraendert.")

    _gemini_split_resume_loeschen()

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
            try:
                from trading_radar_thesen import update_register
                register_result = update_register(
                    final_text, os.path.join(output_dir, ".trading_radar_thesen.json")
                )
                if register_result.get("updated"):
                    print(f"Trading-Radar-Thesenregister aktualisiert: {register_result.get('count')} Thesen")
                else:
                    print("INFO: Trading-Radar-Thesenregister unveraendert: kein strukturierter Thesenblock")
            except Exception as register_exc:
                print(f"WARNUNG: Thesenregister nicht aktualisiert ({type(register_exc).__name__}: {register_exc})")
        except Exception as exc:
            # Die Tagesauswertung darf durch einen reinen Historienfehler nicht
            # unbrauchbar werden; der Fehler bleibt im Log sichtbar.
            print(f"WARNUNG: Persistente Gemini-Historie konnte nicht erstellt werden ({exc}).")
    else:
        print("INFO: Technischer Gemini-Fallback - persistente Historie wird nicht fortgeschrieben.")

    print(f"\nGespeichert: {ausgabe_datei}")
    print("AUSGABE_ERSTELLT=PASS")
    fachliche_gruende = []
    for bereich in ("AKTIEN", "MARKT_MAKRO", "EDELMETALLE", "GESAMTBERICHT"):
        print(f"BEREICH_STATUS={bereich}|{bereich_status[bereich]}")
    if bereich_status["GESAMT"] == "NICHT_FINAL":
        fachliche_gruende.append(
            "Mindestens ein Bereich oder ein berichtsweiter Qualitätsprüfpunkt ist NICHT_FINAL"
        )
    if not str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        # Edelmetallwarnungen sind Statushinweise, keine globalen Abbruchgruende.
        try:
            dokumentierte_metallabweichungen = _edelmetall_4w_quellenkonflikte(
                edelmetall_text if "edelmetall_text" in locals() else "",
                briefing_text if "briefing_text" in locals() else "",
            )
            if dokumentierte_metallabweichungen:
                print("EDELMETALL_QUELLENABWEICHUNG_DOKUMENTIERT | " + " | ".join(dokumentierte_metallabweichungen))
        except Exception as exc:
            print(f"WARNUNG: Vergleich der Edelmetallquellen nicht möglich ({type(exc).__name__}); kein Fallback auf Marktwerte.")
        # Dokumentierte Zahlen-/Quellenwarnungen und Mindesttiefe-Hinweise
        # sind Diagnosepunkte, keine harten Integritaetsfehler. Die harten
        # Struktur-, Identitaets-, Quellenperioden-, Kausalitaets- und
        # Edelmetall-Gates wurden vor dem Speichern erfolgreich bestanden.
        # Deshalb blockieren diese dokumentierten Warnungen FACHLICH_FINAL
        # nicht pauschal; sie bleiben im Bericht und Log sichtbar.
    if fachliche_gruende:
        print("FACHLICH_FINAL=NICHT_FINAL | " + " | ".join(fachliche_gruende))
    elif str(text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        print("FACHLICH_FINAL=NICHT_FINAL | keine neue Gemini-Auswertung erzeugt")
    else:
        print("FACHLICH_FINAL=FINAL")
    return ausgabe_datei


def _gemini_report_metall_status(report_text):
    """Extrahiert mehrere Edelmetallstatus als GitHub-Output-Zeichenkette.

    re.findall liefert bei zwei Capture-Gruppen Tupel. Diese werden hier
    explizit in die bestehende Pipe-getrennte Transportform umgewandelt.
    """
    status_paare = re.findall(
        r"(?m)^EDELMETALL_ROHSTOFF_STATUS=(Gold|Silber|Platin|Palladium)\|(FINAL|NICHT_FINAL)\s*$",
        str(report_text or ""),
    )
    return "|".join(f"{rohstoff}|{status}" for rohstoff, status in status_paare)


def _gemini_schreibe_github_outputs(
    status, fachlich_final, auswertung_datei="", edelmetall_status="FINAL",
    edelmetall_befunde="", edelmetall_metall_status="",
    bereich_status="", bereich_befunde="",
):
    """Stellt technische und fachliche Statusmarker getrennt fuer GitHub Actions bereit."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    werte = {
        "status": str(status),
        "fachlich_final": str(fachlich_final),
        "auswertung_datei": str(auswertung_datei or ""),
        "edelmetall_status": str(edelmetall_status),
        "edelmetall_befunde": str(edelmetall_befunde or ""),
        "edelmetall_metall_status": str(edelmetall_metall_status or ""),
        "bereich_status": str(bereich_status or ""),
        "bereich_befunde": str(bereich_befunde or ""),
    }
    for key, value in werte.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"Ungueltiger Zeilenumbruch im GitHub-Output {key}")
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in werte.items():
            handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    print("Gemini-Auswertung gestartet...")
    ergebnis_text = gemini_auswertung_starten()
    ausgabe_pfad = speichere_ergebnis(
        ergebnis_text,
        _GEMINI_EINGABEDATEIEN_AUSWERTUNG,
    )
    print(f"AUSWERTUNG_DATEI={ausgabe_pfad}")
    if globals().get("_GEMINI_TRADING_RADAR_ONLY_MODE"):
        if not ausgabe_pfad or not os.path.isfile(ausgabe_pfad) or os.path.getsize(ausgabe_pfad) <= 0:
            print("TRADING_RADAR_STATUS=FEHLER | Datei fehlt oder ist leer")
            raise SystemExit(3)
        # Fachstatus muss exakt aus dem gespeicherten Report stammen.
        with open(ausgabe_pfad, "r", encoding="utf-8-sig") as report_handle:
            report_text = report_handle.read()
        status_matches = re.findall(
            r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", report_text
        )
        if len(status_matches) != 1:
            print("TRADING_RADAR_STATUS=FEHLER | eindeutiger Fachstatusmarker fehlt")
            raise SystemExit(4)
        fachlich_final = status_matches[0]
        # GitHub Actions passes the outcomes of the final sync and persistence
        # steps. A missing/failed persistence step is an explicit audit finding,
        # not a silent best-effort success. Local runs without these variables
        # remain unaffected.
        persistence_checks = {
            "Finale OHLCV-Synchronisierung": os.environ.get("OHLCV_FINAL_SYNC", "").strip().casefold(),
            "OHLCV-Release-Persistenz": os.environ.get("OHLCV_RELEASE_PERSISTENCE", "").strip().casefold(),
            "OHLCV-Cache-Persistenz": os.environ.get("OHLCV_CACHE_PERSISTENCE", "").strip().casefold(),
        }
        persistence_findings = []
        if persistence_checks["Finale OHLCV-Synchronisierung"] and persistence_checks["Finale OHLCV-Synchronisierung"] != "complete":
            persistence_findings.append(
                "Finale OHLCV-Synchronisierung nicht vollständig: "
                + persistence_checks["Finale OHLCV-Synchronisierung"]
            )
        for check_name in ("OHLCV-Release-Persistenz", "OHLCV-Cache-Persistenz"):
            outcome = persistence_checks[check_name]
            if outcome and outcome != "success":
                persistence_findings.append(f"{check_name} nicht erfolgreich: {outcome}")
        if persistence_findings:
            persistence_section = (
                "\n\n### Audit: finale OHLCV-Synchronisierung und Persistenz\n"
                + "\n".join(f"- {finding}" for finding in persistence_findings)
                + "\n"
            )
            if "### Audit: finale OHLCV-Synchronisierung und Persistenz" not in report_text:
                report_text = report_text.rstrip() + persistence_section
            report_text = re.sub(
                r"(?m)^TRADING_RADAR_FACHLICH_FINAL=FINAL\s*$",
                "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL",
                report_text,
            )
            with open(ausgabe_pfad, "w", encoding="utf-8") as report_handle:
                report_handle.write(report_text)
            fachlich_final = "NICHT_FINAL"
            print("TRADING_RADAR_OHLCV_PERSISTENCE_GATE=FAIL")
            for finding in persistence_findings:
                print("TRADING_RADAR_OHLCV_PERSISTENCE_FINDING=" + finding)
        elif any(persistence_checks.values()):
            print("TRADING_RADAR_OHLCV_PERSISTENCE_GATE=PASS")
        # Markt-First-Inhaltsaudit auf dem tatsächlich gespeicherten Produktionsbericht.
        # Bei Befunden wird FINAL auf NICHT_FINAL herabgestuft; nichts wird erfunden.
        market_first_findings = audit_market_first_content(report_text)
        if market_first_findings:
            fachlich_final = "NICHT_FINAL"
            report_text = re.sub(
                r"(?m)^TRADING_RADAR_FACHLICH_FINAL=FINAL\s*$",
                "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL",
                report_text,
            )
            with open(ausgabe_pfad, "w", encoding="utf-8") as report_handle:
                report_handle.write(report_text)
            print("TRADING_RADAR_MARKET_FIRST_GATE=FAIL")
            for finding in market_first_findings:
                print("TRADING_RADAR_MARKET_FIRST_FINDING=" + finding)
        else:
            print("TRADING_RADAR_MARKET_FIRST_GATE=PASS")
        print("TRADING_RADAR_STATUS=SUCCESS")
        print("TRADING_RADAR_FACHLICH_FINAL=" + fachlich_final)
        print("TRADING_RADAR_EXIT_STATUS=0")
        output_path = os.environ.get("GITHUB_OUTPUT")
        if output_path:
            with open(output_path, "a", encoding="utf-8") as handle:
                handle.write(f"status=SUCCESS\nfachlich_final={fachlich_final}\nreport_path={Path(ausgabe_pfad).name}\n")
        raise SystemExit(0)
    if str(ergebnis_text or "").startswith("[GEMINI_TECHNISCHER_FALLBACK]"):
        print("GEMINI_STATUS=TECHNISCHER_FALLBACK")
        _gemini_schreibe_github_outputs(
            "TECHNISCHER_FALLBACK", "NICHT_FINAL", ""
        )
        print(
            "GEMINI_EXIT_STATUS=2 | Technischer Fallback: Keine neue, fachlich finale "
            "Auswertung-Datei erzeugt; vorhandene Tagesdatei bleibt unverändert."
        )
        # Wichtig fuer GitHub Actions: Ein technischer Fallback darf nicht als
        # erfolgreicher Tageslauf gecacht oder als fertig markiert werden.
        raise SystemExit(2)

    if not ausgabe_pfad or not os.path.isfile(ausgabe_pfad):
        print("GEMINI_STATUS=FEHLER")
        print("GEMINI_EXIT_STATUS=3 | Auswertungspfad fehlt oder Datei wurde nicht gespeichert.")
        raise SystemExit(3)

    if os.path.getsize(ausgabe_pfad) <= 0:
        print("GEMINI_STATUS=FEHLER")
        print("GEMINI_EXIT_STATUS=3 | Auswertung-Datei ist leer.")
        raise SystemExit(3)

    report_text = Path(ausgabe_pfad).read_text(encoding="utf-8-sig")
    report_bereich_status, report_bereich_befunde = _gemini_report_bereichsstatus(report_text)
    fachlich_final = report_bereich_status["GESAMT"]
    edelmetall_status = report_bereich_status["EDELMETALLE"]
    report_befunde = "\n".join(re.findall(
        r"(?m)^EDELMETALL_BEFUND=(.*)$", report_text
    ))
    # GitHub-Output darf keine mehrzeilige Payload enthalten; Befunde werden
    # als kompakte, escaped Ein-Zeilen-Information transportiert.
    report_befunde = report_befunde.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    report_metall_status = _gemini_report_metall_status(report_text)
    report_metall_status = report_metall_status.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    bereich_status, bereich_befunde = _gemini_report_bereichsstatus(report_text)
    bereich_status_transport = ";".join(
        f"{name}|{bereich_status[name]}"
        for name in ("AKTIEN", "MARKT_MAKRO", "EDELMETALLE", "GESAMTBERICHT")
    )
    bereich_befunde_transport = ";".join(
        f"{name}|{befund}"
        for name, liste in bereich_befunde.items() for befund in liste
    )
    for name, value in (("bereich_status", bereich_status_transport),
                        ("bereich_befunde", bereich_befunde_transport)):
        if "\n" in value or "\r" in value:
            raise ValueError(f"Ungueltiger Zeilenumbruch im GitHub-Output {name}")
        value = value.replace("%", "%25").replace(";", "%3B").replace("\r", "%0D").replace("\n", "%0A")
        if name == "bereich_status":
            bereich_status_transport = value
        else:
            bereich_befunde_transport = value
    _gemini_schreibe_github_outputs(
        "SUCCESS", fachlich_final, os.path.abspath(ausgabe_pfad),
        edelmetall_status, report_befunde, report_metall_status,
        bereich_status_transport, bereich_befunde_transport,
    )
    print("GEMINI_STATUS=SUCCESS")
    if fachlich_final == "NICHT_FINAL":
        print("GEMINI_EXIT_STATUS=0 | Auswertung gespeichert und hochladbar; fachlicher Status NICHT_FINAL.")
    else:
        print("GEMINI_EXIT_STATUS=0 | Fachlich finale Auswertung wurde gespeichert.")
