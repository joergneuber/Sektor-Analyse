#!/usr/bin/env python3
"""
V9 - STRUCTURE METHODS TEST (INDEPENDENT DIAGNOSTIC RUN)

Same GitHub workflow name and same filenames as the previous V9, but this is a
separate diagnostic design. It does NOT try to change or validate production
data. Its sole purpose is to determine which extraction method can see the real
table/DOM structure on our already-known sources.

KNOWN SOURCES ONLY
------------------
1. Official ISM
2. Trading Economics public
3. LME official
4. S&P Global public

PRIMARY METHODS
---------------
A) pandas.read_html(URL) directly
B) requests -> pandas.read_html(StringIO(html))
C) BeautifulSoup <table>/<tr>/<th>/<td>
D) lxml XPath over the raw HTML
E) DOM/parent-chain inspection with BeautifulSoup
F) Selenium rendered DOM fallback for dynamic/blocked pages

NO NEW SOURCE FAMILIES
----------------------
No Investing, MarketWatch, ForexFactory, etc. are introduced here.

TARGETS
-------
ISM Services + Manufacturing: July 2026
LME: 2026-08-28
TE Commodities: inspect public table rows and column names
S&P: inspect public HTML structure

NON-ABORTING
------------
Every page/method is isolated. A method failure is recorded and the run
continues. The workflow always exits 0.

CRITICAL
--------
This script deliberately does NOT infer or promote any value. It saves the
actual table schema, headers, rows, XPath matches, DOM context and method
availability so that the production parser can later be written against the
real structure.
"""

import csv
import html
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import pandas as pd
except Exception:
    pd = None

try:
    from lxml import html as lxml_html
except Exception:
    lxml_html = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
except Exception:
    webdriver = None
    Options = None


VERSION = "V9-STRUCTURE-METHODS-INDEPENDENT"
OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

METHODS_JSONL = OUT / "methods.jsonl"
TABLES_JSONL = OUT / "tables.jsonl"
TARGETS_JSONL = OUT / "target_matches.jsonl"
DOM_JSONL = OUT / "dom_matches.jsonl"
HTTP_CSV = OUT / "http_attempts.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

ISM_MONTH = "2026-07"
LME_DATE = "2026-08-28"

PAGES = [
    # ISM official
    ("ISM_SERVICES_JULY", "ISM_SERVICES",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "ISM_MANUFACTURING",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    ("ISM_SERVICES_INDEX", "ISM_SERVICES",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
    ("ISM_MANUFACTURING_INDEX", "ISM_MANUFACTURING",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/"),

    # Trading Economics public PMI
    ("TE_SERVICES_NONMAN", "TE_SERVICES",
     "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES", "TE_SERVICES",
     "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_MANUFACTURING", "TE_MANUFACTURING",
     "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_SERVICES_DE", "TE_SERVICES",
     "https://de.tradingeconomics.com/united-states/non-manufacturing-pmi"),

    # Trading Economics public commodities
    ("TE_COMMODITIES_EN", "TE_COMMODITIES",
     "https://tradingeconomics.com/commodities"),
    ("TE_COMMODITIES_DE", "TE_COMMODITIES",
     "https://de.tradingeconomics.com/commodities"),
    ("TE_COBALT", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/cobalt"),
    ("TE_NICKEL", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/nickel"),
    ("TE_LEAD", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/lead"),
    ("TE_TIN", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/tin"),

    # LME official
    ("LME_OFFICIAL", "LME",
     "https://www.lme.com/market-data/reports-and-data/lme-official-prices"),
    ("LME_NICKEL", "LME",
     "https://www.lme.com/Metals/Non-ferrous/LME-Nickel"),
    ("LME_LEAD", "LME",
     "https://www.lme.com/Metals/Non-ferrous/LME-Lead"),
    ("LME_TIN", "LME",
     "https://www.lme.com/Metals/Non-ferrous/LME-Tin"),
    ("LME_COBALT", "LME",
     "https://www.lme.com/Metals/Minor-metals/LME-Cobalt"),

    # S&P Global public
    ("SPG_PUBLIC_DE", "SPG",
     "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "SPG",
     "https://www.pmi.spglobal.com/Public?language=en"),
]

TARGETS = {
    "ISM_SERVICES": [
        "PMI", "Business Activity", "New Orders", "New Export Orders",
        "Employment", "Prices", "Supplier Deliveries", "Backlog",
        "Inventories", "Inventory Sentiment", "Imports", "Exports",
    ],
    "ISM_MANUFACTURING": [
        "PMI", "New Orders", "Production", "Employment", "Prices",
        "Supplier Deliveries", "Backlog of Orders", "Inventories",
        "Customers' Inventories", "Imports", "Exports", "New Export Orders",
    ],
    "TE_SERVICES": [
        "Services PMI", "Business Activity", "New Orders", "Employment", "Prices",
        "Backlog of Orders", "Supplier Deliveries",
    ],
    "TE_MANUFACTURING": [
        "Manufacturing PMI", "New Orders", "Production", "Employment", "Prices",
    ],
    "TE_COMMODITIES": ["Nickel", "Lead", "Tin", "Cobalt"],
    "TE_COMMODITY": ["Nickel", "Lead", "Tin", "Cobalt"],
    "LME": ["Nickel", "Lead", "Tin", "Cobalt"],
    "SPG": ["Services PMI", "Business Activity", "New Business",
            "New Export Business", "Employment", "Backlogs",
            "Input Prices", "Prices Charged", "Future Activity"],
}

ISM_ALIASES = {
    "PMI": ["PMI", "Services PMI", "Manufacturing PMI", "ISM Services PMI", "ISM Manufacturing PMI"],
    "Business Activity": ["Business Activity"],
    "New Orders": ["New Orders"],
    "New Export Orders": ["New Export Orders"],
    "Employment": ["Employment"],
    "Prices": ["Prices"],
    "Supplier Deliveries": ["Supplier Deliveries"],
    "Backlog": ["Backlog", "Backlog of Orders"],
    "Backlog of Orders": ["Backlog of Orders", "Backlog"],
    "Inventories": ["Inventories"],
    "Inventory Sentiment": ["Inventory Sentiment"],
    "Imports": ["Imports"],
    "Exports": ["Exports"],
    "Production": ["Production"],
    "Customers' Inventories": ["Customers' Inventories", "Customers Inventories"],
}

LME_ALIASES = {
    "Nickel": ["Nickel", "LME Nickel"],
    "Lead": ["Lead", "LME Lead"],
    "Tin": ["Tin", "LME Tin"],
    "Cobalt": ["Cobalt", "LME Cobalt"],
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
})
TIMEOUT = (8, 15)
BROWSER_TIMEOUT = 25
MAX_BYTES = 10_000_000

records = []
attempts = []
errors = []


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()



def parse_number(value):
    s = normalize(value)
    pattern = r"[-+]?\d+(?:[.,]\d+)?"
    for token in re.findall(pattern, s):
        try:
            if token.count(",") == 1 and token.count(".") == 0:
                num = float(token.replace(",", "."))
            elif token.count(",") == 1 and token.count(".") == 1:
                # In this diagnostic helper, the rightmost separator is treated
                # as the decimal separator.
                if token.rfind(",") > token.rfind("."):
                    num = float(token.replace(".", "").replace(",", "."))
                else:
                    num = float(token.replace(",", ""))
            else:
                num = float(token)
        except Exception:
            continue
        if 1900 <= abs(num) <= 2100:
            continue
        return token, num
    return None, None

def emit(route_name, **data):
    rec = {"route": route_name, "timestamp_utc": now(), **data}
    records.append(rec)
    with METHODS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def safe(label, fn):
    try:
        return fn()
    except Exception as exc:
        errors.append({"label": label, "error": repr(exc)})
        print(f"WARNUNG: {label}: {type(exc).__name__}: {exc}")
        return None


def fetch_requests(label, domain, url):
    started = time.monotonic()
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        body = r.content[:MAX_BYTES]
        row = {
            "label": label, "domain": domain, "route": "requests",
            "url": url, "final_url": r.url, "status": r.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "bytes": len(body), "content_type": r.headers.get("content-type", ""),
            "redirected": r.url != url, "error": "",
        }
        attempts.append(row)
        emit("HTTP", **row)
        return r, body
    except Exception as exc:
        row = {
            "label": label, "domain": domain, "route": "requests",
            "url": url, "final_url": "", "status": "ERROR",
            "elapsed_s": round(time.monotonic() - started, 3),
            "bytes": 0, "content_type": "", "redirected": False,
            "error": repr(exc),
        }
        attempts.append(row)
        emit("HTTP", **row)
        print(f"WARNUNG: requests {label}: {type(exc).__name__}: {exc}")
        return None, b""


def fetch_browser(label, domain, url):
    if webdriver is None:
        emit("SELENIUM", label=label, domain=domain, url=url,
             status="UNAVAILABLE", reason="selenium_not_available")
        return b""
    driver = None
    started = time.monotonic()
    try:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(BROWSER_TIMEOUT)
        driver.get(url)
        time.sleep(3)
        body = driver.page_source.encode("utf-8", "ignore")[:MAX_BYTES]
        emit("SELENIUM", label=label, domain=domain, url=url,
             final_url=driver.current_url,
             status=200 if body else "EMPTY", bytes=len(body),
             elapsed_s=round(time.monotonic() - started, 3))
        return body
    except Exception as exc:
        emit("SELENIUM", label=label, domain=domain, url=url,
             status="ERROR", error=repr(exc),
             elapsed_s=round(time.monotonic() - started, 3))
        print(f"WARNUNG: Selenium {label}: {type(exc).__name__}: {exc}")
        return b""
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def schema_from_soup(label, domain, url, raw, method):
    if BeautifulSoup is None:
        emit("METHOD_UNAVAILABLE", method=method, label=label, domain=domain,
             url=url, reason="beautifulsoup_unavailable")
        return

    soup = BeautifulSoup(raw, "html.parser")
    tables = soup.find_all("table")
    table_count = len(tables)
    emit("METHOD_START", method=method, label=label, domain=domain,
         url=url, table_count=table_count)

    for idx, table in enumerate(tables):
        rows = []
        for tr_idx, tr in enumerate(table.find_all("tr")):
            cells = [
                {
                    "tag": c.name,
                    "text": normalize(c.get_text(" ", strip=True)),
                    "attrs": {
                        k: normalize(v if isinstance(v, str) else " ".join(map(str, v)))
                        for k, v in c.attrs.items()
                    },
                }
                for c in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append({"row_index": tr_idx, "cells": cells})

        text_blob = normalize(table.get_text(" ", strip=True))
        payload = {
            "method": method,
            "label": label,
            "domain": domain,
            "url": url,
            "table_index": idx,
            "table_attrs": {
                k: normalize(v if isinstance(v, str) else " ".join(map(str, v)))
                for k, v in table.attrs.items()
            },
            "table_text": text_blob[:5000],
            "row_count": len(rows),
            "max_columns": max((len(r["cells"]) for r in rows), default=0),
            "rows": rows[:250],
        }
        with TABLES_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

        targets = TARGETS.get(domain, [])
        matched = []
        for row in rows:
            if not row["cells"]:
                continue
            first = row["cells"][0]["text"]
            for target in targets:
                aliases = ISM_ALIASES.get(target, [target]) + LME_ALIASES.get(target, [])
                if any(normalize(first).lower() == normalize(a).lower() for a in aliases):
                    matched.append({
                        "target": target,
                        "row_index": row["row_index"],
                        "row_cells": row["cells"],
                    })

        if matched:
            emit("TARGET_TABLE_MATCH",
                 method=method, label=label, domain=domain, url=url,
                 table_index=idx, table_text=text_blob[:5000],
                 target_matches=matched[:150])


def pandas_direct_url(label, domain, url):
    if pd is None:
        emit("METHOD_UNAVAILABLE", method="pandas_direct_url",
             label=label, domain=domain, url=url, reason="pandas_unavailable")
        return
    started = time.monotonic()
    try:
        frames = pd.read_html(url)
        emit("PANDAS_DIRECT_URL", label=label, domain=domain, url=url,
             status="OK", table_count=len(frames),
             elapsed_s=round(time.monotonic() - started, 3),
             tables=[
                 {
                     "index": i,
                     "shape": list(df.shape),
                     "columns": [str(c) for c in df.columns],
                     "rows": df.fillna("").astype(str).head(120).to_dict("records"),
                 }
                 for i, df in enumerate(frames[:120])
             ])
    except Exception as exc:
        emit("PANDAS_DIRECT_URL", label=label, domain=domain, url=url,
             status="ERROR", error=repr(exc),
             elapsed_s=round(time.monotonic() - started, 3))


def pandas_loaded_html(label, domain, raw):
    if pd is None:
        emit("METHOD_UNAVAILABLE", method="pandas_loaded_html",
             label=label, domain=domain, reason="pandas_unavailable")
        return
    started = time.monotonic()
    try:
        frames = pd.read_html(io.StringIO(raw.decode("utf-8", "ignore")))
        emit("PANDAS_LOADED_HTML", label=label, domain=domain,
             status="OK", table_count=len(frames),
             elapsed_s=round(time.monotonic() - started, 3),
             tables=[
                 {
                     "index": i,
                     "shape": list(df.shape),
                     "columns": [str(c) for c in df.columns],
                     "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
                     "rows": df.fillna("").astype(str).head(120).to_dict("records"),
                 }
                 for i, df in enumerate(frames[:120])
             ])
    except Exception as exc:
        emit("PANDAS_LOADED_HTML", label=label, domain=domain,
             status="ERROR", error=repr(exc),
             elapsed_s=round(time.monotonic() - started, 3))


def lxml_scan(label, domain, url, raw):
    if lxml_html is None:
        emit("METHOD_UNAVAILABLE", method="lxml_xpath",
             label=label, domain=domain, url=url, reason="lxml_unavailable")
        return

    try:
        root = lxml_html.fromstring(raw)
    except Exception as exc:
        emit("LXML_XPATH", label=label, domain=domain, url=url,
             status="ERROR", error=repr(exc))
        return

    tables = root.xpath("//table")
    rows = root.xpath("//table//tr")
    cells = root.xpath("//table//th | //table//td")
    table_ids = []
    for i, table in enumerate(tables[:150]):
        attrs = dict(table.attrib)
        text_blob = normalize(" ".join(table.xpath(".//text()")))
        table_ids.append({
            "index": i,
            "attrs": attrs,
            "text_preview": text_blob[:5000],
            "rows": len(table.xpath(".//tr")),
            "cells": len(table.xpath(".//th | .//td")),
        })

    target_hits = {}
    targets = TARGETS.get(domain, [])
    for target in targets:
        # Case-insensitive XPath contains via lower-case translation.
        xpath = (
            "//tr[td or th]"
            "[contains(translate(normalize-space(string(.)), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), "
            f"'{target.lower()}')]"
        )
        found = root.xpath(xpath)
        target_hits[target] = [
            normalize(" ".join(node.xpath(".//text()")))[:2500]
            for node in found[:80]
        ]

    emit("LXML_XPATH", label=label, domain=domain, url=url,
         status="OK", table_count=len(tables), row_count=len(rows),
         cell_count=len(cells), tables=table_ids, target_hits=target_hits)


def dom_parent_scan(label, domain, url, raw):
    if BeautifulSoup is None:
        return
    soup = BeautifulSoup(raw, "html.parser")
    targets = TARGETS.get(domain, [])
    matches = []

    for target in targets:
        pattern = re.compile(re.escape(target), re.I)
        for text_node in soup.find_all(string=pattern)[:80]:
            current = text_node.parent
            chain = []
            for _ in range(7):
                if current is None:
                    break
                chain.append({
                    "tag": current.name,
                    "id": current.get("id"),
                    "class": current.get("class"),
                    "attrs": {
                        k: normalize(v if isinstance(v, str) else " ".join(map(str, v)))
                        for k, v in current.attrs.items()
                        if str(k).lower().startswith("data-")
                        or str(k).lower() in {"id", "class", "data-symbol", "data-code", "data-value"}
                    },
                    "text": normalize(current.get_text(" ", strip=True))[:2500],
                })
                current = current.parent
            matches.append({"target": target, "chain": chain})

    with DOM_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "label": label, "domain": domain, "url": url,
            "matches": matches[:800]
        }, ensure_ascii=False, default=str) + "\n")

    emit("DOM_PARENT_SCAN", label=label, domain=domain, url=url,
         match_count=len(matches), matches=matches[:800])


def source_scan(label, domain, raw):
    txt = raw.decode("utf-8", "ignore")
    emit("SOURCE_SCAN",
         label=label, domain=domain,
         july_hits=re.findall(r"July\s+2026|Jul\s+2026|2026[-/]07", txt, re.I)[:1000],
         lme_hits=re.findall(
             r"2026[-/]08[-/]28|28\.08\.2026|08/28/2026|August\s+28,\s+2026",
             txt, re.I
         )[:1000],
         symbol_hits=re.findall(
             r'data-(?:symbol|code|id|value)\s*=\s*["\']([^"\']+)["\']',
             txt, re.I
         )[:2500],
         url_like=re.findall(
             r'https?://[^"\'>\s]+|/(?:api|ajax|chart|data|historical|forecast)[^"\'>\s]+',
             txt, re.I
         )[:2500],
         price_terms=re.findall(
             r"(?is).{0,500}(?:Official Price|Official Cash|Cash Bid|Cash Offer|Price|Date).{0,1800}",
             txt
         )[:800])


def inspect(label, domain, url):
    response, raw = fetch_requests(label, domain, url)

    need_browser = (
        not raw
        or response is None
        or response.status_code in {403, 429, 451}
        or len(raw) < 5000
        or (domain.startswith("ISM_") and (
            "SSO/Login" in (response.url if response else "")
            or b"<table" not in raw.lower()
        ))
    )

    if need_browser:
        browser_raw = safe(
            f"browser:{label}",
            lambda: fetch_browser(label, domain, url)
        )
        if browser_raw:
            # Browser-rendered DOM is always archived separately.
            raw_browser_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + "_browser.html"
            (RAW / raw_browser_name).write_bytes(browser_raw)

    if not raw:
        emit("PAGE_SKIPPED", label=label, domain=domain, url=url)
        return

    raw_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".html"
    (RAW / raw_name).write_bytes(raw)

    # Independent methods. Failures never abort other methods.
    safe(f"bs4:{label}", lambda: schema_from_soup(
        label, domain, url, raw, "beautifulsoup"
    ))
    safe(f"pandas-url:{label}", lambda: pandas_direct_url(
        label, domain, url
    ))
    safe(f"pandas-html:{label}", lambda: pandas_loaded_html(
        label, domain, raw
    ))
    safe(f"lxml:{label}", lambda: lxml_scan(
        label, domain, url, raw
    ))
    safe(f"dom:{label}", lambda: dom_parent_scan(
        label, domain, url, raw
    ))
    safe(f"source:{label}", lambda: source_scan(
        label, domain, raw
    ))


def initialize_outputs():
    # CSV headers are created before network activity, so outputs exist even if
    # every page fails.
    if not HTTP_CSV.exists():
        with HTTP_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(
                f,
                fieldnames=[
                    "label", "domain", "route", "url", "final_url", "status",
                    "elapsed_s", "bytes", "content_type", "redirected", "error"
                ],
            ).writeheader()


def write_summary():
    initialize_outputs()

    with HTTP_CSV.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "label", "domain", "route", "url", "final_url", "status",
            "elapsed_s", "bytes", "content_type", "redirected", "error"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)

    method_counts = {}
    for rec in records:
        route = rec.get("route", "UNKNOWN")
        method_counts[route] = method_counts.get(route, 0) + 1

    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "known_sources_only": True,
        "recursive_crawl": False,
        "te_api_used": False,
        "production_file_modified": False,
        "ism_reference_month": ISM_MONTH,
        "lme_target_date": LME_DATE,
        "fixed_pages": len(PAGES),
        "http_attempts": len(attempts),
        "evidence_records": len(records),
        "errors": len(errors),
        "method_record_counts": method_counts,
        "purpose": "HTML/table/DOM structure discovery only",
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    table_details = [r for r in records if r.get("route") == "TABLE_SCHEMA"]
    lxml_details = [r for r in records if r.get("route") == "LXML_XPATH"]
    pandas_details = [r for r in records if r.get("route") in {"PANDAS_DIRECT_URL", "PANDAS_LOADED_HTML"}]
    target_rows = [r for r in records if r.get("route") == "TARGET_TABLE_MATCH"]

    lines = [
        f"=== {VERSION} ===",
        "FINISHED=True",
        "NON_ABORTING=True",
        "KNOWN_SOURCES_ONLY=True",
        "RECURSIVE_CRAWL=False",
        "TE_API_USED=False",
        "PRODUCTION_FILE_MODIFIED=False",
        f"ISM_REFERENCE_MONTH={ISM_MONTH}",
        f"LME_TARGET_DATE={LME_DATE}",
        f"FIXED_PAGES={len(PAGES)}",
        f"HTTP_ATTEMPTS={len(attempts)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"TABLE_RECORDS={len(table_details)}",
        f"LXML_RECORDS={len(lxml_details)}",
        f"PANDAS_RECORDS={len(pandas_details)}",
        f"TARGET_TABLE_MATCH_RECORDS={len(target_rows)}",
        f"ERRORS={len(errors)}",
        "",
        "METHOD COUNTS:",
    ]
    for k, v in sorted(method_counts.items()):
        lines.append(f"{k}={v}")

    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines.extend(f'{e["label"]}: {e["error"]}' for e in errors)

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    initialize_outputs()

    print(f"=== {VERSION} ===")
    print("MODE=KNOWN_SOURCES_ONLY|HTML_TABLE_SCHEMA|PANDAS_URL|PANDAS_HTML|LXML_XPATH|BS4_DOM|SELENIUM")
    print("NO_RECURSIVE_CRAWL=True")
    print("TE_API_USED=False")
    print(f"ISM_REFERENCE_MONTH={ISM_MONTH}")
    print(f"LME_TARGET_DATE={LME_DATE}")
    print(f"FIXED_PAGE_COUNT={len(PAGES)}")

    for label, domain, url in PAGES:
        safe(f"PAGE:{label}", lambda label=label, domain=domain, url=url:
             inspect(label, domain, url))

    safe("WRITE_SUMMARY", write_summary)

    print(f"V9_HTTP_ATTEMPTS={len(attempts)}")
    print(f"V9_EVIDENCE_RECORDS={len(records)}")
    print(f"V9_ERRORS={len(errors)}")
    print("V9_RESULT=SCHEMA_ANALYSIS_COMPLETE")
    print("V9_PRODUCTION_FILE_MODIFIED=False")
    print("V9_EXIT_POLICY=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
