import os
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Arbeitsverzeichnis festlegen
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def get_drive_service():
    token_json = os.getenv('GDRIVE_TOKEN')
    if not token_json:
        raise ValueError("Das Secret 'GDRIVE_TOKEN' wurde nicht gefunden.")
    creds = Credentials.from_authorized_user_info(json.loads(token_json))
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
        # Scannt nach allen Dateien, die Performance oder Setups enthalten
        if (filename.startswith("Performance") or filename.startswith("Setups")) and filename.endswith(".csv"):
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID)
            found = True
            
    if not found:
        print("Keine passenden Dateien zum Hochladen gefunden.")
