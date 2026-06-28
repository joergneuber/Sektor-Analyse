import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Konfiguration
FOLDER_ID = 1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def upload_file(filename):
    # JSON-Creds aus dem Environment-Secret lesen
    creds_json = os.getenv('GDRIVE_JSON')
    if not creds_json:
        raise Exception("GDRIVE_CREDENTIALS Secret nicht gefunden!")
    
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    service = build('drive', 'v3', credentials=creds)

    file_metadata = {'name': filename, 'parents': [FOLDER_ID]}
    media = MediaFileUpload(filename, mimetype='text/csv')
    
    service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print(f"Erfolgreich hochgeladen: {filename}")

# Dateien hochladen
if __name__ == "__main__":
    for f in ['Performance.csv', 'Setups.csv']:
        if os.path.exists(f):
            upload_file(f)
        else:
            print(f"Datei nicht gefunden: {f}")
