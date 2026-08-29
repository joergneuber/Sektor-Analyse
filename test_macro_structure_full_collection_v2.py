#!/usr/bin/env python3
from __future__ import annotations

import ast
import csv
import json
import os
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

TARGET = Path("makro_szenario.py")
OUT = Path(".macro_structure_test")
OUT.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NeuberMacroStructureTest/2.0)",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

TE_URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
SP_URLS = {
    "public": "https://www.pmi.spglobal.com/Public?language=de",
    "releases": "https://www.pmi.spglobal.com/Public/Release/PressReleases?language=de",
}

# Fixed project structure: fields are classified for the test report only.
TIER1 = {
    "Fed Funds Effective Rate", "Fed Target Range Upper", "Fed Target Range Lower",
    "US 2Y Treasury", "US 10Y Treasury", "Realzins 10Y TIPS", "Core CPI",
    "Core PCE", "PPI", "Reales BIP", "Industrieproduktion",
    "Kapazitaetsauslastung", "NFP / Nonfarm Payrolls", "Arbeitslosenquote",
    "Initial Jobless Claims", "JOLTS Job Openings", "ISM Manufacturing PMI",
    "ISM Services PMI", "S&P 500", "VIX", "US High Yield OAS",
    "US Investment Grade OAS", "Chicago Fed NFCI",
}
TIER2 = {
    "CPI", "PCE", "Durchschnittlicher Stundenlohn", "M2", "US 5Y Treasury",
    "US 30Y Treasury", "ECB Deposit Facility Rate", "SLOOS C&I Tightening",
    "DXY", "Nasdaq Composite", "Russell 2000", "DAX", "EuroStoxx 50",
    "Nikkei 225", "EUR/USD", "USD/JPY", "WTI", "Brent", "Erdgas",
    "Gold", "Silber", "Platin", "Palladium", "Kupfer", "Aluminium", "Zink",
    "Nickel", "Blei", "Zinn", "Kobalt", "Eisenerz",
    "ISM Services Business Activity", "ISM Services Supplier Deliveries",
    "ISM Services Backlog of Orders", "ISM Services Inventories",
    "ISM Services Inventory Sentiment", "ISM Services Imports",
    "ISM Services Exports", "ISM Services New Export Orders",
    "S&P Global Services PMI", "S&P Global Services Business Activity",
    "S&P Global Services New Business", "S&P Global Services New Export Business",
    "S&P Global Services Employment", "S&P Global Services Outstanding Business",
    "S&P Global Services Input Prices", "S&P Global Services Prices Charged",
    "S&P Global Services Future Activity",
}
TIER3 = {
    "Consumer Sentiment", "Global Economic Policy Uncertainty",
    "US Federal Debt/GDP", "GSCPI", "Bitcoin", "Ethereum", "Lithium",
}

def tier(field):
    if field in TIER1: return "TIER1"
    if field in TIER2: return "TIER2"
    if field in TIER3: return "TIER3"
    return "UNASSIGNED"

def add(rows, source, field, value=None, reference=None, release=None,
        status="UNAVAILABLE", note=""):
    rows.append({
        "source": source, "field": field, "value": value,
        "reference": reference, "release": release, "status": status,
        "tier": tier(field), "note": note,
    })

def safe(label, fn, rows):
    try:
        fn()
    except Exception as exc:
        print(f"SAFE_ERROR={label}|{type(exc).__name__}|{exc}")
        traceback.print_exc(limit=1)
        add(rows, "TEST", label, status="ERROR",
            note=f"{type(exc).__name__}: {exc}")

def fetch(url, timeout=30):
    return requests.get(url, headers=HEADERS, timeout=timeout,
                        allow_redirects=True)

def clean_html(html):
    x = re.sub(r"<script.*?</script>|<style.*?</style>", " ",
               html, flags=re.I | re.S)
    x = re.sub(r"<[^>]+>", " ", x)
    return re.sub(r"\s+", " ", x).strip()

def num(x):
    if x is None:
        return None
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(x).replace(",", "."))
    return float(m.group(0)) if m else None

def collect_te(rows):
    print("=== TRADING ECONOMICS SERVICES ===")
    try:
        r = fetch(TE_URL)
        print(f"TE_STATUS={r.status_code}")
        print(f"TE_FINAL_URL={r.url}")
        (OUT / "te_services.html").write_text(r.text, encoding="utf-8")
        text = clean_html(r.text)

        specs = {
            "ISM Services PMI": ["Services PMI", "ISM Services PMI"],
            "ISM Services Business Activity": ["Business Activity"],
            "ISM Services New Orders": ["New Orders"],
            "ISM Services Employment": ["Employment"],
            "ISM Services Prices": ["Prices"],
            "ISM Services Supplier Deliveries": ["Supplier Deliveries"],
            "ISM Services Backlog of Orders": ["Backlog of Orders", "Backlog"],
            "ISM Services Inventories": ["Inventories"],
            "ISM Services Inventory Sentiment": ["Inventory Sentiment"],
            "ISM Services Imports": ["Imports"],
            "ISM Services Exports": ["Exports"],
            "ISM Services New Export Orders": ["New Export Orders"],
        }

        try:
            tables = pd.read_html(StringIO(r.text))
        except Exception as exc:
            tables = []
            print(f"TE_TABLE_PARSE=ERROR|{exc}")

        for field, needles in specs.items():
            found = any(n.casefold() in text.casefold() for n in needles)
            value = None
            ref = None

            if found:
                for table in tables:
                    df = table.astype(str)
                    table_text = df.to_string(index=False)
                    if not any(n.casefold() in table_text.casefold()
                               for n in needles):
                        continue
                    cols = [str(c).strip().casefold() for c in df.columns]
                    last_idx = next((i for i,c in enumerate(cols)
                                     if c == "last"), None)
                    if last_idx is None:
                        continue
                    for _, row in df.iterrows():
                        row_text = " | ".join(row.tolist())
                        if any(n.casefold() in row_text.casefold()
                               for n in needles):
                            value = num(row.iloc[last_idx])
                            m = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b",
                                          row_text)
                            ref = (f"{m.group(1)}-{int(m.group(2)):02d}"
                                   if m else None)
                            if value is not None:
                                break
                    if value is not None:
                        break

            status = ("REAL_PUBLIC" if value is not None else
                      "LABEL_FOUND_VALUE_UNRESOLVED" if found else "NOT_FOUND")
            add(rows, "TradingEconomics", field, value, ref,
                status=status,
                note="Numeric value accepted only from explicit Last column.")
    except Exception as exc:
        add(rows, "TradingEconomics", "COLLECTION",
            status="ERROR", note=f"{type(exc).__name__}: {exc}")

def collect_sp(rows):
    print("=== S&P GLOBAL PUBLIC SERVICES ===")
    combined = ""
    for name, url in SP_URLS.items():
        try:
            r = fetch(url)
            print(f"SP_{name.upper()}_STATUS={r.status_code}")
            print(f"SP_{name.upper()}_FINAL_URL={r.url}")
            (OUT / f"sp_{name}.html").write_text(r.text, encoding="utf-8")
            combined += "\n" + r.text
        except Exception as exc:
            add(rows, "S&P Global", f"PAGE {name}",
                status="ERROR", note=f"{type(exc).__name__}: {exc}")

    text = clean_html(combined)
    specs = {
        "S&P Global Services PMI": ["Services PMI"],
        "S&P Global Services Business Activity": ["Business Activity"],
        "S&P Global Services New Business": ["New Business"],
        "S&P Global Services New Export Business": ["New Export Business"],
        "S&P Global Services Employment": ["Employment"],
        "S&P Global Services Outstanding Business": ["Outstanding Business", "Backlog"],
        "S&P Global Services Input Prices": ["Input Prices"],
        "S&P Global Services Prices Charged": ["Prices Charged"],
        "S&P Global Services Future Activity": ["Future Activity", "Business Expectations"],
    }
    for field, needles in specs.items():
        count = sum(len(re.findall(re.escape(n), text, re.I)) for n in needles)
        add(rows, "S&P Global", field,
            status="LABEL_FOUND" if count else "NOT_FOUND",
            note=f"Public-page label occurrences={count}; value not guessed.")

def load_production_definitions():
    source = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fred = {}
    market = {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"FRED_SERIES", "MARKET_DATA"}:
                    try:
                        value = ast.literal_eval(node.value)
                        if target.id == "FRED_SERIES" and isinstance(value, dict):
                            fred = value
                        if target.id == "MARKET_DATA" and isinstance(value, dict):
                            market = value
                    except Exception:
                        pass
    return fred, market

def collect_fred(rows, fred_defs):
    print(f"=== FRED API DIRECT TEST ({len(fred_defs)} SERIES) ===")
    key = os.environ.get("FRED_API_KEY")
    print(f"FRED_API_KEY_PRESENT={'YES' if key else 'NO'}")
    if not key:
        for name, sid in fred_defs.items():
            add(rows, "FRED", name, status="CONFIG_MISSING",
                note=f"FRED_API_KEY missing; series={sid}")
        return

    def one(item):
        name, sid = item
        try:
            url = "https://api.stlouisfed.org/fred/series/observations"
            params = {
                "api_key": key, "file_type": "json", "series_id": sid,
                "sort_order": "desc", "limit": 1,
            }
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            if not obs:
                return name, sid, None, None, "NO_DATA", ""
            o = obs[0]
            value = num(o.get("value"))
            ref = o.get("date")
            if value is None:
                return name, sid, None, ref, "INVALID_VALUE", ""
            return name, sid, value, ref, "REAL_API", ""
        except Exception as exc:
            return name, sid, None, None, "ERROR", f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(fred_defs)))) as pool:
        futures = [pool.submit(one, item) for item in fred_defs.items()]
        for fut in as_completed(futures):
            name, sid, value, ref, status, note = fut.result()
            print(f"FRED_RESULT={name}|SERIES={sid}|STATUS={status}|VALUE={value}|DATE={ref}")
            add(rows, "FRED", name, value, ref, status=status,
                note=f"series={sid}|{note}")

def inspect_market_definitions(rows, market_defs):
    print(f"=== MARKET DEFINITIONS ({len(market_defs)}) ===")
    for name, definition in market_defs.items():
        try:
            ticker, dtype = definition
            add(rows, "MARKET_DEFINITION", name,
                value=ticker, status="DEFINED",
                note=f"data_type={dtype}")
        except Exception as exc:
            add(rows, "MARKET_DEFINITION", name,
                status="INVALID_DEFINITION", note=str(exc))

def write_reports(rows):
    seen = set()
    unique = []
    for row in rows:
        key = tuple(row.items())
        if key not in seen:
            seen.add(key)
            unique.append(row)

    (OUT / "macro_structure_results.json").write_text(
        json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (OUT / "macro_structure_results.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        w = csv.DictWriter(f, fieldnames=list(unique[0].keys()) if unique else
                           ["source","field","value","reference","release","status","tier","note"])
        w.writeheader()
        w.writerows(unique)

    status_counts = {}
    for r in unique:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    tier_counts = {}
    for r in unique:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"], 0) + 1

    lines = [
        "=== MACRO STRUCTURE FULL COLLECTION V2 ===",
        f"RUN_DATE={date.today().isoformat()}",
        f"RECORDS={len(unique)}",
        "",
        "STATUS_COUNTS:",
    ]
    lines += [f"{k}={v}" for k,v in sorted(status_counts.items())]
    lines += ["", "TIER_COUNTS:"]
    lines += [f"{k}={v}" for k,v in sorted(tier_counts.items())]
    lines += [
        "", "RULES:",
        "Previous is never accepted as Actual.",
        "Forecast/Consensus is never accepted as Actual.",
        "Missing data never aborts the test.",
        "Production file is read-only.",
        "",
        "FILES:",
        "macro_structure_results.json",
        "macro_structure_results.csv",
        "macro_structure_summary.txt",
    ]
    (OUT / "macro_structure_summary.txt").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print("\n".join(lines))

def main():
    print("=== MACRO STRUCTURE FULL COLLECTION V2 ===")
    print("MODE=ONE_RUN|NON_ABORTING|READ_ONLY_PRODUCTION")

    rows = []
    if not TARGET.exists():
        add(rows, "TEST", "makro_szenario.py",
            status="MISSING", note="Production file not found.")
    else:
        try:
            ast.parse(TARGET.read_text(encoding="utf-8"))
            print("TARGET_SYNTAX=GREEN")
        except Exception as exc:
            add(rows, "TEST", "makro_szenario.py",
                status="SYNTAX_ERROR", note=str(exc))

    fred_defs, market_defs = ({}, {})
    if TARGET.exists():
        try:
            fred_defs, market_defs = load_production_definitions()
            print(f"FRED_DEFINITIONS={len(fred_defs)}")
            print(f"MARKET_DEFINITIONS={len(market_defs)}")
        except Exception as exc:
            add(rows, "TEST", "production definitions",
                status="ERROR", note=str(exc))

    safe("TE_COLLECTION", lambda: collect_te(rows), rows)
    safe("SP_COLLECTION", lambda: collect_sp(rows), rows)
    safe("FRED_COLLECTION", lambda: collect_fred(rows, fred_defs), rows)
    safe("MARKET_DEFINITION_COLLECTION",
         lambda: inspect_market_definitions(rows, market_defs), rows)
    safe("REPORT_WRITER", lambda: write_reports(rows), rows)

    print("RESULT=COLLECTION_COMPLETE")
    print("EXIT_POLICY=0")
    return 0

if __name__ == "__main__":
    sys.exit(main())
