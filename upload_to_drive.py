import os
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

print(f"Aktuelles Arbeitsverzeichnis: {os.getcwd()}")
print("Gefundene Dateien im Ordner:")
print(os.listdir('.'))


def get_drive_service():
    """Baut den Drive-Service auf und sorgt dafür, dass der Access-Token
    aktuell ist - inklusive klarer Fehlermeldungen, falls das Refreshen
    fehlschlägt (z.B. weil das Secret unvollständig ist oder der Google-
    OAuth-Client noch im "Testing"-Status mit 7-Tage-Ablauf steht)."""
    token_str = os.environ.get("GDRIVE_TOKEN")

    if not token_str:
        print("FEHLER: Umgebungsvariable GDRIVE_TOKEN nicht gefunden!")
        raise EnvironmentError("GDRIVE_TOKEN ist nicht gesetzt.")

    try:
        token_data = json.loads(token_str)
    except Exception as e:
        print(f"FEHLER beim Parsen des Tokens (kein gültiges JSON): {e}")
        raise

    # Prüfen, ob alle für ein automatisches Refresh nötigen Felder vorhanden sind.
    # Fehlt eines davon, kann die Bibliothek einen abgelaufenen Access-Token
    # NICHT automatisch erneuern - das ist die häufigste Ursache dafür, dass
    # ein Upload manuell kurz nach dem Erzeugen des Tokens klappt, beim
    # automatischen Cron-Lauf Stunden später aber stillschweigend scheitert.
    required_fields = ["refresh_token", "client_id", "client_secret", "token_uri"]
    fehlende_felder = [f for f in required_fields if not token_data.get(f)]
    if fehlende_felder:
        print(f"FEHLER: GDRIVE_TOKEN fehlen folgende Felder für ein automatisches "
              f"Token-Refresh: {fehlende_felder}. Ohne diese kann der Token nach "
              f"Ablauf (Access-Token: ~1 Stunde) nicht erneuert werden.")
        raise EnvironmentError(f"GDRIVE_TOKEN unvollständig: {fehlende_felder} fehlen.")

    try:
        creds = Credentials.from_authorized_user_info(token_data)
    except Exception as e:
        print(f"FEHLER beim Erstellen der Credentials aus dem Token: {e}")
        raise

    # Access-Token aktiv erneuern, falls abgelaufen oder ungültig - statt
    # darauf zu vertrauen, dass die Bibliothek das beim ersten API-Call
    # automatisch und stillschweigend erledigt.
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            print("Access-Token abgelaufen - versuche Refresh...")
            try:
                creds.refresh(Request())
                print("Token-Refresh erfolgreich.")
            except Exception as e:
                print(f"FEHLER beim Token-Refresh: {e}")
                print("Mögliche Ursachen: Refresh-Token widerrufen/abgelaufen "
                      "(z.B. 7-Tage-Ablauf bei OAuth-Client im 'Testing'-Status "
                      "in der Google Cloud Console), falsche client_id/client_secret "
                      "im Secret, oder Google-Server-Problem.")
                raise
        else:
            print("FEHLER: Token ist ungültig und kann nicht automatisch erneuert "
                  "werden (kein Refresh-Token vorhanden oder anderer Grund).")
            raise EnvironmentError("GDRIVE_TOKEN: Credentials ungültig, kein Refresh möglich.")

    return build('drive', 'v3', credentials=creds)


# Arbeitsverzeichnis festlegen
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def upload_file(filename, folder_id, service):
    file_metadata = {'name': filename, 'parents': [folder_id]}
    media = MediaFileUpload(filename, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print(f"Datei '{filename}' erfolgreich hochgeladen. ID: {file.get('id')}")


if __name__ == '__main__':
    # HIER DEINE FOLDER-ID EINTRAGEN
    FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'

    # Service EINMAL aufbauen (inkl. Refresh-Check) statt bei jedem Upload neu -
    # spart unnötige Refresh-Versuche und macht Fehler früher sichtbar.
    drive_service = get_drive_service()

    print("Suche nach neuen Dateien zum Hochladen...")
    found = False
    for filename in os.listdir('.'):
        # Scannt nach Performance, Setups (CSV) ODER Briefing (TXT) - "in" statt
        # "startswith", damit auch Trendwende_Setups(...).csv und
        # Trendwende_Briefing(...).txt erfasst werden (eigener Scanner, eigene
        # Dateien, siehe trendwende_scanner.py).
        if ("Performance" in filename or "Setups" in filename or "Langfrist_Bewertung" in filename) and filename.endswith(".csv"):
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID, drive_service)
            found = True
        elif ("Briefing" in filename or "Auswertung" in filename) and filename.endswith(".txt"):
            # "Auswertung" (NEU): die von claude_auswertung.py erzeugte fertige
            # Daten-Übersicht, landet genau wie die anderen Text-Dateien direkt
            # im selben Drive-Ordner.
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID, drive_service)
            found = True

    if not found:
        print("Keine passenden Dateien zum Hochladen gefunden.")
