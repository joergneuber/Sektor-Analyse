import os
import json
import pickle
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Festlegen des Arbeitsverzeichnisses
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def get_drive_service():
    # Lädt das Token aus dem GitHub Secret
    token_json = os.getenv('GDRIVE_TOKEN')
    if not token_json:
        raise ValueError("Das Secret 'GDRIVE_TOKEN' wurde nicht gefunden.")
    
    # Erstellt die Credentials aus dem JSON-Inhalt
    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    return build('drive', 'v3', credentials=creds)

def upload_file(filename, folder_id):
    service = get_drive_service()
    
    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }
    
    media = MediaFileUpload(filename, resumable=True)
    
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()
    
    print(f"Datei '{filename}' erfolgreich hochgeladen. ID: {file.get('id')}")

if __name__ == '__main__':
    # HIER: Deine FOLDER_ID eintragen
    FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL' 
    
    # Beispielaufruf für eine Datei, die analysiert wurde
    # upload_file('deine_datei.csv', FOLDER_ID)
