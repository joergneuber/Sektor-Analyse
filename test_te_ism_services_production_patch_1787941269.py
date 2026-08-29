#!/usr/bin/env python3
"""
VERY LARGE MACRO STRUCTURE COLLECTION TEST

Purpose:
- One run for the complete current macro data structure.
- Reuse the production project's existing FRED and market definitions/functions.
- Collect the expanded ISM Services and S&P Global Services blocks.
- Never use Previous/Forecast as Actual.
- Never modify makro_szenario.py.
- Never abort because one source/field is unavailable.
- Produce a structured CSV + JSON report in .macro_structure_test/.
"""

from __future__ import annotations

import csv
import importlib.util
import json
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
    "User-Agent": "Mozilla/5.0 (compatible; NeuberMacroStructureTest/1.0)",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

TE_SERVICES = (
    "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
)

SP_GLOBAL = {
    "public": "https://www.pmi.spglobal.com/Public?language=de",
    "releases": "https://www.pmi.spglobal.com/Public/Release/PressReleases?language=de",
}

# Fixed macro structure agreed for the project.
TIERS = {
    "TIER1": [
        "Fed Funds Effective Rate", "Fed Target Range Upper",
        "Fed Target Range Lower", "US 2Y Treasury", "US 10Y Treasury",
        "Realzins 10Y TIPS", "Core CPI", "Core PCE", "PPI",
        "Reales BIP", "Industrieproduktion", "Kapazitaetsauslastung",
        "NFP / Nonfarm Payrolls", "Arbeitslosenquote",
        "Initial Jobless Claims", "JOLTS Job Openings",
        "ISM Manufacturing PMI", "ISM Services PMI",
        "S&P 500", "VIX", "US High Yield OAS",
        "US Investment Grade OAS", "Chicago Fed NFCI",
    ],
    "TIER2": [
        "CPI", "PCE", "Durchschnittlicher Stundenlohn", "M2",
        "US 5Y Treasury", "US 30Y Treasury", "ECB Deposit Facility Rate",
        "SLOOS C&I Tightening", "DXY", "Nasdaq Composite",
        "Russell 2000", "DAX", "EuroStoxx 50", "Nikkei 225",
        "EUR/USD", "USD/JPY", "WTI", "Brent", "Erdgas",
        "Gold", "Silber", "Platin", "Palladium", "Kupfer",
        "Aluminium", "Zink", "Nickel", "Blei", "Zinn", "Kobalt",
        "Eisenerz",
        # Expanded PMI confirmation/context.
        "ISM Services Business Activity",
        "ISM Services Supplier Deliveries",
        "ISM Services Backlog of Orders",
        "ISM Services Inventories",
        "ISM Services Inventory Sentiment",
        "ISM Services Imports",
        "ISM Services Exports",
        "ISM Services New Export Orders",
        "S&P Global Services Business Activity",
        "S&P Global Services New Business",
        "S&P Global Services New Export Business",
        "S&P Global Services Employment",
        "S&P Global Services Outstanding Business",
        "S&P Global Services Input Prices",
        "S&P Global Services Prices Charged",
        "S&P Global Services Future Activity",
    ],
    "TIER3": [
        "Consumer Sentiment", "Global Economic Policy Uncertainty",
        "US Federal Debt/GDP", "GSCPI", "Bitcoin", "Ethereum",
        "Lithium",
    ],
}

def result(source, field, value=None, reference=None, release=None,
           status="UNAVAILABLE", tier=None, note=""):
    return {
        "source": source,
        "field": field,
        "value": value,
        "reference": reference,
        "release": release,
        "status": status,
        "tier": tier,
        "note": note,
    }

def tier_for(field):
    for tier, fields in TIERS.items():
        if field in fields:
            return tier
    return "UNASSIGNED"

def add(results, source, field, value=None, reference=None, release=None,
        status="UNAVAILABLE", note=""):
    results.append(result(
        source, field, value, reference, release,
        status, tier_for(field), note
    ))

def safe(label, fn):
    try:
        return fn()
    except Exception as exc:
        print(f"SAFE_FAIL={label}|{type(exc).__name__}|{exc}")
        traceback.print_exc(limit=1)
        return None

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    return r

def clean_html(html):
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ",
                  html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def save_page(name, response):
    (OUT / f"{name}.html").write_text(response.text, encoding="utf-8")
    return response

def extract_number(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace(",", ".")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None

def collect_te_services(results):
    print("=== TE SERVICES ===")
    r = safe("TE_SERVICES_FETCH", lambda: fetch(TE_SERVICES))
    if not r:
        return
    print(f"TE_STATUS={r.status_code}")
    print(f"TE_FINAL_URL={r.url}")
    save_page("te_services", r)
    html = r.text
    text = clean_html(html)

    # Evidence inventory for the expanded Services block.
    labels = {
        "ISM Services PMI": ["ISM Services PMI", "Services PMI"],
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

    for field, needles in labels.items():
        found = False
        contexts = []
        for needle in needles:
            for m in re.finditer(re.escape(needle), text, re.I):
                found = True
                contexts.append(text[max(0, m.start()-180):m.start()+700])
                if len(contexts) >= 3:
                    break
            if contexts:
                break

        if not found:
            add(results, "TradingEconomics", field,
                status="NOT_FOUND",
                note="Label not found on public Services PMI page.")
            continue

        # Do not guess a value from arbitrary nearby numbers.
        # First inspect HTML tables and only accept a value if the row/column
        # explicitly identifies the component and Last/current value.
        value = None
        reference = None
        try:
            tables = pd.read_html(StringIO(html))
        except Exception:
            tables = []

        for table in tables:
            flat = table.astype(str)
            table_text = flat.to_string(index=False)
            if not any(n.lower() in table_text.lower() for n in needles):
                continue

            # Search rows with the field label.
            for _, row in flat.iterrows():
                row_text = " | ".join(map(str, row.tolist()))
                if not any(n.lower() in row_text.lower() for n in needles):
                    continue

                # Prefer an explicit "Last" column.
                cols = [str(c).strip().lower() for c in flat.columns]
                last_idx = next((i for i,c in enumerate(cols)
                                 if c == "last" or "last" == c.strip()), None)
                if last_idx is not None:
                    value = extract_number(row.iloc[last_idx])
                else:
                    # No explicit Last column -> keep value unknown.
                    value = None

                ref_match = re.search(
                    r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b", row_text
                )
                if ref_match:
                    reference = f"{ref_match.group(1)}-{int(ref_match.group(2)):02d}"
                break
            if value is not None:
                break

        status = "REAL_PUBLIC" if value is not None else "LABEL_FOUND_VALUE_UNRESOLVED"
        note = "Value accepted only from explicit current/Last table field."
        add(results, "TradingEconomics", field, value, reference,
            status=status, note=note)

    # Release/current-page evidence.
    for term in ["Jul 2026", "July 2026", "Aug 5, 2026",
                 "2026-08-05", "202608051417"]:
        if term.lower() in text.lower():
            add(results, "TradingEconomics", "ISM Services Release Evidence",
                term, "2026-07", "2026-08-05",
                status="REAL_PUBLIC", tier="TIER2",
                note="Release/reference evidence found on page.")
            break

def collect_sp_global(results):
    print("=== S&P GLOBAL SERVICES ===")
    pages = {}
    for name, url in SP_GLOBAL.items():
        r = safe(f"SP_FETCH_{name}", lambda url=url: fetch(url))
        if not r:
            continue
        print(f"SP_{name.upper()}_STATUS={r.status_code}")
        print(f"SP_{name.upper()}_FINAL_URL={r.url}")
        pages[name] = r.text
        save_page(f"sp_{name}", r)

    labels = {
        "S&P Global Services Business Activity":
            ["Business Activity"],
        "S&P Global Services New Business":
            ["New Business"],
        "S&P Global Services New Export Business":
            ["New Export Business"],
        "S&P Global Services Employment":
            ["Employment"],
        "S&P Global Services Outstanding Business":
            ["Outstanding Business", "Backlogs", "Backlog"],
        "S&P Global Services Input Prices":
            ["Input Prices"],
        "S&P Global Services Prices Charged":
            ["Prices Charged"],
        "S&P Global Services Future Activity":
            ["Future Activity", "Business Expectations"],
    }

    combined = "\n".join(pages.values())
    text = clean_html(combined)

    for field, needles in labels.items():
        occurrences = sum(
            len(re.findall(re.escape(n), text, re.I)) for n in needles
        )
        if occurrences:
            add(results, "S&P Global", field,
                status="LABEL_FOUND",
                note=f"Public page evidence; occurrences={occurrences}. "
                     "Numeric acceptance requires explicit published value.")
        else:
            add(results, "S&P Global", field,
                status="NOT_FOUND",
                note="No matching public label found in tested pages.")

    if re.search(r"S&P\s*Global.*Services\s*PMI|Services\s*PMI", text, re.I):
        add(results, "S&P Global",
            "S&P Global Services PMI",
            status="LABEL_FOUND",
            tier="TIER2",
            note="Public-page evidence found; numeric value intentionally not guessed.")

def collect_production_fred_and_market(results):
    print("=== EXISTING PRODUCTION DATA STRUCTURE ===")
    if not TARGET.exists():
        print("PRODUCTION_FILE=RED")
        return

    spec = importlib.util.spec_from_file_location("macro_prod", TARGET)
    if spec is None or spec.loader is None:
        print("PRODUCTION_IMPORT_SPEC=RED")
        return

    module = importlib.util.module_from_spec(spec)
    safe("PRODUCTION_MODULE_LOAD", lambda: spec.loader.exec_module(module))

    fred_names = list(getattr(module, "FRED_SERIES", {}).keys())
    market_data = getattr(module, "MARKET_DATA", {})

    print(f"FRED_FIELDS={len(fred_names)}")
    print(f"MARKET_FIELDS={len(market_data)}")

    fred_fn = getattr(module, "fred_snapshot", None)
    market_fn = getattr(module, "market_snapshot", None)

    if fred_fn:
        def fred_one(name):
            try:
                return name, fred_fn(name, module.FRED_SERIES[name])
            except Exception as exc:
                return name, f"{name}: NICHT VERFUEGBAR | FEHLER={exc}"

        with ThreadPoolExecutor(max_workers=min(12, max(1, len(fred_names)))) as pool:
            futures = [pool.submit(fred_one, n) for n in fred_names]
            for fut in as_completed(futures):
                name, line = fut.result()
                print(f"FRED_RESULT={line}")
                status = "REAL_OR_CACHED" if "NICHT VERFUEGBAR" not in line else "UNAVAILABLE"
                value = None
                m = re.search(r":\s*([-+]?\d+(?:[.,]\d+)?)", line)
                if m:
                    value = extract_number(m.group(1))
                ref = None
                m = re.search(r"Datenstand=(\d{4}-\d{2}-\d{2})", line)
                if m:
                    ref = m.group(1)
                add(results, "FRED", name, value, ref,
                    status=status,
                    note=f"series_id={module.FRED_SERIES[name]}")

    if market_fn:
        def market_one(item):
            name, (ticker, data_type) = item
            try:
                return name, market_fn(name, ticker, data_type)
            except Exception as exc:
                return name, f"{name}: NICHT VERFUEGBAR | FEHLER={exc}"

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(market_one, item)
                       for item in market_data.items()]
            for fut in as_completed(futures):
                name, line = fut.result()
                print(f"MARKET_RESULT={line}")
                status = "REAL_OR_CACHED" if "NICHT VERFUEGBAR" not in line else "UNAVAILABLE"
                ref = None
                m = re.search(r"Datenstand=(\d{4}-\d{2}-\d{2})", line)
                if m:
                    ref = m.group(1)
                add(results, "MARKET", name, None, ref,
                    status=status,
                    note=f"ticker={market_data[name][0]}|type={market_data[name][1]}")

def write_reports(results):
    # De-duplicate exact source/field/status records.
    seen = set()
    unique = []
    for row in results:
        key = tuple(row.items())
        if key not in seen:
            seen.add(key)
            unique.append(row)

    json_path = OUT / "macro_structure_results.json"
    csv_path = OUT / "macro_structure_results.csv"
    summary_path = OUT / "macro_structure_summary.txt"

    json_path.write_text(
        json.dumps(unique, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source", "field", "value", "reference", "release",
                "status", "tier", "note"
            ]
        )
        writer.writeheader()
        writer.writerows(unique)

    counts = {}
    for row in unique:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    tier_counts = {}
    for row in unique:
        t = row.get("tier") or "UNASSIGNED"
        tier_counts[t] = tier_counts.get(t, 0) + 1

    lines = [
        "=== MACRO STRUCTURE FULL COLLECTION SUMMARY ===",
        f"RUN_DATE={date.today().isoformat()}",
        f"RECORDS={len(unique)}",
        "",
        "STATUS COUNTS:",
    ]
    lines.extend(f"{k}={v}" for k,v in sorted(counts.items()))
    lines.append("")
    lines.append("TIER COUNTS:")
    lines.extend(f"{k}={v}" for k,v in sorted(tier_counts.items()))
    lines.extend([
        "",
        "RULES:",
        "Previous is never accepted as Actual.",
        "Forecast/Consensus is never accepted as Actual.",
        "Missing data never aborts this collection test.",
        "No production file is modified.",
        f"JSON={json_path}",
        f"CSV={csv_path}",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    return unique

def main():
    print("=== MACRO STRUCTURE FULL COLLECTION TEST ===")
    print("MODE=ONE_RUN|NON_ABORTING|NO_PRODUCTION_WRITE")
    print(f"TARGET={TARGET}")

    if TARGET.exists():
        try:
            compile(TARGET.read_text(encoding="utf-8"),
                    str(TARGET), "exec")
            print("TARGET_SYNTAX=GREEN")
        except Exception as exc:
            print(f"TARGET_SYNTAX=RED|{exc}")
    else:
        print("TARGET_EXISTS=RED")

    results = []

    safe("TE_COLLECTION", lambda: collect_te_services(results))
    safe("SP_COLLECTION", lambda: collect_sp_global(results))
    safe("PRODUCTION_COLLECTION",
         lambda: collect_production_fred_and_market(results))

    safe("WRITE_REPORTS", lambda: write_reports(results))

    print("RESULT=COLLECTION_COMPLETE")
    print("EXIT_POLICY=0")
    sys.exit(0)

if __name__ == "__main__":
    main()
