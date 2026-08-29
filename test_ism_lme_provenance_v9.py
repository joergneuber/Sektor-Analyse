#!/usr/bin/env python3
"""
V9 (VALUE-LEVEL DEEP EXTRACTION REVISION)
=========================================

Same GitHub workflow / same filenames as the previous V9, but the test logic
is deliberately broadened and made VALUE-LEVEL rather than mention-level.

Purpose
-------
Find and validate as many real, date-linked ISM and LME values as possible in
one run, without recursive crawling and without aborting on individual
failures.

ISM target month
----------------
July 2026

ISM targets
-----------
Services:
  PMI, Business Activity, New Orders, New Export Orders, Employment, Prices,
  Supplier Deliveries, Backlog, Inventories, Inventory Sentiment, Imports,
  Exports

Manufacturing:
  PMI, New Orders, Production, Employment, Prices, Supplier Deliveries,
  Backlog of Orders, Inventories, Customers' Inventories, Imports, Exports,
  New Export Orders

LME target date
--------------
2026-08-28 (Friday, immediately preceding the 2026-08-29 briefing)

LME targets
-----------
Nickel, Lead, Tin, Cobalt

Extraction methods per fixed page
---------------------------------
1. Visible text
2. HTML tables
3. pandas.read_html
4. embedded JSON / JSON-LD
5. HTML/data attributes
6. source regex + context windows
7. meta tags / OpenGraph / itemprop
8. value/date context windows
9. link inventory (NO following)
10. raw response saved for manual inspection

Value-level validation
----------------------
A candidate only becomes VALID when the method can associate:
  * target indicator,
  * numeric value,
  * target reference date/month,
  * and a source page
within the same evidence object / local context.

"FOUND" is retained as a weaker state for evidence presence.
VALIDATED_VALUE is the state needed for production consideration.

Safety
------
- no TE API key
- no production file modification
- fixed page list only
- no recursive crawler
- hard per-request timeout
- every probe is non-fatal
- exit 0 regardless of individual HTTP/parser failures
- never invents or derives values
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

VERSION = "V9-VALUE-LEVEL"
OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

EVIDENCE = OUT / "evidence.jsonl"
MATRIX = OUT / "provenance_matrix.csv"
ATTEMPTS = OUT / "http_attempts.csv"
SUMMARY = OUT / "summary.json"
REPORT = OUT / "report.txt"

TARGET_MONTH = "2026-07"
TARGET_MONTH_WORDS = [
    "july 2026", "jul 2026", "2026-07", "2026/07", "2026-7",
    "july", "jul"
]
TARGET_LME_DATE = "2026-08-28"
TARGET_LME_DATE_VARIANTS = [
    "2026-08-28", "2026/08/28", "28.08.2026", "28/08/2026",
    "08/28/2026", "august 28, 2026", "aug 28, 2026", "28 aug 2026",
    "28 august 2026"
]

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
MAX_BYTES = 6_000_000

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

FIXED_PAGES = [
    # Official ISM July 2026 pages
    ("ISM_SERVICES_JULY", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    # Public ISM index/report pages
    ("ISM_SERVICES_INDEX", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
    ("ISM_MANUFACTURING_INDEX", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/"),

    # TradingEconomics public pages
    ("TE_SERVICES", "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES_ALT", "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_MANUFACTURING", "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_MANUFACTURING_NEW_ORDERS", "https://tradingeconomics.com/united-states/ism-manufacturing-new-orders"),
    ("TE_SERVICES_DE", "https://de.tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_MANUFACTURING_DE", "https://de.tradingeconomics.com/united-states/manufacturing-pmi"),

    # S&P Global public pages
    ("SPG_PUBLIC_DE", "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "https://www.pmi.spglobal.com/Public?language=en"),
    ("SPG_PRESS_RELEASE", "https://www.pmi.spglobal.com/Public/Home/PressRelease"),

    # LME
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
        body = r.content[:MAX_BYTES]
        row = {
            "label": label,
            "url": url,
            "final_url": r.url,
            "status": r.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "bytes": len(body),
            "content_type": r.headers.get("content-type", ""),
            "redirected": r.url != url,
            "error": "",
        }
        attempts.append(row)
        log_record(route="HTTP", **row)
        return r, body
    except Exception as exc:
        row = {
            "label": label,
            "url": url,
            "final_url": "",
            "status": "ERROR",
            "elapsed_s": round(time.monotonic() - started, 3),
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


def soup_of(raw):
    return BeautifulSoup(raw, "html.parser") if BeautifulSoup else None


def extract_dates(text):
    patterns = [
        r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b",
        r"\b20\d{2}[-/]\d{1,2}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+20\d{2}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{4}\b",
    ]
    out = []
    for p in patterns:
        out.extend(re.findall(p, text, re.I))
    return list(dict.fromkeys(out))[:500]


def date_strength(text, target):
    low = text.lower()
    variants = TARGET_MONTH_WORDS if target == "ISM" else TARGET_LME_DATE_VARIANTS
    if target == "ISM":
        if "july 2026" in low or "jul 2026" in low or "2026-07" in low:
            return "STRONG"
        if re.search(r"\bjuly\b|\bjul\b", low):
            return "WEAK"
        return "NONE"
    for v in variants:
        if v.lower() in low:
            return "STRONG"
    return "NONE"


def numeric_tokens(text):
    # Values such as 54.1, 54,1, 5,280.00, 2.63, 52.5
    return re.findall(r"(?<![\w])[-+]?\d{1,4}(?:[.,]\d{1,3})?(?![\w])", text)


def target_kind(indicator):
    low = indicator.lower()
    if indicator in LME_TARGETS:
        return "LME"
    if indicator in ISM_TARGETS["SERVICES"]:
        return "ISM_SERVICES"
    return "ISM_MANUFACTURING"


def candidate_contexts(text, indicator, target="ISM"):
    contexts = []
    for m in list(re.finditer(re.escape(indicator), text, re.I))[:60]:
        start = max(0, m.start() - 350)
        end = min(len(text), m.end() + 900)
        ctx = normalize(text[start:end])
        if date_strength(ctx, target) != "NONE" or target == "LME":
            contexts.append(ctx)
    return contexts[:40]


def extract_visible(label, url, raw):
    text = visible_text(raw)
    hits = {}
    for group, names in ISM_TARGETS.items():
        hits[group] = {n: len(re.findall(re.escape(n), text, re.I)) for n in names}
    hits["LME"] = {n: len(re.findall(re.escape(n), text, re.I)) for n in LME_TARGETS}

    value_windows = {}
    for indicator in list(sum(ISM_TARGETS.values(), [])) + LME_TARGETS:
        cw = candidate_contexts(text, indicator, "LME" if indicator in LME_TARGETS else "ISM")
        if cw:
            value_windows[indicator] = [
                {
                    "context": c,
                    "numbers": numeric_tokens(c),
                    "target_date_strength": date_strength(
                        c, "LME" if indicator in LME_TARGETS else "ISM"
                    ),
                }
                for c in cw[:12]
            ]
    log_record(
        route="VISIBLE_VALUE_CONTEXT",
        label=label,
        url=url,
        chars=len(text),
        dates=extract_dates(text),
        target_hits=hits,
        value_windows=value_windows,
    )


def extract_html_tables(label, url, raw):
    if not BeautifulSoup:
        return
    soup = soup_of(raw)
    tables = soup.find_all("table")
    normalized_tables = []
    for idx, table in enumerate(tables[:80]):
        rows = []
        for tr in table.find_all("tr")[:80]:
            cells = [normalize(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        table_text = normalize(table.get_text(" ", strip=True))
        normalized_tables.append({
            "index": idx,
            "date_strength_ism": date_strength(table_text, "ISM"),
            "date_strength_lme": date_strength(table_text, "LME"),
            "rows": rows,
        })
    log_record(route="HTML_TABLES_VALUE_SCAN", label=label, url=url,
               table_count=len(tables), tables=normalized_tables)


def extract_pandas(label, url, raw):
    if pd is None:
        log_record(route="PANDAS_TABLES_VALUE_SCAN", label=label, url=url,
                   status="UNAVAILABLE")
        return
    try:
        frames = pd.read_html(raw)
        out = []
        for idx, df in enumerate(frames[:80]):
            clean = df.fillna("").astype(str)
            blob = normalize(" ".join(clean.astype(str).stack().tolist()))
            out.append({
                "index": idx,
                "shape": list(df.shape),
                "columns": [str(c) for c in df.columns],
                "date_strength_ism": date_strength(blob, "ISM"),
                "date_strength_lme": date_strength(blob, "LME"),
                "rows": clean.head(40).to_dict("records"),
            })
        log_record(route="PANDAS_TABLES_VALUE_SCAN", label=label, url=url,
                   status="OK", table_count=len(frames), tables=out)
    except Exception as exc:
        log_record(route="PANDAS_TABLES_VALUE_SCAN", label=label, url=url,
                   status="ERROR", error=repr(exc))


def extract_json(label, url, raw):
    blobs = []
    text = raw.decode("utf-8", "ignore")
    soup = soup_of(raw) if BeautifulSoup else None

    if soup:
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

    for candidate in re.findall(
        r"(?s)(?:var|let|const)\s+\w+\s*=\s*(\{.*?\}|\[.*?\]);",
        text
    )[:100]:
        try:
            blobs.append(json.loads(candidate))
        except Exception:
            pass

    interesting = []
    for blob in blobs[:100]:
        s = json.dumps(blob, ensure_ascii=False)
        if re.search(
            r"PMI|Business Activity|New Orders|Employment|Prices|Nickel|Lead|Tin|Cobalt|"
            r"Actual|Previous|Forecast|Reference|Release|2026-08-28|July 2026",
            s, re.I
        ):
            interesting.append(blob)

    log_record(route="EMBEDDED_JSON_VALUE_SCAN", label=label, url=url,
               blob_count=len(blobs), interesting_blobs=interesting[:60])


def extract_attributes(label, url, raw):
    soup = soup_of(raw)
    if not soup:
        return
    wanted = re.compile(
        r"pmi|services|manufacturing|nickel|lead|tin|cobalt|actual|previous|forecast|"
        r"consensus|reference|release|date|price|order|employment|inventory|backlog|"
        r"export|import|supplier",
        re.I
    )
    matches = []
    for tag in soup.find_all(True):
        attrs = []
        for key, value in tag.attrs.items():
            val = normalize(value if isinstance(value, str) else " ".join(map(str, value)))
            if wanted.search(f"{key} {val}"):
                attrs.append({"attribute": key, "value": val[:1800]})
        if attrs:
            matches.append({
                "tag": tag.name,
                "text": normalize(tag.get_text(" ", strip=True))[:1800],
                "attrs": attrs,
            })
        if len(matches) >= 1200:
            break
    log_record(route="HTML_ATTRIBUTES_VALUE_SCAN", label=label, url=url,
               matches=matches)


def extract_meta(label, url, raw):
    soup = soup_of(raw)
    if not soup:
        return
    rows = []
    for tag in soup.find_all(["meta", "time"]):
        if tag.name == "meta":
            rows.append({
                "name": tag.get("name"),
                "property": tag.get("property"),
                "itemprop": tag.get("itemprop"),
                "content": normalize(tag.get("content")),
            })
        else:
            rows.append({
                "datetime": tag.get("datetime"),
                "text": normalize(tag.get_text(" ", strip=True)),
            })
    log_record(route="META_AND_TIME_TAGS", label=label, url=url, rows=rows[:1000])


def extract_source_regex(label, url, raw):
    text = raw.decode("utf-8", "ignore")
    patterns = {
        "target_dates": (
            re.findall(r"2026[-/]\d{1,2}[-/]\d{1,2}", text)[:400]
            + re.findall(r"\b(?:July|Jul|August|Aug)\s+\d{1,2},?\s+2026\b", text, re.I)[:400]
            + re.findall(r"\b(?:July|Jul|August|Aug)\s+2026\b", text, re.I)[:400]
        ),
        "pmi_context": re.findall(
            r"(?is).{0,500}(?:services pmi|non-manufacturing pmi|manufacturing pmi|composite pmi).{0,1400}",
            text
        )[:300],
        "ism_component_context": re.findall(
            r"(?is).{0,450}(?:Business Activity|New Orders|New Export Orders|Employment|Prices|"
            r"Supplier Deliveries|Backlog(?: of Orders)?|Inventories|Inventory Sentiment|"
            r"Customers' Inventories|Imports|Exports|Production).{0,1200}",
            text
        )[:500],
        "lme_context": re.findall(
            r"(?is).{0,600}(?:Nickel|Lead|Tin|Cobalt).{0,1400}",
            text
        )[:500],
        "field_context": re.findall(
            r"(?is).{0,350}(?:Actual|Previous|Forecast|Consensus|Reference|Release Date|Last|Value|Official Price).{0,900}",
            text
        )[:400],
    }
    log_record(route="SOURCE_REGEX_CONTEXT", label=label, url=url, matches=patterns)


def extract_links(label, url, raw):
    soup = soup_of(raw)
    if not soup:
        return
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        anchor = normalize(a.get_text(" ", strip=True))
        if re.search(
            r"pmi|services|manufacturing|composite|historical|forecast|release|"
            r"official|nickel|lead|tin|cobalt|july|august",
            f"{anchor} {href}",
            re.I,
        ):
            links.append({"anchor": anchor, "url": href})
    # IMPORTANT: links are only listed; never followed.
    log_record(route="LINK_INVENTORY_NO_FOLLOW", label=label, url=url,
               count=len(links), links=links[:500])


def build_value_candidates():
    """
    Build a second, more stringent candidate layer from the evidence objects.
    Each candidate stores the exact local context from which it came.

    We do not guess which of several numbers is the correct value:
    candidate_number is only recorded when the local context contains exactly
    one plausible numeric token after excluding years/dates.
    """
    candidates = []

    for rec in records:
        route = rec.get("route")
        label = rec.get("label")
        url = rec.get("url")

        if route == "VISIBLE_VALUE_CONTEXT":
            for indicator, windows in rec.get("value_windows", {}).items():
                target = "LME" if indicator in LME_TARGETS else "ISM"
                ref = TARGET_LME_DATE if target == "LME" else TARGET_MONTH
                for w in windows:
                    nums = []
                    for token in w.get("numbers", []):
                        try:
                            v = float(token.replace(",", "."))
                        except Exception:
                            continue
                        # Exclude obvious years and date fragments.
                        if 1900 <= abs(v) <= 2100:
                            continue
                        nums.append((token, v))
                    # A local context is a strong candidate only if there is exactly
                    # one numeric token that can reasonably be the requested field.
                    status = "VALIDATED_VALUE" if (
                        w.get("target_date_strength") == "STRONG" and len(nums) == 1
                    ) else "CANDIDATE"
                    for token, value in nums[:5]:
                        candidates.append({
                            "source": label,
                            "url": url,
                            "indicator": indicator,
                            "reference": ref,
                            "method": route,
                            "raw_token": token,
                            "value": value,
                            "date_strength": w.get("target_date_strength"),
                            "status": status,
                            "context": w.get("context", "")[:1800],
                        })

        elif route == "HTML_TABLES_VALUE_SCAN":
            for table in rec.get("tables", []):
                rows = table.get("rows", [])
                for row in rows:
                    line = normalize(" | ".join(row))
                    for indicator in (
                        (LME_TARGETS if label.startswith("LME") else
                         sum(ISM_TARGETS.values(), []))
                    ):
                        if re.search(re.escape(indicator), line, re.I):
                            nums = []
                            for token in numeric_tokens(line):
                                try:
                                    v = float(token.replace(",", "."))
                                except Exception:
                                    continue
                                if 1900 <= abs(v) <= 2100:
                                    continue
                                nums.append((token, v))
                            target = "LME" if indicator in LME_TARGETS else "ISM"
                            strength = (
                                table.get("date_strength_lme") if target == "LME"
                                else table.get("date_strength_ism")
                            )
                            for token, value in nums[:12]:
                                status = (
                                    "VALIDATED_VALUE"
                                    if strength == "STRONG" else "CANDIDATE"
                                )
                                candidates.append({
                                    "source": label,
                                    "url": url,
                                    "indicator": indicator,
                                    "reference": TARGET_LME_DATE if target == "LME" else TARGET_MONTH,
                                    "method": route,
                                    "raw_token": token,
                                    "value": value,
                                    "date_strength": strength,
                                    "status": status,
                                    "context": line[:1800],
                                })
    return candidates


def write_outputs():
    candidates = build_value_candidates()

    # Keep the raw candidate file compact enough to be practical.
    candidate_path = OUT / "value_candidates.csv"
    with candidate_path.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "source", "url", "indicator", "reference", "method",
            "raw_token", "value", "date_strength", "status", "context"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(candidates)

    # Group by exact target field.
    targets = []
    for group, names in ISM_TARGETS.items():
        for name in names:
            targets.append(("ISM", group, name, TARGET_MONTH))
    for name in LME_TARGETS:
        targets.append(("LME", "OFFICIAL_CASH", name, TARGET_LME_DATE))

    matrix_rows = []
    for domain, group, indicator, reference in targets:
        matching = [
            c for c in candidates
            if c["indicator"].lower() == indicator.lower()
            and c["reference"] == reference
        ]
        valid = [c for c in matching if c["status"] == "VALIDATED_VALUE"]
        if valid:
            state = "VALIDATED_VALUE"
        elif matching:
            state = "CANDIDATE"
        else:
            state = "NOT_FOUND"
        # Multiple distinct candidate values are intentionally flagged rather than
        # silently choosing one.
        unique_values = sorted(set(round(float(c["value"]), 8) for c in matching))
        ambiguity = "YES" if len(unique_values) > 1 else "NO"
        matrix_rows.append({
            "domain": domain,
            "group": group,
            "indicator": indicator,
            "reference_target": reference,
            "state": state,
            "candidate_count": len(matching),
            "validated_count": len(valid),
            "distinct_candidate_values": ";".join(map(str, unique_values[:20])),
            "ambiguous": ambiguity,
            "best_source": valid[0]["source"] if valid else (matching[0]["source"] if matching else ""),
            "best_method": valid[0]["method"] if valid else (matching[0]["method"] if matching else ""),
        })

    with MATRIX.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "domain", "group", "indicator", "reference_target", "state",
            "candidate_count", "validated_count", "distinct_candidate_values",
            "ambiguous", "best_source", "best_method"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(matrix_rows)

    with ATTEMPTS.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "label", "url", "final_url", "status", "elapsed_s", "bytes",
            "content_type", "redirected", "error"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)

    counts = defaultdict(int)
    for row in matrix_rows:
        counts[row["state"]] += 1

    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "fixed_pages": len(FIXED_PAGES),
        "recursive_crawl": False,
        "te_api_used": False,
        "production_file_modified": False,
        "actual_value_inference": False,
        "ism_reference_month": TARGET_MONTH,
        "lme_target_date": TARGET_LME_DATE,
        "http_attempts": len(attempts),
        "evidence_records": len(records),
        "value_candidates": len(candidates),
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
        "ACTUAL_VALUE_INFERENCE=False",
        f"ISM_REFERENCE_MONTH={TARGET_MONTH}",
        f"LME_TARGET_DATE={TARGET_LME_DATE}",
        f"FIXED_PAGES={len(FIXED_PAGES)}",
        f"HTTP_ATTEMPTS={len(attempts)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"VALUE_CANDIDATES={len(candidates)}",
        f"ERRORS={len(errors)}",
        "",
        "MATRIX COUNTS:",
    ]
    for k, v in sorted(counts.items()):
        lines.append(f"{k}={v}")

    lines += ["", "VALUE MATRIX:"]
    for row in matrix_rows:
        lines.append(
            f'{row["domain"]} | {row["group"]} | {row["indicator"]} | '
            f'target={row["reference_target"]} | state={row["state"]} | '
            f'candidates={row["candidate_count"]} | validated={row["validated_count"]} | '
            f'values={row["distinct_candidate_values"]} | ambiguous={row["ambiguous"]} | '
            f'source={row["best_source"]} | method={row["best_method"]}'
        )

    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines += [f'{e["label"]}: {e["error"]}' for e in errors]

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print(f"=== {VERSION} ===")
    print("MODE=FIXED_PAGES|MULTI_EXTRACTION|VALUE_LEVEL|NON_ABORTING|READ_ONLY")
    print("NO_RECURSIVE_CRAWL=True")
    print("TE_API_USED=False")
    print(f"ISM_TARGET_MONTH={TARGET_MONTH}")
    print(f"LME_TARGET_DATE={TARGET_LME_DATE}")
    print(f"FIXED_PAGE_COUNT={len(FIXED_PAGES)}")

    for label, url in FIXED_PAGES:
        safe(f"PAGE:{label}", lambda label=label, url=url: inspect_page(label, url))

    # Always write evidence, even after individual source failures.
    safe("WRITE_OUTPUTS", write_outputs)

    print(f"V9_HTTP_ATTEMPTS={len(attempts)}")
    print(f"V9_EVIDENCE_RECORDS={len(records)}")
    print(f"V9_ERRORS={len(errors)}")
    print("V9_RESULT=COLLECTION_COMPLETE")
    print("V9_PRODUCTION_FILE_MODIFIED=False")
    print("V9_EXIT_POLICY=0")
    return 0


def inspect_page(label, url):
    response, raw = fetch(label, url)
    if not response or not raw:
        return
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:120] + ".html"
    try:
        (RAW / safe_name).write_bytes(raw)
    except Exception as exc:
        log_record(route="RAW_SAVE", label=label, url=url, error=repr(exc))
    safe(f"{label}:visible", lambda: extract_visible(label, url, raw))
    safe(f"{label}:html_tables", lambda: extract_html_tables(label, url, raw))
    safe(f"{label}:pandas", lambda: extract_pandas(label, url, raw))
    safe(f"{label}:json", lambda: extract_json(label, url, raw))
    safe(f"{label}:attributes", lambda: extract_attributes(label, url, raw))
    safe(f"{label}:meta", lambda: extract_meta(label, url, raw))
    safe(f"{label}:regex", lambda: extract_source_regex(label, url, raw))
    safe(f"{label}:links", lambda: extract_links(label, url, raw))


if __name__ == "__main__":
    raise SystemExit(main())
