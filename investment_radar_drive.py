#!/usr/bin/env python3
"""Download newest existing Investment-Radar input files from Google Drive.
Uses the existing upload_to_drive.get_drive_service() OAuth implementation.
Does not run scanners, market-data calls, Gemini, or the main workflow.
"""
from __future__ import annotations
import fnmatch
import re
from datetime import date
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

def download_latest_inputs(destination: Path, report_date: date | None = None) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    service = get_drive_service()
    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken,files(id,name,mimeType,modifiedTime)",
            pageSize=1000,
            orderBy="modifiedTime desc",
            pageToken=page_token,
        ).execute(num_retries=3)
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    selected = {}
    for pattern in INPUT_PATTERNS:
        matches = [f for f in files if fnmatch.fnmatch(f.get("name", ""), pattern)
                  and f.get("mimeType") != "application/vnd.google-apps.folder"]
        if matches:
            matches.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
            selected[pattern] = matches[0]
    if not selected:
        raise RuntimeError("Keine Radar-Eingabedateien im konfigurierten Google-Drive-Ordner gefunden.")

    # Download the latest report strictly before this run date as comparison baseline.
    report_date = report_date or date.today()
    report_re = re.compile(r"^Investment_Radar_(\d{4}-\d{2}-\d{2})\.txt$")
    report_files = []
    for item in files:
        match = report_re.match(item.get("name", ""))
        if match and match.group(1) < report_date.isoformat() and item.get("mimeType") != "application/vnd.google-apps.folder":
            report_files.append((match.group(1), item))
    if report_files:
        report_files.sort(key=lambda pair: pair[0], reverse=True)
        selected["previous_radar_report"] = report_files[0][1]

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
        if pattern == "previous_radar_report":
            report_text = target.read_text(encoding="utf-8", errors="replace")
            if not report_text.strip():
                raise RuntimeError(f"Vortagsbericht ist leer: {item['name']}")
            if not re.search(r"(?m)^\s*2\.\s*AUSSICHTSREICHE TRADECHANCEN\s*$", report_text):
                raise RuntimeError(
                    f"Vortagsbericht hat keine erwartete Kandidaten-Sektion: {item['name']}"
                )
        print(f"RADAR_INPUT | {item['name']} | modified={item.get('modifiedTime')} | bytes={target.stat().st_size}")
        downloaded.append(target)
    print(f"RADAR_INPUT_COUNT={len(downloaded)}")
    return downloaded

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="investment_radar_input")
    parser.add_argument("--report-date", help="Radar-Datum YYYY-MM-DD (standardmäßig heute)")
    args = parser.parse_args()
    parsed_date = date.fromisoformat(args.report_date) if args.report_date else date.today()
    download_latest_inputs(Path(args.output_dir), report_date=parsed_date)
