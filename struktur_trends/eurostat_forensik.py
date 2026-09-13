#!/usr/bin/env python3
"""
Eurostat-Forensik / Validierungslauf
------------------------------------
Isoliertes Prüfskript: ändert KEINE Projektdateien und benötigt keine
GitHub-Verbindung.

Prüft:
- nrg_cb_pem / nrg_cb_em Dataflow + Struktur-Metadaten
- DSD/Dimensionen
- Content Constraint
- gezielte Deutschland-/EU-Datenabfragen
- CSV/JSON/SDMX-Antworten
- letzten verfügbaren Monat
- OBS_FLAG / Missing Values
- konkrete Kandidaten für electricity_generation / electricity_balance

Ausführung:
    python eurostat_forensik.py

Optional:
    EUROSTAT_TIMEOUT=30 python eurostat_forensik.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from typing import Any


BASE = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/3.0"
TIMEOUT = int(os.getenv("EUROSTAT_TIMEOUT", "30"))

DATASETS = {
    "nrg_cb_pem": {
        "purpose": "electricity_generation",
        "description": "Net electricity generation by type of fuel - monthly data",
    },
    "nrg_cb_em": {
        "purpose": "electricity_balance",
        "description": "Supply, transformation and consumption of electricity - monthly data",
    },
}


@dataclass
class HttpResult:
    url: str
    status: int | None
    content_type: str
    body: bytes
    error: str | None = None


def fetch(url: str, accept: str = "*/*") -> HttpResult:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Sektor-Analyse-Eurostat-Forensik/1.0",
            "Accept": accept,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return HttpResult(
                url=url,
                status=response.status,
                content_type=response.headers.get("Content-Type", ""),
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return HttpResult(
            url=url,
            status=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body=body,
            error=str(exc),
        )
    except Exception as exc:
        return HttpResult(
            url=url,
            status=None,
            content_type="",
            body=b"",
            error=repr(exc),
        )


def print_result(label: str, result: HttpResult) -> None:
    print(f"\n[{label}]")
    print(f"URL: {result.url}")
    print(f"HTTP: {result.status}")
    print(f"Content-Type: {result.content_type}")
    if result.error:
        print(f"ERROR: {result.error}")
    print(f"Bytes: {len(result.body):,}")


def try_json(result: HttpResult) -> Any | None:
    if not result.body:
        return None
    try:
        return json.loads(result.body.decode("utf-8"))
    except Exception:
        return None


def text_head(result: HttpResult, n: int = 1200) -> str:
    try:
        return result.body.decode("utf-8", errors="replace")[:n]
    except Exception:
        return repr(result.body[:n])


def structure_url(dataset: str) -> str:
    return (
        f"{BASE}/structure/dataflow/ESTAT/{dataset}/1.0"
        "?references=descendants&detail=referencepartial&compress=false"
    )


def constraint_url(dataset: str) -> str:
    return (
        f"{BASE}/structure/dataconstraint/ESTAT/{dataset}/1.0"
        "?compress=false"
    )


def data_url(dataset: str, key: str = "*.*.*.*", geo: str | None = None) -> str:
    if geo:
        key = key.rstrip(".") + "." + geo
    params = urllib.parse.urlencode({"format": "csvdata", "formatVersion": "2.0"})
    return f"{BASE}/data/dataflow/ESTAT/{dataset}/1.0/{key}?{params}"


def analyse_structure(result: HttpResult) -> None:
    text = result.body.decode("utf-8", errors="replace")
    print("\n--- Struktur-Signaturen ---")
    patterns = [
        r'<s:Dataflow[^>]+id="([^"]+)"',
        r'<s:DataStructure[^>]+id="([^"]+)"',
        r'<s:Dimension[^>]+id="([^"]+)"',
        r'<s:DimensionList[^>]*>',
        r'<s:DataStructureComponents[^>]*>',
        r'<s:DataConstraint[^>]+id="([^"]+)"',
    ]
    for pattern in patterns:
        hits = re.findall(pattern, text)
        if hits:
            print(pattern, "=>", list(dict.fromkeys(hits))[:80])


def analyse_constraint(result: HttpResult) -> None:
    text = result.body.decode("utf-8", errors="replace")
    print("\n--- Constraint-Code-Signaturen ---")
    for dim in ("freq", "nrg_bal", "siec", "unit", "geo"):
        hits = re.findall(
            rf'<s:KeyValue[^>]+id="{dim}"[^>]*>(.*?)</s:KeyValue>',
            text,
            flags=re.DOTALL,
        )
        if hits:
            values = re.findall(r'<s:Value>(.*?)</s:Value>', hits[0])
            print(f"{dim}: {values[:100]}")
    for token in ("AIM", "E7000", "GWH", "DE", "EU27_2020", "M"):
        print(f"{token}: {'JA' if token in text else 'nein'}")


def analyse_csv(result: HttpResult) -> None:
    text = result.body.decode("utf-8", errors="replace")
    lines = text.splitlines()
    print("\n--- Datenantwort ---")
    if not lines:
        print("Keine Datenzeilen.")
        return
    print("Header:")
    print(lines[0][:1000])
    print(f"Anzahl Zeilen: {max(0, len(lines)-1):,}")
    print("Erste Datenzeilen:")
    for line in lines[1:6]:
        print(line[:1000])

    header = lines[0].split(",")
    idx = {name: i for i, name in enumerate(header)}
    for wanted in ("TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "OBS_FLAG", "freq", "geo", "unit"):
        if wanted in idx:
            print(f"Spalte {wanted}: vorhanden")

    time_idx = idx.get("TIME_PERIOD")
    if time_idx is not None:
        periods = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) > time_idx and parts[time_idx]:
                periods.append(parts[time_idx].strip('"'))
        if periods:
            print(f"Erster Zeitraum: {min(periods)}")
            print(f"Letzter Zeitraum: {max(periods)}")


def run() -> int:
    print("=" * 78)
    print("EUROSTAT FORENSIK / VALIDIERUNG")
    print("Isoliert | keine GitHub-Verbindung | kein Produktionscode")
    print(datetime.now().astimezone().isoformat())
    print("=" * 78)

    network_failed = False

    for dataset, meta in DATASETS.items():
        print("\n" + "#" * 78)
        print(f"{dataset} -> {meta['purpose']}")
        print(meta["description"])
        print("#" * 78)

        r = fetch(structure_url(dataset), "application/vnd.sdmx.structure+xml;version=3.0.0")
        print_result("DATAFLOW + DSD + CODELISTS", r)
        if r.status != 200:
            network_failed = True
        else:
            analyse_structure(r)

        r = fetch(constraint_url(dataset), "application/vnd.sdmx.structure+xml;version=3.0.0")
        print_result("CONTENT CONSTRAINT", r)
        if r.status != 200:
            network_failed = True
        else:
            analyse_constraint(r)

        # Deutschland: zunächst bewusst breit, damit die tatsächliche Struktur
        # aus der Antwort sichtbar wird. Danach kann ein exakter Key festgelegt werden.
        r = fetch(data_url(dataset, "*.*.*.*", "DE"), "text/csv")
        print_result("DATEN DE (breiter Test)", r)
        if r.status != 200:
            network_failed = True
        else:
            analyse_csv(r)

        # EU27_2020 separat testen.
        r = fetch(data_url(dataset, "*.*.*.*", "EU27_2020"), "text/csv")
        print_result("DATEN EU27_2020 (breiter Test)", r)
        if r.status != 200:
            network_failed = True
        else:
            analyse_csv(r)

    print("\n" + "=" * 78)
    if network_failed:
        print("ERGEBNIS: Mindestens ein Live-Endpunkt war nicht erfolgreich erreichbar.")
        print("Das ist ein Infrastruktur-/Netzwerktest, kein fachlicher Negativbefund.")
        print("Die Produktionsdateien wurden NICHT verändert.")
        return 2

    print("ERGEBNIS: Alle angeforderten Eurostat-Live-Tests waren erfolgreich.")
    print("Nächster Schritt: exakte Codes für Generation und Balance festlegen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
