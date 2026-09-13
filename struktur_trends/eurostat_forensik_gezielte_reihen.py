#!/usr/bin/env python3
"""
Eurostat-Forensik / Validierungslauf
------------------------------------
Isoliertes Prüfskript: ändert KEINE Projektdateien und benötigt keine
GitHub-Verbindung.

Finaler Prüfumfang:
- nrg_cb_pem: Net electricity generation / TOTAL / GWH
- nrg_cb_em: alle sieben relevanten nrg_bal-Flows für Strom
  IMP, IMP_FROM_EU, EXP, EXP_TO_EU, TI_EHG_EPS, DL, AIM
- jeweils Deutschland und EU27_2020
- DSD/Dimensionen und Content Constraint
- gezielte SDMX-CSV-Reihen
- Zeitreihen-Vollständigkeit und letzter verfügbarer Monat
- numerische Werte, Missing Values und OBS_STATUS/OBS_FLAG
- Dubletten je TIME_PERIOD
- einfache fachliche Konsistenzprüfungen der Handels-/EU-Handelsflüsse
- keine Produktionsänderung

Die endgültige Produktionsentscheidung wird NICHT durch dieses Skript
automatisch getroffen. Es liefert den forensischen Befund für die
anschließende Umstellung von IEA auf Eurostat.
"""

from __future__ import annotations

import csv
import io
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

BALANCE_FLOWS = [
    ("IMP", "Importe gesamt"),
    ("IMP_FROM_EU", "Importe aus EU"),
    ("EXP", "Exporte gesamt"),
    ("EXP_TO_EU", "Exporte in EU"),
    ("TI_EHG_EPS", "Transmission / electricity system flow"),
    ("DL", "Distribution losses"),
    ("AIM", "Available to internal market"),
]

TARGETS = {
    "nrg_cb_pem": [
        ("DE / TOTAL / GWH", "M.TOTAL.GWH.DE"),
        ("EU27_2020 / TOTAL / GWH", "M.TOTAL.GWH.EU27_2020"),
    ],
    "nrg_cb_em": [
        *(
            (f"DE / {code} / E7000 / GWH", f"M.{code}.E7000.GWH.DE")
            for code, _ in BALANCE_FLOWS
        ),
        *(
            (f"EU27_2020 / {code} / E7000 / GWH", f"M.{code}.E7000.GWH.EU27_2020")
            for code, _ in BALANCE_FLOWS
        ),
    ],
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
            "User-Agent": "Sektor-Analyse-Eurostat-Forensik/1.1",
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


def data_url(dataset: str, key: str) -> str:
    params = urllib.parse.urlencode({
        "format": "csvdata",
        "formatVersion": "2.0",
        "compress": "false",
    })
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
            values = re.findall(r"<s:Value>(.*?)</s:Value>", hits[0])
            print(f"{dim}: {values[:100]}")
    for token in (
        "IMP", "IMP_FROM_EU", "EXP", "EXP_TO_EU",
        "TI_EHG_EPS", "DL", "AIM", "E7000", "GWH",
        "DE", "EU27_2020", "M",
    ):
        print(f"{token}: {'JA' if token in text else 'nein'}")


def _parse_csv(result: HttpResult) -> tuple[list[str], list[dict[str, str]]]:
    text = result.body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], []
    rows = list(reader)
    return list(reader.fieldnames), rows


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value in {":", "NA", "N/A"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def analyse_csv(result: HttpResult, label: str) -> dict[str, Any]:
    print("\n--- Datenantwort ---")
    if result.status != 200:
        print("Keine erfolgreiche Datenantwort.")
        return {"ok": False, "label": label}

    header, rows = _parse_csv(result)
    print(f"Spalten: {', '.join(header)}")
    print(f"Anzahl Beobachtungen: {len(rows):,}")

    required = {"TIME_PERIOD", "OBS_VALUE"}
    missing_columns = sorted(required - set(header))
    if missing_columns:
        print(f"FEHLENDE PFLICHTSPALTEN: {missing_columns}")
        return {"ok": False, "label": label, "missing_columns": missing_columns}

    periods = [r.get("TIME_PERIOD", "").strip() for r in rows if r.get("TIME_PERIOD")]
    unique_periods = sorted(set(periods))
    duplicates = sorted(p for p in set(periods) if periods.count(p) > 1)

    numeric = [_as_float(r.get("OBS_VALUE")) for r in rows]
    missing_values = sum(v is None for v in numeric)
    flags = {}
    for flag_col in ("OBS_STATUS", "OBS_FLAG"):
        if flag_col in header:
            flags[flag_col] = {}
            for row in rows:
                value = (row.get(flag_col) or "").strip()
                if value:
                    flags[flag_col][value] = flags[flag_col].get(value, 0) + 1

    print(f"Erster Zeitraum: {unique_periods[0] if unique_periods else '<none>'}")
    print(f"Letzter Zeitraum: {unique_periods[-1] if unique_periods else '<none>'}")
    print(f"Eindeutige Monate: {len(unique_periods):,}")
    print(f"Dubletten TIME_PERIOD: {len(duplicates):,}")
    if duplicates:
        print(f"  Dubletten: {duplicates[:20]}")
    print(f"Fehlende/nichtnumerische OBS_VALUE: {missing_values:,}")
    if flags:
        print(f"OBS-Flags: {json.dumps(flags, ensure_ascii=False, sort_keys=True)}")

    if rows:
        print("Letzte 3 Beobachtungen:")
        for row in rows[-3:]:
            print(
                "  "
                + " | ".join(
                    f"{key}={row.get(key, '')}"
                    for key in ("TIME_PERIOD", "OBS_VALUE", "OBS_STATUS", "OBS_FLAG")
                    if key in header
                )
            )

    return {
        "ok": True,
        "label": label,
        "rows": rows,
        "periods": unique_periods,
        "duplicates": duplicates,
        "missing_values": missing_values,
        "flags": flags,
    }


def run() -> int:
    print("=" * 90)
    print("EUROSTAT FORENSIK / FINALER BALANCE-VALIDIERUNGSTEST")
    print("Isoliert | keine GitHub-Verbindung | kein Produktionscode")
    print(datetime.now().astimezone().isoformat())
    print("=" * 90)

    network_failed = False
    results: dict[str, dict[str, dict[str, Any]]] = {}

    for dataset, meta in DATASETS.items():
        print("\n" + "#" * 90)
        print(f"{dataset} -> {meta['purpose']}")
        print(meta["description"])
        print("#" * 90)

        r = fetch(
            structure_url(dataset),
            "application/vnd.sdmx.structure+xml;version=3.0.0",
        )
        print_result("DATAFLOW + DSD + CODELISTS", r)
        if r.status != 200:
            network_failed = True
        else:
            analyse_structure(r)

        r = fetch(
            constraint_url(dataset),
            "application/vnd.sdmx.structure+xml;version=3.0.0",
        )
        print_result("CONTENT CONSTRAINT", r)
        if r.status != 200:
            network_failed = True
        else:
            analyse_constraint(r)

        results[dataset] = {}
        for label, key in TARGETS[dataset]:
            # Offizielles SDMX-CSV-2.0-MIME-Type. text/csv führte im
            # vorherigen GitHub-Lauf bei den Datenabfragen zu HTTP 406.
            accept = "application/vnd.sdmx.data+csv;version=2.0.0"
            r = fetch(data_url(dataset, key), accept)
            print_result(f"GEZIELTE REIHE: {label}", r)
            if r.status != 200:
                network_failed = True
                results[dataset][label] = {"ok": False, "key": key}
            else:
                results[dataset][label] = analyse_csv(r, label)
                results[dataset][label]["key"] = key

    # ------------------------------------------------------------------
    # Fachliche Konsistenz der sieben nrg_cb_em-Flows:
    # Wir behaupten bewusst KEINE nicht dokumentierte Bilanzgleichung.
    # Geprüft werden nur sichere Relationen:
    # - EU-Teilflüsse dürfen nicht grösser sein als die jeweiligen
    #   Gesamtflüsse im absoluten Wert.
    # - alle sieben Reihen müssen denselben Monatsbereich abdecken.
    # ------------------------------------------------------------------
    print("\n" + "#" * 90)
    print("FACHLICHE KONSISTENZPRÜFUNG nrg_cb_em")
    print("#" * 90)

    balance_by_geo: dict[str, dict[str, dict[str, Any]]] = {}
    for label, result in results.get("nrg_cb_em", {}).items():
        if not result.get("ok"):
            continue
        geo = "DE" if label.startswith("DE /") else "EU27_2020"
        flow = label.split(" / ")[1]
        balance_by_geo.setdefault(geo, {})[flow] = result

    consistency_failed = False

    for geo, flows in balance_by_geo.items():
        print(f"\n[{geo}]")
        period_sets = {
            flow: set(data.get("periods", []))
            for flow, data in flows.items()
        }
        if period_sets:
            common = set.intersection(*period_sets.values())
            union = set.union(*period_sets.values())
            print(f"Gemeinsamer Monatsbereich: {min(common) if common else '<none>'} "
                  f"bis {max(common) if common else '<none>'}")
            if common != union:
                print(
                    "WARNUNG: Nicht alle sieben Flows decken exakt denselben "
                    "Monatsbereich ab."
                )
                consistency_failed = True
            else:
                print("OK: Alle sieben Flows haben denselben Monatsbestand.")

        def values(flow: str) -> dict[str, float]:
            data = flows.get(flow, {})
            return {
                row["TIME_PERIOD"]: _as_float(row.get("OBS_VALUE"))
                for row in data.get("rows", [])
                if row.get("TIME_PERIOD") and _as_float(row.get("OBS_VALUE")) is not None
            }

        imp = values("IMP")
        imp_eu = values("IMP_FROM_EU")
        exp = values("EXP")
        exp_eu = values("EXP_TO_EU")

        imp_violations = [
            p for p in set(imp) & set(imp_eu)
            if abs(imp_eu[p]) > abs(imp[p]) + 1e-9
        ]
        exp_violations = [
            p for p in set(exp) & set(exp_eu)
            if abs(exp_eu[p]) > abs(exp[p]) + 1e-9
        ]

        print(
            f"IMP_FROM_EU > IMP (Betrag): {len(imp_violations)} "
            f"Verletzungen"
        )
        print(
            f"EXP_TO_EU > EXP (Betrag): {len(exp_violations)} "
            f"Verletzungen"
        )

        if imp_violations:
            print(f"  Beispiele IMP: {imp_violations[:10]}")
            consistency_failed = True
        if exp_violations:
            print(f"  Beispiele EXP: {exp_violations[:10]}")
            consistency_failed = True

    print("\n" + "=" * 90)
    if network_failed:
        print("ERGEBNIS: Mindestens ein Live-Endpunkt war nicht erfolgreich erreichbar.")
        print("Das ist ein Infrastruktur-/Netzwerktest, kein fachlicher Negativbefund.")
        print("Die Produktionsdateien wurden NICHT verändert.")
        return 2

    if consistency_failed:
        print("ERGEBNIS: Alle Reihen wurden abgerufen, aber es gibt Konsistenz-WARNUNGEN.")
        print("Die Produktionsdateien wurden NICHT verändert.")
        return 3

    print("ERGEBNIS: Alle angeforderten Eurostat-Live-Tests waren erfolgreich.")
    print("ERGEBNIS: nrg_cb_pem + alle sieben nrg_cb_em-Flows wurden geprüft.")
    print("ERGEBNIS: DE und EU27_2020, Zeiträume, Missing Values und Flags geprüft.")
    print("ERGEBNIS: Keine sichere Flow-Konsistenzverletzung festgestellt.")
    print("Die Produktionsdateien wurden NICHT verändert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
