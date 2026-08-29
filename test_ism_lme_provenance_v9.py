#!/usr/bin/env python3
"""
V9 - BROAD EXTRACTION WITH KNOWN SOURCES ONLY

Same filenames/workflow as the previous V9, but the collection logic is wider
within the already approved source set:

  * ISM official
  * Trading Economics public
  * LME official
  * S&P Global public

No new external source families are introduced.

Goal
----
Try many representations/locations/formats within the known sources in one
controlled run, specifically to find the real numerical values for:

ISM Services / Manufacturing - July 2026
LME Nickel / Lead / Tin / Cobalt - 2026-08-28

Important:
- no TE API
- no recursive crawling
- no production-file modification
- no guessing / no period carry-forward
- every probe is non-fatal
- the test always writes its evidence and exits 0
- "FOUND" and "CANDIDATE" are not the same as VALIDATED_VALUE
- TE public commodity values stay explicitly separate from LME Official values
"""

import csv
import html
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

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


VERSION = "V9-BROAD-KNOWN-SOURCES"

OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

EVIDENCE = OUT / "evidence.jsonl"
ATTEMPTS = OUT / "http_attempts.csv"
CANDIDATES = OUT / "structured_candidates.csv"
MATRIX = OUT / "provenance_matrix.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

ISM_MONTH = "2026-07"
LME_DATE = "2026-08-28"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
TIMEOUT = (8, 15)
BROWSER_TIMEOUT = 25
MAX_BYTES = 8_000_000


# ---------------------------------------------------------------------------
# Exact target vocabulary. Aliases are intentionally conservative.
# ---------------------------------------------------------------------------

ISM_TARGETS = {
    "ISM_SERVICES": {
        "PMI": ["PMI", "Services PMI", "ISM Services PMI", "ISM Services"],
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
        # No "Exports" alias on purpose: do not equate it to New Export Orders.
        "Exports": ["Exports"],
    },
    "ISM_MANUFACTURING": {
        "PMI": ["PMI", "Manufacturing PMI", "ISM Manufacturing PMI", "ISM Manufacturing"],
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
    "Nickel": ["Nickel", "LME Nickel"],
    "Lead": ["Lead", "LME Lead"],
    "Tin": ["Tin", "LME Tin"],
    "Cobalt": ["Cobalt", "LME Cobalt"],
}


# ---------------------------------------------------------------------------
# Known-source URL families only.
# We intentionally enumerate alternate public representations instead of
# discovering and recursively following arbitrary links.
# ---------------------------------------------------------------------------

BASE_PAGES = [
    # Official ISM
    ("ISM_SERVICES_JULY", "ISM_SERVICES", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "ISM_MANUFACTURING", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    ("ISM_SERVICES_INDEX", "ISM_SERVICES", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
    ("ISM_MANUFACTURING_INDEX", "ISM_MANUFACTURING", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/"),

    # Trading Economics public PMI
    ("TE_SERVICES_NONMAN", "TE_SERVICES", "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES", "TE_SERVICES", "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_SERVICES_DE", "TE_SERVICES", "https://de.tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_MANUFACTURING", "TE_MANUFACTURING", "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_MANUFACTURING_DE", "TE_MANUFACTURING", "https://de.tradingeconomics.com/united-states/manufacturing-pmi"),

    # Trading Economics public commodities
    ("TE_COMMODITIES_EN", "TE_COMMODITIES", "https://tradingeconomics.com/commodities"),
    ("TE_COMMODITIES_DE", "TE_COMMODITIES", "https://de.tradingeconomics.com/commodities"),
    ("TE_NICKEL", "TE_COMMODITY", "https://tradingeconomics.com/commodity/nickel"),
    ("TE_LEAD", "TE_COMMODITY", "https://tradingeconomics.com/commodity/lead"),
    ("TE_TIN", "TE_COMMODITY", "https://tradingeconomics.com/commodity/tin"),
    ("TE_COBALT", "TE_COMMODITY", "https://tradingeconomics.com/commodity/cobalt"),
    ("TE_NICKEL_DE", "TE_COMMODITY", "https://de.tradingeconomics.com/commodity/nickel"),
    ("TE_LEAD_DE", "TE_COMMODITY", "https://de.tradingeconomics.com/commodity/lead"),
    ("TE_TIN_DE", "TE_COMMODITY", "https://de.tradingeconomics.com/commodity/tin"),
    ("TE_COBALT_DE", "TE_COMMODITY", "https://de.tradingeconomics.com/commodity/cobalt"),

    # LME official
    ("LME_OFFICIAL", "LME", "https://www.lme.com/market-data/reports-and-data/lme-official-prices"),
    ("LME_NICKEL", "LME", "https://www.lme.com/Metals/Non-ferrous/LME-Nickel"),
    ("LME_LEAD", "LME", "https://www.lme.com/Metals/Non-ferrous/LME-Lead"),
    ("LME_TIN", "LME", "https://www.lme.com/Metals/Non-ferrous/LME-Tin"),
    ("LME_COBALT", "LME", "https://www.lme.com/Metals/Minor-metals/LME-Cobalt"),

    # S&P Global public
    ("SPG_PUBLIC_DE", "SPG", "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "SPG", "https://www.pmi.spglobal.com/Public?language=en"),
    ("SPG_PRESS_RELEASE", "SPG", "https://www.pmi.spglobal.com/Public/Home/PressRelease"),
]


# ---------------------------------------------------------------------------
# Additional variants WITHIN the same source families.
# These are fixed generated URLs, not arbitrary link crawling.
# ---------------------------------------------------------------------------

def build_known_variants():
    pages = list(BASE_PAGES)

    # ISM URL/query variants that sometimes expose the same page differently.
    pages += [
        ("ISM_SERVICES_JULY_TRAILING", "ISM_SERVICES", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/?output=1"),
        ("ISM_MANUFACTURING_JULY_TRAILING", "ISM_MANUFACTURING", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/?output=1"),
        ("ISM_SERVICES_JULY_PRINT", "ISM_SERVICES", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/?print=1"),
        ("ISM_MANUFACTURING_JULY_PRINT", "ISM_MANUFACTURING", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/?print=1"),
    ]

    # TE same source: public chart/history/forecast/page variants.
    te_variants = [
        ("TE_SERVICES_HISTORY", "TE_SERVICES", "https://tradingeconomics.com/united-states/services-pmi?output=1"),
        ("TE_NONMAN_HISTORY", "TE_SERVICES", "https://tradingeconomics.com/united-states/non-manufacturing-pmi?output=1"),
        ("TE_MANUFACTURING_HISTORY", "TE_MANUFACTURING", "https://tradingeconomics.com/united-states/manufacturing-pmi?output=1"),
        ("TE_COMMODITIES_QUERY", "TE_COMMODITIES", "https://tradingeconomics.com/commodities?output=1"),
    ]
    pages += te_variants

    # LME query variants on the same official source, for possible printable/
    # locale renderings. Still fixed; no arbitrary discovery.
    pages += [
        ("LME_OFFICIAL_QUERY", "LME", "https://www.lme.com/market-data/reports-and-data/lme-official-prices?output=1"),
        ("LME_OFFICIAL_PRINT", "LME", "https://www.lme.com/market-data/reports-and-data/lme-official-prices?print=1"),
    ]

    return pages


PAGES = build_known_variants()

records = []
attempts = []
errors = []
page_bodies = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(v):
    return re.sub(r"\s+", " ", html.unescape(str(v or ""))).strip()


def record(route_name, **data):
    # IMPORTANT: route_name is positional; the keyword "route" is never passed
    # by callers, preventing the previous V9 runtime error.
    rec = {"route": route_name, "timestamp_utc": now(), **data}
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


def fetch_requests(label, domain, url):
    t0 = time.monotonic()
    try:
        response = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        body = response.content[:MAX_BYTES]
        row = {
            "label": label, "domain": domain, "route": "requests",
            "url": url, "final_url": response.url,
            "status": response.status_code,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "bytes": len(body),
            "content_type": response.headers.get("content-type", ""),
            "redirected": response.url != url,
            "error": "",
        }
        attempts.append(row)
        record("HTTP_REQUESTS", **row)
        return response, body
    except Exception as exc:
        row = {
            "label": label, "domain": domain, "route": "requests",
            "url": url, "final_url": "",
            "status": "ERROR",
            "elapsed_s": round(time.monotonic() - t0, 3),
            "bytes": 0, "content_type": "",
            "redirected": False, "error": repr(exc),
        }
        attempts.append(row)
        record("HTTP_REQUESTS", **row)
        print(f"WARNUNG: requests {label}: {type(exc).__name__}: {exc}")
        return None, b""


def fetch_browser(label, domain, url):
    if webdriver is None:
        record("SELENIUM", label=label, domain=domain, url=url, status="UNAVAILABLE",
               reason="selenium_not_installed")
        return b""

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
        driver.set_page_load_timeout(BROWSER_TIMEOUT)
        driver.get(url)
        time.sleep(3)
        body = driver.page_source.encode("utf-8", "ignore")[:MAX_BYTES]
        record("SELENIUM", label=label, domain=domain, url=url,
               final_url=driver.current_url,
               status=200 if body else "EMPTY",
               bytes=len(body), elapsed_s=round(time.monotonic() - t0, 3))
        return body
    except Exception as exc:
        record("SELENIUM", label=label, domain=domain, url=url,
               status="ERROR", error=repr(exc),
               elapsed_s=round(time.monotonic() - t0, 3))
        print(f"WARNUNG: Selenium {label}: {type(exc).__name__}: {exc}")
        return b""
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def page_soup(raw):
    return BeautifulSoup(raw, "html.parser") if BeautifulSoup else None


def visible_text(raw):
    soup = page_soup(raw)
    if soup:
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return normalize(soup.get_text(" ", strip=True))
    return normalize(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore")))


def parse_number(cell):
    s = normalize(cell)
    # Preserve decimal semantics for common forms:
    # 54.1 / 54,1 / 16,806 / 16.806
    candidates = re.findall(r"[-+]?\d{1,6}(?:[.,]\d{1,4})?", s)
    for token in candidates:
        try:
            # Heuristic only for a single cell. It is not a production parser.
            if token.count(",") == 1 and token.count(".") == 0:
                value = float(token.replace(",", "."))
            elif token.count(".") == 1 and token.count(",") == 0:
                value = float(token)
            elif token.count(",") == 1 and token.count(".") == 1:
                # Decide based on rightmost separator as decimal separator.
                if token.rfind(",") > token.rfind("."):
                    value = float(token.replace(".", "").replace(",", "."))
                else:
                    value = float(token.replace(",", ""))
            else:
                value = float(token)
        except Exception:
            continue
        if 1900 <= abs(value) <= 2100:
            continue
        return token, value
    return None, None


def month_proven(text):
    low = normalize(text).lower()
    return bool(
        "july 2026" in low
        or "jul 2026" in low
        or "2026-07" in low
        or "2026/07" in low
    )


def lme_date_proven(text):
    low = normalize(text).lower()
    variants = [
        "2026-08-28", "2026/08/28", "28.08.2026", "28/08/2026",
        "08/28/2026", "august 28, 2026", "aug 28, 2026",
        "28 august 2026", "28 aug 2026",
    ]
    return any(v in low for v in variants)


def exact_alias(label, aliases):
    low = normalize(label).lower()
    for alias in sorted(aliases, key=len, reverse=True):
        if low == alias.lower() or re.fullmatch(re.escape(alias.lower()) + r"\s*[:\-]?", low):
            return alias
    return None


def extract_structured_ism(label, domain, raw):
    soup = page_soup(raw)
    if soup is None:
        return

    target_map = ISM_TARGETS.get(domain)
    if not target_map:
        return

    for table_index, table in enumerate(soup.find_all("table")):
        rows = []
        for tr in table.find_all("tr"):
            cells = [normalize(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)

        if not rows:
            continue

        table_text = normalize(table.get_text(" ", strip=True))
        table_month = month_proven(table_text)

        for row_index, row in enumerate(rows):
            if not row:
                continue

            indicator = None
            for target, aliases in target_map.items():
                if exact_alias(row[0], aliases):
                    indicator = target
                    break
            if indicator is None:
                continue

            # Month can be explicit in a header or in the official July page itself.
            header = rows[0]
            month_col = None
            for j, c in enumerate(header):
                if month_proven(c):
                    month_col = j
                    break

            row_has_month = table_month or any(month_proven(c) for c in row)
            if label.endswith("_JULY") and domain.startswith("ISM_"):
                row_has_month = True

            numeric_cells = []
            for j, c in enumerate(row[1:], start=1):
                token, value = parse_number(c)
                if value is not None:
                    numeric_cells.append((j, token, value))

            selections = []
            if month_col is not None and month_col < len(row):
                tok, val = parse_number(row[month_col])
                if val is not None:
                    selections.append((month_col, tok, val, "EXACT_MONTH_HEADER"))
            elif row_has_month and len(numeric_cells) == 1:
                selections.append((numeric_cells[0][0], numeric_cells[0][1],
                                   numeric_cells[0][2], "SINGLE_NUMERIC_ROW_CELL"))
            elif row_has_month and numeric_cells:
                # Wider diagnostic mode: retain all row-level numbers as CANDIDATE,
                # but never mark them validated.
                for j, tok, val in numeric_cells:
                    selections.append((j, tok, val, "ROW_LEVEL_CANDIDATE"))

            for col_index, token, value, basis in selections:
                validated = bool(
                    row_has_month
                    and basis in {"EXACT_MONTH_HEADER", "SINGLE_NUMERIC_ROW_CELL"}
                )
                record(
                    "ISM_STRUCTURED_VALUE",
                    source=label,
                    domain=domain,
                    indicator=indicator,
                    reference_month=ISM_MONTH,
                    value=value,
                    raw_value=token,
                    table_index=table_index,
                    row_index=row_index,
                    column_index=col_index,
                    row=row,
                    header=header,
                    validation_basis=basis,
                    month_proven=row_has_month,
                    validated=validated,
                )


def extract_pandas_ism(label, domain, raw):
    if pd is None or domain not in {"ISM_SERVICES", "ISM_MANUFACTURING"}:
        return
    try:
        frames = pd.read_html(raw)
    except Exception as exc:
        record("PANDAS_ISM", label=label, domain=domain, status="ERROR", error=repr(exc))
        return

    for idx, df in enumerate(frames[:100]):
        clean = df.fillna("").astype(str)
        header_blob = normalize(" ".join(str(x) for x in df.columns.tolist()))
        month_in_header = month_proven(header_blob)
        for ridx, row in clean.iterrows():
            cells = [normalize(x) for x in row.tolist()]
            if not cells:
                continue
            indicator = None
            for target, aliases in ISM_TARGETS[domain].items():
                if exact_alias(cells[0], aliases):
                    indicator = target
                    break
            if not indicator:
                continue
            numeric_cells = []
            for j, c in enumerate(cells[1:], start=1):
                tok, val = parse_number(c)
                if val is not None:
                    numeric_cells.append((j, tok, val))
            if not numeric_cells:
                continue
            # Only one numeric row cell can be validated absent an unambiguous
            # matching month column.
            validated = month_in_header and len(numeric_cells) == 1
            for j, tok, val in numeric_cells:
                record(
                    "ISM_PANDAS_VALUE",
                    source=label,
                    domain=domain,
                    indicator=indicator,
                    reference_month=ISM_MONTH,
                    value=val,
                    raw_value=tok,
                    table_index=idx,
                    row_index=int(ridx),
                    column_index=j,
                    header=list(df.columns),
                    row=cells,
                    month_proven=month_in_header,
                    validated=validated,
                    validation_basis="PANDAS_HEADER_SINGLE_CELL" if validated else "PANDAS_CANDIDATE",
                )


def extract_te_commodity_rows(label, raw):
    if not label.startswith("TE_"):
        return
    soup = page_soup(raw)
    if soup is None:
        return

    # Table row extraction: exact commodity row; we keep all cells for audit.
    for table_index, table in enumerate(soup.find_all("table")):
        for row_index, tr in enumerate(table.find_all("tr")):
            cells = [normalize(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            row_text = " | ".join(cells)
            metal = None
            for m, aliases in LME_TARGETS.items():
                if any(re.search(rf"\b{re.escape(alias)}\b", row_text, re.I) for alias in aliases):
                    metal = m
                    break
            if metal is None:
                continue

            date_ok = lme_date_proven(row_text)
            numeric = []
            for j, cell in enumerate(cells):
                tok, val = parse_number(cell)
                if val is not None:
                    numeric.append((j, tok, val))

            record(
                "TE_COMMODITY_STRUCTURED_ROW",
                source=label,
                indicator=metal,
                target_date=LME_DATE,
                table_index=table_index,
                row_index=row_index,
                row=cells,
                date_proven=date_ok,
                numeric_cells=numeric,
                validated=bool(date_ok and numeric),
                datatype="TE_PUBLIC_COMMODITY",
            )


def extract_html_tables_generic(label, domain, raw):
    soup = page_soup(raw)
    if soup is None:
        return
    tables = []
    for i, table in enumerate(soup.find_all("table")[:120]):
        text_blob = normalize(table.get_text(" ", strip=True))
        rows = []
        for tr in table.find_all("tr")[:120]:
            cells = [normalize(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        tables.append({
            "index": i,
            "month_proven": month_proven(text_blob),
            "lme_date_proven": lme_date_proven(text_blob),
            "rows": rows,
        })
    record(
        "HTML_TABLE_INVENTORY",
        label=label,
        domain=domain,
        table_count=len(tables),
        tables=tables,
    )


def extract_embedded_json(label, domain, raw):
    soup = page_soup(raw)
    if soup is None:
        return
    blobs = []
    for tag in soup.find_all("script"):
        content = (tag.string or tag.get_text()).strip()
        if not content:
            continue
        typ = (tag.get("type") or "").lower()
        if typ == "application/ld+json" or content.startswith("{") or content.startswith("["):
            try:
                blobs.append(json.loads(content))
            except Exception:
                pass
    raw_text = raw.decode("utf-8", "ignore")
    for text_blob in re.findall(
        r"(?s)(?:var|let|const)\s+\w+\s*=\s*(\{.*?\}|\[.*?\]);",
        raw_text
    )[:150]:
        try:
            blobs.append(json.loads(text_blob))
        except Exception:
            pass

    interesting = []
    for blob in blobs[:150]:
        blob_text = json.dumps(blob, ensure_ascii=False)
        if re.search(
            r"PMI|Business Activity|New Orders|New Export Orders|Employment|Prices|"
            r"Supplier Deliveries|Backlog|Inventor|Imports|Exports|Production|"
            r"Nickel|Lead|Tin|Cobalt|Official Price|Cash Bid|Cash Offer|"
            r"2026-08-28|August 28, 2026|July 2026",
            blob_text, re.I
        ):
            interesting.append(blob)
    record(
        "EMBEDDED_JSON_SCAN",
        label=label,
        domain=domain,
        blob_count=len(blobs),
        interesting_blobs=interesting[:100],
    )


def extract_attributes_and_meta(label, domain, raw):
    soup = page_soup(raw)
    if soup is None:
        return
    attr_rows = []
    meta_rows = []

    interest = re.compile(
        r"pmi|services|manufacturing|actual|previous|forecast|consensus|reference|"
        r"release|official|price|cash|date|time|symbol|code|value|nickel|lead|tin|"
        r"cobalt|order|employment|inventory|backlog|export|import|supplier",
        re.I
    )
    for tag in soup.find_all(True):
        attrs = []
        for key, value in tag.attrs.items():
            val = normalize(value if isinstance(value, str) else " ".join(map(str, value)))
            if interest.search(f"{key} {val}"):
                attrs.append({"name": key, "value": val[:1800]})
        if attrs:
            attr_rows.append({
                "tag": tag.name,
                "text": normalize(tag.get_text(" ", strip=True))[:1800],
                "attrs": attrs,
            })
        if len(attr_rows) >= 1800:
            break

    for tag in soup.find_all(["meta", "time"]):
        meta_rows.append({
            "tag": tag.name,
            "name": tag.get("name"),
            "property": tag.get("property"),
            "itemprop": tag.get("itemprop"),
            "datetime": tag.get("datetime"),
            "content": normalize(tag.get("content")),
            "text": normalize(tag.get_text(" ", strip=True)),
        })

    record("HTML_ATTRIBUTES", label=label, domain=domain, rows=attr_rows)
    record("META_TIME", label=label, domain=domain, rows=meta_rows[:2000])


def extract_contexts(label, domain, raw):
    txt = visible_text(raw)
    terms = []
    if domain in {"ISM_SERVICES", "ISM_MANUFACTURING"}:
        for aliases in ISM_TARGETS[domain].values():
            terms.extend(aliases)
    if domain in {"LME", "TE_COMMODITIES", "TE_COMMODITY"}:
        terms.extend(LME_TARGETS.keys())
    for term in sorted(set(terms), key=len, reverse=True):
        windows = []
        for m in list(re.finditer(re.escape(term), txt, re.I))[:80]:
            c = normalize(txt[max(0, m.start()-500):m.end()+1200])
            windows.append({
                "context": c,
                "month_proven": month_proven(c),
                "lme_date_proven": lme_date_proven(c),
                "numbers": [
                    {"token": tok, "value": val}
                    for tok, val in [parse_number(c)]
                    if tok is not None
                ],
            })
        if windows:
            record("VISIBLE_CONTEXT", label=label, domain=domain,
                   indicator=term, windows=windows[:60])


def extract_source_patterns(label, domain, raw):
    raw_text = raw.decode("utf-8", "ignore")
    record(
        "SOURCE_PATTERNS",
        label=label,
        domain=domain,
        july_tokens=re.findall(
            r"July\s*2026|Jul\s*2026|2026[-/]07", raw_text, re.I
        )[:600],
        lme_date_tokens=re.findall(
            r"2026[-/]08[-/]28|28\.08\.2026|08/28/2026|August\s+28,\s+2026",
            raw_text, re.I
        )[:600],
        data_symbols=re.findall(
            r'data-(?:symbol|code|id)\s*=\s*["\']([^"\']+)["\']',
            raw_text, re.I
        )[:1500],
        official_terms=re.findall(
            r"(?is).{0,500}(?:Official Price|Official Cash|Cash Bid|Cash Offer).{0,1800}",
            raw_text
        )[:500],
    )


def extract_links_no_follow(label, domain, url, raw):
    soup = page_soup(raw)
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
    record("LINK_INVENTORY_NO_FOLLOW", label=label, domain=domain,
           count=len(links), links=links[:1200])


def extract_all(label, domain, url, raw):
    safe(f"{label}:tables", lambda: extract_html_tables_generic(label, domain, raw))
    safe(f"{label}:json", lambda: extract_embedded_json(label, domain, raw))
    safe(f"{label}:attrs", lambda: extract_attributes_and_meta(label, domain, raw))
    safe(f"{label}:contexts", lambda: extract_contexts(label, domain, raw))
    safe(f"{label}:source", lambda: extract_source_patterns(label, domain, raw))
    safe(f"{label}:links", lambda: extract_links_no_follow(label, domain, url, raw))

    if domain in {"ISM_SERVICES", "ISM_MANUFACTURING"}:
        safe(f"{label}:ism_structured", lambda: extract_structured_ism(label, domain, raw))
        safe(f"{label}:ism_pandas", lambda: extract_pandas_ism(label, domain, raw))

    if domain in {"TE_COMMODITIES", "TE_COMMODITY"}:
        safe(f"{label}:te_commodity_rows", lambda: extract_te_commodity_rows(label, raw))


def build_candidate_matrix():
    # First, preserve exact structured records from ISM and TE commodity rows.
    candidates = [
        r for r in records
        if r.get("route") in {"ISM_STRUCTURED_VALUE", "ISM_PANDAS_VALUE",
                              "TE_COMMODITY_STRUCTURED_ROW"}
    ]

    with CANDIDATES.open("w", newline="", encoding="utf-8") as f:
        all_keys = set()
        for c in candidates:
            all_keys.update(c.keys())
        fields = [
            "route", "source", "domain", "indicator", "reference_month",
            "target_date", "value", "raw_value", "validated",
            "validation_basis", "table_index", "row_index", "column_index",
            "date_proven", "datatype", "row", "header",
        ]
        fields += sorted(all_keys - set(fields))
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(candidates)

    rows = []

    # ISM matrix
    for domain, target_map in ISM_TARGETS.items():
        for indicator in target_map:
            matches = [
                c for c in candidates
                if c.get("domain") == domain
                and c.get("indicator") == indicator
                and c.get("reference_month") == ISM_MONTH
            ]
            valid = [c for c in matches if c.get("validated") is True]
            values = sorted(set(round(float(c["value"]), 8) for c in valid))
            if len(values) == 1:
                state = "VALIDATED_VALUE"
            elif len(values) > 1:
                state = "AMBIGUOUS_VALUES"
            elif matches:
                state = "CANDIDATE"
            else:
                state = "NOT_FOUND"

            rows.append({
                "domain": domain,
                "indicator": indicator,
                "reference": ISM_MONTH,
                "state": state,
                "candidate_count": len(matches),
                "validated_count": len(valid),
                "values": ";".join(map(str, values)),
                "sources": ";".join(sorted(set(c.get("source", "") for c in matches))),
                "notes": "structured table/row/column validation only",
            })

    # LME official and TE public commodity are strictly separated.
    for metal in LME_TARGETS:
        te_matches = [
            c for c in candidates
            if c.get("route") == "TE_COMMODITY_STRUCTURED_ROW"
            and c.get("indicator") == metal
        ]
        te_valid = [
            c for c in te_matches
            if c.get("validated") is True
            and c.get("target_date") == LME_DATE
        ]
        te_values = sorted(
            set(round(float(v), 8)
                for c in te_valid
                for _, _, v in c.get("numeric_cells", []))
        )

        # LME official structured value records are only considered if an explicit
        # LME date and the metal appear in the same record. No TE row can validate it.
        lme_records = [
            r for r in records
            if r.get("route") in {"HTML_TABLE_INVENTORY", "SOURCE_PATTERNS", "VISIBLE_CONTEXT"}
            and str(r.get("label", "")).startswith("LME_")
            and re.search(rf"\b{re.escape(metal)}\b",
                          json.dumps(r, ensure_ascii=False), re.I)
            and (LME_DATE in json.dumps(r, ensure_ascii=False)
                 or lme_date_proven(json.dumps(r, ensure_ascii=False)))
        ]

        rows.append({
            "domain": "LME_OFFICIAL",
            "indicator": metal,
            "reference": LME_DATE,
            "state": "CANDIDATE" if lme_records else "NOT_FOUND",
            "candidate_count": len(lme_records),
            "validated_count": 0,
            "values": "",
            "sources": ";".join(sorted(set(r.get("label", "") for r in lme_records))),
            "notes": "LME official requires exact metal + exact date + exact price field; TE is never equivalent",
        })

        rows.append({
            "domain": "TE_PUBLIC_COMMODITY",
            "indicator": metal,
            "reference": LME_DATE,
            "state": "VALIDATED_VALUE" if len(te_values) == 1 else (
                "AMBIGUOUS_VALUES" if len(te_values) > 1 else
                "CANDIDATE" if te_matches else "NOT_FOUND"
            ),
            "candidate_count": len(te_matches),
            "validated_count": len(te_valid),
            "values": ";".join(map(str, te_values)),
            "sources": ";".join(sorted(set(c.get("source", "") for c in te_matches))),
            "notes": "public TE commodity value; NOT an LME Official Price",
        })

    return rows


def write_outputs():
    matrix = build_candidate_matrix()

    with MATRIX.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "domain", "indicator", "reference", "state",
            "candidate_count", "validated_count", "values",
            "sources", "notes"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(matrix)

    with ATTEMPTS.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "label", "domain", "route", "url", "final_url", "status",
            "elapsed_s", "bytes", "content_type", "redirected", "error"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)

    counts = defaultdict(int)
    for row in matrix:
        counts[row["state"]] += 1

    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "fixed_known_source_pages": len(PAGES),
        "recursive_crawl": False,
        "trading_economics_api_used": False,
        "production_file_modified": False,
        "value_inference": False,
        "ism_reference_month": ISM_MONTH,
        "lme_target_date": LME_DATE,
        "http_attempts": len(attempts),
        "evidence_records": len(records),
        "errors": len(errors),
        "matrix_counts": dict(counts),
        "important_rules": [
            "New Export Orders is not treated as Exports",
            "TE public commodities are not treated as LME Official Prices",
            "page-wide numbers never validate a value",
            "only structured row/column/date evidence can validate ISM values",
        ],
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"=== {VERSION} ===",
        "FINISHED=True",
        "NON_ABORTING=True",
        "RECURSIVE_CRAWL=False",
        "TRADING_ECONOMICS_API_USED=False",
        "PRODUCTION_FILE_MODIFIED=False",
        "VALUE_INFERENCE=False",
        f"ISM_REFERENCE_MONTH={ISM_MONTH}",
        f"LME_TARGET_DATE={LME_DATE}",
        f"FIXED_KNOWN_SOURCE_PAGES={len(PAGES)}",
        f"HTTP_ATTEMPTS={len(attempts)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"ERRORS={len(errors)}",
        "",
        "MATRIX COUNTS:",
    ]
    lines += [f"{k}={v}" for k, v in sorted(counts.items())]
    lines += ["", "PROVENANCE MATRIX:"]
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
    print("MODE=KNOWN_SOURCES|BROAD_EXTRACTION|STRUCTURED_VALIDATION|OPTIONAL_BROWSER|NON_ABORTING")
    print(f"ISM_REFERENCE_MONTH={ISM_MONTH}")
    print(f"LME_TARGET_DATE={LME_DATE}")
    print("TRADING_ECONOMICS_API_USED=False")
    print("NO_RECURSIVE_CRAWL=True")
    print(f"FIXED_KNOWN_SOURCE_PAGES={len(PAGES)}")

    for label, domain, url in PAGES:
        response, raw = safe(
            f"REQUEST:{label}",
            lambda label=label, domain=domain, url=url: fetch_requests(label, domain, url)
        ) or (None, b"")

        # Use browser fallback only for likely dynamic/block pages. This is still
        # within the same known source URL and never follows discovered links.
        browser_needed = (
            not raw
            or response is None
            or response.status_code in {403, 429, 451}
            or len(raw) < 500
            or (domain.startswith("ISM_") and (
                "SSO/Login" in (response.url if response else "")
                or b"report-headline" not in raw
            ))
            or (domain == "LME" and response is not None and response.status_code >= 400)
        )
        if browser_needed:
            browser_raw = safe(
                f"BROWSER:{label}",
                lambda label=label, domain=domain, url=url: fetch_browser(label, domain, url)
            )
            if browser_raw:
                raw = browser_raw

        if not raw:
            continue

        page_bodies[label] = raw
        try:
            raw_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".html"
            (RAW / raw_name).write_bytes(raw)
        except Exception as exc:
            record("RAW_SAVE", label=label, domain=domain, error=repr(exc))

        safe(f"EXTRACT:{label}", lambda label=label, domain=domain, url=url, raw=raw:
             extract_all(label, domain, url, raw))

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

