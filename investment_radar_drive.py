#!/usr/bin/env python3
"""Download newest existing Investment-Radar input files from Google Drive.
Uses the existing upload_to_drive.get_drive_service() OAuth implementation.
Does not run scanners, market-data calls, Gemini, or the main workflow.
"""
from __future__ import annotations
import fnmatch
from pathlib import Path
from googleapiclient.http import MediaIoBaseDownload
from upload_to_drive import get_drive_service

DRIVE_FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
INPUT_PATTERNS = (
    "Trade_Story_Universum(*).json", "Setups(*).csv",
    "Edelmetalle_Setups(*).csv", "Trendwende_Setups(*).csv",
    "Short_Setups(*).csv", "Performance(*).csv", "Performance_EU(*).csv",
    "Makro_Briefing(*).txt", "Struktur_Trend_Briefing(*).txt",
    "Trade_Story_Bitcoin(*).json",
)

def download_latest_inputs(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    service = get_drive_service()
    response = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false",
        spaces="drive", fields="files(id,name,mimeType,modifiedTime)",
        pageSize=1000, orderBy="modifiedTime desc",
    ).execute()
    files = response.get("files", [])
    selected = {}
    for pattern in INPUT_PATTERNS:
        matches = [f for f in files if fnmatch.fnmatch(f.get("name", ""), pattern)
                  and f.get("mimeType") != "application/vnd.google-apps.folder"]
        if matches:
            matches.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
            selected[pattern] = matches[0]
    if not selected:
        raise RuntimeError("Keine Radar-Eingabedateien im konfigurierten Google-Drive-Ordner gefunden.")

    downloaded = []
    for pattern, item in selected.items():
        target = destination / item["name"]
        request = service.files().get_media(fileId=item["id"])
        with target.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        if target.stat().st_size == 0:
            raise RuntimeError(f"Google-Drive-Datei wurde leer heruntergeladen: {item['name']}")
        print(f"RADAR_INPUT | {item['name']} | modified={item.get('modifiedTime')} | bytes={target.stat().st_size}")
        downloaded.append(target)
    print(f"RADAR_INPUT_COUNT={len(downloaded)}")
    return downloaded

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="investment_radar_input")
    args = parser.parse_args()
    download_latest_inputs(Path(args.output_dir))
