import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Deine Ordner-ID: Kopiere sie aus der Adresszeile deines Drive-Ordners
FOLDER_ID = https://drive.google.com/drive/my-drive

def upload_file(filename):
    # Holt das JSON aus dem GitHub-Geheimnis
    creds_dict = json.loads(os.environ['GDRIVE_JSON'])
    creds = service_account.Credentials.from_service_account_info(creds_dict)
    service = build('drive', 'v3', credentials=creds)

    file_metadata = {
        'name': filename,
        'parents': [FOLDER_ID]
    }
    media = MediaFileUpload(filename, mimetype='text/csv')
    service.files().create(body=file_metadata, media_body=media, fields='id').execute()

if __name__ == "__main__":
    upload_file('Performance.csv')
    upload_file('Setups.csv')
    print("Dateien erfolgreich in Google Drive hochgeladen.")
