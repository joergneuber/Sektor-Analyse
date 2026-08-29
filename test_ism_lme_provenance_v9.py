#!/usr/bin/env python3
"""
V9 - TABLE / HTML SCHEMA ANALYZER
Same workflow and filenames as the previous V9; content revised only.

Purpose
-------
Do NOT try to validate production values yet.

Instead, inspect the HTML/DOM/table schema of our already known sources so we
can see EXACTLY how the useful values are represented and build the production
parser from the real structure.

Known sources only:
- official ISM
- Trading Economics public
- LME official
- S&P Global public

Focus:
- ISM Services July 2026
- ISM Manufacturing July 2026
- TE US ISM/PMI pages
- TE public commodities table and individual commodity pages
- LME Official Prices + known metal pages
- S&P Global public page

The analyzer records:
- page title / final URL / status
- every HTML table (bounded, but structurally complete for normal tables)
- every header row and row cell with indexes
- normalized multi-row headers
- target-row matches
- parent/child DOM structure around target labels
- relevant data-* / itemprop / meta / time attributes
- embedded JSON snippets
- source-code patterns / endpoint-looking strings (discovery only)
- links (inventory only, never followed)
- optional Selenium-rendered DOM for pages that look dynamic or blocked

IMPORTANT:
  This is a diagnostic schema test, NOT a production parser.
  No value is automatically promoted to production.
  No new source family is introduced.
  Every individual probe is non-fatal and the workflow always exits 0.
"""

import csv
import html
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
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
except Exception:
    webdriver = None
    Options = None


VERSION = "V9-TABLE-SCHEMA"

OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

SCHEMA_JSONL = OUT / "table_schema.jsonl"
ROWS_CSV = OUT / "table_rows.csv"
TARGET_CSV = OUT / "target_row_matches.csv"
DOM_JSONL = OUT / "dom_target_context.jsonl"
HTTP_CSV = OUT / "http_attempts.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

ISM_MONTH = "2026-07"
LME_DATE = "2026-08-28"

ISM_SERVICES = [
    "PMI", "Business Activity", "New Orders", "New Export Orders",
    "Employment", "Prices", "Supplier Deliveries", "Backlog",
    "Inventories", "Inventory Sentiment", "Imports", "Exports",
]
ISM_MANUFACTURING = [
    "PMI", "New Orders", "Production", "Employment", "Prices",
    "Supplier Deliveries", "Backlog of Orders", "Inventories",
    "Customers' Inventories", "Imports", "Exports", "New Export Orders",
]
LME_TARGETS = ["Nickel", "Lead", "Tin", "Cobalt"]

TARGETS_BY_PAGE = {
    "ISM_SERVICES": ISM_SERVICES,
    "ISM_MANUFACTURING": ISM_MANUFACTURING,
    "TE_SERVICES": ["Services PMI", "Business Activity", "New Orders", "Employment", "Prices"],
    "TE_MANUFACTURING": ["Manufacturing PMI", "New Orders", "Production", "Employment", "Prices"],
    "TE_COMMODITIES": LME_TARGETS,
    "TE_COMMODITY": LME_TARGETS,
    "LME": LME_TARGETS,
}

PAGES = [
    ("ISM_SERVICES_JULY", "ISM_SERVICES",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "ISM_MANUFACTURING",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    ("ISM_SERVICES_INDEX", "ISM_SERVICES",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
    ("ISM_MANUFACTURING_INDEX", "ISM_MANUFACTURING",
     "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/"),
    ("TE_SERVICES_NONMAN", "TE_SERVICES",
     "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES", "TE_SERVICES",
     "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_MANUFACTURING", "TE_MANUFACTURING",
     "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_COMMODITIES_EN", "TE_COMMODITIES",
     "https://tradingeconomics.com/commodities"),
    ("TE_COBALT", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/cobalt"),
    ("TE_NICKEL", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/nickel"),
    ("TE_LEAD", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/lead"),
    ("TE_TIN", "TE_COMMODITY",
     "https://tradingeconomics.com/commodity/tin"),
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
    ("SPG_PUBLIC_DE", "SPG",
     "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "SPG",
     "https://www.pmi.spglobal.com/Public?language=en"),
]

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
MAX_BYTES = 8_000_000

records = []
attempts = []
errors = []


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def emit(route_name, **data):
    rec = {"route": route_name, "timestamp_utc": now(), **data}
    records.append(rec)
    with SCHEMA_JSONL.open("a", encoding="utf-8") as f:
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
        emit("HTTP_REQUESTS", **row)
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
        emit("HTTP_REQUESTS", **row)
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
        time.sleep(2.5)
        body = driver.page_source.encode("utf-8", "ignore")[:MAX_BYTES]
        emit("SELENIUM", label=label, domain=domain, url=url,
             final_url=driver.current_url,
             status=200 if body else "EMPTY",
             bytes=len(body),
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


def header_matrix(rows):
    """
    Return a conservative normalized header representation.
    We preserve every original header row and also produce a simple combined
    label for inspection. No value is inferred from this function.
    """
    if not rows:
        return {"raw_header_rows": [], "combined": []}

    # Treat first up to 3 rows as possible headers only if they contain no target
    # row labels and are reasonably header-like.
    head_count = min(3, len(rows))
    header_rows = rows[:head_count]
    width = max(len(r) for r in header_rows)
    combined = []

    for col in range(width):
        parts = []
        for row in header_rows:
            cell = normalize(row[col] if col < len(row) else "")
            if cell and cell not in parts:
                parts.append(cell)
        combined.append(" | ".join(parts))

    return {"raw_header_rows": header_rows, "combined": combined}


def parse_table(table):
    rows = []
    row_meta = []
    for tr_idx, tr in enumerate(table.find_all("tr")):
        cells = []
        for cell_idx, cell in enumerate(tr.find_all(["th", "td"])):
            text = normalize(cell.get_text(" ", strip=True))
            cells.append(text)
        if cells:
            rows.append(cells)
            row_meta.append({
                "row_index": tr_idx,
                "cell_count": len(cells),
                "cell_types": [
                    c.name for c in tr.find_all(["th", "td"])
                ],
            })
    return rows, row_meta


def target_match(row_label, target):
    low = normalize(row_label).lower()
    t = normalize(target).lower()
    if low == t:
        return True
    if low.startswith(t + " ") or low.startswith(t + ":") or low.startswith(t + "-"):
        return True
    return False


def table_schema_scan(label, domain, url, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return

    targets = TARGETS_BY_PAGE.get(domain, [])
    tables = soup.find_all("table")
    emit("TABLE_SCHEMA",
         label=label, domain=domain, url=url,
         table_count=len(tables))

    row_csv_records = []
    target_records = []

    for table_idx, table in enumerate(tables):
        rows, row_meta = parse_table(table)
        hm = header_matrix(rows)
        table_text = normalize(table.get_text(" ", strip=True))

        table_record = {
            "label": label,
            "domain": domain,
            "url": url,
            "table_index": table_idx,
            "row_count": len(rows),
            "max_columns": max((len(r) for r in rows), default=0),
            "month_mentions": [
                x for x in re.findall(
                    r"July\s+2026|Jul\s+2026|2026[-/]07",
                    table_text, re.I
                )
            ][:30],
            "lme_date_mentions": [
                x for x in re.findall(
                    r"2026[-/]08[-/]28|28\.08\.2026|08/28/2026|August\s+28,\s+2026",
                    table_text, re.I
                )
            ][:30],
            "header": hm,
            "row_meta": row_meta[:200],
        }
        emit("TABLE_SCHEMA_DETAIL", **table_record)

        # Write every table row into CSV for direct inspection.
        for row_idx, row in enumerate(rows):
            row_csv_records.append({
                "label": label,
                "domain": domain,
                "url": url,
                "table_index": table_idx,
                "row_index": row_idx,
                "row": json.dumps(row, ensure_ascii=False),
                "header_combined": json.dumps(hm["combined"], ensure_ascii=False),
            })

            for target in targets:
                if row and target_match(row[0], target):
                    target_records.append({
                        "label": label,
                        "domain": domain,
                        "url": url,
                        "table_index": table_idx,
                        "row_index": row_idx,
                        "target": target,
                        "row": row,
                        "header_combined": hm["combined"],
                        "target_month_present_in_table": bool(
                            re.search(r"July\s+2026|Jul\s+2026|2026[-/]07",
                                      table_text, re.I)
                        ),
                        "target_lme_date_present_in_table": bool(
                            re.search(
                                r"2026[-/]08[-/]28|28\.08\.2026|08/28/2026|August\s+28,\s+2026",
                                table_text, re.I
                            )
                        ),
                    })

    with ROWS_CSV.open("a", newline="", encoding="utf-8") as f:
        fields = ["label","domain","url","table_index","row_index","row","header_combined"]
        w = csv.DictWriter(f, fieldnames=fields)
        if f.tell() == 0:
            w.writeheader()
        w.writerows(row_csv_records)

    with TARGET_CSV.open("a", newline="", encoding="utf-8") as f:
        fields = [
            "label","domain","url","table_index","row_index","target",
            "row","header_combined","target_month_present_in_table",
            "target_lme_date_present_in_table"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        if f.tell() == 0:
            w.writeheader()
        for rec in target_records:
            out = dict(rec)
            out["row"] = json.dumps(rec["row"], ensure_ascii=False)
            out["header_combined"] = json.dumps(rec["header_combined"], ensure_ascii=False)
            w.writerow(out)

    emit("TARGET_ROW_SUMMARY", label=label, domain=domain, url=url,
         target_matches=target_records)


def dom_context_scan(label, domain, url, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return
    targets = TARGETS_BY_PAGE.get(domain, [])
    matches = []

    for target in targets:
        # Find exact/near text nodes and capture parent chain structure.
        for text_node in soup.find_all(string=re.compile(re.escape(target), re.I))[:40]:
            node = text_node.parent
            chain = []
            current = node
            for _ in range(6):
                if current is None:
                    break
                chain.append({
                    "tag": current.name,
                    "id": current.get("id"),
                    "class": current.get("class"),
                    "data_attrs": {
                        k: normalize(v if isinstance(v, str) else " ".join(map(str, v)))
                        for k, v in current.attrs.items()
                        if str(k).lower().startswith("data-")
                    },
                    "text": normalize(current.get_text(" ", strip=True))[:2200],
                })
                current = current.parent

            matches.append({
                "target": target,
                "chain": chain,
            })

    with DOM_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "label": label,
            "domain": domain,
            "url": url,
            "matches": matches[:500],
        }, ensure_ascii=False, default=str) + "\n")

    emit("DOM_TARGET_CONTEXT", label=label, domain=domain, url=url,
         match_count=len(matches), matches=matches[:500])


def json_scan(label, domain, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return
    blobs = []
    for script_tag in soup.find_all("script"):
        content = (script_tag.string or script_tag.get_text()).strip()
        if not content:
            continue
        typ = (script_tag.get("type") or "").lower()
        if typ == "application/ld+json" or content.startswith("{") or content.startswith("["):
            try:
                blobs.append(json.loads(content))
            except Exception:
                pass

    interesting = []
    for blob in blobs[:200]:
        s = json.dumps(blob, ensure_ascii=False)
        if re.search(
            r"PMI|Business Activity|New Orders|Employment|Prices|Backlog|"
            r"Inventor|Imports|Exports|Production|Nickel|Lead|Tin|Cobalt|"
            r"Official Price|Cash Bid|2026-08-28|July 2026",
            s, re.I
        ):
            interesting.append(blob)

    emit("JSON_SCAN", label=label, domain=domain,
         blob_count=len(blobs), interesting=interesting[:100])


def attr_meta_scan(label, domain, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return

    interest = re.compile(
        r"symbol|code|id|date|time|price|value|pmi|actual|previous|forecast|"
        r"reference|release|official|cash|nickel|lead|tin|cobalt|order|"
        r"employment|backlog|inventory|export|import",
        re.I
    )
    attrs = []
    for tag in soup.find_all(True):
        good = []
        for k, v in tag.attrs.items():
            val = normalize(v if isinstance(v, str) else " ".join(map(str, v)))
            if interest.search(f"{k} {val}"):
                good.append({"name": k, "value": val[:1800]})
        if good:
            attrs.append({
                "tag": tag.name,
                "text": normalize(tag.get_text(" ", strip=True))[:1800],
                "attrs": good,
            })
        if len(attrs) >= 1800:
            break

    meta = []
    for tag in soup.find_all(["meta", "time"]):
        meta.append({
            "tag": tag.name,
            "name": tag.get("name"),
            "property": tag.get("property"),
            "itemprop": tag.get("itemprop"),
            "datetime": tag.get("datetime"),
            "content": normalize(tag.get("content")),
            "text": normalize(tag.get_text(" ", strip=True)),
        })

    emit("ATTR_META_SCAN", label=label, domain=domain,
         attrs=attrs, meta=meta[:1800])


def source_pattern_scan(label, domain, raw):
    txt = raw.decode("utf-8", "ignore")
    emit("SOURCE_PATTERN_SCAN", label=label, domain=domain,
         july_tokens=re.findall(r"July\s+2026|Jul\s+2026|2026[-/]07", txt, re.I)[:800],
         lme_tokens=re.findall(
             r"2026[-/]08[-/]28|28\.08\.2026|08/28/2026|August\s+28,\s+2026",
             txt, re.I
         )[:800],
         symbols=re.findall(
             r'data-(?:symbol|code|id)\s*=\s*["\']([^"\']+)["\']',
             txt, re.I
         )[:2000],
         endpoint_like=re.findall(
             r'https?://[^"\'>\s]+|/(?:api|ajax|data|chart|historical|forecast)[^"\'>\s]+',
             txt, re.I
         )[:1500],
         official_price_context=re.findall(
             r"(?is).{0,450}(?:Official Price|Official Cash|Cash Bid|Cash Offer).{0,1600}",
             txt
         )[:500])


def links_inventory(label, domain, url, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        anchor = normalize(a.get_text(" ", strip=True))
        if re.search(
            r"pmi|services|manufacturing|commodity|nickel|lead|tin|cobalt|"
            r"official|historical|forecast|release|july|august",
            f"{anchor} {href}", re.I
        ):
            links.append({"anchor": anchor, "url": href})
    emit("LINKS_NO_FOLLOW", label=label, domain=domain,
         count=len(links), links=links[:1500])


def inspect_page(label, domain, url):
    response, raw = safe(
        f"REQUEST:{label}",
        lambda: fetch_requests(label, domain, url)
    ) or (None, b"")

    # Browser fallback is diagnostic only and only for likely dynamic/blocking
    # pages. It does not follow any discovered links.
    need_browser = (
        not raw
        or response is None
        or response.status_code in {403, 429, 451}
        or (domain.startswith("ISM_") and (
            "SSO/Login" in (response.url if response else "")
            or len(raw) < 5000
        ))
        or (domain == "LME" and response is not None and response.status_code >= 400)
        or (domain == "SPG" and len(raw) < 5000)
    )
    if need_browser:
        browser_raw = safe(
            f"BROWSER:{label}",
            lambda: fetch_browser(label, domain, url)
        )
        if browser_raw:
            # For diagnostics, prefer browser DOM if requests is blocked/too sparse.
            raw = browser_raw

    if not raw:
        emit("PAGE_SKIPPED", label=label, domain=domain, url=url,
             reason="no usable response body")
        return

    try:
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".html"
        (RAW / filename).write_bytes(raw)
    except Exception as exc:
        emit("RAW_SAVE", label=label, domain=domain, error=repr(exc))

    safe(f"TABLES:{label}", lambda: table_schema_scan(label, domain, url, raw))
    safe(f"DOM:{label}", lambda: dom_context_scan(label, domain, url, raw))
    safe(f"JSON:{label}", lambda: json_scan(label, domain, raw))
    safe(f"ATTR:{label}", lambda: attr_meta_scan(label, domain, raw))
    safe(f"SOURCE:{label}", lambda: source_pattern_scan(label, domain, raw))
    safe(f"LINKS:{label}", lambda: links_inventory(label, domain, url, raw))


def write_outputs():
    if not ROWS_CSV.exists():
        ROWS_CSV.write_text(
            "label,domain,url,table_index,row_index,row,header_combined\n",
            encoding="utf-8"
        )
    if not TARGET_CSV.exists():
        TARGET_CSV.write_text(
            "label,domain,url,table_index,row_index,target,row,header_combined,"
            "target_month_present_in_table,target_lme_date_present_in_table\n",
            encoding="utf-8"
        )

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
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Compact console/report digest: page/table counts + target row count.
    table_details = [r for r in records if r.get("route") == "TABLE_SCHEMA_DETAIL"]
    target_summaries = [r for r in records if r.get("route") == "TARGET_ROW_SUMMARY"]

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
        f"TABLES_ANALYZED={len(table_details)}",
        f"TARGET_ROW_SUMMARIES={len(target_summaries)}",
        f"ERRORS={len(errors)}",
        "",
        "PAGE/TABLE DIGEST:",
    ]
    for rec in table_details:
        lines.append(
            f'{rec.get("label")} | table={rec.get("table_index")} | '
            f'rows={rec.get("row_count")} | max_cols={rec.get("max_columns")} | '
            f'July2026={rec.get("month_mentions")} | LME28Aug={rec.get("lme_date_mentions")}'
        )
    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines.extend(f'{e["label"]}: {e["error"]}' for e in errors)

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print(f"=== {VERSION} ===")
    print("MODE=KNOWN_SOURCES|HTML_SCHEMA|TABLE_STRUCTURE|DOM|JSON|BROWSER_FALLBACK|NON_ABORTING")
    print("RECURSIVE_CRAWL=False")
    print("TE_API_USED=False")
    print(f"ISM_REFERENCE_MONTH={ISM_MONTH}")
    print(f"LME_TARGET_DATE={LME_DATE}")
    print(f"FIXED_PAGE_COUNT={len(PAGES)}")

    # Validate output headers at startup so files are usable even if every page fails.
    write_outputs()

    for label, domain, url in PAGES:
        safe(f"PAGE:{label}", lambda label=label, domain=domain, url=url:
             inspect_page(label, domain, url))

    safe("FINAL_WRITE", write_outputs)

    print(f"V9_HTTP_ATTEMPTS={len(attempts)}")
    print(f"V9_EVIDENCE_RECORDS={len(records)}")
    print(f"V9_ERRORS={len(errors)}")
    print("V9_RESULT=SCHEMA_COLLECTION_COMPLETE")
    print("V9_PRODUCTION_FILE_MODIFIED=False")
    print("V9_EXIT_POLICY=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
