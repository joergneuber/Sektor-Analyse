
#!/usr/bin/env python3
"""
V6.1 Public Trading Economics + PMI Evidence Deep Dive
Purpose:
  Exhaustively investigate public ISM Services / S&P Global Services evidence
  without ever treating an unverified candidate as an Actual.

Design:
  - Every probe is isolated by safe().
  - No probe is allowed to abort the complete run.
  - Raw HTML, DOM, attributes, scripts, links, JSON fragments, tables,
    embedded state, HTTP headers and discovered endpoints are recorded.
  - Candidate values are EVIDENCE ONLY. Actual/Previous/Forecast are never
    inferred merely from proximity.
  - Production files are read-only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
from lxml import html as lxml_html

OUT = Path("macro_pmi_evidence_v6_1")
OUT.mkdir(parents=True, exist_ok=True)

TIMEOUT = 25
MAX_LINKS = 180
MAX_SCRIPTS = 80
MAX_ENDPOINTS = 120
MAX_FETCHED_DISCOVERED = 180
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

TARGETS = {
    "TE_SERVICES": "https://tradingeconomics.com/united-states/services-pmi",
    "TE_NON_MANUFACTURING": "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
    "TE_SERVICES_WORLD": "https://tradingeconomics.com/country-list/services-pmi",
    "TE_SERVICES_G20": "https://tradingeconomics.com/country-list/services-pmi?continent=g20",
    "TE_SERVICES_EUROPE": "https://tradingeconomics.com/country-list/services-pmi?continent=europe",
    "TE_SERVICES_FORECAST": "https://tradingeconomics.com/forecast/services-pmi",
    "TE_SERVICES_FORECAST_G20": "https://tradingeconomics.com/forecast/services-pmi?continent=g20",
    "TE_SERVICES_FORECAST_EUROPE": "https://tradingeconomics.com/forecast/services-pmi?continent=europe",
    "TE_COMPOSITE_US": "https://tradingeconomics.com/united-states/composite-pmi",
    "TE_COMPOSITE_G20": "https://tradingeconomics.com/country-list/composite-pmi?continent=g20",
    "TE_COMPOSITE_WORLD": "https://tradingeconomics.com/country-list/composite-pmi",
    "SPG_DE": "https://www.pmi.spglobal.com/Public?language=de",
    "SPG_EN": "https://www.pmi.spglobal.com/Public",
}

FIELD_ALIASES = {
    "pmi": [
        "services pmi", "service pmi", "services business activity",
        "non-manufacturing pmi", "pmi services",
    ],
    "business_activity": [
        "business activity", "activity index", "business activity index",
    ],
    "new_orders": [
        "new orders", "new business", "new export orders",
        "new export business",
    ],
    "employment": ["employment", "employment index"],
    "prices": [
        "prices", "prices paid", "input prices", "prices charged",
        "price charges", "prices received",
    ],
    "supplier_deliveries": ["supplier deliveries", "suppliers' deliveries"],
    "backlog": ["backlog", "backlogs"],
    "inventories": ["inventories", "inventory"],
    "inventory_sentiment": ["inventory sentiment"],
    "imports": ["imports"],
    "exports": ["exports"],
    "future_activity": ["future activity"],
    "release": ["release", "release date", "published", "publication date"],
}

VALUE_WORDS = ("actual", "previous", "forecast", "consensus", "last", "value")
DATE_RE = re.compile(
    r"\b(?:20\d{2}[-/](?:0?[1-9]|1[0-2])(?:[-/]\d{1,2})?|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+\d{4})\b", re.I)
NUMBER_RE = re.compile(r"(?<![\w.])(?:-?\d{1,3}(?:[.,]\d{1,3})?)(?![\w.])")
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)

session = requests.Session()
session.headers.update(HEADERS)

rows = []
errors = []


def now():
    return datetime.now(timezone.utc).isoformat()


def safe(label, fn):
    try:
        return fn()
    except Exception as exc:
        errors.append({
            "time": now(),
            "probe": label,
            "type": type(exc).__name__,
            "error": str(exc),
        })
        print(f"WARNUNG: {label}: {type(exc).__name__}: {exc}")
        return None


def record(source, probe, url, status, method, **extra):
    item = {
        "time": now(),
        "source": source,
        "probe": probe,
        "url": url,
        "status": status,
        "method": method,
    }
    item.update(extra)
    rows.append(item)


def fetch(url, method="GET"):
    try:
        if method == "HEAD":
            r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        else:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        return r, None
    except Exception as exc:
        return None, exc


def normalized_text(s):
    return re.sub(r"\s+", " ", s or "").strip()


def field_hits(text):
    low = (text or "").casefold()
    found = []
    for field, aliases in FIELD_ALIASES.items():
        if any(a.casefold() in low for a in aliases):
            found.append(field)
    return found


def context_windows(text, aliases, radius=900, limit=40):
    text = text or ""
    out = []
    for alias in aliases:
        for m in re.finditer(re.escape(alias), text, re.I):
            start = max(0, m.start() - radius)
            end = min(len(text), m.end() + radius)
            chunk = normalized_text(text[start:end])
            out.append({
                "alias": alias,
                "context": chunk,
                "numbers": NUMBER_RE.findall(chunk)[:40],
                "dates": DATE_RE.findall(chunk)[:20],
                "value_words": [
                    w for w in VALUE_WORDS if re.search(rf"\b{re.escape(w)}\b", chunk, re.I)
                ],
            })
            if len(out) >= limit:
                return out
    return out


def extract_json_like(text, limit=80):
    candidates = []
    # Script/config assignment patterns
    patterns = [
        r'(?is)(?:window\.__INITIAL_STATE__|__NEXT_DATA__|initialState|'
        r'chartData|series|dataset|data)\s*[:=]\s*(\{.{0,50000}\})',
        r'(?is)(?:window\.__INITIAL_STATE__|__NEXT_DATA__|initialState|'
        r'chartData|series|dataset|data)\s*[:=]\s*(\[.{0,50000}\])',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            frag = m.group(1)
            status = "NOT_JSON"
            parsed_type = None
            try:
                parsed = json.loads(frag)
                status = "JSON_VALID"
                parsed_type = type(parsed).__name__
            except Exception:
                pass
            candidates.append({
                "status": status,
                "type": parsed_type,
                "fragment": frag[:50000],
            })
            if len(candidates) >= limit:
                return candidates
    return candidates


def discover_urls(text, base_url):
    found = set()
    for raw in URL_RE.findall(text or ""):
        u = raw.rstrip(".,);]}>'\"")
        if u.startswith(("http://", "https://")):
            found.add(u)
    return sorted(found)


def relevant_url(u, label=""):
    blob = f"{u} {label}".casefold()
    terms = (
        "pmi", "ism", "service", "non-manufact", "release", "press",
        "report", "chart", "api", "data", "series", "calendar",
        "new-order", "employment", "price", "backlog",
    )
    return any(t in blob for t in terms)


def html_inventory(source, url, raw):
    root = lxml_html.fromstring(raw)
    body = normalized_text(root.text_content())

    links = []
    for a in root.xpath("//a[@href]"):
        href = urljoin(url, a.get("href"))
        label = normalized_text(" ".join(a.itertext()))
        links.append({"url": href, "label": label[:600]})

    scripts = []
    for s in root.xpath("//script"):
        src = s.get("src")
        if src:
            scripts.append(urljoin(url, src))
        else:
            inline = s.text or ""
            if field_hits(inline):
                scripts.append({"inline": inline[:100000]})

    attrs = []
    for el in root.xpath("//*[@*]"):
        at = {str(k): str(v) for k, v in el.attrib.items()}
        blob = " ".join(f"{k}={v}" for k, v in at.items())
        if field_hits(blob):
            attrs.append({"tag": el.tag, "attributes": at})

    tables = []
    for idx, table in enumerate(root.xpath("//table")[:100], 1):
        table_text = normalized_text(" ".join(table.itertext()))
        if field_hits(table_text):
            trs = []
            for tr in table.xpath(".//tr")[:100]:
                cells = [normalized_text(" ".join(c.itertext())) for c in tr.xpath("./th|./td")]
                if cells:
                    trs.append(cells)
            tables.append({"index": idx, "text": table_text[:15000], "rows": trs})

    return body, links, scripts, attrs, tables


def deep_probe_page(source, url):
    print(f"INFO: Deep page probe {source}: {url}")
    r, err = fetch(url)
    if err:
        record(source, "PAGE", url, "ERROR", "GET", error=str(err))
        return
    raw = r.text
    body, links, scripts, attrs, tables = html_inventory(source, url, raw)

    record(
        source, "PAGE", url, r.status_code, "GET",
        final_url=r.url,
        bytes=len(r.content),
        content_type=r.headers.get("content-type", ""),
        redirect_chain=[x.url for x in r.history] + [r.url],
        field_hits=field_hits(body),
    )

    # Save raw evidence and normalized body.
    digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    (OUT / f"{source}_{digest}_raw.html").write_text(raw, encoding="utf-8", errors="ignore")
    (OUT / f"{source}_{digest}_body.txt").write_text(body, encoding="utf-8", errors="ignore")

    for field, aliases in FIELD_ALIASES.items():
        hits = context_windows(raw, aliases)
        if hits:
            record(
                source, f"FIELD_{field}", url, "EVIDENCE", "RAW_CONTEXT",
                evidence=hits,
            )

    json_hits = extract_json_like(raw)
    record(
        source, "JSON_SCAN", url,
        "EVIDENCE" if json_hits else "NONE", "RAW_JSON_SCAN",
        candidates=json_hits,
    )

    # Full link inventory + relevant links.
    selected_links = [x for x in links if relevant_url(x["url"], x["label"])]
    selected_links = list({x["url"]: x for x in selected_links}.values())[:MAX_LINKS]
    (OUT / f"{source}_{digest}_links.json").write_text(
        json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / f"{source}_{digest}_selected_links.json").write_text(
        json.dumps(selected_links, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    record(
        source, "LINK_INVENTORY", url, "OK", "DOM_LINKS",
        total=len(links), relevant=len(selected_links),
        selected=selected_links,
    )

    # Tables.
    (OUT / f"{source}_{digest}_tables.json").write_text(
        json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    record(
        source, "TABLE_SCAN", url,
        "EVIDENCE" if tables else "NONE", "LXML_TABLES",
        tables=tables,
    )

    # Attributes.
    record(
        source, "ATTRIBUTE_SCAN", url,
        "EVIDENCE" if attrs else "NONE", "LXML_ATTRIBUTES",
        matches=attrs[:200],
    )

    # Inline scripts + external scripts.
    external_scripts = [x for x in scripts if isinstance(x, str)]
    inline_scripts = [x["inline"] for x in scripts if isinstance(x, dict)]
    for i, inline in enumerate(inline_scripts[:30], 1):
        if field_hits(inline) or extract_json_like(inline):
            record(
                source, f"INLINE_SCRIPT_{i}", url, "EVIDENCE", "INLINE_SCRIPT",
                fields=field_hits(inline),
                contexts=context_windows(inline, sum(FIELD_ALIASES.values(), []), 700, 30),
                json_candidates=extract_json_like(inline, 20),
            )

    for i, script_url in enumerate(list(dict.fromkeys(external_scripts))[:MAX_SCRIPTS], 1):
        safe(
            f"{source}_SCRIPT_{i}",
            lambda script_url=script_url, i=i: deep_probe_script(source, script_url, i),
        )

    # Fetch relevant linked pages, but only a bounded, unique set.
    discovered = []
    for link in selected_links:
        u = link["url"]
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https"):
            continue
        if u.rstrip("/") == url.rstrip("/"):
            continue
        discovered.append(u)
    for i, u in enumerate(list(dict.fromkeys(discovered))[:MAX_FETCHED_DISCOVERED], 1):
        safe(
            f"{source}_LINK_{i}",
            lambda u=u, i=i: deep_probe_discovered_page(source, u, i),
        )

    # URLs embedded anywhere in source.
    embedded = [u for u in discover_urls(raw, url) if relevant_url(u)]
    record(
        source, "EMBEDDED_URL_SCAN", url,
        "EVIDENCE" if embedded else "NONE", "RAW_URL_SCAN",
        urls=embedded[:MAX_ENDPOINTS],
    )


def deep_probe_script(source, url, index):
    r, err = fetch(url)
    if err:
        record(source, f"SCRIPT_{index}", url, "ERROR", "GET", error=str(err))
        return
    text = r.text
    fields = field_hits(text)
    json_hits = extract_json_like(text)
    urls = [u for u in discover_urls(text, url) if relevant_url(u)]
    endpoint_patterns = [
        r'(?i)(?:fetch|axios\.(?:get|post)|url|endpoint|api)\s*\(\s*[\'"]([^\'"]+)',
        r'(?i)(?:src|href|url|endpoint|apiUrl)\s*[:=]\s*[\'"]([^\'"]+)',
    ]
    endpoints = set()
    for pat in endpoint_patterns:
        for m in re.finditer(pat, text):
            candidate = urljoin(url, m.group(1))
            if urlparse(candidate).scheme in ("http", "https"):
                endpoints.add(candidate)
    endpoints.update(urls)

    record(
        source, f"SCRIPT_{index}", url, r.status_code, "SCRIPT_GET",
        bytes=len(r.content),
        fields=fields,
        contexts=context_windows(text, sum(FIELD_ALIASES.values(), []), 900, 60),
        json_candidates=json_hits[:50],
        discovered_endpoints=sorted(endpoints)[:MAX_ENDPOINTS],
    )

    digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]
    if fields or json_hits or endpoints:
        (OUT / f"{source}_script_{index}_{digest}.txt").write_text(
            text[:500000], encoding="utf-8", errors="ignore"
        )

    # Probe likely API/data endpoints directly.
    for j, endpoint in enumerate(sorted(endpoints)[:30], 1):
        if relevant_url(endpoint):
            safe(
                f"{source}_ENDPOINT_{index}_{j}",
                lambda endpoint=endpoint, index=index, j=j:
                    probe_endpoint(source, endpoint, f"{index}_{j}"),
            )


def deep_probe_discovered_page(source, url, index):
    r, err = fetch(url)
    if err:
        record(source, f"DISCOVERED_{index}", url, "ERROR", "GET", error=str(err))
        return
    text = normalized_text(r.text)
    fields = field_hits(text)
    contexts = context_windows(r.text, sum(FIELD_ALIASES.values(), []), 1000, 80)
    json_hits = extract_json_like(r.text, 30)
    record(
        source, f"DISCOVERED_{index}", url, r.status_code, "LINK_GET",
        final_url=r.url,
        bytes=len(r.content),
        fields=fields,
        contexts=contexts,
        json_candidates=json_hits,
        title=(re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S).group(1).strip()
               if re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S) else ""),
    )


def probe_endpoint(source, url, tag):
    # GET only. No POSTs, no credentials, no state changes.
    r, err = fetch(url)
    if err:
        record(source, f"ENDPOINT_{tag}", url, "ERROR", "GET", error=str(err))
        return
    text = r.text[:1000000]
    fields = field_hits(text)
    json_hits = extract_json_like(text, 30)
    date_hits = DATE_RE.findall(text)[:50]
    num_hits = NUMBER_RE.findall(text)[:100]
    record(
        source, f"ENDPOINT_{tag}", url, r.status_code, "GET",
        final_url=r.url,
        content_type=r.headers.get("content-type", ""),
        bytes=len(r.content),
        fields=fields,
        dates=date_hits,
        numbers=num_hits,
        json_candidates=json_hits,
        preview=normalized_text(text)[:6000],
    )


def explicit_release_probe():
    """
    Probe likely release/news routes by searching the public S&P page's
    relevant links, then inspect pages containing services terminology.
    """
    source = "SPG_RELEASE_DEEP"
    for root_url in (TARGETS["SPG_DE"], TARGETS["SPG_EN"]):
        r, err = fetch(root_url)
        if err:
            record(source, "ROOT", root_url, "ERROR", "GET", error=str(err))
            continue
        try:
            root = lxml_html.fromstring(r.text)
            candidates = []
            for a in root.xpath("//a[@href]"):
                u = urljoin(root_url, a.get("href"))
                label = normalized_text(" ".join(a.itertext()))
                if relevant_url(u, label):
                    candidates.append((u, label))
            candidates = list(dict.fromkeys(candidates))[:100]
            record(source, "ROOT_LINKS", root_url, "OK", "DISCOVERY",
                   candidates=[{"url": u, "label": l[:500]} for u, l in candidates])
            for i, (u, label) in enumerate(candidates[:80], 1):
                safe(
                    f"{source}_{i}",
                    lambda u=u, label=label, i=i: probe_release_candidate(
                        source, u, label, i
                    ),
                )
        except Exception as exc:
            record(source, "ROOT_PARSE", root_url, "ERROR", "PARSE", error=str(exc))


def probe_release_candidate(source, url, label, index):
    r, err = fetch(url)
    if err:
        record(source, f"RELEASE_{index}", url, "ERROR", "GET",
               label=label[:500], error=str(err))
        return
    text = normalized_text(r.text)
    # Require some PMI/services evidence before storing a large page.
    hits = field_hits(text)
    serviceish = bool(re.search(r"\bservices?\b|\bnon[- ]manufacturing\b", text, re.I))
    if not serviceish and not hits:
        return
    record(
        source, f"RELEASE_{index}", url, r.status_code, "GET",
        final_url=r.url,
        label=label[:500],
        bytes=len(r.content),
        fields=hits,
        contexts=context_windows(r.text, sum(FIELD_ALIASES.values(), []), 1200, 100),
        dates=DATE_RE.findall(r.text)[:80],
        numbers=NUMBER_RE.findall(r.text)[:150],
    )



def te_public_structured_probe():
    """Targeted, unauthenticated TE public-page probe; never aborts."""
    source = "TE_PUBLIC_STRUCTURED"
    targets = {
        "US_SERVICES": TARGETS["TE_SERVICES"],
        "US_NON_MANUFACTURING": TARGETS["TE_NON_MANUFACTURING"],
        "SERVICES_WORLD": TARGETS["TE_SERVICES_WORLD"],
        "SERVICES_G20": TARGETS["TE_SERVICES_G20"],
        "SERVICES_EUROPE": TARGETS["TE_SERVICES_EUROPE"],
        "SERVICES_FORECAST": TARGETS["TE_SERVICES_FORECAST"],
        "SERVICES_FORECAST_G20": TARGETS["TE_SERVICES_FORECAST_G20"],
        "SERVICES_FORECAST_EUROPE": TARGETS["TE_SERVICES_FORECAST_EUROPE"],
        "COMPOSITE_US": TARGETS["TE_COMPOSITE_US"],
        "COMPOSITE_G20": TARGETS["TE_COMPOSITE_G20"],
        "COMPOSITE_WORLD": TARGETS["TE_COMPOSITE_WORLD"],
    }
    for label, url in targets.items():
        safe(f"{source}_{label}", lambda label=label, url=url: te_public_page_extract(source, label, url))


def te_public_page_extract(source, label, url):
    r, err = fetch(url)
    if err:
        record(source, label, url, "ERROR", "GET", error=str(err))
        return
    raw = r.text
    text = normalized_text(raw)
    body, links, scripts, attrs, tables = html_inventory(source, url, raw)

    # We deliberately report explicit labels and nearby context. No value is
    # promoted to Actual solely because it appears near a label.
    wanted = [
        "last", "previous", "reference", "actual", "consensus", "forecast",
        "release", "release date", "historical", "source", "unit",
        "business activity", "new business", "new orders", "new export business",
        "employment", "backlogs", "input prices", "prices charged", "future activity",
        "new export orders", "supplier deliveries", "inventories", "inventory sentiment",
        "imports", "exports", "flash estimate", "revised higher", "revised lower",
    ]
    aliases = sum(([x] for x in wanted), [])
    contexts = context_windows(raw, aliases, 1200, 160)

    record(
        source, f"STRUCTURED_{label}", url, r.status_code, "PUBLIC_GET",
        final_url=r.url,
        content_type=r.headers.get("content-type", ""),
        bytes=len(r.content),
        fields=field_hits(text),
        explicit_value_words=[w for w in VALUE_WORDS if re.search(rf"\b{re.escape(w)}\b", text, re.I)],
        dates=DATE_RE.findall(text)[:120],
        numbers=NUMBER_RE.findall(text)[:300],
        contexts=contexts,
        tables=tables[:30],
        links=[x for x in links if relevant_url(x["url"], x["label"])][:200],
        json_candidates=extract_json_like(raw, 80),
    )

    # Capture the page text for later offline comparison/review.
    digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    (OUT / f"TE_PUBLIC_{label}_{digest}.txt").write_text(text, encoding="utf-8", errors="ignore")


def write_outputs():
    (OUT / "errors.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "evidence.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    keys = sorted({k for row in rows for k in row})
    with (OUT / "evidence.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({
                k: json.dumps(row.get(k), ensure_ascii=False)
                if isinstance(row.get(k), (dict, list))
                else row.get(k, "")
                for k in keys
            })

    summary = {
        "generated_utc": now(),
        "result": "COLLECTION_COMPLETE",
        "exit_policy": 0,
        "errors": len(errors),
        "records": len(rows),
        "actual_inference": "FORBIDDEN",
        "previous_as_actual": "FORBIDDEN",
        "forecast_as_actual": "FORBIDDEN",
        "production_file_modified": False,
        "routes": [
            "page_http",
            "raw_context",
            "lxml_dom",
            "lxml_tables",
            "lxml_attributes",
            "inline_scripts",
            "external_scripts",
            "embedded_json_patterns",
            "embedded_urls",
            "relevant_links",
            "discovered_pages",
            "script_endpoints",
            "direct_endpoint_get",
            "s_and_p_release_candidates",
        ],
        "targets": TARGETS,
    }
    (OUT / "SUMMARY.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("=== V6 PMI EVIDENCE DEEP DIVE ===")
    print(f"RECORDS={len(rows)}")
    print(f"ERRORS={len(errors)}")
    print("RESULT=COLLECTION_COMPLETE")
    print("EXIT_POLICY=0")
    print("ACTUAL_INFERENCE=FORBIDDEN")
    print("PRODUCTION_FILE_MODIFIED=False")


def main():
    print("=== PMI EVIDENCE DEEP DIVE V6.1 ===")
    print("MODE=READ_ONLY|NON_ABORTING|MULTI_ROUTE")
    print("TARGETS=PUBLIC_TE|SPG_DE|SPG_EN|TE_SERVICE_LISTS|TE_FORECAST|TE_COMPOSITE")

    for source, url in TARGETS.items():
        safe(f"PAGE_{source}", lambda source=source, url=url: deep_probe_page(source, url))

    safe("SPG_RELEASE_DEEP", explicit_release_probe)
    safe("TE_PUBLIC_STRUCTURED", te_public_structured_probe)

    # A final health check is intentionally last; it cannot abort.
    safe("FINAL_WRITE", write_outputs)
    if not (OUT / "SUMMARY.json").exists():
        # Even this fallback must not make the process fail.
        try:
            write_outputs()
        except Exception as exc:
            print(f"WARNUNG: FINAL_FALLBACK_WRITE: {type(exc).__name__}: {exc}")

    print("COLLECTION_FINISHED_UNCONDITIONALLY=True")


if __name__ == "__main__":
    main()
