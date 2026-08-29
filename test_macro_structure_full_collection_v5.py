#!/usr/bin/env python3
from __future__ import annotations

import ast
import csv
import json
import os
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

TARGET = Path("makro_szenario.py")
OUT = Path("macro_structure_test_v4")
OUT.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; NeuberMacroDiscovery/4.0)",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
})

TE_URLS = [
    "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
    "https://tradingeconomics.com/united-states/services-pmi",
]

SP_URLS = [
    "https://www.pmi.spglobal.com/Public?language=de",
    "https://www.pmi.spglobal.com/Public?language=en",
    "https://www.pmi.spglobal.com/Public/Release/PressReleases?language=de",
]

TE_FIELDS = {
    "ISM Services PMI": ["Services PMI", "Non Manufacturing PMI", "ISM Services PMI"],
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

SP_FIELDS = {
    "S&P Global Services PMI": ["Services PMI"],
    "S&P Global Services Business Activity": ["Business Activity"],
    "S&P Global Services New Business": ["New Business"],
    "S&P Global Services New Export Business": ["New Export Business"],
    "S&P Global Services Employment": ["Employment"],
    "S&P Global Services Outstanding Business": ["Outstanding Business", "Backlog"],
    "S&P Global Services Input Prices": ["Input Prices"],
    "S&P Global Services Prices Charged": ["Prices Charged", "Prices"],
    "S&P Global Services Future Activity": ["Future Activity", "Business Expectations"],
}

def add(rows, source, field, status, value=None, reference=None,
        release=None, url=None, method=None, note="", evidence=""):
    rows.append({
        "source": source,
        "field": field,
        "status": status,
        "value": value,
        "reference": reference,
        "release": release,
        "method": method,
        "url": url,
        "evidence": str(evidence)[:5000],
        "note": note,
    })

def safe(label, fn, rows):
    try:
        fn()
    except Exception as exc:
        print(f"SAFE_ERROR={label}|{type(exc).__name__}|{exc}")
        add(
            rows, "TEST", label, "ERROR",
            note=f"{type(exc).__name__}: {exc}",
            evidence=traceback.format_exc(limit=6),
        )

def fetch(url, timeout=30):
    try:
        return SESSION.get(url, timeout=timeout, allow_redirects=True), None
    except Exception as exc:
        return None, exc

def clean_html(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

def safe_str(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value)

def row_to_text(row):
    # V3 bug fix: convert every cell explicitly before join.
    return " | ".join(safe_str(v) for v in row.tolist())

def contexts(text, labels, radius=450):
    found = []
    low = text.casefold()
    for label in labels:
        start = 0
        needle = label.casefold()
        while True:
            pos = low.find(needle, start)
            if pos < 0:
                break
            found.append(text[max(0, pos-radius):pos+len(label)+radius])
            start = pos + max(1, len(label))
            if len(found) >= 12:
                return found
    return found

def parse_tables(html):
    try:
        return pd.read_html(StringIO(html))
    except Exception as exc:
        return [{"__parse_error__": f"{type(exc).__name__}: {exc}"}]

def table_inventory(tables):
    out = []
    for i, obj in enumerate(tables):
        if isinstance(obj, dict):
            out.append({"index": i, "error": obj["__parse_error__"]})
            continue
        df = obj
        out.append({
            "index": i,
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "headers": [safe_str(c) for c in df.columns],
            "preview": df.head(20).to_string(index=False)[:6000],
        })
    return out

def classify_table_row(row_text):
    low = row_text.casefold()
    classes = []
    for token in ("actual", "previous", "forecast", "release", "date", "last"):
        if token in low:
            classes.append(token.upper())
    return ",".join(classes) if classes else "NONE"


def extract_regex_evidence(text, labels):
    """Return only textual evidence around labels and nearby numeric/date tokens."""
    results = []
    for label in labels:
        pattern = re.compile(
            rf"(.{{0,650}}{re.escape(label)}.{{0,650}})",
            re.I | re.S,
        )
        for match in pattern.finditer(text):
            chunk = re.sub(r"\s+", " ", match.group(1)).strip()
            # Numeric/date inventory is evidence, not interpretation.
            nums = re.findall(
                r"(?<!\w)(?:\d{1,3}(?:[.,]\d{1,3})?|\d{1,2}/\d{1,2}/\d{2,4}|"
                r"\d{4}-\d{2}(?:-\d{2})?)(?!\w)",
                chunk,
            )
            results.append({
                "label": label,
                "snippet": chunk[:5000],
                "numeric_tokens": nums[:80],
            })
            if len(results) >= 20:
                return results
    return results


def extract_dom_evidence(html, labels):
    """Second independent route: parse DOM text/attributes with lxml."""
    out = []
    try:
        from lxml import html as lxml_html
        root = lxml_html.fromstring(html)
        elements = root.xpath("//body//*[self::td or self::th or self::div or self::span or self::p or self::a]")
        for el in elements:
            txt = " ".join(" ".join(el.itertext()).split())
            if not txt:
                continue
            low = txt.casefold()
            if any(label.casefold() in low for label in labels):
                attrs = {str(k): str(v) for k, v in el.attrib.items()}
                out.append({
                    "text": txt[:5000],
                    "tag": el.tag,
                    "attrs": attrs,
                })
                if len(out) >= 50:
                    break
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    return out


def extract_attribute_evidence(html, labels):
    """Look for labels/values in data-* attributes and common chart attributes."""
    out = []
    try:
        from lxml import html as lxml_html
        root = lxml_html.fromstring(html)
        for el in root.xpath("//*[@*]"):
            attrs = {str(k): str(v) for k, v in el.attrib.items()}
            combined = " | ".join(f"{k}={v}" for k, v in attrs.items())
            if any(label.casefold() in combined.casefold() for label in labels):
                out.append({
                    "tag": el.tag,
                    "attributes": attrs,
                })
                if len(out) >= 100:
                    break
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    return out


def extract_json_candidates(text):
    """Find JSON-looking fragments without assuming their meaning."""
    candidates = []
    patterns = [
        r'(?s)\{[^{}]{0,20000}"(?:actual|previous|forecast|value|date)"[^{}]{0,20000}\}',
        r'(?s)\[[^\[\]]{0,20000}"(?:actual|previous|forecast|value|date)"[^\[\]]{0,20000}\]',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            fragment = m.group(0)
            parsed = None
            parse_status = "NOT_JSON"
            try:
                parsed = json.loads(fragment)
                parse_status = "JSON_VALID"
            except Exception:
                pass
            candidates.append({
                "parse_status": parse_status,
                "parsed_type": type(parsed).__name__ if parsed is not None else None,
                "fragment": fragment[:20000],
            })
            if len(candidates) >= 50:
                return candidates
    return candidates


def classify_value_tokens(text):
    """Inventory candidate labels and values; never assign Actual/Previous/Forecast."""
    patterns = [
        r"\bactual\b.{0,120}",
        r"\bprevious\b.{0,120}",
        r"\bforecast\b.{0,120}",
        r"\blast\b.{0,120}",
        r"\bvalue\b.{0,120}",
        r"\bdate\b.{0,120}",
        r"\brelease\b.{0,120}",
        r"\b2026[-/](?:0?[1-9]|1[0-2])(?:[-/]\d{1,2})?\b.{0,200}",
    ]
    hits = []
    for p in patterns:
        for m in re.finditer(p, text, re.I | re.S):
            hits.append(re.sub(r"\s+", " ", m.group(0)).strip()[:1000])
            if len(hits) >= 150:
                return hits
    return hits


def fetch_relevant_assets(rows, source, page_url, raw, fields, prefix, page_no):
    """Third route: fetch script assets and likely report/release links."""
    try:
        from lxml import html as lxml_html
        root = lxml_html.fromstring(raw)
    except Exception as exc:
        add(rows, source, f"ASSET_PARSER_{page_no}", "ERROR",
            url=page_url, method="ASSET_DISCOVERY",
            note=f"{type(exc).__name__}: {exc}")
        return

    all_links = []
    for el in root.xpath("//a[@href]"):
        href = urljoin(page_url, el.get("href"))
        label = " ".join(" ".join(el.itertext()).split())
        all_links.append((href, label))

    scripts = [
        urljoin(page_url, el.get("src"))
        for el in root.xpath("//script[@src]")
        if el.get("src")
    ]

    label_blob = " ".join(fields.keys()) + " " + " ".join(
        label for labels in fields.values() for label in labels
    )
    key_terms = re.compile(
        r"(pmi|ism|services|non.?manufactur|release|press|report|"
        r"actual|new.?orders|employment|prices)",
        re.I,
    )

    selected_links = []
    for href, label in all_links:
        if key_terms.search(href) or key_terms.search(label) or any(
            key_terms.search(x) for x in (href, label, label_blob)
        ):
            selected_links.append((href, label))
    # Keep a useful but bounded sample; never let one page dominate the run.
    selected_links = list(dict.fromkeys(selected_links))[:80]
    scripts = list(dict.fromkeys(scripts))[:20]

    asset_report = {
        "selected_links": [{"url": u, "label": l[:500]} for u, l in selected_links],
        "scripts": scripts,
    }
    (OUT / f"{prefix}_selected_assets_{page_no}.json").write_text(
        json.dumps(asset_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Fetch scripts independently. A script can contain chart/config data
    # that is invisible to normal HTML-table extraction.
    for i, asset_url in enumerate(scripts, 1):
        response, err = fetch(asset_url, timeout=20)
        if err:
            add(rows, source, f"SCRIPT_{page_no}_{i}", "ERROR",
                url=asset_url, method="SCRIPT_GET", note=str(err))
            continue
        text = response.text
        evidence = extract_json_candidates(text)
        classified = classify_value_tokens(text)
        if evidence or classified:
            add(
                rows, source, f"SCRIPT_{page_no}_{i}", "SCRIPT_EVIDENCE",
                url=asset_url, method="SCRIPT_SCAN",
                evidence=json.dumps({
                    "json_candidates": evidence[:20],
                    "value_token_contexts": classified[:80],
                }, ensure_ascii=False),
                note=f"status={response.status_code}|bytes={len(response.content)}",
            )

    # Fetch selected report/release links and run the same discovery methods.
    for i, (link_url, link_label) in enumerate(selected_links, 1):
        response, err = fetch(link_url, timeout=20)
        if err:
            add(rows, source, f"LINK_{page_no}_{i}", "ERROR",
                url=link_url, method="LINK_GET", note=str(err))
            continue
        text = clean_html(response.text)
        hits = extract_regex_evidence(text, [x for ls in fields.values() for x in ls])
        if hits:
            add(
                rows, source, f"LINK_{page_no}_{i}", "LINK_EVIDENCE",
                url=link_url, method="LINK_PAGE_SCAN",
                evidence=json.dumps(hits[:30], ensure_ascii=False),
                note=f"label={link_label[:300]}|status={response.status_code}",
            )


def discover_site(rows, source, urls, fields, prefix):
    """V4 baseline + V5 multi-route extraction; every route is isolated."""
    safe(
        f"{source}_V4_BASELINE",
        lambda: discover_site_v4(rows, source, urls, fields, prefix),
        rows,
    )

    print(f"=== {source} V5 MULTI-ROUTE EXTRACTION ===")
    for page_no, url in enumerate(urls, 1):
        response, err = fetch(url)
        if err:
            add(rows, source, f"V5_PAGE_{page_no}", "ERROR",
                url=url, method="V5_GET", note=str(err))
            continue

        raw = response.text
        plain = clean_html(raw)
        all_labels = [x for labels in fields.values() for x in labels]

        # Route A: raw HTML source, before tag stripping.
        safe(
            f"{source}_RAW_REGEX_{page_no}",
            lambda raw=raw, page_no=page_no, url=url:
                add(
                    rows, source, f"RAW_REGEX_{page_no}", "EVIDENCE" if extract_regex_evidence(raw, all_labels) else "NO_EVIDENCE",
                    url=url, method="RAW_HTML_REGEX",
                    evidence=json.dumps(extract_regex_evidence(raw, all_labels)[:80], ensure_ascii=False),
                ),
            rows,
        )

        # Route B: rendered-ish DOM text and attributes.
        safe(
            f"{source}_DOM_{page_no}",
            lambda raw=raw, page_no=page_no, url=url:
                add(
                    rows, source, f"DOM_{page_no}", "EVIDENCE" if extract_dom_evidence(raw, all_labels) else "NO_EVIDENCE",
                    url=url, method="LXML_DOM",
                    evidence=json.dumps(extract_dom_evidence(raw, all_labels)[:100], ensure_ascii=False),
                ),
            rows,
        )
        safe(
            f"{source}_ATTRIBUTES_{page_no}",
            lambda raw=raw, page_no=page_no, url=url:
                add(
                    rows, source, f"ATTRIBUTES_{page_no}", "EVIDENCE" if extract_attribute_evidence(raw, all_labels) else "NO_EVIDENCE",
                    url=url, method="LXML_ATTRIBUTES",
                    evidence=json.dumps(extract_attribute_evidence(raw, all_labels)[:100], ensure_ascii=False),
                ),
            rows,
        )

        # Route C: JSON-looking fragments in source.
        safe(
            f"{source}_JSON_CANDIDATES_{page_no}",
            lambda raw=raw, page_no=page_no, url=url:
                add(
                    rows, source, f"JSON_CANDIDATES_{page_no}", "EVIDENCE" if extract_json_candidates(raw) else "NO_EVIDENCE",
                    url=url, method="RAW_JSON_PATTERN",
                    evidence=json.dumps(extract_json_candidates(raw)[:50], ensure_ascii=False),
                ),
            rows,
        )

        # Route D: contextual value/metadata inventory.
        safe(
            f"{source}_VALUE_CONTEXT_{page_no}",
            lambda plain=plain, page_no=page_no, url=url:
                add(
                    rows, source, f"VALUE_CONTEXT_{page_no}", "EVIDENCE" if classify_value_tokens(plain) else "NO_EVIDENCE",
                    url=url, method="VALUE_METADATA_SCAN",
                    evidence=json.dumps(classify_value_tokens(plain)[:150], ensure_ascii=False),
                ),
            rows,
        )

        # Route E: scripts + likely report/release links.
        safe(
            f"{source}_ASSET_FETCH_{page_no}",
            lambda raw=raw, page_no=page_no, url=url:
                fetch_relevant_assets(rows, source, url, raw, fields, prefix, page_no),
            rows,
        )


def discover_site_v4(rows, source, urls, fields, prefix):
    print(f"=== {source} DEEP DISCOVERY V4 ===")
    for page_no, url in enumerate(urls, 1):
        response, err = fetch(url)
        if err:
            add(rows, source, f"PAGE_{page_no}", "ERROR",
                url=url, method="GET", note=str(err))
            continue

        raw = response.text
        text = clean_html(raw)
        tables = parse_tables(raw)

        (OUT / f"{prefix}_page_{page_no}.html").write_text(raw, encoding="utf-8")
        (OUT / f"{prefix}_tables_{page_no}.json").write_text(
            json.dumps(table_inventory(tables), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(
            f"{prefix.upper()}_PAGE={page_no}|STATUS={response.status_code}"
            f"|FINAL={response.url}|BYTES={len(response.content)}"
        )
        add(
            rows, source, f"PAGE_{page_no}",
            "HTTP_OK" if response.ok else "HTTP_ERROR",
            url=url, method="GET",
            note=f"final_url={response.url}|bytes={len(response.content)}",
            evidence=text[:3000],
        )

        for field, labels in fields.items():
            ctx = contexts(text, labels)
            add(
                rows, source, field,
                "LABEL_FOUND" if ctx else "NOT_FOUND",
                url=url, method="HTML_LABEL_SCAN",
                evidence="\n---\n".join(ctx),
                note="Discovery only; no value inferred.",
            )

            for table_index, table in enumerate(tables):
                if isinstance(table, dict):
                    continue

                # IMPORTANT: use the string-normalized dataframe for scanning.
                normalized_df = table.map(safe_str)

                for row_index, row in normalized_df.iterrows():
                    row_text = row_to_text(row)
                    if any(normalize(label) in normalize(row_text) for label in labels):
                        add(
                            rows, source, field, "ROW_FOUND",
                            url=url, method="PANDAS_TABLE_ROW",
                            evidence=row_text,
                            note=(
                                f"table_index={table_index}|row={int(row_index)}"
                                f"|row_classes={classify_table_row(row_text)}"
                            ),
                        )

                        # Capture neighboring rows because Actual/Last/Release
                        # may be represented on adjacent rows.
                        lo = max(0, int(row_index) - 2)
                        hi = min(len(normalized_df), int(row_index) + 3)
                        neighborhood = normalized_df.iloc[lo:hi]
                        add(
                            rows, source, field, "ROW_NEIGHBORHOOD",
                            url=url, method="TABLE_NEIGHBORHOOD",
                            evidence="\n".join(row_to_text(r) for _, r in neighborhood.iterrows()),
                            note=f"table_index={table_index}|rows={lo}:{hi}",
                        )
                        break

        # Discover links, scripts and embedded JSON without assuming an API.
        links = re.findall(r'href=["\']([^"\']+)["\']', raw, flags=re.I)
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', raw, flags=re.I)
        json_blocks = re.findall(
            r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
            raw, flags=re.I | re.S
        )

        assets = {
            "url": url,
            "final_url": response.url,
            "links": [urljoin(response.url, x) for x in links[:1000]],
            "scripts": [urljoin(response.url, x) for x in scripts[:1000]],
            "application_json_blocks": [x[:20000] for x in json_blocks[:50]],
        }
        (OUT / f"{prefix}_assets_{page_no}.json").write_text(
            json.dumps(assets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"{prefix.upper()}_ASSETS={page_no}|LINKS={len(links)}"
            f"|SCRIPTS={len(scripts)}|JSON={len(json_blocks)}"
        )

def load_definitions(rows):
    if not TARGET.exists():
        add(rows, "TEST", "makro_szenario.py", "MISSING")
        return {}, {}

    try:
        source = TARGET.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        add(rows, "TEST", "makro_szenario.py", "SYNTAX_ERROR", note=str(exc))
        return {}, {}

    fred = {}
    market = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in {"FRED_SERIES", "MARKET_DATA"}:
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception as exc:
                add(rows, "TEST", target.id, "DEFINITION_PARSE_ERROR", note=str(exc))
                continue
            if target.id == "FRED_SERIES" and isinstance(value, dict):
                fred = value
            elif target.id == "MARKET_DATA" and isinstance(value, dict):
                market = value

    print(f"FRED_DEFINITIONS={len(fred)}|MARKET_DEFINITIONS={len(market)}")
    add(rows, "TEST", "FRED_DEFINITIONS", "FOUND", value=len(fred), method="AST_READ_ONLY")
    add(rows, "TEST", "MARKET_DEFINITIONS", "FOUND", value=len(market), method="AST_READ_ONLY")
    return fred, market

def collect_fred(rows, definitions):
    print(f"=== FRED DEEP TEST: {len(definitions)} SERIES ===")
    key = os.environ.get("FRED_API_KEY")
    print(f"FRED_API_KEY_PRESENT={'YES' if key else 'NO'}")
    if not key:
        for name, sid in definitions.items():
            add(rows, "FRED", name, "CONFIG_MISSING", note=f"series={sid}")
        return

    def one(name, sid):
        try:
            params = {
                "api_key": key,
                "file_type": "json",
                "series_id": sid,
                "sort_order": "desc",
                "limit": 5,
            }
            response = SESSION.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params=params,
                timeout=20,
            )
            if response.status_code != 200:
                return name, sid, f"HTTP_{response.status_code}", None, None, response.text[:500]
            observations = response.json().get("observations", [])
            latest = observations[0] if observations else {}
            return (
                name, sid,
                "REAL_API" if latest else "NO_DATA",
                latest.get("value"),
                latest.get("date"),
                f"observations={len(observations)}",
            )
        except Exception as exc:
            return name, sid, "ERROR", None, None, f"{type(exc).__name__}: {exc}"

    workers = min(12, max(1, len(definitions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, name, sid) for name, sid in definitions.items()]
        for future in as_completed(futures):
            name, sid, status, value, date, note = future.result()
            print(
                f"FRED_RESULT={name}|SERIES={sid}|STATUS={status}"
                f"|VALUE={value}|DATE={date}"
            )
            add(
                rows, "FRED", name, status, value=value, reference=date,
                method="FRED_API", note=f"series={sid}|{note}",
            )

def probe_fred_metadata(rows, definitions):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return

    print("=== FRED METADATA V4 ===")
    for name, sid in definitions.items():
        try:
            response = SESSION.get(
                "https://api.stlouisfed.org/fred/series",
                params={"api_key": key, "file_type": "json", "series_id": sid},
                timeout=20,
            )
            if not response.ok:
                add(rows, "FRED", name + " [metadata]",
                    f"HTTP_{response.status_code}",
                    method="FRED_SERIES_METADATA",
                    note=response.text[:500])
                continue

            metadata = (response.json().get("seriess") or [{}])[0]
            selected = {
                key: metadata.get(key)
                for key in (
                    "title", "frequency", "units", "seasonal_adjustment",
                    "observation_start", "observation_end", "last_updated",
                )
            }
            add(
                rows, "FRED", name + " [metadata]", "REAL_API",
                method="FRED_SERIES_METADATA",
                note=json.dumps(selected, ensure_ascii=False),
            )
        except Exception as exc:
            add(rows, "FRED", name + " [metadata]", "ERROR", note=str(exc))

def inspect_market(rows, definitions):
    print(f"=== MARKET DEFINITIONS: {len(definitions)} ===")
    for name, definition in definitions.items():
        add(
            rows, "MARKET_DEFINITION", name, "DEFINED",
            value=str(definition), method="AST_READ_ONLY",
        )

def build_source_matrix(rows):
    fields = [
        "ISM Services PMI",
        "ISM Services New Orders",
        "ISM Services Employment",
        "ISM Services Prices",
        "ISM Services Business Activity",
        "ISM Services Supplier Deliveries",
        "ISM Services Backlog of Orders",
        "ISM Services Inventories",
        "ISM Services Inventory Sentiment",
        "ISM Services Imports",
        "ISM Services Exports",
        "ISM Services New Export Orders",
        "S&P Global Services PMI",
        "S&P Global Services Business Activity",
        "S&P Global Services New Business",
        "S&P Global Services New Export Business",
        "S&P Global Services Employment",
        "S&P Global Services Outstanding Business",
        "S&P Global Services Input Prices",
        "S&P Global Services Prices Charged",
        "S&P Global Services Future Activity",
    ]
    matrix = []
    for field in fields:
        source_rows = [r for r in rows if r["field"] == field]
        matrix.append({
            "field": field,
            "TE_label_or_row": any(r["source"] == "TradingEconomics" and
                                   r["status"] in {"LABEL_FOUND", "ROW_FOUND"}
                                   for r in source_rows),
            "SP_label_or_row": any(r["source"] == "S&P Global" and
                                   r["status"] in {"LABEL_FOUND", "ROW_FOUND"}
                                   for r in source_rows),
            "evidence_records": len(source_rows),
        })
    (OUT / "source_matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def write_reports(rows):
    if not rows:
        rows.append({
            "source": "TEST", "field": "NO_RECORDS", "status": "EMPTY",
            "value": None, "reference": None, "release": None,
            "method": None, "url": None, "evidence": "", "note": "",
        })

    with (OUT / "results.json").open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)

    with (OUT / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    build_source_matrix(rows)

    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    summary = [
        "=== MACRO STRUCTURE DISCOVERY V4 ===",
        f"GENERATED_UTC={datetime.now(timezone.utc).isoformat()}",
        f"RECORDS={len(rows)}",
        "NON_ABORTING=True",
        "PRODUCTION_FILE_MODIFIED=False",
        "PREVIOUS_AS_ACTUAL=FORBIDDEN",
        "FORECAST_AS_ACTUAL=FORBIDDEN",
        "VALUE_INFERENCE=FORBIDDEN",
        "EXTRACTION_ROUTES=V4_BASELINE|RAW_REGEX|LXML_DOM|ATTRIBUTES|JSON_CANDIDATES|VALUE_CONTEXT|SCRIPTS|LINK_PAGES",
        "",
        "STATUS_COUNTS:",
    ]
    summary.extend(f"{key}={value}" for key, value in sorted(counts.items()))
    (OUT / "summary.txt").write_text("\n".join(summary), encoding="utf-8")
    print("\n".join(summary))

def main():
    print("=== MACRO STRUCTURE FULL DISCOVERY V5 ===")
    print("PURPOSE=MAXIMUM_INFORMATION_PER_RUN|MULTI_ROUTE_EXTRACTION")
    print("POLICY=NON_ABORTING|READ_ONLY|NO_GUESSED_ACTUALS")

    rows = []

    if TARGET.exists():
        try:
            ast.parse(TARGET.read_text(encoding="utf-8"))
            print("TARGET_SYNTAX=GREEN")
            add(rows, "TEST", "makro_szenario.py", "SYNTAX_GREEN")
        except Exception as exc:
            add(rows, "TEST", "makro_szenario.py", "SYNTAX_ERROR", note=str(exc))

    fred, market = load_definitions(rows)

    safe(
        "TE_DEEP_DISCOVERY",
        lambda: discover_site(rows, "TradingEconomics", TE_URLS, TE_FIELDS, "te"),
        rows,
    )
    safe(
        "SP_DEEP_DISCOVERY",
        lambda: discover_site(rows, "S&P Global", SP_URLS, SP_FIELDS, "sp"),
        rows,
    )
    safe(
        "FRED_API_COLLECTION",
        lambda: collect_fred(rows, fred),
        rows,
    )
    safe(
        "FRED_METADATA_COLLECTION",
        lambda: probe_fred_metadata(rows, fred),
        rows,
    )
    safe(
        "MARKET_DEFINITION_COLLECTION",
        lambda: inspect_market(rows, market),
        rows,
    )
    safe(
        "REPORT_GENERATION",
        lambda: write_reports(rows),
        rows,
    )

    if not (OUT / "results.json").exists():
        try:
            write_reports(rows)
        except Exception as exc:
            print(f"REPORT_FALLBACK_ERROR={type(exc).__name__}|{exc}")

    print("RESULT=COLLECTION_COMPLETE")
    print("EXIT_POLICY=0")
    return 0

if __name__ == "__main__":
    sys.exit(main())
