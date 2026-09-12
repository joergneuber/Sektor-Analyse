#!/usr/bin/env python3
"""Deep browser/network discovery of the official IEA MES data access path.

This script is diagnostic only. It does not modify production data or infer
bullish/bearish direction. It records browser navigation, DOM candidates,
network traffic, downloads, storage/config clues and small payload metadata so
an official IEA MES access path can be reconstructed without guessing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

PRODUCT_URL = "https://www.iea.org/data-and-statistics/data-product/monthly-electricity-statistics"
TOOLS_URL = "https://www.iea.org/data-and-statistics/data-tools/monthly-electricity-statistics"
SERVICE_ROOTS = (
    "https://sis-cc-api-stable.iea.org/",
    "https://sis-cc-nsi-stable.iea.org/",
)

DISCOVERY_TERMS = (
    ".stat", "sdmx", "dataflow", "datastructure", "/rest/", "csv", "zip",
    "json", "xml", "monthly electricity statistics", "mesgen", "mesbal",
    "generation", "balance", "access", "explorer", "data set", "download",
    "view data", "api", "dataset",
)
MES_MARKERS = (
    "monthly electricity statistics", "mesgen", "mesbal", "electricity statistics",
    "energy_balance_flow", "energy_product", "time_period", "obs_value",
)
CLICK_PATTERNS = (
    r"\.stat", r"access", r"data\s*sets?", r"data\s*set", r"view\s+data",
    r"download", r"explorer", r"csv", r"sdmx",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relevant(value: str) -> bool:
    text = (value or "").lower()
    return any(term in text for term in DISCOVERY_TERMS)


def mes_score(value: str) -> int:
    text = (value or "").lower()
    return sum(term in text for term in MES_MARKERS)


def safe_name(value: str, suffix: str = ".bin") -> str:
    base = Path(urlparse(value).path).name or "payload"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    if "." not in base:
        base += suffix
    return f"{digest(value.encode())[:12]}_{base}"


def save_bytes(data: bytes, url: str, out: Path, suffix: str = ".bin") -> str:
    path = out / safe_name(url, suffix)
    path.write_bytes(data)
    return str(path)


def inspect_payload(data: bytes, content_type: str, url: str, out: Path) -> dict:
    text = data[:2_000_000].decode("utf-8", "ignore")
    result = {
        "url": url,
        "content_type": content_type or "",
        "size": len(data),
        "sha256": digest(data),
        "mes_score": mes_score(text),
        "looks_sdmx": any(k in text.lower() for k in (
            "sdmx", "obs_value", "time_period", "energy_balance_flow",
            "energy_product", "dataflow", "structure",
        )),
    }
    is_zip = data[:2] == b"PK" or "zip" in (content_type or "").lower() or url.lower().endswith(".zip")
    if is_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                result["zip_members"] = names[:500]
                result["zip_mes_members"] = [n for n in names if mes_score(n)]
                result["saved"] = save_bytes(data, url, out, ".zip")
                members = []
                for name in names[:500]:
                    lower = name.lower()
                    if not lower.endswith((".csv", ".xml", ".json", ".txt")):
                        continue
                    try:
                        raw = archive.read(name)
                        member_text = raw[:2_000_000].decode("utf-8", "ignore")
                        members.append({
                            "name": name,
                            "size": len(raw),
                            "mes_score": mes_score(member_text),
                            "looks_sdmx": "time_period" in member_text.lower() or "obs_value" in member_text.lower(),
                        })
                    except Exception as exc:
                        members.append({"name": name, "error": str(exc)})
                result["text_members"] = members
        except zipfile.BadZipFile:
            result["zip_error"] = "invalid ZIP"
    elif data and ("text" in (content_type or "").lower() or any(x in (content_type or "").lower() for x in ("json", "xml", "csv"))):
        result["saved"] = save_bytes(data, url, out, ".txt")
        result["preview"] = text[:4000]
    return result


def extract_urls(text: str, base: str) -> list[str]:
    found = set(re.findall(r'''(?:href|src|action)\s*=\s*["']([^"']+)["']''', text, re.I))
    found.update(re.findall(r"https?://[^\"'<>\\\s]+", text))
    urls = {urljoin(base, x.rstrip("),;")) for x in found}
    return sorted(u for u in urls if u.startswith(("http://", "https://")) and relevant(u))


def get_page(session: requests.Session, url: str, out: Path) -> dict:
    result = {"url": url}
    try:
        response = session.get(url, timeout=45, allow_redirects=True)
        result.update({
            "status": response.status_code,
            "final_url": response.url,
            "content_type": response.headers.get("content-type", ""),
            "history": [{"status": h.status_code, "url": h.url} for h in response.history],
        })
        if response.content:
            result["payload"] = inspect_payload(response.content, response.headers.get("content-type", ""), response.url, out)
        if response.ok:
            result["candidate_urls"] = extract_urls(response.text, response.url)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def service_probes(session: requests.Session, out: Path) -> list[dict]:
    paths = ("", "rest/", "rest/dataflow", "rest/v1/dataflow", "rest/v2/dataflow", "SdmxRegistryService")
    results = []
    for root in SERVICE_ROOTS:
        for path in paths:
            url = urljoin(root, path)
            item = {"url": url}
            try:
                response = session.get(url, timeout=25, allow_redirects=True)
                item.update({
                    "status": response.status_code,
                    "final_url": response.url,
                    "content_type": response.headers.get("content-type", ""),
                })
                if response.content:
                    item["payload"] = inspect_payload(response.content, response.headers.get("content-type", ""), response.url, out)
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            results.append(item)
    return results


def browser_discovery(urls: list[str], out: Path, headed: bool, wait_seconds: int) -> dict:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"available": False, "error": "Install Playwright and Chromium first."}

    result = {
        "available": True,
        "pages": [],
        "navigations": [],
        "requests": [],
        "responses": [],
        "downloads": [],
        "console": [],
        "page_errors": [],
        "clicks": [],
        "storage": [],
        "scripts": [],
        "saved_payloads": [],
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(
            accept_downloads=True,
            service_workers="block",
            ignore_https_errors=False,
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
        )
        try:
            context.tracing.start(screenshots=False, snapshots=True, sources=False)
        except Exception as exc:
            result["tracing_error"] = str(exc)

        def record_request(request):
            result["requests"].append({
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "headers": dict(request.headers),
                "post_data": request.post_data,
                "relevant": relevant(request.url) or relevant(request.post_data or ""),
            })

        def record_response(response):
            item = {
                "status": response.status,
                "url": response.url,
                "headers": dict(response.headers),
                "resource_type": response.request.resource_type,
                "relevant": relevant(response.url),
            }
            # Capture bodies for data-like responses and for anything that looks relevant.
            content_type = response.headers.get("content-type", "")
            should_capture = (
                relevant(response.url)
                or any(k in content_type.lower() for k in ("json", "xml", "csv", "zip", "text/"))
                or response.request.resource_type in ("xhr", "fetch")
            )
            if should_capture:
                try:
                    body = response.body()
                    if body:
                        payload = inspect_payload(body, content_type, response.url, out)
                        item["payload"] = payload
                        if payload.get("saved"):
                            result["saved_payloads"].append(payload["saved"])
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            result["responses"].append(item)

        def record_download(download):
            item = {"url": download.url, "suggested_filename": download.suggested_filename}
            try:
                path = out / f"download_{digest(download.url.encode())[:12]}_{re.sub(r'[^A-Za-z0-9._-]+', '_', download.suggested_filename or 'download')}"
                download.save_as(path)
                item["path"] = str(path)
                raw = path.read_bytes()
                item["payload"] = inspect_payload(raw, "", download.url, out)
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            result["downloads"].append(item)

        def record_console(msg):
            result["console"].append({"type": msg.type, "text": msg.text[:4000]})

        for start_url in urls:
            page = context.new_page()
            page.on("request", record_request)
            page.on("response", record_response)
            page.on("download", record_download)
            page.on("console", record_console)
            page.on("pageerror", lambda exc: result["page_errors"].append(str(exc)[:4000]))
            page.on("framenavigated", lambda frame: result["navigations"].append({"url": frame.url, "page_url": page.url}))
            page_result = {"start_url": start_url}
            try:
                response = page.goto(start_url, wait_until="domcontentloaded", timeout=120000)
                page_result["goto_status"] = response.status if response else None
                page_result["final_url"] = page.url
                page_result["title"] = page.title()
                page.wait_for_timeout(wait_seconds * 1000)

                # Capture complete DOM and script URLs after JS has settled.
                html = page.content()
                page_result["html_size"] = len(html)
                page_result["dom_mes_score"] = mes_score(html)
                page_result["dom_candidate_urls"] = extract_urls(html, page.url)
                html_path = out / f"page_{digest(page.url.encode())[:12]}.html"
                html_path.write_text(html, encoding="utf-8", errors="replace")
                page_result["html_path"] = str(html_path)

                scripts = page.locator("script[src]").evaluate_all("els=>els.map(x=>x.src).filter(Boolean)")
                result["scripts"].extend(x for x in scripts if x not in result["scripts"])
                page_result["scripts"] = scripts

                links = page.locator("a").evaluate_all("""
                    els => els.map(a => ({
                      text:(a.innerText||a.textContent||'').trim(),
                      aria:a.getAttribute('aria-label')||'',
                      title:a.getAttribute('title')||'',
                      href:a.href||'',
                      target:a.target||''
                    }))
                """)
                page_result["links"] = links[:1000]

                buttons = page.locator("button, [role=button], input[type=button], input[type=submit]").evaluate_all("""
                    els => els.map((x,i) => ({
                      index:i,
                      tag:x.tagName,
                      text:(x.innerText||x.value||x.textContent||'').trim(),
                      aria:x.getAttribute('aria-label')||'',
                      title:x.getAttribute('title')||'',
                      disabled:!!x.disabled
                    }))
                """)
                page_result["buttons"] = buttons[:1000]

                # Extract browser storage without exposing cookies/auth headers.
                try:
                    page_result["local_storage"] = page.evaluate("Object.fromEntries(Object.entries(localStorage))")
                except Exception as exc:
                    page_result["local_storage_error"] = str(exc)
                try:
                    page_result["session_storage"] = page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
                except Exception as exc:
                    page_result["session_storage_error"] = str(exc)

                # Click candidate controls. We use DOM text/attributes, not guessed URLs.
                candidates = []
                for idx, control in enumerate(buttons):
                    text = " ".join(str(control.get(k, "")) for k in ("text", "aria", "title"))
                    if any(re.search(pattern, text, re.I) for pattern in CLICK_PATTERNS) and not control.get("disabled"):
                        candidates.append(("button", idx, text[:500]))
                for idx, link in enumerate(links):
                    text = " ".join(str(link.get(k, "")) for k in ("text", "aria", "title", "href"))
                    if any(re.search(pattern, text, re.I) for pattern in CLICK_PATTERNS):
                        candidates.append(("link", idx, text[:500]))

                page_result["click_candidates"] = [{"kind": k, "index": i, "text": t} for k, i, t in candidates[:100]]

                for kind, idx, label in candidates[:30]:
                    before_pages = len(context.pages)
                    before_url = page.url
                    click_item = {"kind": kind, "index": idx, "label": label, "before_url": before_url}
                    try:
                        locator = (page.locator("button, [role=button], input[type=button], input[type=submit]").nth(idx)
                                   if kind == "button" else page.locator("a").nth(idx))
                        if not locator.is_visible(timeout=1500):
                            click_item["skipped"] = "not visible"
                            result["clicks"].append(click_item)
                            continue
                        try:
                            with context.expect_page(timeout=5000) as popup_info:
                                locator.click(timeout=15000, no_wait_after=True)
                            popup = popup_info.value
                            popup.wait_for_load_state("domcontentloaded", timeout=30000)
                            popup.wait_for_timeout(wait_seconds * 1000)
                            popup_url = popup.url
                            click_item["popup_url"] = popup_url
                            click_item["popup_title"] = popup.title()
                            popup_html = popup.content()
                            popup_path = out / f"popup_{digest(popup_url.encode())[:12]}.html"
                            popup_path.write_text(popup_html, encoding="utf-8", errors="replace")
                            click_item["popup_html_path"] = str(popup_path)
                            page_result["popup_pages"] = page_result.get("popup_pages", []) + [popup_url]
                            popup.close()
                        except PlaywrightTimeoutError:
                            # No popup is normal. The click may have navigated the existing page.
                            if page.url != before_url:
                                page.wait_for_timeout(wait_seconds * 1000)
                            click_item["after_url"] = page.url
                            click_item["new_pages"] = len(context.pages) - before_pages
                        except Exception as exc:
                            click_item["click_error"] = f"{type(exc).__name__}: {exc}"
                    except Exception as exc:
                        click_item["click_error"] = f"{type(exc).__name__}: {exc}"
                    result["clicks"].append(click_item)

                # Re-capture after interaction.
                page_result["post_click_url"] = page.url
                page_result["post_click_title"] = page.title()
                page_result["post_click_dom_score"] = mes_score(page.content())
            except Exception as exc:
                page_result["error"] = f"{type(exc).__name__}: {exc}"
            result["pages"].append(page_result)
            try:
                page.close()
            except Exception:
                pass

        try:
            trace_path = out / "playwright_trace.zip"
            context.tracing.stop(path=str(trace_path))
            result["trace_path"] = str(trace_path)
        except Exception as exc:
            result["tracing_error"] = str(exc)
        context.close()
        browser.close()

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait", type=int, default=20)
    parser.add_argument("--output", default="iea_forensic_discovery")
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "*/*",
    })

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report = {
        "version": "2.0",
        "started_utc": started,
        "product_page": get_page(session, PRODUCT_URL, out),
        "data_tools_page": get_page(session, TOOLS_URL, out),
        "service_probes": service_probes(session, out),
    }
    report["browser"] = browser_discovery([PRODUCT_URL, TOOLS_URL], out, args.headed, args.wait)

    candidates = set(report["product_page"].get("candidate_urls", []))
    candidates.update(report["data_tools_page"].get("candidate_urls", []))
    for page in report["browser"].get("pages", []):
        candidates.update(page.get("dom_candidate_urls", []))
        for link in page.get("links", []):
            href = link.get("href", "")
            if href and (relevant(href) or relevant(link.get("text", ""))):
                candidates.add(href)
    for navigation in report["browser"].get("navigations", []):
        if relevant(navigation.get("url", "")):
            candidates.add(navigation["url"])

    requests_log = report["browser"].get("requests", [])
    responses_log = report["browser"].get("responses", [])
    relevant_requests = [x for x in requests_log if x.get("relevant")]
    relevant_responses = [x for x in responses_log if x.get("relevant")]
    data_responses = [x for x in responses_log if x.get("resource_type") in ("xhr", "fetch")]

    report["candidate_urls"] = sorted(x for x in candidates if x.startswith(("http://", "https://")))
    report["summary"] = {
        "candidate_count": len(report["candidate_urls"]),
        "total_requests": len(requests_log),
        "relevant_requests": len(relevant_requests),
        "total_responses": len(responses_log),
        "relevant_responses": len(relevant_responses),
        "xhr_fetch_responses": len(data_responses),
        "downloads": len(report["browser"].get("downloads", [])),
        "clicks_attempted": len(report["browser"].get("clicks", [])),
        "scripts_seen": len(report["browser"].get("scripts", [])),
        "pages": len(report["browser"].get("pages", [])),
    }

    report_path = out / "iea_forensic_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print("Report:", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
