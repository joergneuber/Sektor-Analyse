import os
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def get_drive_service():
    token_str = os.environ.get("GDRIVE_TOKEN")
    
    # Debug: Falls das Secret in GitHub nicht ankommt
    if not token_str:
        print("FEHLER: Umgebungsvariable GDRIVE_TOKEN nicht gefunden!")
        raise EnvironmentError("GDRIVE_TOKEN ist nicht gesetzt.")
        
    try:
        token_data = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_data)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"FEHLER beim Parsen des Tokens: {e}")
        raise

def get_drive_service():
    # Lädt den Token aus dem GitHub-Secret "GDRIVE_TOKEN"
    token_data = json.loads(os.environ.get("GDRIVE_TOKEN"))
    creds = Credentials.from_authorized_user_info(token_data)
    # Erstellt den Service
    return build('drive', 'v3', credentials=creds)

# Arbeitsverzeichnis festlegen
os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
