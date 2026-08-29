#!/usr/bin/env python3
"""
V9 - ISM / LME PROVENANCE & EXTRACTION TEST

Focused diagnostic run for two open production issues:

1) ISM Services + Manufacturing July 2026:
   - Core + extended components
   - official ISM public report
   - TradingEconomics public pages
   - multiple extraction methods

2) LME Official Prices for 2026-08-28:
   - Nickel / Lead / Tin / Cobalt
   - explicit target date
   - multiple public-page extraction methods

Rules:
- read-only
- no production-file changes
- no TE API key
- fixed URL list, NO recursive crawler
- every failure is non-fatal
- no number is promoted to "Actual" without provenance evidence
- a public-source hit and a valid target-date hit are reported separately
"""

import csv
import html
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import pandas as pd
except Exception:
    pd = None


VERSION = "V9"
OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

EVIDENCE = OUT / "evidence.jsonl"
MATRIX = OUT / "provenance_matrix.csv"
ATTEMPTS = OUT / "http_attempts.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

TARGET_DATE_LME = "2026-08-28"
TARGET_MONTH_ISM = "2026-07"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
})
TIMEOUT = (8, 15)
MAX_BYTES = 5_000_000

ISM_TARGETS = {
    "SERVICES": [
        "PMI", "Business Activity", "New Orders", "New Export Orders",
        "Employment", "Prices", "Supplier Deliveries", "Backlog",
        "Inventories", "Inventory Sentiment", "Imports", "Exports",
    ],
    "MANUFACTURING": [
        "PMI", "New Orders", "Production", "Employment", "Prices",
        "Supplier Deliveries", "Backlog of Orders", "Inventories",
        "Customers' Inventories", "Imports", "Exports", "New Export Orders",
    ],
}

LME_TARGETS = ["Nickel", "Lead", "Tin", "Cobalt"]

PAGES = [
    # ISM official / public
    ("ISM_SERVICES_JULY", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    ("ISM_SERVICES_INDEX", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
    ("ISM_MANUFACTURING_INDEX", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/"),

    # TradingEconomics public
    ("TE_SERVICES", "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES_ALT", "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_MANUFACTURING", "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_MANUFACTURING_NEW_ORDERS", "https://tradingeconomics.com/united-states/ism-manufacturing-new-orders"),
    ("TE_SERVICES_DE", "https://de.tradingeconomics.com/united-states/non-manufacturing-pmi"),

    # LME official
    ("LME_OFFICIAL_PRICES", "https://www.lme.com/market-data/reports-and-data/lme-official-prices"),
    ("LME_NICKEL", "https://www.lme.com/Metals/Non-ferrous/LME-Nickel"),
    ("LME_LEAD", "https://www.lme.com/Metals/Non-ferrous/LME-Lead"),
    ("LME_TIN", "https://www.lme.com/Metals/Non-ferrous/LME-Tin"),
    ("LME_COBALT", "https://www.lme.com/Metals/Minor-metals/LME-Cobalt"),
]

records = []
attempts = []
errors = []


def now():
    return datetime.now(timezone.utc).isoformat()


def log_record(**data):
    data["timestamp_utc"] = now()
    records.append(data)
    with EVIDENCE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


def safe(label, fn):
    try:
        return fn()
    except Exception as exc:
        errors.append({"label": label, "error": repr(exc)})
        print(f"WARNUNG: {label}: {type(exc).__name__}: {exc}")
        return None


def normalize(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def fetch(label, url):
    started = time.monotonic()
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        elapsed = round(time.monotonic() - started, 3)
        body = r.content[:MAX_BYTES]
        row = {
            "label": label,
            "url": url,
            "final_url": r.url,
            "status": r.status_code,
            "elapsed_s": elapsed,
            "bytes": len(body),
            "content_type": r.headers.get("content-type", ""),
            "redirected": r.url != url,
            "error": "",
        }
        attempts.append(row)
        log_record(route="HTTP", **row)
        return r, body
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 3)
        row = {
            "label": label,
            "url": url,
            "final_url": "",
            "status": "ERROR",
            "elapsed_s": elapsed,
            "bytes": 0,
            "content_type": "",
            "redirected": False,
            "error": repr(exc),
        }
        attempts.append(row)
        log_record(route="HTTP", **row)
        print(f"WARNUNG: HTTP {label}: {type(exc).__name__}: {exc}")
        return None, b""


def visible_text(raw):
    if BeautifulSoup:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return normalize(soup.get_text(" ", strip=True))
    return normalize(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore")))


def page_title(raw):
    if not BeautifulSoup:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    return normalize(soup.title.get_text(" ", strip=True)) if soup.title else ""


def dates_in(text):
    patterns = [
        r"\b2026[-/]\d{1,2}[-/]\d{1,2}\b",
        r"\b2026[-/]\d{1,2}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+2026\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+2026\b",
    ]
    vals = []
    for p in patterns:
        vals += re.findall(p, text, re.I)
    return list(dict.fromkeys(vals))[:300]


def target_hits(text):
    low = text.lower()
    result = {}
    for group, names in ISM_TARGETS.items():
        result[group] = {n: len(re.findall(re.escape(n.lower()), low)) for n in names}
    result["LME"] = {n: len(re.findall(re.escape(n.lower()), low)) for n in LME_TARGETS}
    return result


def numeric_context(text, term, radius=260):
    windows = []
    for m in list(re.finditer(re.escape(term), text, re.I))[:15]:
        windows.append(text[max(0, m.start()-radius):m.end()+650])
    return windows


def extract_visible(label, url, raw):
    text = visible_text(raw)
    log_record(
        route="VISIBLE_TEXT",
        label=label,
        url=url,
        title=page_title(raw),
        chars=len(text),
        dates=dates_in(text),
        target_hits=target_hits(text),
        target_windows={
            n: numeric_context(text, n)[:8]
            for n in set(sum(ISM_TARGETS.values(), []) + LME_TARGETS)
            if re.search(re.escape(n), text, re.I)
        },
        # These terms let us distinguish a value from a page mention.
        provenance_terms={
            term: len(re.findall(re.escape(term), text, re.I))
            for term in [
                "Actual", "Previous", "Forecast", "Consensus", "Reference",
                "Release Date", "Last", "Value", "Data", "Date",
                TARGET_DATE_LME, TARGET_MONTH_ISM
            ]
        },
    )


def extract_html_tables(label, url, raw):
    if not BeautifulSoup:
        return
    soup = BeautifulSoup(raw, "html.parser")
    tables = soup.find_all("table")
    rows_out = []
    for i, table in enumerate(tables[:60]):
        rows = []
        for tr in table.find_all("tr")[:60]:
            cells = [normalize(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        rows_out.append({"index": i, "rows": rows})
    log_record(route="HTML_TABLES", label=label, url=url, table_count=len(tables),
               tables=rows_out)


def extract_pandas(label, url, raw):
    if pd is None:
        log_record(route="PANDAS_TABLES", label=label, url=url, status="UNAVAILABLE")
        return
    try:
        frames = pd.read_html(raw)
        result = []
        for i, df in enumerate(frames[:60]):
            result.append({
                "index": i,
                "shape": list(df.shape),
                "columns": [str(c) for c in df.columns],
                "rows": df.head(25).fillna("").astype(str).to_dict("records"),
            })
        log_record(route="PANDAS_TABLES", label=label, url=url,
                   status="OK", table_count=len(frames), tables=result)
    except Exception as exc:
        log_record(route="PANDAS_TABLES", label=label, url=url,
                   status="ERROR", error=repr(exc))


def extract_json(label, url, raw):
    blobs = []
    text = raw.decode("utf-8", "ignore")
    if BeautifulSoup:
        soup = BeautifulSoup(raw, "html.parser")
        for script in soup.find_all("script"):
            content = (script.string or script.get_text()).strip()
            if not content:
                continue
            typ = (script.get("type") or "").lower()
            if typ == "application/ld+json" or content.startswith("{") or content.startswith("["):
                try:
                    blobs.append(json.loads(content))
                except Exception:
                    pass
    # Conservative object/array assignment candidates.
    for candidate in re.findall(
        r"(?s)(?:var|let|const)\s+\w+\s*=\s*(\{.*?\}|\[.*?\]);",
        text
    )[:80]:
        try:
            blobs.append(json.loads(candidate))
        except Exception:
            pass
    log_record(route="EMBEDDED_JSON", label=label, url=url,
               blob_count=len(blobs), blobs=blobs[:40])


def extract_attributes(label, url, raw):
    if not BeautifulSoup:
        return
    soup = BeautifulSoup(raw, "html.parser")
    wanted = re.compile(
        r"pmi|services|manufacturing|nickel|lead|tin|cobalt|actual|previous|"
        r"forecast|consensus|reference|release|date|price|order|employment|"
        r"inventory|backlog|export|import|supplier",
        re.I
    )
    matches = []
    for tag in soup.find_all(True):
        for k, v in tag.attrs.items():
            value = normalize(v if isinstance(v, str) else " ".join(map(str, v)))
            if wanted.search(f"{k} {value}"):
                matches.append({"tag": tag.name, "attribute": k, "value": value[:1600]})
            if len(matches) >= 1000:
                break
        if len(matches) >= 1000:
            break
    log_record(route="HTML_ATTRIBUTES", label=label, url=url, matches=matches)


def extract_source_regex(label, url, raw):
    text = raw.decode("utf-8", "ignore")
    results = {
        "target_date": re.findall(r"2026[-/]\d{1,2}[-/]\d{1,2}", text)[:300],
        "month_labels": re.findall(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+2026",
            text, re.I
        )[:100],
        "numbers": re.findall(r"(?<!\w)\d{1,3}(?:[.,]\d{1,2})?(?!\w)", text)[:500],
        "actual_context": re.findall(
            r"(?is).{0,280}(?:Actual|Previous|Forecast|Consensus|Reference|Release Date|Value).{0,750}",
            text
        )[:200],
        "lme_context": re.findall(
            r"(?is).{0,350}(?:Nickel|Lead|Tin|Cobalt).{0,900}",
            text
        )[:250],
        "ism_context": re.findall(
            r"(?is).{0,350}(?:PMI|Business Activity|New Orders|Employment|Prices|"
            r"Supplier Deliveries|Backlog|Inventories|Inventory Sentiment|"
            r"Imports|Exports|Production).{0,900}",
            text
        )[:300],
    }
    log_record(route="SOURCE_REGEX", label=label, url=url, matches=results)


def inspect_page(label, url):
    response, raw = fetch(label, url)
    if not response or not raw:
        return

    safe_file = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:100] + ".html"
    try:
        (RAW / safe_file).write_bytes(raw)
    except Exception as exc:
        log_record(route="RAW_SAVE", label=label, url=url, status="ERROR", error=repr(exc))

    safe(f"{label}:visible", lambda: extract_visible(label, url, raw))
    safe(f"{label}:tables", lambda: extract_html_tables(label, url, raw))
    safe(f"{label}:pandas", lambda: extract_pandas(label, url, raw))
    safe(f"{label}:json", lambda: extract_json(label, url, raw))
    safe(f"{label}:attrs", lambda: extract_attributes(label, url, raw))
    safe(f"{label}:regex", lambda: extract_source_regex(label, url, raw))


def build_matrix():
    # This is evidence inventory, not value inference.
    corpus = "\n".join(json.dumps(r, ensure_ascii=False) for r in records).lower()
    rows = []

    def status_for(indicator):
        mentions = len(re.findall(re.escape(indicator.lower()), corpus))
        if mentions == 0:
            return "NOT_FOUND"
        if mentions < 3:
            return "PARTIAL"
        return "FOUND"

    for group, names in ISM_TARGETS.items():
        for name in names:
            rows.append({
                "domain": "ISM",
                "group": group,
                "indicator": name,
                "reference_target": TARGET_MONTH_ISM,
                "evidence_status": status_for(name),
                "target_date_proven": "NO",
                "value_validated": "NO",
            })

    for metal in LME_TARGETS:
        rows.append({
            "domain": "LME",
            "group": "OFFICIAL_CASH",
            "indicator": metal,
            "reference_target": TARGET_DATE_LME,
            "evidence_status": status_for(metal),
            "target_date_proven": "YES" if TARGET_DATE_LME in corpus else "NO",
            "value_validated": "NO",
        })
    return rows


def write_outputs():
    rows = build_matrix()

    with ATTEMPTS.open("w", newline="", encoding="utf-8") as f:
        fields = ["label","url","final_url","status","elapsed_s","bytes",
                  "content_type","redirected","error"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)

    with MATRIX.open("w", newline="", encoding="utf-8") as f:
        fields = ["domain","group","indicator","reference_target",
                  "evidence_status","target_date_proven","value_validated"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "fixed_pages": len(PAGES),
        "recursive_crawl": False,
        "target_ism_month": TARGET_MONTH_ISM,
        "target_lme_date": TARGET_DATE_LME,
        "trading_economics_api_used": False,
        "production_file_modified": False,
        "actual_values_auto_inferred": False,
        "http_attempts": len(attempts),
        "evidence_records": len(records),
        "errors": len(errors),
        "status_counts": dict(Counter(r["evidence_status"] for r in rows)),
        "rows": rows,
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"=== {VERSION} ISM / LME PROVENANCE & EXTRACTION ===",
        f"FINISHED=True",
        f"NON_ABORTING=True",
        f"FIXED_PAGES={len(PAGES)}",
        "RECURSIVE_CRAWL=False",
        "TRADING_ECONOMICS_API_USED=False",
        "PRODUCTION_FILE_MODIFIED=False",
        "ACTUAL_VALUES_AUTO_INFERRED=False",
        f"ISM_REFERENCE_MONTH={TARGET_MONTH_ISM}",
        f"LME_TARGET_DATE={TARGET_DATE_LME}",
        f"HTTP_ATTEMPTS={len(attempts)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"ERRORS={len(errors)}",
        "",
        "MATRIX:",
    ]
    for r in rows:
        lines.append(
            f'{r["domain"]} | {r["group"]} | {r["indicator"]} | '
            f'target={r["reference_target"]} | evidence={r["evidence_status"]} | '
            f'target_date_proven={r["target_date_proven"]} | '
            f'value_validated={r["value_validated"]}'
        )
    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines += [f'{e["label"]}: {e["error"]}' for e in errors]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print(f"=== {VERSION} ISM / LME PROVENANCE & EXTRACTION ===")
    print("MODE=FIXED_PAGES|MULTI_EXTRACTION|NON_ABORTING|READ_ONLY")
    print(f"ISM_TARGET_MONTH={TARGET_MONTH_ISM}")
    print(f"LME_TARGET_DATE={TARGET_DATE_LME}")
    print("TRADING_ECONOMICS_API_USED=False")
    print(f"FIXED_PAGE_COUNT={len(PAGES)}")

    for label, url in PAGES:
        safe(f"PAGE:{label}", lambda label=label, url=url: inspect_page(label, url))

    safe("WRITE_OUTPUTS", write_outputs)

    print(f"V9_HTTP_ATTEMPTS={len(attempts)}")
    print(f"V9_EVIDENCE_RECORDS={len(records)}")
    print(f"V9_ERRORS={len(errors)}")
    print("V9_RESULT=COLLECTION_COMPLETE")
    print("V9_PRODUCTION_FILE_MODIFIED=False")
    print("V9_EXIT_POLICY=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
