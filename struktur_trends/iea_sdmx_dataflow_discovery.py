#!/usr/bin/env python3
"""
IEA MES .Stat / product-access forensics v6.0

Diagnostic only. No production/cache changes.

Lauf 33 questions:
  Q1  What does the official MES product page expose for the SDMX/.Stat
      "Access" links (real hrefs, redirects, embedded data, scripts)?
  Q2  Which .Stat/API hosts are actually evidenced by the IEA page/client
      resources? No unverified IEA host is treated as authoritative.
  Q3  Do the official product-download references redirect to a usable file,
      a session/login flow, or an interstitial? Capture status/headers/final URL.
  Q4  If a real .Stat host is evidenced, does its documented v2 data route
      (/rest/v2/data/dataflow/{agency}/{id}/{version}/{key}) work for MESGEN or
      MESBAL? The route is tested only when the host was discovered from an
      official IEA resource; no blind host matrix is used.
  Q5  Are authentication/session indicators present? Record only header/cookie
      NAMES and presence, never cookie/token values.

Safeguards:
  - MESGEN/MESBAL are not inferred as dataflows. Lauf 31 already established
    them as real Dataflows; this run reuses that evidence rather than probing
    structure again.
  - No invented ProvisionAgreement IDs and no generic provision-agreement
    endpoint sweep.
  - No arbitrary sdmx.iea.org/api.iea.org host assumptions.
  - No API-key/token guessing or persistence of secrets.
  - At most one evidence-backed data request per discovered target route.
  - Product-page HTML/JS is parsed with stdlib only; no browser automation or
    extra dependency is required.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

PRODUCT_URL = "https://www.iea.org/data-and-statistics/data-product/monthly-electricity-statistics"
DATA_TOOL_URL = "https://www.iea.org/data-and-statistics/data-tools/monthly-electricity-statistics"
TARGETS = {
    "MESGEN": {
        "agency": "OECD.IEA",
        "version": "1.1",
        "download": "https://www.iea.org/product/download/023477-000289-023189",
    },
    "MESBAL": {
        "agency": "OECD.IEA",
        "version": "1.1",
        "download": "https://www.iea.org/product/download/023477-000289-023188",
    },
}
# Only these IEA service hosts were directly observed in previous forensic runs.
KNOWN_IEA_SERVICE_HOSTS = {
    "sis-cc-api-stable.iea.org",
    "sis-cc-nsi-stable.iea.org",
    "growth-sis-cc-api-wv.iea.org",
}

URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
STAT_HOST_RE = re.compile(r"(?:^|\.)stat(?:suite|suite\.com)?\.|\.stat-suite\.|\.stat\.", re.I)
SDMX_PATH_RE = re.compile(r"/(?:rest/)?v?\d*/?data(?:flow)?/|/rest/v2/data/dataflow/", re.I)
AUTH_HEADER_NAMES = {"authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token"}
COOKIE_NAME_RE = re.compile(r"(?:^|,)\s*([^=;,\s]+)=", re.I)

@dataclass
class HttpResult:
    category: str
    url: str
    final_url: str | None
    status: int | None
    content_type: str
    bytes: int
    elapsed_ms: int
    redirect_chain: list[dict[str, Any]]
    response_header_names: list[str]
    set_cookie_names: list[str]
    auth_indicators: list[str]
    saved: str | None
    extracted_urls: list[str]
    extracted_links: list[dict[str, str]]
    extracted_scripts: list[str]
    extracted_hosts: list[str]
    target_hits: list[str]
    error: str | None


def dedupe(items):
    out, seen = [], set()
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, dict) else str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def safe_url(value: str, base: str) -> str | None:
    value = value.strip().strip("\"'<>`)")
    if not value or value.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    u = urljoin(base, value)
    p = urlparse(u)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return u


class PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[str] = []
        self.links: list[dict[str, str]] = []
        self.scripts: list[str] = []
        self._script = False
        self._script_buf: list[str] = []
        self.script_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag.lower() == "a":
            href = safe_url(a.get("href", ""), self.base_url)
            if href:
                text = ""
                self.links.append({"href": href, "text": text})
                self.urls.append(href)
        for key in ("href", "src", "data-href", "data-url", "data-download-url", "data-endpoint"):
            if a.get(key):
                u = safe_url(a[key], self.base_url)
                if u:
                    self.urls.append(u)
        if tag.lower() == "script":
            src = safe_url(a.get("src", ""), self.base_url)
            if src:
                self.scripts.append(src)
            self._script = True
            self._script_buf = []

    def handle_data(self, data):
        if self._script:
            self._script_buf.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._script:
            self.script_text.append("".join(self._script_buf))
            self._script = False
            self._script_buf = []


def extract_embedded_urls(text: str, base_url: str) -> list[str]:
    out = []
    for raw in URL_RE.findall(text):
        u = safe_url(raw, base_url)
        if u:
            out.append(u)
    # JSON/JS frequently escapes slashes.
    for raw in re.findall(r"https?:\\?/\\?/[^\"'\s<>]+", text, re.I):
        u = safe_url(raw.replace("\\/", "/"), base_url)
        if u:
            out.append(u)
    return dedupe(out)


def classify_host(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    if host in KNOWN_IEA_SERVICE_HOSTS:
        return "known_iea_service_host"
    if STAT_HOST_RE.search(host):
        return "stat_candidate_host"
    return "other_host"


def save_body(root: Path, category: str, url: str, status: int | None, body: bytes, suffix: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:20]
    path = root / f"{category}_{status or 'ERR'}_{digest}{suffix}"
    path.write_bytes(body)
    return str(path)


def auth_indicators(headers: requests.structures.CaseInsensitiveDict, set_cookie: str) -> list[str]:
    out = []
    for name in headers.keys():
        if name.lower() in AUTH_HEADER_NAMES:
            out.append(f"response-header:{name.lower()}")
    low = set_cookie.lower()
    for token in ("session", "auth", "token", "sso", "csrf"):
        if token in low:
            out.append(f"set-cookie-name-hint:{token}")
    return sorted(set(out))


def cookie_names(set_cookie: str) -> list[str]:
    return dedupe(COOKIE_NAME_RE.findall(set_cookie or ""))


def fetch(session: requests.Session, root: Path, category: str, url: str, timeout: int, parse_page: bool = False) -> tuple[HttpResult, requests.Response | None]:
    t = time.perf_counter()
    try:
        r = session.get(
            url,
            headers={
                "User-Agent": "NEUBER-MACRO-MES-forensics/6.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        elapsed = int((time.perf_counter() - t) * 1000)
        body = r.content
        ct = r.headers.get("Content-Type", "")
        suffix = ".html" if "html" in ct.lower() else ".json" if "json" in ct.lower() else ".xml" if "xml" in ct.lower() else ".bin"
        saved = save_body(root, category, url, r.status_code, body, suffix)
        chain = []
        for h in r.history:
            chain.append({"status": h.status_code, "url": h.url, "location": h.headers.get("Location")})
        chain.append({"status": r.status_code, "url": r.url, "location": None})
        set_cookie = r.headers.get("Set-Cookie", "")
        extracted_urls: list[str] = []
        extracted_links: list[dict[str, str]] = []
        extracted_scripts: list[str] = []
        if parse_page and "html" in ct.lower():
            text = body.decode("utf-8", "replace")
            parser = PageParser(url)
            parser.feed(text)
            extracted_links = parser.links
            extracted_scripts = parser.scripts
            extracted_urls = dedupe(parser.urls + extract_embedded_urls(text, url))
            for script_text in parser.script_text:
                extracted_urls.extend(extract_embedded_urls(script_text, url))
            extracted_urls = dedupe(extracted_urls)
        hosts = dedupe([urlparse(u).netloc.lower() for u in extracted_urls if urlparse(u).netloc])
        target_hits = [k for k, spec in TARGETS.items() if spec["download"] in extracted_urls or k.lower() in r.text.lower()]
        return HttpResult(
            category, url, r.url, r.status_code, ct, len(body), elapsed, chain,
            sorted(r.headers.keys()), cookie_names(set_cookie), auth_indicators(r.headers, set_cookie),
            saved, extracted_urls, extracted_links, extracted_scripts, hosts, target_hits, None
        ), r
    except Exception as e:
        return HttpResult(category, url, None, None, "", 0, int((time.perf_counter() - t) * 1000), [], [], [], [], None, [], [], [], [], [], str(e)), None


def download_probe(session: requests.Session, root: Path, key: str, url: str, timeout: int):
    res, r = fetch(session, root, f"official_download_{key}", url, timeout, parse_page=False)
    zip_members = []
    if r is not None and r.content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                zip_members = z.namelist()[:100]
        except zipfile.BadZipFile:
            pass
    return {
        "category": "official_download",
        "target": key,
        "url": url,
        "final_url": res.final_url,
        "status": res.status,
        "content_type": res.content_type,
        "bytes": res.bytes,
        "elapsed_ms": res.elapsed_ms,
        "redirect_chain": res.redirect_chain,
        "response_header_names": res.response_header_names,
        "set_cookie_names": res.set_cookie_names,
        "auth_indicators": res.auth_indicators,
        "saved": res.saved,
        "zip_members": zip_members,
        "looks_like_sdmx": bool(r is not None and (b"OBS_VALUE" in r.content or b"TIME_PERIOD" in r.content)),
        "error": res.error,
    }


def derive_stat_hosts(page_results: list[HttpResult]) -> list[str]:
    hosts = []
    for r in page_results:
        for h in r.extracted_hosts:
            if h in KNOWN_IEA_SERVICE_HOSTS or STAT_HOST_RE.search(h):
                hosts.append(h)
    return sorted(set(hosts))


def derive_stat_links(page_results: list[HttpResult]) -> list[str]:
    urls = []
    for r in page_results:
        for u in r.extracted_urls:
            low = u.lower()
            if any(x in low for x in (".stat", "/rest/", "/api/", "sdmx", "dataflow", "product/download")):
                urls.append(u)
    return dedupe(urls)


def build_evidence_backed_data_url(host: str, target: str) -> str:
    spec = TARGETS[target]
    # This syntax is tested only after the host itself was found in an official
    # IEA page/client resource. The empty key is the least invasive collection query.
    return f"https://{host}/rest/v2/data/dataflow/{spec['agency']}/{target}/{spec['version']}/"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="iea_sdmx_dataflow_discovery")
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()

    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    page_results: list[HttpResult] = []

    # Q1/Q2: only the two official IEA pages are fetched initially.
    for label, url in (("product_page", PRODUCT_URL), ("data_tool_page", DATA_TOOL_URL)):
        res, _ = fetch(session, root, label, url, args.timeout, parse_page=True)
        page_results.append(res)

    stat_hosts = derive_stat_hosts(page_results)
    stat_links = derive_stat_links(page_results)

    # Follow only URLs actually emitted by the official IEA pages and only a
    # small bounded number. Never crawl arbitrary third-party assets.
    page_follow_candidates = []
    for u in stat_links:
        host = urlparse(u).netloc.lower()
        if host in KNOWN_IEA_SERVICE_HOSTS or STAT_HOST_RE.search(host) or "/product/download/" in u:
            page_follow_candidates.append(u)
    page_follow_candidates = dedupe(page_follow_candidates)[:12]

    follow_results: list[HttpResult] = []
    for u in page_follow_candidates:
        res, _ = fetch(session, root, "page_discovered_resource", u, args.timeout, parse_page=True)
        follow_results.append(res)

    all_page_results = page_results + follow_results
    stat_hosts = sorted(set(stat_hosts + derive_stat_hosts(follow_results)))

    # Q3: official product download references exactly once each.
    downloads = [download_probe(session, root, k, spec["download"], args.timeout) for k, spec in TARGETS.items()]

    # Q4/Q5: only hosts actually evidenced by the official IEA pages/client
    # resources can reach a .Stat v2 data probe. At most one request per target.
    evidenced_data_urls = []
    data_results = []
    for host in stat_hosts:
        for target in TARGETS:
            url = build_evidence_backed_data_url(host, target)
            evidenced_data_urls.append({"target": target, "url": url, "host": host, "source": "official_ia_page_evidence"})
            # Exactly one request per target on the first evidenced host only.
            break
        break
    if stat_hosts:
        host = stat_hosts[0]
        for target in TARGETS:
            url = build_evidence_backed_data_url(host, target)
            res, r = fetch(session, root, f"stat_v2_data_{target}", url, args.timeout, parse_page=False)
            data_results.append({
                "target": target,
                "url": url,
                "host": host,
                "status": res.status,
                "final_url": res.final_url,
                "content_type": res.content_type,
                "bytes": res.bytes,
                "elapsed_ms": res.elapsed_ms,
                "redirect_chain": res.redirect_chain,
                "response_header_names": res.response_header_names,
                "set_cookie_names": res.set_cookie_names,
                "auth_indicators": res.auth_indicators,
                "saved": res.saved,
                "strict_payload": bool(r is not None and r.status_code == 200 and b"OBS_VALUE" in r.content and b"TIME_PERIOD" in r.content),
                "error": res.error,
            })

    all_results = all_page_results
    report = {
        "version": "6.0-targeted-stat-access",
        "questions": [
            "official MES product/data-tool page Access and client-resource discovery",
            ".Stat/API host discovery from official IEA resources only",
            "official MESGEN/MESBAL product-download redirect/session behavior",
            "authentication/session indicators without retaining secrets",
            "one evidence-backed .Stat v2 data request per target; no blind host matrix",
        ],
        "official_sources": [PRODUCT_URL, DATA_TOOL_URL],
        "targets": TARGETS,
        "known_iea_service_hosts": sorted(KNOWN_IEA_SERVICE_HOSTS),
        "discovered_stat_hosts": stat_hosts,
        "discovered_stat_links": stat_links,
        "followed_page_resources": page_follow_candidates,
        "counts": {
            "official_page_probes": len(page_results),
            "page_discovered_resource_probes": len(follow_results),
            "official_download_probes": len(downloads),
            "evidence_backed_data_probes": len(data_results),
        },
        "official_downloads": downloads,
        "evidence_backed_data_urls": evidenced_data_urls,
        "data_probes": data_results,
        "results": [asdict(r) for r in all_results],
        "security_note": "No cookie/token/API-key values are stored; only header names and cookie-name indicators are recorded.",
    }
    (root / "iea_sdmx_dataflow_discovery_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("IEA MES .STAT / PRODUCT-ACCESS FORENSICS – targeted v6.0")
    print(f"Official page probes:             {len(page_results)}")
    print(f"Page-discovered resource probes:  {len(follow_results)}")
    print(f"Discovered .Stat/API hosts:       {stat_hosts}")
    print(f"Discovered relevant links:        {len(stat_links)}")
    print(f"Official download probes:         {len(downloads)}")
    print(f"Evidence-backed data probes:      {len(data_results)}")
    print("Blind host/data matrix:            0")
    print(f"Report:                            {root / 'iea_sdmx_dataflow_discovery_report.json'}")


if __name__ == "__main__":
    main()
