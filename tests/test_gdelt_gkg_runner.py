"""Isolierter GDELT-GKG/Bulk-Runner-Test.

Kein Produktionscode: prueft nur, ob ein GitHub-Runner den offiziellen GDELT-
Bulk-Datenstrom erreichen, eine aktuelle GKG-Datei laden/lesen und daraus
relevante Datensaetze der letzten 24h extrahieren kann.

Standard: 8 Stichproben ueber 24h (1 Datei alle 3h), damit der Test leichtgewichtig
bleibt. Mit --full werden alle 15-Minuten-Slices des 24h-Fensters geprueft.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import zipfile
from urllib.parse import urljoin

import requests

BASE_HTTPS = "https://data.gdeltproject.org/gdeltv2/"
BASE_HTTP = "http://data.gdeltproject.org/gdeltv2/"
LASTUPDATE = "lastupdate.txt"
TIMEOUT = 30
HEADERS = {"User-Agent": "NeuberMacro-GDELT-GKG-Test/1.0"}

CATEGORIES = {
    "Nahost": re.compile(r"\b(Iran|Israel|Middle East|Gaza|Lebanon|Syria|Yemen|Hormuz)\b", re.I),
    "China/Taiwan": re.compile(r"\b(China|Taiwan|Taiwan Strait|South China Sea|PLA)\b", re.I),
    "Russland/Ukraine": re.compile(r"\b(Russia|Ukraine|NATO|Crimea|Donbas)\b", re.I),
    "Handel/Sanktionen": re.compile(r"\b(tariff|tariffs|sanction|sanctions|export controls|trade war|embargo)\b", re.I),
    "Lieferketten/Schifffahrt": re.compile(r"\b(shipping|supply chain|Red Sea|Suez|Panama Canal|freight|container)\b", re.I),
    "Globale Markt-/Börsenrisiken": re.compile(r"\b(oil|brent|WTI|gas|market|stocks|equities|bond yields|dollar|risk[- ]off|risk[- ]on)\b", re.I),
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def get(url: str) -> requests.Response:
    return requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)


def parse_latest_gkg_url(text: str) -> str | None:
    # lastupdate.txt contains three dated GDELT V2 files and metadata lines.
    urls = re.findall(r"https?://data\.gdeltproject\.org/gdeltv2/\d{14}\.gkg\.csv\.zip", text)
    if not urls:
        # Be tolerant if the server returns paths without scheme.
        urls = re.findall(r"(?:https?://)?data\.gdeltproject\.org/gdeltv2/\d{14}\.gkg\.csv\.zip", text)
    return urls[0] if urls else None


def ts_from_gkg_url(url: str) -> dt.datetime:
    name = url.rsplit("/", 1)[-1]
    stamp = name[:14]
    return dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)


def build_slice_urls(latest: dt.datetime, full: bool) -> list[str]:
    step = dt.timedelta(minutes=15 if full else 180)
    count = 97 if full else 9
    urls = []
    cursor = latest
    for _ in range(count):
        stamp = cursor.strftime("%Y%m%d%H%M%S")
        urls.append(urljoin(BASE_HTTPS, f"{stamp}.gkg.csv.zip"))
        cursor -= step
    return list(dict.fromkeys(urls))


def inspect_gkg_zip(content: bytes, now_utc: dt.datetime) -> tuple[int, dict[str, int], int]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".gkg.csv")]
        if not names:
            raise ValueError("ZIP enthaelt keine .gkg.csv-Datei")
        with zf.open(names[0]) as raw:
            wrapper = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            reader = csv.reader(wrapper, delimiter="\t")
            rows = 0
            counts = {k: 0 for k in CATEGORIES}
            rows_with_timestamp = 0
            for row in reader:
                if not row or row[0] == "GKGRECORDID":
                    continue
                rows += 1
                if len(row) < 2:
                    continue
                # GKGDATE is normally YYYYMMDDHHMMSS near column 1; fall back
                # to the first 14-digit timestamp-looking field.
                stamp = next((c for c in row[:8] if re.fullmatch(r"\d{14}", c or "")), None)
                if stamp:
                    rows_with_timestamp += 1
                text = "\t".join(row[:20])
                for category, pattern in CATEGORIES.items():
                    if pattern.search(text):
                        counts[category] += 1
                if rows >= 5000:
                    break
    return rows, counts, rows_with_timestamp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="alle 15-Minuten-Slices des 24h-Fensters pruefen")
    args = parser.parse_args()

    print("GDELT GKG RUNNER TEST")
    print(f"Runner Python: {sys.version.split()[0]}")

    response = get(urljoin(BASE_HTTPS, LASTUPDATE))
    if response.status_code != 200:
        # Static file servers sometimes redirect/serve HTTP more reliably.
        response = get(urljoin(BASE_HTTP, LASTUPDATE))
    if response.status_code != 200:
        fail(f"lastupdate.txt nicht erreichbar: HTTP {response.status_code}")

    latest_url = parse_latest_gkg_url(response.text)
    if not latest_url:
        fail("lastupdate.txt enthaelt keinen erkennbaren aktuellen GKG-Downloadlink")

    latest = ts_from_gkg_url(latest_url)
    now_utc = dt.datetime.now(dt.timezone.utc)
    age = now_utc - latest
    print(f"PASS: lastupdate.txt erreichbar | latest_gkg={latest.isoformat()} | alter={age}")

    urls = build_slice_urls(latest, args.full)
    successful = 0
    total_rows = 0
    category_counts = {k: 0 for k in CATEGORIES}
    timestamps = 0

    for idx, url in enumerate(urls, 1):
        try:
            r = get(url)
            if r.status_code != 200:
                print(f"WARN: Slice {idx}/{len(urls)} HTTP {r.status_code}: {url}")
                continue
            rows, counts, ts_count = inspect_gkg_zip(r.content, now_utc)
            successful += 1
            total_rows += rows
            timestamps += ts_count
            for k, v in counts.items():
                category_counts[k] += v
            print(f"PASS: GKG {idx}/{len(urls)} | rows={rows} | {url.rsplit('/',1)[-1]}")
        except Exception as exc:
            print(f"WARN: Slice {idx}/{len(urls)} fehlgeschlagen: {type(exc).__name__}: {exc}")

    minimum = len(urls) if args.full else 5
    if successful < minimum:
        fail(f"zu wenige GKG-Slices erreichbar: {successful}/{len(urls)} (Minimum {minimum})")
    if total_rows <= 0:
        fail("keine GKG-Datensaetze gelesen")

    print(f"GDELT_24H_SAMPLE: slices={successful}/{len(urls)} | rows={total_rows} | timestamp_fields={timestamps}")
    for category, count in category_counts.items():
        print(f"CATEGORY {category}: matches={count}")
    print("GDELT_GKG_RUNNER_TEST: PASS")


if __name__ == "__main__":
    main()
