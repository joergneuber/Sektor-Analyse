"""
hebeltrader_sync.py

Lädt den zuletzt erfolgreich verarbeiteten HEBELTRADER-Einzelcheck aus
Google Drive. Fehlt die Datei, wird bewusst NICHTS lokal erzeugt.
"""

from __future__ import annotations

import io
import json
import os

from googleapiclient.http import MediaIoBaseDownload

from upload_to_drive import get_drive_service

FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
DATEINAME = "hebeltrader_einzel_check.json"


def lade_aus_drive() -> bool:
    service = get_drive_service()
    query = (
        f"name = '{DATEINAME}' and '{FOLDER_ID}' in parents "
        "and trashed = false"
    )
    antwort = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id,name,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=10,
    ).execute()
    treffer = antwort.get("files", [])
    if not treffer:
        print(
            f"INFO: {DATEINAME} ist in Drive noch nicht vorhanden. "
            "Es wird bewusst keine lokale Ersatzdatei angelegt."
        )
        return False

    datei_id = treffer[0]["id"]
    request = service.files().get_media(fileId=datei_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    fertig = False
    while not fertig:
        _, fertig = downloader.next_chunk()

    daten = json.loads(buffer.getvalue().decode("utf-8-sig"))
    if not isinstance(daten, dict) or not daten.get("issue_number"):
        raise RuntimeError(
            f"{DATEINAME} aus Drive ist kein gültiger HEBELTRADER-Ergebnisstand."
        )

    temp = DATEINAME + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(temp, DATEINAME)

    print(
        f"{DATEINAME} aus Drive geladen: "
        f"{daten.get('issue_label')} | "
        f"{len(daten.get('candidates', []))} Kandidaten | "
        f"Drive-ID {datei_id}"
    )
    return True


if __name__ == "__main__":
    lade_aus_drive()
