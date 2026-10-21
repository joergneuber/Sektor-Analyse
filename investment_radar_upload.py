#!/usr/bin/env python3
"""Create or replace the dated Investment Radar report in its Google Drive folder."""
from pathlib import Path
import argparse

from googleapiclient.http import MediaFileUpload
from upload_to_drive import get_drive_service
from investment_radar_drive import DRIVE_FOLDER_ID


def upload_report(path: Path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Radar-Bericht fehlt oder ist leer: {path}")

    service = get_drive_service()
    # Restrict lookup to the configured folder: same-named files elsewhere are untouched.
    escaped_name = path.name.replace("'", "\\'")
    matches = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"name = '{escaped_name}' and '{DRIVE_FOLDER_ID}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken,files(id,name,modifiedTime)",
            pageSize=1000,
            orderBy="modifiedTime desc",
            pageToken=page_token,
        ).execute(num_retries=3)
        matches.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    media = MediaFileUpload(str(path), mimetype="text/plain", resumable=True)

    if matches:
        # Update the existing daily report rather than creating another same-day file.
        target = matches[0]
        result = service.files().update(
            fileId=target["id"],
            media_body=media,
            fields="id,name",
        ).execute(num_retries=3)
        if not result.get("id") or result.get("name") != path.name:
            raise RuntimeError(
                "Google Drive bestätigte das Update nicht eindeutig; "
                "Duplikate bleiben unangetastet."
            )
        # Only clean legacy duplicates after the primary update is confirmed.
        cleanup_errors = []
        for duplicate in matches[1:]:
            try:
                service.files().delete(fileId=duplicate["id"]).execute(num_retries=3)
            except Exception as exc:
                cleanup_errors.append(f"{duplicate.get('id')}: {exc}")
        action = "REPLACED"
        if cleanup_errors:
            print(
                "RADAR_DRIVE_DUPLICATE_CLEANUP_INCOMPLETE | "
                + " | ".join(cleanup_errors)
            )
    else:
        metadata = {
            "name": path.name,
            "parents": [DRIVE_FOLDER_ID],
            "description": "Datiertes Investment-Radar-Tagesprotokoll",
        }
        result = service.files().create(
            body=metadata,
            media_body=media,
            fields="id,name",
        ).execute(num_retries=3)
        action = "CREATED"

    if not result.get("id"):
        raise RuntimeError("Google Drive bestätigte keinen Radar-Upload.")
    print(f"RADAR_DRIVE_UPLOAD_SUCCESS | action={action} | name={result.get('name')} | id={result['id']}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    upload_report(Path(ap.parse_args().report))
