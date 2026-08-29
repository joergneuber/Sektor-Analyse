#!/usr/bin/env python3
"""
V9 FINAL - STRUCTURED ISM/LME/TE VALUE PROVENANCE TEST

Same GitHub workflow/file names as previous V9, but this revision uses
structured, field-level extraction instead of broad number harvesting.

Targets
-------
ISM Services + Manufacturing: July 2026
LME Official Prices: 2026-08-28
Trading Economics public commodities: current public table, with special
attention to 2026-08-28 and explicit separation from LME Official Prices.

Extraction routes
-----------------
A. requests HTML
B. BeautifulSoup visible DOM
C. HTML tables
D. pandas.read_html
E. embedded JSON / JSON-LD
F. data-* / itemprop / meta/time attributes
G. source-code regex
H. optional headless Chrome/Selenium fallback for dynamic/blocked pages

No recursive crawling. Discovered links are recorded but never followed.

Validation philosophy
---------------------
A value is VALID only when field + numeric value + target reference period/date
can be established from the same structured row/table or equivalent local
structured record.

A mere mention of a word, a page-wide date, or an unrelated nearby number is
NEVER sufficient.

Important:
- "New Export Orders" is distinct from a hypothetical "Exports" index.
- TE_PUBLIC_COMMODITY values are NEVER labeled as LME Official Prices.
- No values are inferred, estimated, or copied from another period.
- Every individual failure is non-fatal; the collector always reaches the
  output-writing phase and exits 0.
"""

import csv
import html
import json
import re
import time
from collections import defaultdict
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

VERSION = "V9-FINAL-STRUCTURED"
OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

EVIDENCE = OUT / "evidence.jsonl"
CANDIDATES = OUT / "structured_candidates.csv"
MATRIX = OUT / "provenance_matrix.csv"
ATTEMPTS = OUT / "http_attempts.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

ISM_MONTH = "2026-07"
ISM_MONTH_LABELS = {"july 2026", "jul 2026", "2026-07", "2026/07", "july 2026"}
LME_DATE = "2026-08-28"
LME_DATE_LABELS = {
    "2026-08-28", "2026/08/28", "28.08.2026", "28/08/2026",
    "08/28/2026", "august 28, 2026", "aug 28, 2026",
    "28 august 2026", "28 aug 2026",
}

ISM_TARGETS = {
    "SERVICES": {
        "PMI": ["PMI", "Services PMI"],
        "Business Activity": ["Business Activity"],
        "New Orders": ["New Orders"],
        "New Export Orders": ["New Export Orders"],
        "Employment": ["Employment"],
        "Prices": ["Prices"],
        "Supplier Deliveries": ["Supplier Deliveries"],
        "Backlog": ["Backlog", "Backlog of Orders"],
        "Inventories": ["Inventories"],
        "Inventory Sentiment": ["Inventory Sentiment"],
        "Imports": ["Imports"],
        # Intentionally no alias from "New Export Orders" to "Exports".
        "Exports": ["Exports"],
    },
    "MANUFACTURING": {
        "PMI": ["PMI", "Manufacturing PMI"],
        "New Orders": ["New Orders"],
        "Production": ["Production"],
        "Employment": ["Employment"],
        "Prices": ["Prices"],
        "Supplier Deliveries": ["Supplier Deliveries"],
        "Backlog of Orders": ["Backlog of Orders", "Backlog"],
        "Inventories": ["Inventories"],
        "Customers' Inventories": ["Customers' Inventories", "Customers Inventories"],
        "Imports": ["Imports"],
        "Exports": ["Exports"],
        "New Export Orders": ["New Export Orders"],
    },
}

LME_TARGETS = {
    "Nickel": ["Nickel"],
    "Lead": ["Lead", "LME Lead"],
    "Tin": ["Tin", "LME Tin"],
    "Cobalt": ["Cobalt", "LME Cobalt"],
}

PAGES = [
    ("ISM_SERVICES_JULY", "ISM_SERVICES", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "ISM_MANUFACTURING", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    ("TE_SERVICES_NONMAN", "TE_SERVICES", "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES", "TE_SERVICES", "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_MANUFACTURING", "TE_MANUFACTURING", "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_SERVICES_DE", "TE_SERVICES", "https://de.tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_COMMODITIES_EN", "TE_COMMODITIES", "https://tradingeconomics.com/commodities"),
    ("TE_COMMODITIES_DE", "TE_COMMODITIES", "https://de.tradingeconomics.com/commodities"),
    ("TE_COBALT", "TE_COMMODITY", "https://tradingeconomics.com/commodity/cobalt"),
    ("TE_NICKEL", "TE_COMMODITY", "https://tradingeconomics.com/commodity/nickel"),
    ("TE_LEAD", "TE_COMMODITY", "https://tradingeconomics.com/commodity/lead"),
    ("TE_TIN", "TE_COMMODITY", "https://tradingeconomics.com/commodity/tin"),
    ("LME_OFFICIAL", "LME", "https://www.lme.com/market-data/reports-and-data/lme-official-prices"),
    ("LME_NICKEL", "LME", "https://www.lme.com/Metals/Non-ferrous/LME-Nickel"),
    ("LME_LEAD", "LME", "https://www.lme.com/Metals/Non-ferrous/LME-Lead"),
    ("LME_TIN", "LME", "https://www.lme.com/Metals/Non-ferrous/LME-Tin"),
    ("LME_COBALT", "LME", "https://www.lme.com/Metals/Minor-metals/LME-Cobalt"),
    ("SPG_PUBLIC_DE", "SPG", "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "SPG", "https://www.pmi.spglobal.com/Public?language=en"),
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
})
TIMEOUT = (8, 15)
MAX_BYTES = 8_000_000

records, attempts, errors = [], [], []


def now():
    return datetime.now(timezone.utc).isoformat()


def norm(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def log(route, **data):
    rec = {"route": route, "timestamp_utc": now(), **data}
    records.append(rec)
    with EVIDENCE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def safe(label, fn):
    try:
        return fn()
    except Exception as exc:
        errors.append({"label": label, "error": repr(exc)})
        print(f"WARNUNG: {label}: {type(exc).__name__}: {exc}")
        return None


def fetch_requests(label, url):
    t0 = time.monotonic()
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        body = r.content[:MAX_BYTES]
        row = {
            "label": label, "route": "requests", "url": url, "final_url": r.url,
            "status": r.status_code, "elapsed_s": round(time.monotonic() - t0, 3),
            "bytes": len(body), "content_type": r.headers.get("content-type", ""),
            "redirected": r.url != url, "error": "",
        }
        attempts.append(row)
        log("HTTP", **row)
        return r, body
    except Exception as exc:
        row = {
            "label": label, "route": "requests", "url": url, "final_url": "",
            "status": "ERROR", "elapsed_s": round(time.monotonic() - t0, 3),
            "bytes": 0, "content_type": "", "redirected": False,
            "error": repr(exc),
        }
        attempts.append(row)
        log("HTTP", **row)
        print(f"WARNUNG: requests {label}: {type(exc).__name__}: {exc}")
        return None, b""


def fetch_selenium(label, url):
    if webdriver is None:
        log("SELENIUM", label=label, url=url, status="UNAVAILABLE")
        return None, b""
    driver = None
    t0 = time.monotonic()
    try:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(25)
        driver.get(url)
        time.sleep(3)
        source = driver.page_source.encode("utf-8", "ignore")[:MAX_BYTES]
        final_url = driver.current_url
        log("SELENIUM", label=label, url=url, final_url=final_url,
            status=200 if source else "EMPTY",
            bytes=len(source), elapsed_s=round(time.monotonic() - t0, 3))
        return driver, source
    except Exception as exc:
        log("SELENIUM", label=label, url=url, status="ERROR", error=repr(exc),
            elapsed_s=round(time.monotonic() - t0, 3))
        print(f"WARNUNG: Selenium {label}: {type(exc).__name__}: {exc}")
        return None, b""
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def parse_number(s):
    s = norm(s)
    # Remove thousands separators only when the decimal part is clear.
    s = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", s)
    s = s.replace("%", " ")
    # Prefer the first number that is not a year.
    for token in re.findall(r"[-+]?\d{1,6}(?:[.,]\d{1,4})?", s):
        try:
            value = float(token.replace(",", "."))
        except Exception:
            continue
        if 1900 <= abs(value) <= 2100:
            continue
        return token, value
    return None, None


def exact_month_in_record(record):
    low = norm(record).lower()
    return any(v in low for v in ISM_MONTH_LABELS if len(v) > 4)


def exact_lme_date_in_record(record):
    low = norm(record).lower()
    return any(v in low for v in LME_DATE_LABELS)


def equivalent_indicator(label, aliases):
    low = norm(label).lower()
    # Longest alias first prevents "Exports" matching "New Export Orders".
    for alias in sorted(aliases, key=len, reverse=True):
        if re.fullmatch(re.escape(alias.lower()), low):
            return alias
        if re.fullmatch(re.escape(alias.lower()) + r"\s*[:\-]?", low):
            return alias
    return None


def extract_structured_tables(page_label, domain, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return

    # Tables: preserve exact row/column relationships.
    tables = soup.find_all("table")
    table_records = []
    for idx, table in enumerate(tables):
        rows = []
        for tr in table.find_all("tr"):
            cells = [norm(x.get_text(" ", strip=True)) for x in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        table_text = norm(table.get_text(" ", strip=True))
        table_records.append({
            "index": idx,
            "header_context": table_text[:1800],
            "rows": rows[:120],
            "ism_month": exact_month_in_record(table_text),
            "lme_date": exact_lme_date_in_record(table_text),
        })
    log("STRUCTURED_TABLES", label=page_label, domain=domain,
        table_count=len(table_records), tables=table_records[:120])

    # Generic structured candidate extraction from rows.
    if domain in {"ISM_SERVICES", "ISM_MANUFACTURING"}:
        target_map = ISM_TARGETS["SERVICES" if domain == "ISM_SERVICES" else "MANUFACTURING"]
        for table in table_records:
            rows = table["rows"]
            for row_idx, row in enumerate(rows):
                if not row:
                    continue
                row_label = norm(row[0])
                target = None
                for key, aliases in target_map.items():
                    if equivalent_indicator(row_label, aliases):
                        target = key
                        break
                if not target:
                    continue

                # Determine whether the row/table itself provides the target month.
                has_month = table["ism_month"] or any(exact_month_in_record(c) for c in row)
                if not has_month:
                    # July-specific official report URL is strong context, but only
                    # for official ISM pages and only as "page-specific reference".
                    has_month = page_label.endswith("_JULY") and domain.startswith("ISM_")

                # Prefer an explicit header cell matching July 2026 or a July 2026
                # column position; otherwise record candidates without validating.
                headers = rows[0] if rows else []
                col_index = None
                for j, cell in enumerate(headers):
                    if exact_month_in_record(cell):
                        col_index = j
                        break

                # For single-value rows, the only numeric cell after the label
                # may be the index. Never use a page-wide unrelated number.
                numeric_cells = []
                for j, cell in enumerate(row[1:], start=1):
                    _, value = parse_number(cell)
                    if value is not None:
                        numeric_cells.append((j, cell, value))

                selected = []
                if col_index is not None and col_index < len(row):
                    tok, value = parse_number(row[col_index])
                    if value is not None:
                        selected.append((col_index, tok, value, "EXACT_MONTH_COLUMN"))
                elif len(numeric_cells) == 1 and has_month:
                    j, tok, value = numeric_cells[0]
                    selected.append((j, tok, value, "SINGLE_NUMERIC_CELL"))

                for col, tok, value, basis in selected:
                    log("ISM_VALUE_CANDIDATE",
                        source=page_label,
                        domain=domain,
                        indicator=target,
                        reference_month=ISM_MONTH,
                        value=value,
                        raw_value=tok,
                        table_index=table["index"],
                        row_index=row_idx,
                        column_index=col,
                        validation_basis=basis,
                        month_proven=bool(has_month),
                        row=row,
                        validated=bool(has_month and value is not None),
                    )


def extract_commodity_rows(page_label, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return
    target_terms = list(LME_TARGETS)
    for tr in soup.find_all("tr"):
        cells = [norm(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        if not cells:
            continue
        row_text = norm(" | ".join(cells))
        metal = None
        for name, aliases in LME_TARGETS.items():
            if any(re.search(rf"\b{re.escape(a)}\b", row_text, re.I) for a in aliases):
                metal = name
                break
        if not metal:
            continue

        date_ok = exact_lme_date_in_record(row_text)
        numeric = []
        for c in cells:
            tok, value = parse_number(c)
            if value is not None:
                numeric.append((tok, value))
        log("TE_COMMODITY_ROW",
            source=page_label,
            indicator=metal,
            target_date=LME_DATE,
            row=cells,
            date_proven=date_ok,
            numeric_cells=numeric,
            validated=bool(date_ok and numeric),
            datatype="TE_PUBLIC_COMMODITY",
        )


def extract_json_and_attrs(page_label, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return

    blobs = []
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
    log("EMBEDDED_JSON", label=page_label, blob_count=len(blobs),
        interesting=[
            x for x in blobs[:100]
            if re.search(
                r"PMI|Services|Manufacturing|Business Activity|New Orders|Employment|"
                r"Prices|Cobalt|Nickel|Lead|Tin|Official|2026-08-28|July 2026",
                json.dumps(x, ensure_ascii=False), re.I
            )
        ][:80])

    attrs = []
    for tag in soup.find_all(True):
        for key, value in tag.attrs.items():
            val = norm(value if isinstance(value, str) else " ".join(map(str, value)))
            if re.search(
                r"symbol|code|id|date|time|price|value|pmi|cobalt|nickel|lead|tin|"
                r"actual|previous|reference|release",
                f"{key} {val}", re.I
            ):
                attrs.append({
                    "tag": tag.name, "attribute": key, "value": val[:1800],
                    "text": norm(tag.get_text(" ", strip=True))[:1800],
                })
            if len(attrs) >= 1800:
                break
        if len(attrs) >= 1800:
            break
    log("STRUCTURED_ATTRIBUTES", label=page_label, matches=attrs)


def extract_source_regex(page_label, raw):
    text = raw.decode("utf-8", "ignore")
    log("SOURCE_REGEX", label=page_label,
        target_date_hits=re.findall(r"2026[-/]08[-/]28|28\.08\.2026|August 28, 2026", text, re.I)[:300],
        july_hits=re.findall(r"July 2026|Jul 2026|2026[-/]07", text, re.I)[:300],
        symbol_hits=re.findall(r'data-(?:symbol|code|id)\s*=\s*["\']([^"\']+)["\']', text, re.I)[:1000],
        official_context=re.findall(
            r"(?is).{0,500}(?:Official Price|Official Cash|Cash Bid|Cash Offer).{0,1500}",
            text
        )[:300])


def extract_links(page_label, url, raw):
    soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
    if soup is None:
        return
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        anchor = norm(a.get_text(" ", strip=True))
        if re.search(
            r"pmi|services|manufacturing|commodity|cobalt|nickel|lead|tin|"
            r"official|historical|forecast|july|august",
            f"{anchor} {href}", re.I
        ):
            links.append({"anchor": anchor, "url": href})
    log("LINK_INVENTORY_NO_FOLLOW", label=page_label, count=len(links),
        links=links[:800])


def inspect(label, domain, url):
    response, raw = fetch_requests(label, url)
    # The browser route is a fallback, not a second uncontrolled crawl.
    browser_needed = (
        not raw
        or response is None
        or response.status_code in {403, 429, 451}
        or len(raw) < 500
        or (
            domain.startswith("ISM_")
            and ("SSO/Login" in (response.url if response else "") or b"report-headline" not in raw)
        )
    )
    if browser_needed:
        safe(f"SELENIUM:{label}", lambda: fetch_selenium(label, url))

    if not raw and not browser_needed:
        return
    if not raw:
        # Re-attempt via browser for page data if requests gave nothing.
        browser = fetch_selenium(label, url)
        if browser[1]:
            raw = browser[1]

    if not raw:
        return

    try:
        (RAW / (re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".html")).write_bytes(raw)
    except Exception as exc:
        log("RAW_SAVE", label=label, error=repr(exc))

    safe(f"TABLES:{label}", lambda: extract_structured_tables(label, domain, raw))
    safe(f"COMMODITIES:{label}", lambda: extract_commodity_rows(label, raw))
    safe(f"JSON_ATTRS:{label}", lambda: extract_json_and_attrs(label, raw))
    safe(f"SOURCE:{label}", lambda: extract_source_regex(label, raw))
    safe(f"LINKS:{label}", lambda: extract_links(label, url, raw))

    if pd is not None:
        safe(f"PANDAS:{label}", lambda: extract_pandas_tables(label, raw))


def extract_pandas_tables(label, raw):
    try:
        frames = pd.read_html(raw)
    except Exception as exc:
        log("PANDAS_TABLES", label=label, status="ERROR", error=repr(exc))
        return
    payload = []
    for i, df in enumerate(frames[:100]):
        clean = df.fillna("").astype(str)
        payload.append({
            "index": i,
            "shape": list(df.shape),
            "columns": [str(c) for c in df.columns],
            "rows": clean.head(100).to_dict("records"),
        })
    log("PANDAS_TABLES", label=label, status="OK", table_count=len(payload), tables=payload)


def build_matrix():
    rows = []

    # ISM: use only explicit ISM candidates produced from structured rows.
    for domain, target_map in [("ISM_SERVICES", ISM_TARGETS["SERVICES"]),
                               ("ISM_MANUFACTURING", ISM_TARGETS["MANUFACTURING"])]:
        for indicator in target_map:
            candidates = [
                r for r in records
                if r.get("route") == "ISM_VALUE_CANDIDATE"
                and r.get("domain") == domain
                and r.get("indicator") == indicator
                and r.get("reference_month") == ISM_MONTH
            ]
            validated = [r for r in candidates if r.get("validated")]
            values = sorted(set(round(float(r["value"]), 8) for r in validated))
            if len(values) == 1:
                state = "VALIDATED_VALUE"
            elif len(values) > 1:
                state = "AMBIGUOUS_VALUES"
            elif candidates:
                state = "CANDIDATE"
            else:
                state = "NOT_FOUND"
            rows.append({
                "domain": domain,
                "indicator": indicator,
                "reference": ISM_MONTH,
                "state": state,
                "candidate_count": len(candidates),
                "validated_count": len(validated),
                "values": ";".join(map(str, values)),
                "sources": ";".join(sorted(set(r.get("source","") for r in candidates))),
                "notes": "EXACT_ROW_COLUMN_OR_SINGLE_CELL_ON_MONTH_CONTEXT",
            })

    # LME official: only LME-domain rows, never TE commodity rows.
    for metal in LME_TARGETS:
        candidates = [
            r for r in records
            if r.get("route") == "TE_COMMODITY_ROW" and r.get("indicator") == metal
        ]
        # TE is a separate fallback evidence domain. It is never promoted to LME.
        exact_te = [r for r in candidates if r.get("validated") and r.get("target_date") == LME_DATE]

        lme_evidence = [
            r for r in records
            if r.get("route") in {"STRUCTURED_TABLES", "SOURCE_REGEX"}
            and r.get("label","").startswith("LME_")
            and re.search(rf"\b{re.escape(metal)}\b", json.dumps(r, ensure_ascii=False), re.I)
            and (LME_DATE in json.dumps(r, ensure_ascii=False)
                 or any(v.lower() in json.dumps(r, ensure_ascii=False).lower() for v in LME_DATE_LABELS))
        ]

        # Official LME is validated only by future structured LME row evidence.
        rows.append({
            "domain": "LME_OFFICIAL",
            "indicator": metal,
            "reference": LME_DATE,
            "state": "TE_PUBLIC_COMMODITY_CANDIDATE" if exact_te else (
                "CANDIDATE" if lme_evidence else "NOT_FOUND"
            ),
            "candidate_count": len(exact_te) + len(lme_evidence),
            "validated_count": 0,
            "values": ";".join(
                str(v) for r in exact_te for _, v in r.get("numeric_cells", [])
            ),
            "sources": ";".join(sorted(set(
                [r.get("source","") for r in exact_te] +
                [r.get("label","") for r in lme_evidence]
            ))),
            "notes": "TE_PUBLIC_COMMODITY_NEVER_EQUAL_TO_LME_OFFICIAL",
        })

        # Also expose TE public commodity separately.
        rows.append({
            "domain": "TE_PUBLIC_COMMODITY",
            "indicator": metal,
            "reference": LME_DATE,
            "state": "VALIDATED_VALUE" if exact_te else "NOT_FOUND",
            "candidate_count": len(candidates),
            "validated_count": len(exact_te),
            "values": ";".join(
                str(v) for r in exact_te for _, v in r.get("numeric_cells", [])
            ),
            "sources": ";".join(sorted(set(r.get("source","") for r in exact_te))),
            "notes": "PUBLIC_TE_COMMODITY_VALUE;_NOT_LME_OFFICIAL",
        })
    return rows


def write_outputs():
    matrix = build_matrix()
    with MATRIX.open("w", newline="", encoding="utf-8") as f:
        fields = ["domain","indicator","reference","state","candidate_count",
                  "validated_count","values","sources","notes"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(matrix)

    # Preserve every structured candidate in a separate file.
    candidates = [
        r for r in records
        if r.get("route") in {"ISM_VALUE_CANDIDATE", "TE_COMMODITY_ROW"}
    ]
    with CANDIDATES.open("w", newline="", encoding="utf-8") as f:
        fields = sorted(set().union(*(c.keys() for c in candidates))) if candidates else [
            "route", "source", "indicator", "value", "reference"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(candidates)

    with ATTEMPTS.open("w", newline="", encoding="utf-8") as f:
        fields = ["label","route","url","final_url","status","elapsed_s","bytes",
                  "content_type","redirected","error"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(attempts)

    counts = defaultdict(int)
    for row in matrix:
        counts[row["state"]] += 1

    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "fixed_pages": len(PAGES),
        "recursive_crawl": False,
        "te_api_used": False,
        "production_file_modified": False,
        "value_inference": False,
        "ism_reference_month": ISM_MONTH,
        "lme_target_date": LME_DATE,
        "http_attempts": len(attempts),
        "evidence_records": len(records),
        "errors": len(errors),
        "matrix_counts": dict(counts),
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"=== {VERSION} ===",
        "FINISHED=True",
        "NON_ABORTING=True",
        "RECURSIVE_CRAWL=False",
        "TE_API_USED=False",
        "PRODUCTION_FILE_MODIFIED=False",
        "VALUE_INFERENCE=False",
        f"ISM_REFERENCE_MONTH={ISM_MONTH}",
        f"LME_TARGET_DATE={LME_DATE}",
        f"FIXED_PAGES={len(PAGES)}",
        f"HTTP_ATTEMPTS={len(attempts)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"VALUE_CANDIDATES={len(candidates)}",
        f"ERRORS={len(errors)}",
        "",
        "MATRIX:",
    ]
    for row in matrix:
        lines.append(
            f'{row["domain"]} | {row["indicator"]} | target={row["reference"]} | '
            f'state={row["state"]} | candidates={row["candidate_count"]} | '
            f'validated={row["validated_count"]} | values={row["values"]} | '
            f'sources={row["sources"]} | {row["notes"]}'
        )
    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines += [f'{e["label"]}: {e["error"]}' for e in errors]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print(f"=== {VERSION} ===")
    print("MODE=FIXED_PAGES|STRUCTURED_ROWS|MULTI_ROUTE|OPTIONAL_BROWSER|NON_ABORTING|READ_ONLY")
    print("NO_RECURSIVE_CRAWL=True")
    print("TE_API_USED=False")
    print(f"ISM_REFERENCE_MONTH={ISM_MONTH}")
    print(f"LME_TARGET_DATE={LME_DATE}")
    print(f"FIXED_PAGE_COUNT={len(PAGES)}")

    for label, domain, url in PAGES:
        safe(f"PAGE:{label}", lambda label=label, domain=domain, url=url: inspect(label, domain, url))

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
