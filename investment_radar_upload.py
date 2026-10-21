#!/usr/bin/env python3
"""Upload the dated Investment Radar snapshot to its configured Google Drive folder."""
from pathlib import Path
import argparse
from googleapiclient.http import MediaFileUpload
from upload_to_drive import get_drive_service
from investment_radar_drive import DRIVE_FOLDER_ID

def upload_report(path: Path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Radar-Bericht fehlt oder ist leer: {path}")
    service=get_drive_service()
    metadata={"name":path.name,"parents":[DRIVE_FOLDER_ID],"description":"Datiertes Investment-Radar-Tagesprotokoll"}
    created=service.files().create(body=metadata,media_body=MediaFileUpload(str(path),mimetype="text/plain",resumable=True),fields="id,name").execute(num_retries=3)
    if not created.get("id"): raise RuntimeError("Google Drive bestätigte keinen Radar-Upload.")
    print(f"RADAR_DRIVE_UPLOAD_SUCCESS | name={created.get('name')} | id={created['id']}")
if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--report",required=True)
    upload_report(Path(ap.parse_args().report))
