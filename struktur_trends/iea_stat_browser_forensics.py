#!/usr/bin/env python3
"""
IEA .Stat Browser Forensics v8.0 (Lauf 34)

Ziel:
- Regulären öffentlichen Browserzugang zur offiziellen IEA-MES-Seite prüfen.
- Client-seitig sichtbare .Stat/SDMX/API/Download-Endpunkte evidenzbasiert erfassen.
- Keine Cloudflare-Umgehung.
- Keine Login-Automation.
- Keine Speicherung von Cookie-/Authorization-/API-Key-Werten.
- Keine geratenen REST-Datenendpunkte.

Ausgabe:
  iea_stat_browser_forensics/
    iea_stat_browser_forensics_report.json
    rendered_*.html
    screenshot_*.png
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PRODUCT_URL = "https://www.iea.org/data-and-statistics/data-product/monthly-electricity-statistics"
TOOL_URL = "https://www.iea.org/data-and-statistics/data-tools/monthly-electricity-statistics"
OUTPUT_DIR = Path("iea_stat_browser_forensics")

SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "id_token", "code", "key", "api_key", "apikey",
    "signature", "sig", "session", "sessionid", "auth", "authorization",
}
SENSITIVE_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-auth-token",
}
INTEREST_TERMS = (
    "mesgen", "mesbal", "sdmx", ".stat", "dotstat", "stat-suite", "statsuite",
    "/rest/", "/api/", "download", "dataset", "dataflow", "sis-cc",
)
CLOUDFLARE_TERMS = (
    "cf-mitigated", "challenges.cloudflare.com", "cloudflare", "just a moment",
)

def redact_url(url: str) -> str:
    try:
        p = urlsplit(url)
        safe = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            if k.lower() in SENSITIVE_QUERY_KEYS:
                safe.append((k, "<redacted>"))
            else:
                safe.append((k, v))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(safe, doseq=True), p.fragment))
    except Exception:
        return url

def header_metadata(headers: dict) -> dict:
    names = sorted({str(k).lower() for k in headers})
    return {
        "header_names": names,
        "sensitive_header_names_present": sorted(
            n for n in names if n in SENSITIVE_HEADER_NAMES
        ),
        "has_location": "location" in names,
        "has_cf_headers": any(n.startswith("cf-") for n in names),
    }

def classify(url: str, headers: dict | None = None) -> list[str]:
    s = url.lower()
    if headers:
        s += " " + " ".join(str(k).lower() for k in headers)
        s += " " + " ".join(str(v).lower() for k, v in headers.items()
                            if str(k).lower() not in SENSITIVE_HEADER_NAMES)
    tags = set()
    if "iea.org" in s:
        tags.add("IEA")
    if any(x in s for x in ("stat", "dotstat", "sis-cc")):
        tags.add(".Stat")
    if "sdmx" in s:
        tags.add("SDMX")
    if "download" in s:
        tags.add("Download")
    if "/api/" in s or "api." in s:
        tags.add("API")
    if "mesgen" in s:
        tags.add("MESGEN")
    if "mesbal" in s:
        tags.add("MESBAL")
    if any(x in s for x in CLOUDFLARE_TERMS):
        tags.add("Cloudflare")
    return sorted(tags)

def is_interesting(url: str) -> bool:
    s = url.lower()
    return any(term in s for term in INTEREST_TERMS)

def safe_location(headers: dict) -> str | None:
    loc = headers.get("location")
    return redact_url(loc) if loc else None

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    requests = []
    responses = []
    failures = []
    downloads = []
    pages_seen = []
    interactions = []
    console_errors = []
    evidence_urls = set()
    cloudflare_evidence = []
    access_state = {
        "product_page_reached": False,
        "tool_page_reached": False,
        "cloudflare_challenge_seen": False,
        "login_text_seen": False,
        "connect_text_seen": False,
        "stat_text_seen": False,
        "sdmx_text_seen": False,
    }

    def record_request(req):
        try:
            h = req.headers
            u = redact_url(req.url)
            item = {
                "url": u,
                "host": urlsplit(req.url).netloc,
                "method": req.method,
                "resource_type": req.resource_type,
                "is_navigation_request": req.is_navigation_request(),
                "classification": classify(req.url, h),
                **header_metadata(h),
            }
            requests.append(item)
            if is_interesting(req.url):
                evidence_urls.add(u)
        except Exception as exc:
            failures.append({"stage": "request_handler", "error": type(exc).__name__})

    def record_response(res):
        try:
            h = res.headers
            u = redact_url(res.url)
            item = {
                "url": u,
                "host": urlsplit(res.url).netloc,
                "status": res.status,
                "content_type": h.get("content-type", ""),
                "location": safe_location(h),
                "classification": classify(res.url, h),
                **header_metadata(h),
            }
            responses.append(item)
            if is_interesting(res.url) or item["location"]:
                evidence_urls.add(u)
                if item["location"]:
                    evidence_urls.add(item["location"])
            lower_headers = " ".join(f"{k}:{v}" for k, v in h.items()).lower()
            if "cf-mitigated" in lower_headers or "cloudflare" in lower_headers:
                access_state["cloudflare_challenge_seen"] = True
                cloudflare_evidence.append({
                    "url": u, "status": res.status,
                    "cf_header_names": sorted(k for k in h if k.lower().startswith("cf-")),
                })
        except Exception as exc:
            failures.append({"stage": "response_handler", "error": type(exc).__name__})

    def record_failed(req):
        try:
            failures.append({
                "stage": "requestfailed",
                "url": redact_url(req.url),
                "method": req.method,
                "resource_type": req.resource_type,
                "failure": req.failure,
            })
        except Exception:
            failures.append({"stage": "requestfailed", "error": "unreadable"})

    def record_download(download):
        # Keine Datei speichern; nur nicht-sensitive Metadaten.
        try:
            downloads.append({
                "suggested_filename": download.suggested_filename,
                "url": redact_url(download.url),
            })
            evidence_urls.add(redact_url(download.url))
        except Exception as exc:
            failures.append({"stage": "download_handler", "error": type(exc).__name__})

    def inspect_page(page, label: str):
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            url = redact_url(page.url)
        except Exception:
            url = ""
        pages_seen.append({"label": label, "url": url, "title": title})

        try:
            html = page.content()
            (OUTPUT_DIR / f"rendered_{label}.html").write_text(html, encoding="utf-8")
            low = html.lower()
            if "just a moment" in low or "cf-chl-" in low:
                access_state["cloudflare_challenge_seen"] = True
            if re.search(r"\bsign\s*in\b|\blog\s*in\b", low):
                access_state["login_text_seen"] = True
            if re.search(r"\bconnect\b", low):
                access_state["connect_text_seen"] = True
            if ".stat" in low or "stat suite" in low or "data explorer" in low:
                access_state["stat_text_seen"] = True
            if "sdmx" in low:
                access_state["sdmx_text_seen"] = True

            # Nur URLs/Hosts aus tatsächlich gerendertem Inhalt übernehmen.
            for m in re.findall(r'https?://[^"\'<>\s]+', html):
                if is_interesting(m):
                    evidence_urls.add(redact_url(m))
        except Exception as exc:
            failures.append({"stage": f"content_{label}", "error": type(exc).__name__})

        try:
            page.screenshot(path=str(OUTPUT_DIR / f"screenshot_{label}.png"), full_page=True)
        except Exception as exc:
            failures.append({"stage": f"screenshot_{label}", "error": type(exc).__name__})

    def navigate(page, url: str, label: str):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            status = response.status if response else None
            interactions.append({
                "action": "navigate",
                "label": label,
                "url": redact_url(url),
                "final_url": redact_url(page.url),
                "status": status,
            })
            return status
        except PlaywrightTimeoutError:
            interactions.append({
                "action": "navigate",
                "label": label,
                "url": redact_url(url),
                "result": "timeout",
                "final_url": redact_url(page.url),
            })
        except Exception as exc:
            interactions.append({
                "action": "navigate",
                "label": label,
                "url": redact_url(url),
                "result": "error",
                "error": type(exc).__name__,
                "final_url": redact_url(page.url),
            })
        return None

    def click_public_access_candidates(page):
        # Nur sichtbare öffentliche Links/Buttons; kein Login, keine Credentials.
        patterns = [
            re.compile(r"^Access$", re.I),
            re.compile(r"\.Stat", re.I),
            re.compile(r"Data Explorer", re.I),
            re.compile(r"SDMX", re.I),
        ]
        for pat in patterns:
            try:
                loc = page.get_by_role("link", name=pat)
                count = min(loc.count(), 3)
                for i in range(count):
                    candidate = loc.nth(i)
                    try:
                        if not candidate.is_visible():
                            continue
                        href = candidate.get_attribute("href")
                        text = candidate.inner_text(timeout=2000)
                        if href:
                            evidence_urls.add(redact_url(page.url.rstrip("/") + "/" + href)
                                              if href.startswith("/") else redact_url(href))
                        interactions.append({
                            "action": "inspect_link",
                            "text": text[:200],
                            "href": redact_url(href) if href else None,
                        })
                        # Nur genau einen regulären Access/Data-Explorer-Link öffnen.
                        if pat.pattern in (r"^Access$", r"\.Stat", r"Data Explorer"):
                            with page.context.expect_page(timeout=5000) as pi:
                                candidate.click(timeout=5000)
                            popup = pi.value
                            popup.wait_for_load_state("domcontentloaded", timeout=30000)
                            popup.wait_for_timeout(5000)
                            inspect_page(popup, f"popup_{len(pages_seen)+1}")
                            interactions.append({
                                "action": "click_public_access",
                                "text": text[:200],
                                "final_url": redact_url(popup.url),
                            })
                            return
                    except PlaywrightTimeoutError:
                        # Link kann in derselben Seite navigieren oder keinen Popup erzeugen.
                        try:
                            before = page.url
                            candidate.click(timeout=5000)
                            page.wait_for_timeout(5000)
                            interactions.append({
                                "action": "click_public_access",
                                "text": text[:200] if "text" in locals() else "",
                                "before_url": redact_url(before),
                                "final_url": redact_url(page.url),
                            })
                            inspect_page(page, "after_access_click")
                            return
                        except Exception:
                            continue
                    except Exception:
                        continue
            except Exception:
                continue

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-GB",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )

        context.on("request", record_request)
        context.on("response", record_response)
        context.on("requestfailed", record_failed)
        context.on("download", record_download)

        page = context.new_page()
        page.on("console", lambda msg: console_errors.append({
            "type": msg.type, "text": msg.text[:1000]
        }) if msg.type in {"error", "warning"} else None)

        product_status = navigate(page, PRODUCT_URL, "product")
        access_state["product_page_reached"] = bool(product_status and product_status < 400)
        inspect_page(page, "product")

        # Falls Produktseite regulär erreichbar: nur öffentliche Access-Links untersuchen.
        if access_state["product_page_reached"] and not access_state["cloudflare_challenge_seen"]:
            click_public_access_candidates(page)

        # Zweite offizielle IEA-Oberfläche unabhängig prüfen.
        tool_page = context.new_page()
        tool_status = navigate(tool_page, TOOL_URL, "tool")
        access_state["tool_page_reached"] = bool(tool_status and tool_status < 400)
        inspect_page(tool_page, "tool")

        if access_state["tool_page_reached"] and not access_state["cloudflare_challenge_seen"]:
            click_public_access_candidates(tool_page)

        # Kurzes Fenster für nachgelagerte XHR/fetch-Aufrufe.
        time.sleep(3)
        browser.close()

    # Evidenzbasierte Kandidaten: nur tatsächlich beobachtete URLs.
    candidates = []
    for u in sorted(evidence_urls):
        tags = classify(u)
        if any(t in tags for t in (".Stat", "SDMX", "API", "MESGEN", "MESBAL", "Download")):
            candidates.append({"url": u, "host": urlsplit(u).netloc, "classification": tags})

    observed_hosts = sorted({
        x["host"] for x in requests + responses
        if x.get("host")
    })
    candidate_hosts = sorted({
        x["host"] for x in candidates if x.get("host")
    })

    report = {
        "version": "8.0",
        "purpose": "IEA MES public browser/.Stat network forensics",
        "safety": {
            "cloudflare_bypass_attempted": False,
            "login_automation_attempted": False,
            "credentials_used": False,
            "sensitive_header_values_persisted": False,
            "guessed_data_endpoints_probed": False,
        },
        "access_state": access_state,
        "summary": {
            "requests": len(requests),
            "responses": len(responses),
            "failures": len(failures),
            "downloads": len(downloads),
            "pages_seen": len(pages_seen),
            "observed_hosts": observed_hosts,
            "evidence_backed_candidate_hosts": candidate_hosts,
            "evidence_backed_candidate_urls": len(candidates),
        },
        "pages": pages_seen,
        "interactions": interactions,
        "candidate_endpoints": candidates,
        "cloudflare_evidence": cloudflare_evidence,
        "downloads": downloads,
        "requests": requests,
        "responses": responses,
        "failures": failures,
        "console_errors": console_errors,
    }

    out = OUTPUT_DIR / "iea_stat_browser_forensics_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("IEA .STAT BROWSER FORENSICS v8.0")
    print(f"Product page reached:          {access_state['product_page_reached']}")
    print(f"Tool page reached:             {access_state['tool_page_reached']}")
    print(f"Cloudflare challenge seen:     {access_state['cloudflare_challenge_seen']}")
    print(f"Requests / responses:          {len(requests)} / {len(responses)}")
    print(f"Downloads observed:            {len(downloads)}")
    print(f"Evidence-backed candidates:    {len(candidates)}")
    print(f"Candidate hosts:               {candidate_hosts}")
    print(f"Report:                        {out}")

if __name__ == "__main__":
    main()
