#!/usr/bin/env python3
"""Broad forensic discovery of the official IEA Monthly Electricity Statistics access path.

Diagnostic only. It does not modify production data, infer market direction, log in,
or submit credentials. The purpose is to observe the real browser/network path used by
the official IEA MES pages and preserve enough evidence to reconstruct an official
free data-access route without guessing an endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

PRODUCT_URL = "https://www.iea.org/data-and-statistics/data-product/monthly-electricity-statistics"
TOOLS_URL = "https://www.iea.org/data-and-statistics/data-tools/monthly-electricity-statistics"
DATASETS_URL = "https://www.iea.org/data-and-statistics/data-sets?filter=electricity"
EXPLORERS_URL = "https://www.iea.org/data-and-statistics/data-explorers?type=monthly-and-real-time"
SERVICE_ROOTS = (
    "https://sis-cc-api-stable.iea.org/",
    "https://sis-cc-nsi-stable.iea.org/",
)

DISCOVERY_TERMS = (
    ".stat", "sdmx", "dataflow", "datastructure", "/rest/", "csv", "zip", "json", "xml",
    "monthly electricity statistics", "mesgen", "mesbal", "generation", "balance", "access",
    "explorer", "data set", "download", "view data", "api", "dataset", "electricity",
)
MES_MARKERS = (
    "monthly electricity statistics", "mesgen", "mesbal", "electricity statistics",
    "energy_balance_flow", "energy_product", "time_period", "obs_value", "monthly_electricity",
)
CLICK_PATTERNS = (
    r"\.stat", r"access", r"data\s*sets?", r"data\s*set", r"view\s+data", r"download",
    r"explorer", r"interactive\s+data\s+explorer", r"csv", r"sdmx", r"dataset", r"data\s*tool", r"view\s+data\s*sets?",
    r"connect", r"continue", r"access\s+via\s+ip",
)
DATA_CONTENT_TYPES = ("json", "xml", "csv", "zip", "text/", "octet-stream", "excel", "spreadsheet")
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"}
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_TEXT_SCAN = 5_000_000


def digest(data: bytes | str) -> str:
    raw = data if isinstance(data, bytes) else data.encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()


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
    return f"{digest(value)[:12]}_{base}"


def save_bytes(data: bytes, url: str, out: Path, suffix: str = ".bin", prefix: str = "") -> str:
    name = safe_name(url, suffix)
    if prefix:
        name = f"{prefix}_{name}"
    path = out / name
    path.write_bytes(data)
    return str(path)


def redact_text(value: str | None, max_len: int = 12000) -> str | None:
    if value is None:
        return None
    text = value[:max_len]
    # Do not persist obvious credential material from POST bodies.
    text = re.sub(r"(?i)(password|passwd|token|access_token|refresh_token|client_secret|api[_-]?key)\s*[=:]\s*([^&\s,}]+)", r"\1=[REDACTED]", text)
    return text


def safe_headers(headers: dict) -> dict:
    return {k: ("[REDACTED]" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def inspect_payload(data: bytes, content_type: str, url: str, out: Path, source: str = "payload") -> dict:
    scan = data[:MAX_TEXT_SCAN].decode("utf-8", "ignore")
    lower = scan.lower()
    result = {
        "url": url,
        "content_type": content_type or "",
        "size": len(data),
        "sha256": digest(data),
        "mes_score": mes_score(scan),
        "looks_sdmx": any(k in lower for k in ("sdmx", "obs_value", "time_period", "energy_balance_flow", "energy_product", "dataflow", "structure")),
        "source": source,
    }
    is_zip = data[:2] == b"PK" or "zip" in (content_type or "").lower() or url.lower().endswith(".zip")
    if is_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                result["zip_members"] = names[:1000]
                result["zip_mes_members"] = [n for n in names if mes_score(n)]
                result["saved"] = save_bytes(data, url, out, ".zip", prefix=source)
                members = []
                for name in names[:1000]:
                    if not name.lower().endswith((".csv", ".xml", ".json", ".txt")):
                        continue
                    try:
                        raw = archive.read(name)
                        member_text = raw[:2_000_000].decode("utf-8", "ignore")
                        members.append({
                            "name": name,
                            "size": len(raw),
                            "sha256": digest(raw),
                            "mes_score": mes_score(member_text),
                            "looks_sdmx": any(k in member_text.lower() for k in ("time_period", "obs_value", "energy_product", "energy_balance_flow")),
                            "preview": member_text[:1500],
                        })
                    except Exception as exc:
                        members.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
                result["text_members"] = members
        except zipfile.BadZipFile:
            result["zip_error"] = "invalid ZIP"
    elif data and ("text" in (content_type or "").lower() or any(x in (content_type or "").lower() for x in ("json", "xml", "csv"))):
        result["saved"] = save_bytes(data, url, out, ".txt", prefix=source)
        result["preview"] = scan[:5000]
    return result


def extract_urls(text: str, base: str) -> list[str]:
    found = set(re.findall(r'''(?:href|src|action)\s*=\s*["']([^"']+)["']''', text, re.I))
    found.update(re.findall(r"https?://[^\"'<>\\\s]+", text))
    # Also catch quoted API-like strings in inline JS/configuration.
    found.update(re.findall(r'''["']((?:https?://|/)[^"']{3,500})["']''', text, re.I))
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
            "headers": safe_headers(dict(response.headers)),
            "history": [{"status": h.status_code, "url": h.url} for h in response.history],
        })
        if response.content:
            result["payload"] = inspect_payload(response.content, response.headers.get("content-type", ""), response.url, out, "requests")
        if response.ok:
            result["candidate_urls"] = extract_urls(response.text, response.url)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def service_probes(session: requests.Session, out: Path) -> list[dict]:
    paths = (
        "", "rest/", "rest/dataflow", "rest/v1/dataflow", "rest/v2/dataflow",
        "rest/datastructure", "rest/v1/datastructure", "rest/v2/datastructure",
        "rest/data/all/all/latest", "SdmxRegistryService",
    )
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
                    "headers": safe_headers(dict(response.headers)),
                })
                if response.content:
                    item["payload"] = inspect_payload(response.content, response.headers.get("content-type", ""), response.url, out, "service")
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            results.append(item)
    return results


def browser_discovery(urls: list[str], out: Path, headed: bool, wait_seconds: int, max_clicks: int) -> dict:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"available": False, "error": "Install Playwright and Chromium first."}

    result = {
        "available": True, "pages": [], "navigations": [], "requests": [], "responses": [],
        "downloads": [], "console": [], "page_errors": [], "clicks": [], "dom_candidates": [],
        "storage": [], "scripts": [], "script_payloads": [], "saved_payloads": [],
        "frames": [], "websockets": [], "dialogs": [], "events": [],
    }

    with sync_playwright() as playwright:
        har_path = out / "network.har"
        launch_kwargs = {"headless": not headed}
        system_chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if system_chromium:
            launch_kwargs["executable_path"] = system_chromium
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            accept_downloads=True,
            service_workers="block",
            ignore_https_errors=False,
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
            record_har_path=str(har_path),
            record_har_content="embed",
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
        except Exception as exc:
            result["tracing_error"] = str(exc)

        def record_request(request):
            result["requests"].append({
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "headers": safe_headers(dict(request.headers)),
                "post_data": redact_text(request.post_data),
                "relevant": relevant(request.url) or relevant(request.post_data or ""),
                "is_data_like": request.resource_type in ("xhr", "fetch") or relevant(request.url),
            })

        def record_response(response):
            content_type = response.headers.get("content-type", "")
            should_capture = (
                relevant(response.url)
                or any(k in content_type.lower() for k in DATA_CONTENT_TYPES)
                or response.request.resource_type in ("xhr", "fetch")
            )
            item = {
                "status": response.status,
                "url": response.url,
                "headers": safe_headers(dict(response.headers)),
                "resource_type": response.request.resource_type,
                "content_type": content_type,
                "relevant": relevant(response.url),
                "captured_body": False,
            }
            if should_capture:
                try:
                    body = response.body()
                    item["body_size"] = len(body)
                    if len(body) <= MAX_BODY_BYTES:
                        payload = inspect_payload(body, content_type, response.url, out, "response")
                        item["payload"] = payload
                        item["captured_body"] = True
                        if payload.get("saved"):
                            result["saved_payloads"].append(payload["saved"])
                        # Discover API/data URLs hidden inside JSON/config responses.
                        item["body_candidate_urls"] = extract_urls(body[:MAX_TEXT_SCAN].decode("utf-8", "ignore"), response.url)
                    else:
                        item["body_skipped"] = f"over {MAX_BODY_BYTES} bytes"
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            result["responses"].append(item)

        def attach_page(page):
            page.on("request", record_request)
            page.on("response", record_response)
            page.on("download", record_download)
            page.on("console", record_console)
            page.on("pageerror", record_page_error)
            page.on("framenavigated", record_frame)
            page.on("websocket", record_websocket)
            page.on("dialog", record_dialog)
            result["events"].append({"type": "page_attached", "url": page.url})

        def record_download(download):
            item = {"url": download.url, "suggested_filename": download.suggested_filename}
            try:
                filename = re.sub(r"[^A-Za-z0-9._-]+", "_", download.suggested_filename or "download")
                path = out / f"download_{digest(download.url)[:12]}_{filename}"
                download.save_as(path)
                item["path"] = str(path)
                raw = path.read_bytes()
                item["payload"] = inspect_payload(raw, "", download.url, out, "download")
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            result["downloads"].append(item)

        def record_console(msg):
            result["console"].append({"type": msg.type, "text": msg.text[:4000], "location": msg.location})

        def record_page_error(exc):
            result["page_errors"].append(str(exc)[:4000])

        def record_frame(frame):
            result["navigations"].append({"url": frame.url, "page_url": frame.page.url, "is_main_frame": frame == frame.page.main_frame})
            result["frames"].append({"url": frame.url, "page_url": frame.page.url, "name": frame.name})

        def record_websocket(ws):
            item = {"url": ws.url, "frames": []}
            ws.on("framereceived", lambda payload: item["frames"].append({"direction": "received", "payload": str(payload)[:5000]}))
            ws.on("framesent", lambda payload: item["frames"].append({"direction": "sent", "payload": str(payload)[:5000]}))
            result["websockets"].append(item)

        def record_dialog(dialog):
            result["dialogs"].append({"type": dialog.type, "message": dialog.message[:2000]})
            try:
                dialog.dismiss()
            except Exception:
                pass

        context.on("page", attach_page)

        for start_url in urls:
            page = context.new_page()
            # new_page triggers context.on("page") and attaches handlers.
            page_result = {"start_url": start_url, "click_candidates": []}
            try:
                response = page.goto(start_url, wait_until="domcontentloaded", timeout=120000)
                page_result["goto_status"] = response.status if response else None
                page_result["final_url"] = page.url
                page_result["title"] = page.title()
                page.wait_for_timeout(wait_seconds * 1000)

                # Scroll progressively: lazy-loaded IEA components may only appear after viewport movement.
                for _ in range(4):
                    try:
                        page.mouse.wheel(0, 900)
                        page.wait_for_timeout(1500)
                    except Exception:
                        break
                try:
                    page.evaluate("window.scrollTo(0,0)")
                except Exception:
                    pass

                html = page.content()
                page_result["html_size"] = len(html)
                page_result["dom_mes_score"] = mes_score(html)
                page_result["dom_candidate_urls"] = extract_urls(html, page.url)
                html_path = out / f"page_{digest(page.url)}.html"
                html_path.write_text(html, encoding="utf-8", errors="replace")
                page_result["html_path"] = str(html_path)
                try:
                    screenshot_path = out / f"page_{digest(page.url)}.png"
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    page_result["screenshot_path"] = str(screenshot_path)
                except Exception as exc:
                    page_result["screenshot_error"] = str(exc)

                # Scripts and inline configuration.
                scripts = page.locator("script[src]").evaluate_all("els=>els.map(x=>x.src).filter(Boolean)")
                for script_url in scripts:
                    if script_url not in result["scripts"]:
                        result["scripts"].append(script_url)
                page_result["scripts"] = scripts

                # Broad DOM inventory. It includes custom elements, aria metadata and pointer/onclick clues.
                dom_inventory = page.evaluate(r"""
                () => {
                  const out=[];
                  const seen=new Set();
                  const walk=(root, depth=0)=>{
                    if(!root || depth>8) return;
                    for(const el of root.querySelectorAll ? root.querySelectorAll('*') : []){
                      if(seen.has(el)) continue; seen.add(el);
                      const cs=getComputedStyle(el);
                      const txt=((el.innerText||el.textContent||'')+'').trim().replace(/\s+/g,' ').slice(0,500);
                      const attrs={};
                      for(const a of Array.from(el.attributes||[])){
                        if(/^data-|^aria-|^(title|role|href|target|onclick|download|tabindex)$/i.test(a.name)) attrs[a.name]=a.value.slice(0,1000);
                      }
                      const clickable=el.matches('a,button,input,select,textarea,[role=button],[role=link],[tabindex]') ||
                        typeof el.onclick==='function' || cs.cursor==='pointer';
                      if(clickable && (txt || attrs.href || attrs['aria-label'] || attrs.title || attrs.onclick || attrs.role || el.tagName.toLowerCase().includes('-'))){
                        out.push({tag:el.tagName.toLowerCase(),text:txt,attrs,disabled:!!el.disabled,visible:cs.display!=='none'&&cs.visibility!=='hidden'&&parseFloat(cs.opacity||'1')>0, cursor:cs.cursor, x:Math.round(el.getBoundingClientRect().x), y:Math.round(el.getBoundingClientRect().y), w:Math.round(el.getBoundingClientRect().width), h:Math.round(el.getBoundingClientRect().height)});
                      }
                      if(el.shadowRoot) walk(el.shadowRoot, depth+1);
                    }
                  };
                  walk(document,0);
                  return out.slice(0,5000);
                }
                """)
                page_result["dom_inventory"] = dom_inventory
                broad_candidates = []
                for item in dom_inventory:
                    text = " ".join([item.get("text", ""), json.dumps(item.get("attrs", {}), ensure_ascii=False)])
                    if any(re.search(pattern, text, re.I) for pattern in CLICK_PATTERNS):
                        broad_candidates.append(item)
                page_result["dom_click_candidates"] = broad_candidates[:500]
                result["dom_candidates"].extend(broad_candidates[:500])

                # Standard links/buttons as a separate exact inventory.
                links = page.locator("a").evaluate_all("""
                    els => els.map(a => ({text:(a.innerText||a.textContent||'').trim(), aria:a.getAttribute('aria-label')||'', title:a.getAttribute('title')||'', href:a.href||'', target:a.target||'', download:a.getAttribute('download')||''}))
                """)
                buttons = page.locator("button, [role=button], [role=link], input[type=button], input[type=submit]").evaluate_all("""
                    els => els.map((x,i) => ({index:i,tag:x.tagName,text:(x.innerText||x.value||x.textContent||'').trim(),aria:x.getAttribute('aria-label')||'',title:x.getAttribute('title')||'',role:x.getAttribute('role')||'',disabled:!!x.disabled}))
                """)
                page_result["links"] = links[:3000]
                page_result["buttons"] = buttons[:3000]

                # Storage/config, excluding cookies.
                for storage_name, expression in (("local", "Object.fromEntries(Object.entries(localStorage))"), ("session", "Object.fromEntries(Object.entries(sessionStorage))")):
                    try:
                        storage = page.evaluate(expression)
                        sanitized = {str(k): redact_text(str(v), 5000) for k, v in storage.items()}
                        page_result[f"{storage_name}_storage"] = sanitized
                        result["storage"].append({"url": page.url, "type": storage_name, "keys": sorted(sanitized)})
                    except Exception as exc:
                        page_result[f"{storage_name}_storage_error"] = str(exc)

                # Click both semantic candidates and broad pointer candidates, de-duplicated by visible text/href.
                candidates = []
                seen_keys = set()
                for idx, button in enumerate(buttons):
                    text = " ".join(str(button.get(k, "")) for k in ("text", "aria", "title", "role"))
                    if any(re.search(pattern, text, re.I) for pattern in CLICK_PATTERNS) and not button.get("disabled"):
                        key = ("button", idx, text[:300])
                        if key not in seen_keys:
                            candidates.append(key); seen_keys.add(key)
                for idx, link in enumerate(links):
                    text = " ".join(str(link.get(k, "")) for k in ("text", "aria", "title", "href", "download"))
                    if any(re.search(pattern, text, re.I) for pattern in CLICK_PATTERNS):
                        key = ("link", idx, text[:300])
                        if key not in seen_keys:
                            candidates.append(key); seen_keys.add(key)
                # Broad DOM candidates get a JS index. We will click via evaluate fallback because custom/shadow elements may not be locatable normally.
                for dom_idx, item in enumerate(broad_candidates):
                    candidates.append(("dom", dom_idx, item.get("text", "")[:300]))

                # De-duplicate by label, cap attempts.
                unique=[]; labels=set()
                for c in candidates:
                    label=c[2].strip().lower()
                    if label and label not in labels:
                        unique.append(c); labels.add(label)
                unique=unique[:max_clicks]
                page_result["click_candidates"] = [{"kind":k,"index":i,"text":t} for k,i,t in unique]

                for kind, idx, label in unique:
                    before_url = page.url
                    before_pages = len(context.pages)
                    click_item = {"kind": kind, "index": idx, "label": label, "before_url": before_url}
                    try:
                        clicked = False
                        if kind == "button":
                            locator = page.locator("button, [role=button], [role=link], input[type=button], input[type=submit]").nth(idx)
                            locator.scroll_into_view_if_needed(timeout=3000)
                            try:
                                locator.click(timeout=12000, no_wait_after=True)
                                clicked = True
                            except Exception as first_exc:
                                click_item["locator_click_error"] = f"{type(first_exc).__name__}: {first_exc}"
                        elif kind == "link":
                            locator = page.locator("a").nth(idx)
                            locator.scroll_into_view_if_needed(timeout=3000)
                            try:
                                locator.click(timeout=12000, no_wait_after=True)
                                clicked = True
                            except Exception as first_exc:
                                click_item["locator_click_error"] = f"{type(first_exc).__name__}: {first_exc}"
                        else:
                            # Re-identify by text/attributes in the page and click via JS; this covers custom elements.
                            target_text = label
                            js = """
                            (targetText) => {
                              const norm=s=>(s||'').trim().replace(/\\s+/g,' ').toLowerCase();
                              const all=[]; const seen=new Set();
                              const walk=root=>{ if(!root) return; for(const el of (root.querySelectorAll?root.querySelectorAll('*'):[])){ if(seen.has(el))continue; seen.add(el); const cs=getComputedStyle(el); const txt=norm(el.innerText||el.textContent); const attrs=Array.from(el.attributes||[]).map(a=>a.name+'='+a.value).join(' '); if((txt===targetText||txt.includes(targetText)) && (cs.cursor==='pointer'||el.matches('a,button,[role=button],[role=link],[tabindex]')||typeof el.onclick==='function')) all.push(el); if(el.shadowRoot) walk(el.shadowRoot); }};
                              walk(document); const el=all[0]; if(!el) return {found:false}; el.scrollIntoView({block:'center'}); el.click(); return {found:true,tag:el.tagName,text:(el.innerText||el.textContent||'').trim().slice(0,500)};
                            }
                            """
                            click_result = page.evaluate(js, target_text)
                            click_item["js_click"] = click_result
                            clicked = bool(click_result.get("found"))
                        click_item["clicked"] = clicked
                        if not clicked:
                            click_item["skipped"] = "no clickable target"
                        else:
                            page.wait_for_timeout(max(2000, wait_seconds * 1000 // 2))
                        click_item["after_url"] = page.url
                        click_item["new_pages"] = len(context.pages) - before_pages
                        click_item["url_changed"] = page.url != before_url
                        click_item["after_title"] = page.title()
                        click_item["after_dom_score"] = mes_score(page.content())
                        # Capture new tabs/pages produced by click.
                        if len(context.pages) > before_pages:
                            for popup in context.pages[before_pages:]:
                                try:
                                    popup.wait_for_load_state("domcontentloaded", timeout=20000)
                                    popup.wait_for_timeout(max(2000, wait_seconds * 1000 // 2))
                                    popup_html = popup.content()
                                    popup_path = out / f"popup_{digest(popup.url)}.html"
                                    popup_path.write_text(popup_html, encoding="utf-8", errors="replace")
                                    click_item.setdefault("popups", []).append({"url": popup.url, "title": popup.title(), "html_path": str(popup_path), "dom_score": mes_score(popup_html)})
                                except Exception as exc:
                                    click_item.setdefault("popup_errors", []).append(f"{type(exc).__name__}: {exc}")
                    except PlaywrightTimeoutError as exc:
                        click_item["timeout"] = str(exc)
                    except Exception as exc:
                        click_item["click_error"] = f"{type(exc).__name__}: {exc}"
                    result["clicks"].append(click_item)

                page_result["post_click_url"] = page.url
                page_result["post_click_title"] = page.title()
                post_html = page.content()
                page_result["post_click_dom_score"] = mes_score(post_html)
                post_html_path = out / f"postclick_{digest(page.url)}.html"
                post_html_path.write_text(post_html, encoding="utf-8", errors="replace")
                page_result["post_click_html_path"] = str(post_html_path)
            except Exception as exc:
                page_result["error"] = f"{type(exc).__name__}: {exc}"
            result["pages"].append(page_result)
            try:
                page.close()
            except Exception:
                pass

        try:
            context.tracing.stop(path=str(out / "playwright_trace.zip"))
            result["trace_path"] = str(out / "playwright_trace.zip")
        except Exception as exc:
            result["tracing_error"] = str(exc)
        context.close()
        browser.close()

    return result


def summarize(report: dict) -> dict:
    browser = report.get("browser", {})
    requests_log = browser.get("requests", [])
    responses_log = browser.get("responses", [])
    relevant_requests = [x for x in requests_log if x.get("relevant")]
    relevant_responses = [x for x in responses_log if x.get("relevant")]
    data_responses = [x for x in responses_log if x.get("resource_type") in ("xhr", "fetch")]
    body_candidates = set()
    for response in responses_log:
        body_candidates.update(response.get("body_candidate_urls", []))
    return {
        "candidate_count": len(report.get("candidate_urls", [])),
        "total_requests": len(requests_log),
        "relevant_requests": len(relevant_requests),
        "total_responses": len(responses_log),
        "relevant_responses": len(relevant_responses),
        "xhr_fetch_responses": len(data_responses),
        "body_candidate_urls": len(body_candidates),
        "downloads": len(browser.get("downloads", [])),
        "click_candidates": sum(len(p.get("click_candidates", [])) for p in browser.get("pages", [])),
        "clicks_attempted": len(browser.get("clicks", [])),
        "successful_clicks": sum(1 for x in browser.get("clicks", []) if x.get("clicked")),
        "scripts_seen": len(browser.get("scripts", [])),
        "websockets": len(browser.get("websockets", [])),
        "pages": len(browser.get("pages", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait", type=int, default=20)
    parser.add_argument("--max-clicks", type=int, default=40)
    parser.add_argument("--output", default="iea_forensic_discovery")
    args = parser.parse_args()
    if args.wait < 1 or args.wait > 300:
        parser.error("--wait must be between 1 and 300 seconds")
    if args.max_clicks < 1 or args.max_clicks > 200:
        parser.error("--max-clicks must be between 1 and 200")

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "*/*",
    })

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report = {
        "version": "2.1",
        "discovery_surfaces": [PRODUCT_URL, TOOLS_URL, DATASETS_URL, EXPLORERS_URL],
        "started_utc": started,
        "configuration": {"wait_seconds": args.wait, "max_clicks": args.max_clicks, "headed": args.headed},
        "safety": {"credentials_entered": False, "cookies_saved": False, "github_access": False, "production_files_modified": False},
        "product_page": get_page(session, PRODUCT_URL, out),
        "data_tools_page": get_page(session, TOOLS_URL, out),
        "data_sets_page": get_page(session, DATASETS_URL, out),
        "data_explorer_index": get_page(session, EXPLORERS_URL, out),
        "service_probes": service_probes(session, out),
    }
    report["browser"] = browser_discovery([PRODUCT_URL, TOOLS_URL, DATASETS_URL, EXPLORERS_URL], out, args.headed, args.wait, args.max_clicks)

    candidates = set(report["product_page"].get("candidate_urls", []))
    candidates.update(report["data_tools_page"].get("candidate_urls", []))
    candidates.update(report["data_sets_page"].get("candidate_urls", []))
    candidates.update(report["data_explorer_index"].get("candidate_urls", []))
    for page in report["browser"].get("pages", []):
        candidates.update(page.get("dom_candidate_urls", []))
        candidates.update(page.get("post_click_candidate_urls", []))
        for link in page.get("links", []):
            href = link.get("href", "")
            if href and (relevant(href) or relevant(link.get("text", ""))):
                candidates.add(href)
    for response in report["browser"].get("responses", []):
        candidates.update(response.get("body_candidate_urls", []))
    for navigation in report["browser"].get("navigations", []):
        if relevant(navigation.get("url", "")):
            candidates.add(navigation["url"])

    report["candidate_urls"] = sorted(x for x in candidates if x.startswith(("http://", "https://")))
    report["summary"] = summarize(report)
    report_path = out / "iea_forensic_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print("Report:", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
