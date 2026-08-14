"""
beobachtungsliste_sync.py

Lädt die persistente Einzel-Check-Beobachtungsliste aus Google Drive in das
Arbeitsverzeichnis des aktuellen GitHub-Actions-Laufs.

Die Datei ist bewusst eine kleine Synchronisationsstufe vor dem täglichen
Einzel-Check. So kann einzel_check.py die bereits beobachteten B/C-Titel jeden
Tag erneut prüfen, obwohl GitHub Actions mit einem frischen Checkout startet.

Wenn die Datei in Drive noch nicht existiert, wird lokal eine leere JSON-Datei
angelegt. Bei einem echten Drive-/Authentifizierungsfehler bricht das Skript
bewusst ab: Die bestehende Watchlist darf nicht versehentlich durch eine leere
Liste ersetzt werden.
"""

import io
import json
import os

from googleapiclient.http import MediaIoBaseDownload

from upload_to_drive import get_drive_service


FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
DATEINAME = "einzel_check_beobachtung.json"


def lade_aus_drive():
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
        with open(DATEINAME, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(
            f"INFO: {DATEINAME} wurde in Drive noch nicht gefunden - "
            "leere Beobachtungsliste lokal angelegt."
        )
        return

    datei_id = treffer[0]["id"]
    request = service.files().get_media(fileId=datei_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    fertig = False
    while not fertig:
        _, fertig = downloader.next_chunk()

    inhalt = buffer.getvalue().decode("utf-8-sig")

    try:
        daten = json.loads(inhalt)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{DATEINAME} in Drive enthält kein gültiges JSON: {e}"
        ) from e

    if not isinstance(daten, dict):
        raise RuntimeError(
            f"{DATEINAME} in Drive ist kein JSON-Objekt. "
            "Watchlist wird deshalb nicht lokal überschrieben."
        )

    temp = DATEINAME + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(temp, DATEINAME)

    print(
        f"{DATEINAME} aus Drive geladen: "
        f"{len(daten)} beobachtete Titel "
        f"(Drive-ID {datei_id})."
    )


if __name__ == "__main__":
    print("Synchronisiere Einzel-Check-Beobachtungsliste aus Drive...")
    lade_aus_drive()
