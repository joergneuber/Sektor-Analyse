#!/usr/bin/env python3
"""
V8 - CONTROLLED MACRO PMI / ISM MULTI-EXTRACTION TEST

Goal:
  One large, controlled, non-aborting test run to determine which public
  extraction routes can actually deliver our macro PMI data.

Design:
  - NO recursive crawler
  - fixed URL list only
  - several extraction methods per page
  - hard per-request timeout
  - every failure is recorded and the next test continues
  - no Trading Economics API key
  - does NOT modify makro_szenario.py
  - never promotes a number to "Actual" merely because it was found

Output:
  macro_pmi_multi_route_v8/
    evidence.jsonl
    attempts.csv
    indicator_matrix.csv
    summary.json
    report.txt
    raw_pages/*.html
"""

import ast
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


VERSION = "V8"
OUT = Path("macro_pmi_multi_route_v8")
RAW = OUT / "raw_pages"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

EVIDENCE = OUT / "evidence.jsonl"
ATTEMPTS = OUT / "attempts.csv"
MATRIX = OUT / "indicator_matrix.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

# Conservative timeouts: connect, read.
TIMEOUT = (8, 15)
MAX_HTML_BYTES = 5_000_000

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
})

TARGETS = {
    "ISM_SERVICES": [
        "PMI", "Business Activity", "New Orders", "New Export Orders",
        "Employment", "Prices", "Supplier Deliveries", "Backlogs",
        "Inventories", "Inventory Sentiment", "Imports", "Exports",
    ],
    "ISM_MANUFACTURING": [
        "PMI", "New Orders", "Production", "Employment", "Prices",
        "Supplier Deliveries", "Backlog of Orders", "Inventories",
        "Customers' Inventories", "Imports", "Exports", "New Export Orders",
    ],
    "SPG_SERVICES": [
        "Business Activity", "New Business", "New Export Business",
        "Employment", "Backlogs", "Input Prices", "Prices Charged",
        "Future Activity",
    ],
    "TE_PUBLIC": [
        "Services PMI", "Non-Manufacturing PMI", "Composite PMI",
        "Actual", "Previous", "Forecast", "Consensus", "Reference", "Release Date",
    ],
}

# Fixed pages. No link-following and no recursion.
PAGES = [
    # Trading Economics public
    ("TE_SERVICES_EN", "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_SERVICES_DE", "https://de.tradingeconomics.com/united-states/services-pmi"),
    ("TE_NON_MANUFACTURING_EN", "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_COMPOSITE_EN", "https://tradingeconomics.com/united-states/composite-pmi"),
    ("TE_SERVICES_FORECAST", "https://tradingeconomics.com/united-states/services-pmi/forecast"),
    ("TE_COMPOSITE_FORECAST", "https://tradingeconomics.com/united-states/composite-pmi/forecast"),
    ("TE_SERVICES_COUNTRY_LIST", "https://tradingeconomics.com/country-list/services-pmi"),
    ("TE_MANUFACTURING_COUNTRY_LIST", "https://tradingeconomics.com/country-list/manufacturing-pmi"),

    # S&P Global public
    ("SPG_PUBLIC_DE", "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "https://www.pmi.spglobal.com/Public?language=en"),
    ("SPG_PRESS_RELEASE", "https://www.pmi.spglobal.com/Public/Home/PressRelease"),

    # ISM public
    ("ISM_HOME", "https://www.ismworld.org/"),
    ("ISM_SERVICES", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
    ("ISM_MANUFACTURING", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/"),
]

# Explicit public endpoint candidates are tested only when they are directly
# named here; discovered links are NOT followed.
EXPLICIT_ENDPOINTS = [
    ("TE_SERVICES_CHART", "https://api.tradingeconomics.com/historical/country/united%20states/indicator/services%20pmi"),
]

records = []
attempt_rows = []
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


def get_page(label, url):
    started = time.monotonic()
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        elapsed = round(time.monotonic() - started, 3)
        content = r.content[:MAX_HTML_BYTES]
        final_url = r.url
        attempt = {
            "label": label, "url": url, "final_url": final_url,
            "status": r.status_code, "elapsed_s": elapsed,
            "bytes": len(content), "content_type": r.headers.get("content-type", ""),
            "redirected": final_url != url, "error": ""
        }
        attempt_rows.append(attempt)
        log_record(route="HTTP", **attempt)
        return r, content
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 3)
        attempt = {
            "label": label, "url": url, "final_url": "",
            "status": "ERROR", "elapsed_s": elapsed,
            "bytes": 0, "content_type": "", "redirected": False,
            "error": repr(exc)
        }
        attempt_rows.append(attempt)
        log_record(route="HTTP", **attempt)
        print(f"WARNUNG: HTTP {label}: {type(exc).__name__}: {exc}")
        return None, b""


def visible_text(raw):
    if BeautifulSoup:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return normalize(soup.get_text(" ", strip=True))
    return normalize(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore")))


def extract_title(raw):
    if not BeautifulSoup:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    return normalize(soup.title.get_text(" ", strip=True)) if soup.title else ""


def extraction_1_visible_text(raw, label, url):
    txt = visible_text(raw)
    hits = target_hits(txt)
    windows = []
    for group, names in TARGETS.items():
        for name in names:
            for m in list(re.finditer(re.escape(name), txt, re.I))[:5]:
                windows.append({
                    "group": group,
                    "indicator": name,
                    "window": txt[max(0, m.start()-250):m.end()+600]
                })
    log_record(route="EXTRACT_VISIBLE_TEXT", label=label, url=url,
               chars=len(txt), target_hits=hits, windows=windows[:300])


def extraction_2_html_tables(raw, label, url):
    if not BeautifulSoup:
        return
    soup = BeautifulSoup(raw, "html.parser")
    tables = soup.find_all("table")
    data = []
    for i, table in enumerate(tables[:40]):
        rows = []
        for tr in table.find_all("tr")[:30]:
            cells = [normalize(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        data.append({"index": i, "rows": rows})
    log_record(route="EXTRACT_HTML_TABLES", label=label, url=url,
               table_count=len(tables), tables=data)


def extraction_3_pandas(raw, label, url):
    if pd is None:
        log_record(route="EXTRACT_PANDAS_READ_HTML", label=label, url=url,
                   status="UNAVAILABLE", reason="pandas not installed")
        return
    try:
        frames = pd.read_html(raw)
        result = []
        for i, df in enumerate(frames[:40]):
            result.append({
                "index": i,
                "shape": list(df.shape),
                "columns": [str(x) for x in df.columns],
                "rows": df.head(20).fillna("").astype(str).to_dict("records")
            })
        log_record(route="EXTRACT_PANDAS_READ_HTML", label=label, url=url,
                   status="OK", table_count=len(frames), tables=result)
    except Exception as exc:
        log_record(route="EXTRACT_PANDAS_READ_HTML", label=label, url=url,
                   status="ERROR", error=repr(exc))


def extraction_4_embedded_json(raw, label, url):
    blobs = []
    text = raw.decode("utf-8", "ignore")
    if BeautifulSoup:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all("script"):
            s = tag.string or tag.get_text()
            s = s.strip()
            if not s:
                continue
            typ = (tag.get("type") or "").lower()
            if typ == "application/ld+json" or s.startswith("{") or s.startswith("["):
                try:
                    blobs.append(json.loads(s))
                except Exception:
                    pass

    # JSON-looking assignments / arrays embedded in scripts.
    candidates = re.findall(r"(?s)(?:var|let|const)\s+\w+\s*=\s*(\{.*?\}|\[.*?\]);", text)
    for candidate in candidates[:50]:
        try:
            blobs.append(json.loads(candidate))
        except Exception:
            pass

    log_record(route="EXTRACT_EMBEDDED_JSON", label=label, url=url,
               blob_count=len(blobs), blobs=blobs[:30])


def extraction_5_attributes(raw, label, url):
    if not BeautifulSoup:
        return
    soup = BeautifulSoup(raw, "html.parser")
    matches = []
    wanted = re.compile(
        r"pmi|services|manufacturing|actual|previous|forecast|consensus|"
        r"reference|release|employment|price|order|business|backlog|export|import",
        re.I
    )
    for tag in soup.find_all(True):
        for key, value in tag.attrs.items():
            val = normalize(value if isinstance(value, str) else " ".join(map(str, value)))
            if wanted.search(f"{key} {val}"):
                matches.append({
                    "tag": tag.name,
                    "attribute": key,
                    "value": val[:1500]
                })
            if len(matches) >= 800:
                break
        if len(matches) >= 800:
            break
    log_record(route="EXTRACT_DATA_ATTRIBUTES", label=label, url=url,
               matches=matches)


def extraction_6_source_regex(raw, label, url):
    text = raw.decode("utf-8", "ignore")
    patterns = {
        "numbers": r"(?<!\w)\d{1,3}(?:[.,]\d{1,2})?(?!\w)",
        "dates": r"\b(?:20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b",
        "actual_context": r"(?is).{0,350}(?:actual|previous|forecast|consensus|reference|release).{0,700}",
        "pmi_context": r"(?is).{0,300}(?:services pmi|non-manufacturing pmi|composite pmi).{0,900}",
    }
    result = {}
    for key, pattern in patterns.items():
        result[key] = re.findall(pattern, text)[:300]
    log_record(route="EXTRACT_SOURCE_REGEX", label=label, url=url, matches=result)


def extraction_7_links_no_follow(raw, label, url):
    if not BeautifulSoup:
        return
    soup = BeautifulSoup(raw, "html.parser")
    links = []
    base = urlparse(url).netloc
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        anchor = normalize(a.get_text(" ", strip=True))
        if urlparse(href).netloc == base and re.search(
            r"pmi|services|service|manufacturing|composite|forecast|historical|release|press",
            f"{anchor} {href}", re.I
        ):
            links.append({"anchor": anchor, "url": href})
    log_record(route="DISCOVER_LINKS_NO_FOLLOW", label=label, url=url,
               count=len(links), links=links[:300])


def extraction_8_dates_and_fields(raw, label, url):
    text = visible_text(raw)
    dates = list(dict.fromkeys(re.findall(
        r"\b(?:20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?|"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+20\d{2})\b",
        text, re.I
    )))
    fields = {}
    for field in ["Actual", "Previous", "Forecast", "Consensus", "Reference",
                  "Release Date", "Last", "Value"]:
        fields[field] = len(re.findall(re.escape(field), text, re.I))
    log_record(route="EXTRACT_DATES_FIELDS", label=label, url=url,
               dates=dates[:300], field_occurrences=fields)


def target_hits(text):
    low = text.lower()
    result = {}
    for group, names in TARGETS.items():
        result[group] = {}
        for name in names:
            result[group][name] = len(re.findall(re.escape(name.lower()), low))
    return result


def inspect_page(label, url):
    r, raw = get_page(label, url)
    if not r:
        return
    if not raw:
        return

    # Always save the bounded raw response for later inspection.
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:100] + ".html"
    try:
        (RAW / safe_name).write_bytes(raw)
    except Exception as exc:
        log_record(route="RAW_SAVE", label=label, url=url,
                   status="ERROR", error=repr(exc))

    log_record(route="PAGE_META", label=label, url=url,
               final_url=r.url, status=r.status_code,
               title=extract_title(raw),
               content_type=r.headers.get("content-type", ""),
               bytes=len(raw))

    # All extraction routes are independent.
    safe(f"{label}:visible", lambda: extraction_1_visible_text(raw, label, url))
    safe(f"{label}:html_tables", lambda: extraction_2_html_tables(raw, label, url))
    safe(f"{label}:pandas", lambda: extraction_3_pandas(raw, label, url))
    safe(f"{label}:json", lambda: extraction_4_embedded_json(raw, label, url))
    safe(f"{label}:attributes", lambda: extraction_5_attributes(raw, label, url))
    safe(f"{label}:regex", lambda: extraction_6_source_regex(raw, label, url))
    safe(f"{label}:links", lambda: extraction_7_links_no_follow(raw, label, url))
    safe(f"{label}:dates", lambda: extraction_8_dates_and_fields(raw, label, url))


def test_explicit_endpoint(label, url):
    # This endpoint is expected to require an API key. It is tested only to
    # document the public/API boundary. No key is supplied and no failure aborts.
    r, raw = get_page(label, url)
    if r:
        log_record(route="EXPLICIT_ENDPOINT_RESULT", label=label, url=url,
                   status=r.status_code,
                   content_type=r.headers.get("content-type", ""),
                   preview=raw.decode("utf-8", "ignore")[:4000])


def build_matrix():
    # Count direct evidence mentions by extraction route. This deliberately
    # does not claim that a mention is a valid Actual.
    combined = []
    for rec in records:
        combined.append(json.dumps(rec, ensure_ascii=False).lower())
    corpus = "\n".join(combined)

    rows = []
    for group, names in TARGETS.items():
        for name in names:
            mentions = len(re.findall(re.escape(name.lower()), corpus))
            if mentions == 0:
                status = "NOT_FOUND"
            elif mentions < 3:
                status = "PARTIAL"
            else:
                status = "FOUND"
            rows.append({
                "group": group,
                "indicator": name,
                "evidence_mentions": mentions,
                "status": status,
                "actual_validated": "NO",
            })
    return rows


def write_outputs():
    rows = build_matrix()

    with ATTEMPTS.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "label", "url", "final_url", "status", "elapsed_s",
            "bytes", "content_type", "redirected", "error"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(attempt_rows)

    with MATRIX.open("w", newline="", encoding="utf-8") as f:
        fields = ["group", "indicator", "evidence_mentions", "status", "actual_validated"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    counts = Counter(r["status"] for r in rows)
    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "production_file_modified": False,
        "trading_economics_api_key_used": False,
        "actual_values_validated": False,
        "pages_fixed_and_non_recursive": True,
        "page_count": len(PAGES),
        "explicit_endpoint_count": len(EXPLICIT_ENDPOINTS),
        "http_attempts": len(attempt_rows),
        "evidence_records": len(records),
        "errors": len(errors),
        "matrix_status_counts": dict(counts),
        "files": [
            str(EVIDENCE), str(ATTEMPTS), str(MATRIX),
            str(SUMMARY), str(REPORT)
        ],
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"=== {VERSION} CONTROLLED MACRO PMI / ISM MULTI-EXTRACTION ===",
        f"FINISHED=True",
        f"NON_ABORTING=True",
        f"PRODUCTION_FILE_MODIFIED=False",
        f"TE_API_KEY_USED=False",
        f"ACTUAL_VALUES_VALIDATED=False",
        f"FIXED_PAGES={len(PAGES)}",
        f"HTTP_ATTEMPTS={len(attempt_rows)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"ERRORS={len(errors)}",
        "",
        "STATUS COUNTS:",
    ]
    for k, v in sorted(counts.items()):
        lines.append(f"{k}={v}")

    lines += ["", "INDICATOR MATRIX:"]
    for r in rows:
        lines.append(
            f'{r["group"]} | {r["indicator"]} | '
            f'{r["status"]} | mentions={r["evidence_mentions"]} | '
            f'actual_validated={r["actual_validated"]}'
        )

    lines += ["", "HTTP ATTEMPTS:"]
    for a in attempt_rows:
        lines.append(
            f'{a["label"]} | {a["status"]} | {a["elapsed_s"]}s | '
            f'{a["bytes"]} bytes | {a["final_url"]}'
        )

    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines.extend(f'{e["label"]}: {e["error"]}' for e in errors)

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print(f"=== {VERSION} CONTROLLED MACRO PMI / ISM MULTI-EXTRACTION ===")
    print("MODE=FIXED_PAGES|NO_RECURSION|NON_ABORTING|READ_ONLY")
    print("TE_API_KEY_USED=False")
    print(f"FIXED_PAGE_COUNT={len(PAGES)}")

    for label, url in PAGES:
        safe(f"PAGE:{label}", lambda label=label, url=url: inspect_page(label, url))

    print(f"EXPLICIT_ENDPOINT_COUNT={len(EXPLICIT_ENDPOINTS)}")
    for label, url in EXPLICIT_ENDPOINTS:
        safe(f"ENDPOINT:{label}", lambda label=label, url=url: test_explicit_endpoint(label, url))

    # Outputs are always written, even if individual probes failed.
    safe("WRITE_OUTPUTS", write_outputs)

    print(f"V8_HTTP_ATTEMPTS={len(attempt_rows)}")
    print(f"V8_EVIDENCE_RECORDS={len(records)}")
    print(f"V8_ERRORS={len(errors)}")
    print("V8_RESULT=COLLECTION_COMPLETE")
    print("V8_PRODUCTION_FILE_MODIFIED=False")
    print("V8_EXIT_POLICY=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
