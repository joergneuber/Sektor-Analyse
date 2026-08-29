#!/usr/bin/env python3
from __future__ import annotations
import re, sys, json, traceback
from pathlib import Path
from io import StringIO

TARGET = Path("makro_szenario.py")
URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
OUT = Path(".te_services_extended")
OUT.mkdir(exist_ok=True)

def safe(label, fn):
    try:
        return fn()
    except Exception as e:
        print(f"{label}=RED | {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
        return None

def main():
    print("=== TE ISM SERVICES EXTENDED COLLECTION TEST ===")
    print("RULE=collect everything useful; never abort on one failure")
    print("TARGET=", TARGET)

    if not TARGET.exists():
        print("TARGET_EXISTS=RED")
    else:
        print("TARGET_EXISTS=GREEN")

    # Production dependency inventory, without importing production.
    if TARGET.exists():
        source = TARGET.read_text(encoding="utf-8")
        imports = sorted(set(re.findall(
            r'^(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)', source, re.M)))
        print("PRODUCTION_IMPORTS=" + ",".join(imports))

    def fetch():
        import requests
        r = requests.get(URL, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=30)
        html = r.text
        print(f"TE_HTTP={r.status_code}")
        print(f"TE_FINAL_URL={r.url}")
        print(f"TE_BYTES={len(r.content)}")
        (OUT/"services.html").write_text(html, encoding="utf-8")
        return html

    html = safe("TE_FETCH", fetch)
    if not html:
        print("RESULT=COLLECTION_COMPLETE")
        sys.exit(0)

    # Search a broad set of potentially useful Services / ISM fields.
    terms = [
        "ISM Services PMI", "Services PMI", "Business Activity", "New Orders",
        "Employment", "Prices", "Supplier Deliveries", "Backlog",
        "Inventories", "Inventory Sentiment", "Imports", "Exports",
        "Orders", "Order Backlogs", "Prices Paid", "Prices Received",
        "Employment Index", "Business Activity Index", "New Export Orders",
        "Inventory", "Production", "Delivery", "Respondents",
        "Actual", "Previous", "Forecast", "Consensus",
        "Jul 2026", "July 2026", "Aug 5, 2026", "2026-08-05",
    ]
    low = html.lower()
    for term in terms:
        hits = [m.start() for m in re.finditer(re.escape(term.lower()), low)]
        print(f"TERM={term}|COUNT={len(hits)}")
        for p in hits[:3]:
            snippet = re.sub(r"\s+", " ", html[max(0,p-500):p+1800])
            print(f"CONTEXT={term}|{snippet[:2200]}")

    # Parse every HTML table and save candidates.
    def parse_tables():
        import pandas as pd
        tables = pd.read_html(StringIO(html))
        print(f"TABLE_COUNT={len(tables)}")
        candidates = []
        for i, df in enumerate(tables):
            txt = df.astype(str).to_string(index=False)
            interesting = re.search(
                r"actual|previous|forecast|consensus|services|business activity|"
                r"new orders|employment|prices|supplier|backlog|inventory|imports|exports",
                txt, re.I
            )
            if interesting:
                print(f"TABLE_CANDIDATE={i}|SHAPE={df.shape}")
                print(f"COLUMNS={list(map(str,df.columns))}")
                print(txt[:12000])
                candidates.append(i)
        print("TABLE_CANDIDATES=" + ",".join(map(str,candidates)))
    safe("TABLE_PARSE", parse_tables)

    # Extract visible text around rows/JSON-looking data, useful when values
    # are embedded in scripts rather than HTML tables.
    def regex_value_inventory():
        patterns = [
            r'(?i)(business activity|new orders|employment|prices|supplier deliveries|backlog(?:s)?|inventor(?:y|ies)|imports|exports|new export orders)[^<\n]{0,300}',
            r'(?i)(actual|previous|forecast|consensus)[^<\n]{0,300}',
        ]
        for pat in patterns:
            matches = re.findall(pat, html)
            print(f"REGEX_MATCHES={len(matches)}")
            for m in matches[:100]:
                print("REGEX=", re.sub(r"\s+", " ", str(m))[:1000])
    safe("REGEX_INVENTORY", regex_value_inventory)

    print("RAW_HTML_ARTIFACT=" + str(OUT/"services.html"))
    print("RESULT=COLLECTION_COMPLETE")
    print("EXIT_POLICY=0")
    sys.exit(0)

if __name__ == "__main__":
    main()
