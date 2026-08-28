#!/usr/bin/env python3
"""
FX Blue – isolierter Datenweg-/Network-Diagnosetest v2

ZWECK
-----
Nur Diagnose. Keine Änderung an makro_szenario.py.

Der Test öffnet die vier bekannten FX-Blue-Item-Seiten in Chromium und
ermittelt, ob die eigentlichen ISM-Services-Daten über irgendeinen
nachweisbaren Datenweg geliefert werden:

  1. Haupt-HTML / Redirect
  2. Frames / Iframes
  3. Script-Quellen
  4. Inline-JavaScript
  5. Fetch / XHR
  6. WebSocket
  7. gerendertes DOM
  8. data-* / Actual-Attribute
  9. relevante Netzwerk-Responses

WICHTIG
-------
- Keine Werte werden erfunden.
- Forecast/Previous werden niemals als Actual verwendet.
- Kein Gate-/Cache-/LME-/FRED-/Manufacturing-Code.
- Kein Produktiv-Fix.
- Exit 0 nur, wenn ein plausibler dynamischer Datenweg tatsächlich
  nachgewiesen wurde.
- Die Diagnose schreibt fxblue_network_diagnose_v2.json.

Voraussetzungen
---------------
    pip install playwright
    python -m playwright install chromium

GitHub-Aufruf
-------------
    python fxblue_services_network_diagnose_v2.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

TARGET_YEAR = 2026
TARGET_MONTH = 7

WAIT_MS = 12000
MAX_TEXT = 12000
MAX_RESPONSE_BODY = 20000
MAX_MESSAGES_PER_WS = 300

ITEM_URLS = [
    "https://publisher2.fxblue.com/calendar/item/ISM_Services_PMI_US",
    "https://publisher2.fxblue.com/calendar/item/ISM_Services_New_Orders_US",
    "https://publisher2.fxblue.com/calendar/item/ISM_Services_Employment_US",
    "https://publisher2.fxblue.com/calendar/item/ISM_Services_Prices_US",
]

KEYWORDS = (
    "ism",
    "non-manufacturing",
    "services",
    "service",
    "pmi",
    "new orders",
    "employment",
    "prices",
    "prices paid",
    "actual",
    "previous",
    "forecast",
    "2026-07",
    "jul 2026",
    "july 2026",
)


def short(value: Any, limit: int = MAX_TEXT) -> str:
    try:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        text = str(value)
    except Exception:
        text = repr(value)

    text = text.replace("\x00", " ")
    return text[:limit]


def relevant(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in KEYWORDS)


def target_month(text: str) -> bool:
    low = text.lower()
    patterns = (
        rf"\b{TARGET_YEAR}-{TARGET_MONTH:02d}\b",
        rf"\b{TARGET_YEAR}/{TARGET_MONTH:02d}\b",
        rf"\b{TARGET_MONTH:02d}/{TARGET_YEAR}\b",
        rf"\bjul(?:y)?[\s\-_/]+{TARGET_YEAR}\b",
    )
    return any(re.search(p, low) for p in patterns)


def safe_json(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return short(value)


def main() -> int:
    network: list[dict[str, Any]] = []
    websockets: list[dict[str, Any]] = []
    page_results: list[dict[str, Any]] = []

    print("=== FXBLUE DATA-WAY DIAGNOSE V2 ===")
    print(f"TARGET={TARGET_YEAR}-{TARGET_MONTH:02d}")
    print(f"WAIT_MS={WAIT_MS}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        # ------------------------------------------------------------
        # Network listeners
        # ------------------------------------------------------------

        def on_request(req):
            if req.resource_type not in {
                "xhr",
                "fetch",
                "websocket",
                "script",
                "document",
            }:
                return

            item = {
                "kind": "request",
                "resource_type": req.resource_type,
                "method": req.method,
                "url": req.url,
                "post_data": (
                    short(req.post_data, 5000)
                    if req.post_data
                    else ""
                ),
            }

            network.append(item)

            print(
                f"REQUEST [{req.resource_type}] "
                f"{req.method} {req.url}"
            )

        def on_response(resp):
            req = resp.request

            if req.resource_type not in {
                "xhr",
                "fetch",
                "script",
                "document",
            }:
                return

            item = {
                "kind": "response",
                "resource_type": req.resource_type,
                "status": resp.status,
                "url": resp.url,
                "content_type": resp.headers.get(
                    "content-type", ""
                ),
            }

            try:
                ctype = (
                    resp.headers.get("content-type", "")
                    .lower()
                )

                if any(
                    x in ctype
                    for x in (
                        "json",
                        "javascript",
                        "text/",
                        "xml",
                        "html",
                    )
                ):
                    body = resp.text()
                    if relevant(body):
                        item["body_match"] = short(
                            body,
                            MAX_RESPONSE_BODY,
                        )

                        print()
                        print(
                            "=== RELEVANT RESPONSE ==="
                        )
                        print(
                            f"STATUS={resp.status}"
                        )
                        print(f"URL={resp.url}")
                        print(
                            f"CONTENT_TYPE={ctype}"
                        )
                        print(
                            "BODY="
                            + short(
                                body,
                                6000,
                            )
                        )
                        print(
                            "TARGET_MONTH="
                            f"{target_month(body)}"
                        )
                        print()

            except Exception as exc:
                item["body_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

            network.append(item)

        def on_websocket(ws):
            print()
            print("=== WEBSOCKET OPEN ===")
            print(ws.url)
            print()

            record = {
                "url": ws.url,
                "messages": [],
            }

            websockets.append(record)

            def received(payload):
                if (
                    len(record["messages"])
                    >= MAX_MESSAGES_PER_WS
                ):
                    return

                text = (
                    payload
                    if isinstance(payload, str)
                    else repr(payload)
                )

                entry = {
                    "direction": "received",
                    "relevant": relevant(text),
                    "target_month": target_month(text),
                    "payload": short(text, 15000),
                }

                record["messages"].append(entry)

                print(
                    "WS RECEIVED "
                    f"relevant={entry['relevant']} "
                    f"target={entry['target_month']}"
                )

                if entry["relevant"]:
                    print(
                        short(text, 6000)
                    )

            def sent(payload):
                if (
                    len(record["messages"])
                    >= MAX_MESSAGES_PER_WS
                ):
                    return

                text = (
                    payload
                    if isinstance(payload, str)
                    else repr(payload)
                )

                entry = {
                    "direction": "sent",
                    "relevant": relevant(text),
                    "target_month": target_month(text),
                    "payload": short(text, 15000),
                }

                record["messages"].append(entry)

                if entry["relevant"]:
                    print(
                        "WS SENT relevant=True"
                    )
                    print(
                        short(text, 6000)
                    )

            ws.on("framereceived", received)
            ws.on("framesent", sent)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", on_websocket)

        # ------------------------------------------------------------
        # Four individual FX-Blue item URLs
        # ------------------------------------------------------------

        for index, url in enumerate(ITEM_URLS, start=1):
            print()
            print("=" * 78)
            print(f"ITEM {index}/4")
            print(f"URL={url}")
            print("=" * 78)

            result: dict[str, Any] = {
                "url": url,
                "status": None,
                "final_url": None,
                "frames": [],
                "scripts": [],
                "inline_js_matches": [],
                "dom_candidates": [],
                "actual_elements": [],
                "attribute_elements": [],
            }

            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

                result["status"] = (
                    response.status
                    if response
                    else None
                )
                result["final_url"] = page.url

                print(
                    f"HTTP_STATUS={result['status']}"
                )
                print(
                    f"FINAL_URL={result['final_url']}"
                )

                # Wait for SPA/JS activity.
                page.wait_for_timeout(WAIT_MS)

                # ----------------------------------------------------
                # Frames
                # ----------------------------------------------------

                print()
                print("=== FRAMES ===")

                for frame in page.frames:
                    frame_info = {
                        "url": frame.url,
                        "name": frame.name,
                    }
                    result["frames"].append(frame_info)
                    print(
                        f"FRAME name={frame.name!r} "
                        f"url={frame.url}"
                    )

                    try:
                        frame_text = frame.locator(
                            "body"
                        ).inner_text(timeout=3000)

                        if relevant(frame_text):
                            print(
                                "FRAME_RELEVANT_TEXT="
                                + short(
                                    frame_text,
                                    5000,
                                )
                            )
                            frame_info[
                                "relevant_text"
                            ] = short(
                                frame_text,
                                MAX_TEXT,
                            )

                    except Exception:
                        pass

                # ----------------------------------------------------
                # Script sources
                # ----------------------------------------------------

                print()
                print("=== SCRIPT SOURCES ===")

                scripts = page.locator("script")
                script_count = scripts.count()

                print(
                    f"SCRIPT_COUNT={script_count}"
                )

                for i in range(script_count):
                    try:
                        src = scripts.nth(i).get_attribute(
                            "src"
                        )

                        if src:
                            result["scripts"].append(
                                {
                                    "type": "external",
                                    "src": src,
                                }
                            )

                            print(
                                "SCRIPT_SRC:",
                                src,
                            )
                        else:
                            content = scripts.nth(i).inner_text()

                            if relevant(content):
                                match = {
                                    "type": "inline",
                                    "text": short(
                                        content,
                                        10000,
                                    ),
                                }

                                result[
                                    "inline_js_matches"
                                ].append(match)

                                print(
                                    "INLINE_JS_MATCH:"
                                )
                                print(
                                    short(
                                        content,
                                        6000,
                                    )
                                )

                    except Exception as exc:
                        print(
                            "SCRIPT_ERROR:",
                            type(exc).__name__,
                            exc,
                        )

                # ----------------------------------------------------
                # Rendered DOM
                # ----------------------------------------------------

                print()
                print("=== RENDERED DOM ===")

                try:
                    body = page.locator(
                        "body"
                    ).inner_text(timeout=5000)

                    result[
                        "body_text_length"
                    ] = len(body)

                    print(
                        "BODY_TEXT_LENGTH=",
                        len(body),
                    )

                    dom_seen: set[str] = set()

                    for line in body.splitlines():
                        line = " ".join(
                            line.split()
                        )

                        if (
                            line
                            and relevant(line)
                            and line not in dom_seen
                        ):
                            dom_seen.add(line)
                            result[
                                "dom_candidates"
                            ].append(line)

                            print(
                                "DOM_CANDIDATE:",
                                line[:2000],
                            )

                    print(
                        "DOM_CANDIDATES=",
                        len(dom_seen),
                    )

                    print(
                        "DOM_TARGET_MONTH=",
                        target_month(body),
                    )

                except Exception as exc:
                    print(
                        "DOM_ERROR:",
                        type(exc).__name__,
                        exc,
                    )

                # ----------------------------------------------------
                # Actual/data-* inspection
                # ----------------------------------------------------

                print()
                print("=== ACTUAL / DATA ATTRIBUTES ===")

                selector = (
                    "[data-actual], "
                    "[data-actual-value], "
                    "[data-value], "
                    "[class*='Actual'], "
                    "[class*='actual']"
                )

                elements = page.locator(selector)
                count = elements.count()

                print(
                    f"ATTRIBUTE_SELECTOR_COUNT={count}"
                )

                for i in range(min(count, 100)):
                    try:
                        el = elements.nth(i)

                        attrs = el.evaluate(
                            """
                            e => {
                                const out = {};
                                for (const a of e.attributes) {
                                    out[a.name] = a.value;
                                }
                                return out;
                            }
                            """
                        )

                        text = el.inner_text()

                        info = {
                            "tag": el.evaluate(
                                "e => e.tagName"
                            ),
                            "attrs": attrs,
                            "text": text[:1000],
                        }

                        result[
                            "attribute_elements"
                        ].append(info)

                        print(
                            "ATTR_ELEMENT:",
                            short(
                                info,
                                4000,
                            ),
                        )

                    except Exception:
                        pass

                # ----------------------------------------------------
                # Explicit known selectors
                # ----------------------------------------------------

                print()
                print("=== KNOWN FXBLUE SELECTORS ===")

                for css in (
                    ".MetricsBoxActual",
                    ".MetricsBoxActual .MetricsBoxValue",
                    ".PastEventRow",
                    ".PastEventDate",
                    ".PastEventActual",
                ):
                    try:
                        n = page.locator(css).count()
                        print(
                            f"SELECTOR={css} COUNT={n}"
                        )

                        for i in range(min(n, 20)):
                            try:
                                el = page.locator(
                                    css
                                ).nth(i)

                                print(
                                    "  ",
                                    short(
                                        {
                                            "text": el.inner_text(),
                                            "html": el.evaluate(
                                                "(e) => e.outerHTML"
                                            )[:5000],
                                        },
                                        6000,
                                    ),
                                )
                            except Exception:
                                pass

                    except Exception as exc:
                        print(
                            f"SELECTOR_ERROR={css}: "
                            f"{type(exc).__name__}: {exc}"
                        )

            except Exception as exc:
                result["error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

                print(
                    "NAVIGATION_ERROR:",
                    type(exc).__name__,
                    exc,
                )

            page_results.append(result)

        browser.close()

    # ------------------------------------------------------------
    # Final diagnosis
    # ------------------------------------------------------------

    total_ws_messages = sum(
        len(x["messages"])
        for x in websockets
    )

    relevant_ws_messages = sum(
        1
        for x in websockets
        for m in x["messages"]
        if m.get("relevant")
    )

    target_ws_messages = sum(
        1
        for x in websockets
        for m in x["messages"]
        if m.get("target_month")
    )

    relevant_network_responses = sum(
        1
        for x in network
        if x.get("kind") == "response"
        and "body_match" in x
    )

    rendered_candidates = sum(
        len(x["dom_candidates"])
        for x in page_results
    )

    actual_attribute_hits = sum(
        len(x["attribute_elements"])
        for x in page_results
    )

    dynamic_evidence = (
        total_ws_messages > 0
        or relevant_network_responses > 0
    )

    output = {
        "target": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
        "item_urls": ITEM_URLS,
        "page_results": page_results,
        "network": network,
        "websockets": websockets,
        "summary": {
            "websocket_count": len(websockets),
            "websocket_messages": total_ws_messages,
            "relevant_websocket_messages": (
                relevant_ws_messages
            ),
            "target_month_websocket_messages": (
                target_ws_messages
            ),
            "relevant_network_responses": (
                relevant_network_responses
            ),
            "rendered_dom_candidates": (
                rendered_candidates
            ),
            "actual_attribute_hits": (
                actual_attribute_hits
            ),
            "dynamic_evidence": dynamic_evidence,
        },
    }

    output_path = Path(
        "fxblue_network_diagnose_v2.json"
    )
    output_path.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 78)
    print("=== FINAL DIAGNOSIS ===")
    print("=" * 78)
    print(
        f"WEBSOCKET_COUNT={len(websockets)}"
    )
    print(
        f"WEBSOCKET_MESSAGES={total_ws_messages}"
    )
    print(
        "RELEVANT_WEBSOCKET_MESSAGES="
        f"{relevant_ws_messages}"
    )
    print(
        "TARGET_MONTH_WEBSOCKET_MESSAGES="
        f"{target_ws_messages}"
    )
    print(
        "RELEVANT_NETWORK_RESPONSES="
        f"{relevant_network_responses}"
    )
    print(
        "RENDERED_DOM_CANDIDATES="
        f"{rendered_candidates}"
    )
    print(
        "ACTUAL_ATTRIBUTE_HITS="
        f"{actual_attribute_hits}"
    )
    print(
        f"DIAGNOSTIC_FILE={output_path}"
    )

    if dynamic_evidence:
        print(
            "RESULT=GREEN_DYNAMIC_DATA_PATH_FOUND"
        )
        print(
            "WICHTIG: Das beweist einen Datenweg, "
            "noch keinen korrekten Actual-Parser."
        )
        return 0

    print(
        "RESULT=RED_NO_DYNAMIC_DATA_PATH_PROVEN"
    )
    print(
        "Kein verwertbarer dynamischer Datenweg "
        "nachgewiesen. Produktivcode NICHT ändern."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
