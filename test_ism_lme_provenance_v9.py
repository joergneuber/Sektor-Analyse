#!/usr/bin/env python3
"""
V9 broad public-data extraction probe
Same workflow / filename as previous V9, revised content.

Focus:
- ISM Services + Manufacturing July 2026
- LME/commodity prices for 2026-08-28
- TradingEconomics public commodity table as an ADDITIONAL public fallback
- multiple independent extraction methods
- no TE API
- no recursive crawling
- no production changes
- never declare a value valid merely because the word or a number was found

Important distinction:
TradingEconomics commodity prices are NOT automatically treated as LME Official
Prices. They are recorded as TE_PUBLIC_COMMODITY and must not overwrite an LME
OFFICIAL_CASH value without a separate provenance decision.
"""

import csv
import html
import json
import re
import time
from collections import defaultdict
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

VERSION = "V9-TE-COMMODITIES-BROAD"
OUT = Path("ism_lme_provenance_v9")
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True)
RAW.mkdir(exist_ok=True)

EVIDENCE = OUT / "evidence.jsonl"
MATRIX = OUT / "provenance_matrix.csv"
CANDIDATES = OUT / "value_candidates.csv"
ATTEMPTS = OUT / "http_attempts.csv"
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
})
TIMEOUT = (8, 15)
MAX_BYTES = 7_000_000

ISM = {
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
LME = ["Nickel", "Lead", "Tin", "Cobalt"]

# Fixed pages only. No discovered links are followed.
PAGES = [
    ("ISM_SERVICES_JULY", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/"),
    ("ISM_MANUFACTURING_JULY", "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/"),
    ("TE_SERVICES_NONMAN", "https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_SERVICES", "https://tradingeconomics.com/united-states/services-pmi"),
    ("TE_MANUFACTURING", "https://tradingeconomics.com/united-states/manufacturing-pmi"),
    ("TE_SERVICES_DE", "https://de.tradingeconomics.com/united-states/non-manufacturing-pmi"),
    ("TE_COMMODITIES_EN", "https://tradingeconomics.com/commodities"),
    ("TE_COMMODITIES_DE", "https://de.tradingeconomics.com/commodities"),
    ("TE_COBALT", "https://tradingeconomics.com/commodity/cobalt"),
    ("TE_NICKEL", "https://tradingeconomics.com/commodity/nickel"),
    ("TE_LEAD", "https://tradingeconomics.com/commodity/lead"),
    ("TE_TIN", "https://tradingeconomics.com/commodity/tin"),
    ("TE_COBALT_DE", "https://de.tradingeconomics.com/commodity/cobalt"),
    ("LME_OFFICIAL", "https://www.lme.com/market-data/reports-and-data/lme-official-prices"),
    ("LME_NICKEL", "https://www.lme.com/Metals/Non-ferrous/LME-Nickel"),
    ("LME_LEAD", "https://www.lme.com/Metals/Non-ferrous/LME-Lead"),
    ("LME_TIN", "https://www.lme.com/Metals/Non-ferrous/LME-Tin"),
    ("LME_COBALT", "https://www.lme.com/Metals/Minor-metals/LME-Cobalt"),
    ("SPG_PUBLIC_DE", "https://www.pmi.spglobal.com/Public?language=de"),
    ("SPG_PUBLIC_EN", "https://www.pmi.spglobal.com/Public?language=en"),
]

records, attempts, errors = [], [], []


def now():
    return datetime.now(timezone.utc).isoformat()


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


def norm(v):
    return re.sub(r"\s+", " ", html.unescape(str(v or ""))).strip()


def fetch(label, url):
    t0 = time.monotonic()
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        body = r.content[:MAX_BYTES]
        row = {
            "label": label, "url": url, "final_url": r.url,
            "status": r.status_code, "elapsed_s": round(time.monotonic() - t0, 3),
            "bytes": len(body), "content_type": r.headers.get("content-type", ""),
            "redirected": r.url != url, "error": "",
        }
    except Exception as exc:
        row = {
            "label": label, "url": url, "final_url": "",
            "status": "ERROR", "elapsed_s": round(time.monotonic() - t0, 3),
            "bytes": 0, "content_type": "", "redirected": False,
            "error": repr(exc),
        }
        print(f"WARNUNG: {label}: {type(exc).__name__}: {exc}")
    attempts.append(row)
    log("HTTP", **row)
    return (r, body) if row["status"] != "ERROR" else (None, b"")


def soup(raw):
    return BeautifulSoup(raw, "html.parser") if BeautifulSoup else None


def text(raw):
    s = soup(raw)
    if s:
        for t in s(["script", "style", "noscript", "svg"]):
            t.decompose()
        return norm(s.get_text(" ", strip=True))
    return norm(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore")))


def nums(s):
    out = []
    for tok in re.findall(r"(?<![\w])[-+]?\d{1,6}(?:[.,]\d{1,4})?(?![\w])", s):
        try:
            x = float(tok.replace(",", "."))
        except Exception:
            continue
        if 1900 <= abs(x) <= 2100:
            continue
        out.append((tok, x))
    return out


def date_strength(s, domain):
    low = s.lower()
    if domain == "ISM":
        strong = ["july 2026", "jul 2026", "2026-07", "2026/07"]
        weak = ["july", "jul"]
        return "STRONG" if any(x in low for x in strong) else ("WEAK" if any(x in low for x in weak) else "NONE")
    strong = [
        "2026-08-28", "2026/08/28", "28.08.2026", "28/08/2026",
        "08/28/2026", "august 28, 2026", "aug 28, 2026", "28 aug 2026",
    ]
    return "STRONG" if any(x in low for x in strong) else "NONE"


def target_contexts(txt, term, domain):
    out = []
    for m in list(re.finditer(re.escape(term), txt, re.I))[:50]:
        c = norm(txt[max(0, m.start()-450):min(len(txt), m.end()+1100)])
        ds = date_strength(c, domain)
        if ds != "NONE" or domain == "TE_PUBLIC":
            out.append((c, ds))
    return out[:25]


def extract_visible(label, url, raw):
    txt = text(raw)
    groups = {g: {n: txt.lower().count(n.lower()) for n in ns} for g, ns in ISM.items()}
    groups["LME"] = {n: txt.lower().count(n.lower()) for n in LME}

    contexts = {}
    all_terms = list(sum(ISM.values(), [])) + LME
    domain = "LME" if label.startswith("LME") else ("ISM" if label.startswith("ISM_") else "TE_PUBLIC")
    for term in all_terms:
        cs = target_contexts(txt, term, domain)
        if cs:
            contexts[term] = [
                {"context": c, "date_strength": ds, "numbers": nums(c)}
                for c, ds in cs
            ]
    log("VISIBLE_CONTEXT", label=label, url=url, chars=len(txt),
        dates=re.findall(r"\b(?:2026[-/]\d{1,2}(?:[-/]\d{1,2})?|July 2026|August 28, 2026)\b", txt, re.I)[:300],
        target_counts=groups, contexts=contexts)


def extract_tables(label, url, raw):
    s = soup(raw)
    if not s:
        return
    data = []
    for i, table in enumerate(s.find_all("table")[:100]):
        table_txt = norm(table.get_text(" ", strip=True))
        rows = []
        for tr in table.find_all("tr")[:100]:
            cells = [norm(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        data.append({
            "index": i,
            "ism_date_strength": date_strength(table_txt, "ISM"),
            "lme_date_strength": date_strength(table_txt, "LME"),
            "rows": rows,
        })
    log("HTML_TABLES", label=label, url=url, table_count=len(data), tables=data)


def extract_pandas(label, url, raw):
    if pd is None:
        log("PANDAS_TABLES", label=label, url=url, status="UNAVAILABLE")
        return
    try:
        frames = pd.read_html(raw)
        rows = []
        for i, df in enumerate(frames[:100]):
            clean = df.fillna("").astype(str)
            blob = norm(" ".join(clean.astype(str).stack().tolist()))
            rows.append({
                "index": i, "shape": list(df.shape),
                "columns": [str(c) for c in df.columns],
                "ism_date_strength": date_strength(blob, "ISM"),
                "lme_date_strength": date_strength(blob, "LME"),
                "sample": clean.head(50).to_dict("records"),
            })
        log("PANDAS_TABLES", label=label, url=url, status="OK",
            table_count=len(rows), tables=rows)
    except Exception as exc:
        log("PANDAS_TABLES", label=label, url=url, status="ERROR", error=repr(exc))


def extract_json(label, url, raw):
    blobs = []
    rawtxt = raw.decode("utf-8", "ignore")
    s = soup(raw)
    if s:
        for tag in s.find_all("script"):
            content = (tag.string or tag.get_text()).strip()
            if not content:
                continue
            typ = (tag.get("type") or "").lower()
            if typ == "application/ld+json" or content.startswith("{") or content.startswith("["):
                try:
                    blobs.append(json.loads(content))
                except Exception:
                    pass
    for x in re.findall(r"(?s)(?:var|let|const)\s+\w+\s*=\s*(\{.*?\}|\[.*?\]);", rawtxt)[:100]:
        try:
            blobs.append(json.loads(x))
        except Exception:
            pass
    interesting = [
        b for b in blobs
        if re.search(
            r"PMI|Business Activity|New Orders|Employment|Prices|Backlog|"
            r"Inventory|Exports|Imports|Nickel|Lead|Tin|Cobalt|"
            r"56290|16806|54827|1919|2026-08-28|August 28, 2026",
            json.dumps(b, ensure_ascii=False), re.I
        )
    ]
    log("EMBEDDED_JSON", label=label, url=url,
        blob_count=len(blobs), interesting_blobs=interesting[:80])


def extract_attributes(label, url, raw):
    s = soup(raw)
    if not s:
        return
    pat = re.compile(
        r"pmi|services|manufacturing|nickel|lead|tin|cobalt|"
        r"actual|previous|forecast|reference|release|date|price|"
        r"order|employment|inventory|backlog|export|import|supplier|symbol",
        re.I,
    )
    rows = []
    for tag in s.find_all(True):
        attrs = []
        for k, v in tag.attrs.items():
            val = norm(v if isinstance(v, str) else " ".join(map(str, v)))
            if pat.search(f"{k} {val}"):
                attrs.append({"name": k, "value": val[:1800]})
        if attrs:
            rows.append({"tag": tag.name, "text": norm(tag.get_text(" ", strip=True))[:1800], "attrs": attrs})
        if len(rows) >= 1500:
            break
    log("HTML_ATTRIBUTES", label=label, url=url, rows=rows)


def extract_meta(label, url, raw):
    s = soup(raw)
    if not s:
        return
    rows = []
    for tag in s.find_all(["meta", "time"]):
        rows.append({
            "tag": tag.name,
            "name": tag.get("name"),
            "property": tag.get("property"),
            "itemprop": tag.get("itemprop"),
            "datetime": tag.get("datetime"),
            "content": norm(tag.get("content")),
            "text": norm(tag.get_text(" ", strip=True)),
        })
    log("META_TIME", label=label, url=url, rows=rows[:1500])


def extract_source(label, url, raw):
    rawtxt = raw.decode("utf-8", "ignore")
    patterns = {
        "target_dates": (
            re.findall(r"2026[-/]\d{1,2}[-/]\d{1,2}", rawtxt)[:500]
            + re.findall(r"\b(?:July|Jul|August|Aug)\s+\d{1,2},?\s+2026\b", rawtxt, re.I)[:500]
            + re.findall(r"\b(?:July|Jul|August|Aug)\s+2026\b", rawtxt, re.I)[:500]
        ),
        "te_data_symbols": re.findall(r'data-(?:symbol|code|id)\s*=\s*["\']([^"\']+)["\']', rawtxt, re.I)[:800],
        "te_rows_with_metals": re.findall(
            r"(?is)<tr[^>]*>.*?(?:Cobalt|Nickel|Lead|Tin).*?</tr>",
            rawtxt
        )[:200],
        "price_context": re.findall(
            r"(?is).{0,500}(?:Cobalt|Nickel|Lead|Tin).{0,1000}",
            rawtxt
        )[:500],
        "official_context": re.findall(
            r"(?is).{0,500}(?:Official Price|Official Cash|Cash Bid|Cash Offer|"
            r"Actual|Previous|Reference|Release Date).{0,1200}",
            rawtxt
        )[:500],
        "ism_context": re.findall(
            r"(?is).{0,450}(?:Business Activity|New Orders|New Export Orders|"
            r"Employment|Prices|Supplier Deliveries|Backlog(?: of Orders)?|"
            r"Inventories|Inventory Sentiment|Customers' Inventories|Imports|"
            r"Exports|Production).{0,1300}",
            rawtxt
        )[:700],
    }
    log("SOURCE_REGEX", label=label, url=url, matches=patterns)


def extract_links(label, url, raw):
    s = soup(raw)
    if not s:
        return
    rows = []
    for a in s.find_all("a", href=True):
        href = urljoin(url, a["href"])
        anchor = norm(a.get_text(" ", strip=True))
        if re.search(
            r"pmi|services|manufacturing|commodity|cobalt|nickel|lead|tin|"
            r"historical|forecast|release|official|price",
            f"{anchor} {href}", re.I
        ):
            rows.append({"anchor": anchor, "url": href})
    # Deliberately not followed.
    log("LINKS_NO_FOLLOW", label=label, url=url, count=len(rows), links=rows[:700])


def inspect(label, url):
    r, raw = fetch(label, url)
    if not r or not raw:
        return
    try:
        (RAW / (re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".html")).write_bytes(raw)
    except Exception as exc:
        log("RAW_SAVE", label=label, url=url, status="ERROR", error=repr(exc))
    safe(label + ":visible", lambda: extract_visible(label, url, raw))
    safe(label + ":tables", lambda: extract_tables(label, url, raw))
    safe(label + ":pandas", lambda: extract_pandas(label, url, raw))
    safe(label + ":json", lambda: extract_json(label, url, raw))
    safe(label + ":attrs", lambda: extract_attributes(label, url, raw))
    safe(label + ":meta", lambda: extract_meta(label, url, raw))
    safe(label + ":source", lambda: extract_source(label, url, raw))
    safe(label + ":links", lambda: extract_links(label, url, raw))


def build_candidates():
    """
    Conservative candidate builder.
    We intentionally do NOT call a candidate "VALIDATED_VALUE" across arbitrary
    pages. A candidate is VALIDATED_VALUE only when its local context has:
      - exact target indicator,
      - exactly one plausible number,
      - strong matching target date/month,
      - and a page whose declared domain is compatible with the target.
    TE commodity values stay TE_PUBLIC_COMMODITY, never LME_OFFICIAL_CASH.
    """
    cands = []

    for rec in records:
        if rec.get("route") == "VISIBLE_CONTEXT":
            label = rec.get("label", "")
            domain_page = "ISM" if label.startswith("ISM_") else (
                "LME" if label.startswith("LME_") else "TE"
            )
            for indicator, windows in rec.get("contexts", {}).items():
                target_domain = "LME" if indicator in LME else (
                    "ISM" if any(indicator in v for v in ISM.values()) else "TE"
                )
                for w in windows:
                    ns = w.get("numbers", [])
                    ds = w.get("date_strength")
                    # Avoid false validation from TE general pages: only strong date
                    # + exactly one plausible number in a local context.
                    valid = (
                        len(ns) == 1
                        and ds == "STRONG"
                        and (
                            (target_domain == "LME" and domain_page == "LME")
                            or (target_domain == "ISM" and domain_page == "ISM")
                            or (target_domain == "TE" and domain_page == "TE")
                        )
                    )
                    status = "VALIDATED_VALUE" if valid else "CANDIDATE"
                    if target_domain == "LME" and domain_page == "TE":
                        # TE commodity data is useful fallback evidence but not
                        # evidence for LME Official Cash.
                        status = "TE_PUBLIC_COMMODITY_CANDIDATE"
                    for tok, value in ns[:5]:
                        cands.append({
                            "source": label,
                            "url": rec.get("url", ""),
                            "indicator": indicator,
                            "reference": LME_DATE if target_domain == "LME" else ISM_MONTH,
                            "domain": "TE_PUBLIC_COMMODITY" if (
                                target_domain == "LME" and domain_page == "TE"
                            ) else target_domain,
                            "method": rec["route"],
                            "token": tok,
                            "value": value,
                            "date_strength": ds,
                            "status": status,
                            "context": w["context"][:2200],
                        })

    return cands


def write_outputs():
    cands = build_candidates()
    with CANDIDATES.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "source", "url", "indicator", "reference", "domain", "method",
            "token", "value", "date_strength", "status", "context"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(cands)

    targets = []
    for group, names in ISM.items():
        for n in names:
            targets.append(("ISM", group, n, ISM_MONTH))
    for n in LME:
        targets.append(("LME", "OFFICIAL_CASH", n, LME_DATE))

    rows = []
    for domain, group, indicator, ref in targets:
        matches = [
            c for c in cands
            if c["indicator"].lower() == indicator.lower()
            and c["reference"] == ref
        ]
        exact_valid = [c for c in matches if c["status"] == "VALIDATED_VALUE"]
        te_public = [c for c in matches if c["status"] == "TE_PUBLIC_COMMODITY_CANDIDATE"]
        state = (
            "VALIDATED_VALUE" if exact_valid
            else "TE_PUBLIC_COMMODITY_CANDIDATE" if te_public
            else "CANDIDATE" if matches
            else "NOT_FOUND"
        )
        values = sorted(set(round(float(c["value"]), 8) for c in matches))
        rows.append({
            "domain": domain,
            "group": group,
            "indicator": indicator,
            "reference_target": ref,
            "state": state,
            "candidate_count": len(matches),
            "validated_count": len(exact_valid),
            "distinct_values": ";".join(map(str, values[:30])),
            "ambiguous": "YES" if len(values) > 1 else "NO",
            "sources": ";".join(sorted(set(c["source"] for c in matches))[:30]),
            "methods": ";".join(sorted(set(c["method"] for c in matches))),
        })

    with MATRIX.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "domain","group","indicator","reference_target","state",
            "candidate_count","validated_count","distinct_values",
            "ambiguous","sources","methods"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    with ATTEMPTS.open("w", newline="", encoding="utf-8") as f:
        fields = ["label","url","final_url","status","elapsed_s","bytes",
                  "content_type","redirected","error"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(attempts)

    counts = defaultdict(int)
    for r in rows:
        counts[r["state"]] += 1

    summary = {
        "version": VERSION,
        "finished": True,
        "non_aborting": True,
        "fixed_pages": len(PAGES),
        "recursive_crawl": False,
        "te_api_used": False,
        "production_file_modified": False,
        "actual_value_inference": False,
        "ism_month": ISM_MONTH,
        "lme_target_date": LME_DATE,
        "http_attempts": len(attempts),
        "evidence_records": len(records),
        "value_candidates": len(cands),
        "errors": len(errors),
        "matrix_counts": dict(counts),
        "important_note": (
            "Trading Economics commodity values are public fallback evidence and "
            "are not labeled as LME Official Prices."
        ),
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
        f"ISM_MONTH={ISM_MONTH}",
        f"LME_TARGET_DATE={LME_DATE}",
        f"FIXED_PAGES={len(PAGES)}",
        f"HTTP_ATTEMPTS={len(attempts)}",
        f"EVIDENCE_RECORDS={len(records)}",
        f"VALUE_CANDIDATES={len(cands)}",
        f"ERRORS={len(errors)}",
        "",
        "MATRIX COUNTS:",
    ]
    lines += [f"{k}={v}" for k, v in sorted(counts.items())]
    lines += ["", "VALUE MATRIX:"]
    for r in rows:
        lines.append(
            f'{r["domain"]} | {r["group"]} | {r["indicator"]} | '
            f'target={r["reference_target"]} | state={r["state"]} | '
            f'candidates={r["candidate_count"]} | validated={r["validated_count"]} | '
            f'values={r["distinct_values"]} | ambiguous={r["ambiguous"]} | '
            f'sources={r["sources"]} | methods={r["methods"]}'
        )
    if errors:
        lines += ["", "NON-FATAL ERRORS:"]
        lines += [f'{e["label"]}: {e["error"]}' for e in errors]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print(f"=== {VERSION} ===")
    print("MODE=FIXED_PAGES|MULTI_EXTRACTION|VALUE_LEVEL|NON_ABORTING|READ_ONLY")
    print(f"ISM_MONTH={ISM_MONTH}")
    print(f"LME_TARGET_DATE={LME_DATE}")
    print("TE_API_USED=False")
    print("NO_RECURSIVE_CRAWL=True")
    print(f"FIXED_PAGE_COUNT={len(PAGES)}")

    for label, url in PAGES:
        safe(f"PAGE:{label}", lambda label=label, url=url: inspect(label, url))

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
