"""
langfrist_scan_catchup.py

Analog zu short_scan_catchup.py: Nachhol-Mechanismus für main.yml.
langfrist_check.yml (montags 02:50 UTC, eigener Workflow) ist wie jeder
GitHub-Actions-Cron "best effort" und kann ausfallen (beobachtet am
27.07.2026 - laut Verlauf lief der Workflow 7 Tage lang nicht, obwohl der
Cron wöchentlich montags steht).

Anders als beim TÄGLICHEN Short-Scan ist die Prüfung hier WÖCHENTLICH:
geprüft wird, ob seit dem Montag dieser Woche schon eine
Langfrist_Bewertung-Datei in Drive liegt (unabhängig vom genauen Datum im
Dateinamen - ein Nachhol-Lauf am Mittwoch erzeugt z.B.
Langfrist_Bewertung(2026-07-29).csv, trotzdem ist die Woche damit erledigt).

ZEITLICHE REIHENFOLGE (GEÄNDERT 27.07.2026): langfrist_check.yml wurde
bewusst auf 02:50 UTC vorgezogen - deutlich VOR main.yml (03:17 UTC). Der
frühere Montags-Sonderfall (Nachhol-Prüfung am Montag selbst überspringen,
da main.yml sonst vor dem eigenen Cron gelaufen wäre) entfällt dadurch: mit
27 Minuten Vorlauf bis main.yml überhaupt startet, plus der Laufzeit von
positionen_tracker.py/analyse.py/trendwende_scanner.py/
edelmetalle_scanner.py/short_scan_catchup.py davor, ist der reguläre
Montags-Lauf beim Erreichen dieses Schritts erfahrungsgemäß längst
abgeschlossen - die Prüfung läuft daher jetzt an JEDEM Tag (Mo-Fr)
einheitlich gegen Drive, ohne Sonderfall.

Bei Unsicherheit (Drive nicht erreichbar, Token-Problem) wird im Zweifel
IMMER nachgeholt - ein zusätzlicher Langfrist-Lauf in derselben Woche ist
unschädlich (KGV/Fundamentaldaten ändern sich ohnehin nur langsam), ein
komplett fehlender Lauf ist der eigentliche Fehlerfall, den wir vermeiden
wollen.
"""
import os
import json
import datetime
import subprocess
import sys

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'  # gleicher Ordner wie bei short_scan_catchup.py


def get_drive_service():
    """Identisch zu short_scan_catchup.py - siehe dort für Begründung."""
    token_str = os.environ.get("GDRIVE_TOKEN")
    if not token_str:
        print("WARNUNG: GDRIVE_TOKEN nicht gesetzt - kann Drive nicht prüfen, hole Langfrist-Scan sicherheitshalber nach.")
        return None
    try:
        token_data = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_data)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("WARNUNG: GDRIVE_TOKEN ungültig, kein Refresh möglich - hole Langfrist-Scan sicherheitshalber nach.")
                return None
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"WARNUNG: Drive-Verbindung fehlgeschlagen ({e}) - hole Langfrist-Scan sicherheitshalber nach.")
        return None


def diese_woche_montag():
    heute = datetime.date.today()
    return heute - datetime.timedelta(days=heute.weekday())  # weekday(): Montag=0


def langfrist_bewertung_diese_woche_existiert(service):
    """Prüft per modifiedTime (nicht per Datum im Dateinamen - robuster,
    da ein Nachhol-Lauf ein anderes Datum als Montag trägt), ob seit
    Montag dieser Woche schon eine Langfrist_Bewertung-Datei in Drive
    angelegt/geändert wurde."""
    montag = diese_woche_montag()
    query = (
        f"name contains 'Langfrist_Bewertung' "
        f"and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
    )
    try:
        ergebnis = service.files().list(
            q=query, fields="files(id, name, modifiedTime)"
        ).execute()
        treffer = ergebnis.get("files", [])
        for f in treffer:
            geaendert = datetime.datetime.fromisoformat(
                f["modifiedTime"].replace("Z", "+00:00")
            ).date()
            if geaendert >= montag:
                print(f"DEBUG: {f['name']} (geändert {geaendert}) liegt in dieser Woche (seit Montag {montag}).")
                return True
        return False
    except Exception as e:
        print(f"WARNUNG: Drive-Abfrage fehlgeschlagen ({e}) - hole Langfrist-Scan sicherheitshalber nach.")
        return False


if __name__ == "__main__":
    service = get_drive_service()
    if service is not None and langfrist_bewertung_diese_woche_existiert(service):
        print("Langfrist_Bewertung dieser Woche bereits in Drive vorhanden (regulärer Montags-Lauf war erfolgreich) - kein Nachholen nötig.")
        sys.exit(0)

    print("Keine Langfrist_Bewertung dieser Woche in Drive gefunden - hole Langfrist-Scan jetzt nach...")
    ergebnis = subprocess.run([sys.executable, "langfrist_scanner.py"])
    sys.exit(ergebnis.returncode)