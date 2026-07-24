"""
short_scan_catchup.py

NEU (24.07.2026): Nachhol-Mechanismus für main.yml. GitHub-Actions-Cron-
Läufe sind "best effort" - short_check.yml (01:53 UTC) kann an manchen
Tagen einfach nicht feuern (siehe 24.07.2026: Short-Scan lief an einem Tag
gar nicht, wurde erst spät bemerkt - GDW: kein Fehler im Code, sondern
GitHub-seitiges Scheduling-Verhalten).

Läuft in main.yml NACH dem Trendwende-Scan und VOR der Gemini-Auswertung:
prüft, ob die heutige Short_Setups(<Datum>).csv bereits in Drive liegt
(= früher short_check.yml-Lauf war erfolgreich). Falls ja: nichts zu tun.
Falls nein: holt short_scanner.py direkt hier nach, damit die Short-Daten
trotzdem noch in die heutige Gemini-Auswertung einfließen (liegen dann
lokal vor und werden zusätzlich vom regulären "Daten hochladen"-Schritt
am Ende von main.yml mit hochgeladen wie jede andere Setups-Datei).

Bei Unsicherheit (Drive nicht erreichbar, Token-Problem) wird im Zweifel
IMMER nachgeholt - ein doppelter Short-Scan an einem Tag ist unschädlich
(überschreibt nur dieselbe Datei), ein fehlender ist der eigentliche
Fehlerfall, den wir vermeiden wollen.
"""
import os
import json
import datetime
import subprocess
import sys

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'  # gleicher Ordner wie in upload_to_drive.py/gemini_auswertung.py


def get_drive_service():
    """Baut den Drive-Service auf (lesender Zugriff) - identische Auth-Logik
    wie in gemini_auswertung.py/upload_to_drive.py. Gibt None zurück (statt
    zu crashen), falls GDRIVE_TOKEN fehlt oder ungültig ist - der Aufrufer
    behandelt None als "sicherheitshalber nachholen"."""
    token_str = os.environ.get("GDRIVE_TOKEN")
    if not token_str:
        print("WARNUNG: GDRIVE_TOKEN nicht gesetzt - kann Drive nicht prüfen, hole Short-Scan sicherheitshalber nach.")
        return None
    try:
        token_data = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_data)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("WARNUNG: GDRIVE_TOKEN ungültig, kein Refresh möglich - hole Short-Scan sicherheitshalber nach.")
                return None
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"WARNUNG: Drive-Verbindung fehlgeschlagen ({e}) - hole Short-Scan sicherheitshalber nach.")
        return None


def heutige_short_setups_existieren(service):
    heute = datetime.date.today().isoformat()
    query = (
        f"name contains 'Short_Setups' and name contains '{heute}' "
        f"and '{DRIVE_FOLDER_ID}' in parents and trashed = false"
    )
    try:
        ergebnis = service.files().list(q=query, fields="files(id, name)").execute()
        treffer = ergebnis.get("files", [])
        return len(treffer) > 0
    except Exception as e:
        print(f"WARNUNG: Drive-Abfrage fehlgeschlagen ({e}) - hole Short-Scan sicherheitshalber nach.")
        return False


if __name__ == "__main__":
    service = get_drive_service()
    if service is not None and heutige_short_setups_existieren(service):
        print("Short_Setups von heute bereits in Drive vorhanden (früher Lauf war erfolgreich) - kein Nachholen nötig.")
        sys.exit(0)

    print("Keine heutige Short_Setups.csv in Drive gefunden - hole Short-Scan jetzt nach...")
    ergebnis = subprocess.run([sys.executable, "short_scanner.py"])
    sys.exit(ergebnis.returncode)
