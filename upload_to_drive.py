from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import json
import os

# Arbeitsverzeichnis festlegen
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def get_drive_service():
    # Lädt den JSON-Inhalt direkt aus der Umgebungsvariable
    creds_info = json.loads(os.environ.get("SERVICE_ACCOUNT_JSON"))
    creds = service_account.Credentials.from_service_account_info(creds_info)
    # Erstellt den Drive-Service
    return build('drive', 'v3', credentials=creds)

def upload_file(filename, folder_id):
    service = get_drive_service()
    file_metadata = {'name': filename, 'parents': [folder_id]}
    media = MediaFileUpload(filename, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print(f"Datei '{filename}' erfolgreich hochgeladen. ID: {file.get('id')}")

if __name__ == '__main__':
    # HIER DEINE FOLDER-ID EINTRAGEN
    FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
    
    print("Suche nach neuen Dateien zum Hochladen...")
    found = False
    for filename in os.listdir('.'):
        # Scannt nach Performance, Setups (CSV) ODER Briefing (TXT)
        if (filename.startswith("Performance") or filename.startswith("Setups")) and filename.endswith(".csv"):
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID)
            found = True
        elif filename.startswith("Briefing") and filename.endswith(".txt"):
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID)
            found = True
            
    if not found:
        print("Keine passenden Dateien zum Hochladen gefunden.")
