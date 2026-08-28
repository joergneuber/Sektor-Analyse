#!/usr/bin/env python3
"""
FX Blue – isolierter Network-/Browser-Diagnosetest für ISM Services.

ZWECK:
- Keine Änderung am Produktivcode.
- Ermittelt mit einem echten Chromium-Browser, ob FX Blue die
  ISM-Services-Daten per DOM, Fetch/XHR oder WebSocket liefert.
- Protokolliert Requests/Responses und WebSocket-Nachrichten.
- Sucht anschließend gezielt nach:
    PMI
    New Orders
    Employment
    Prices / Prices Paid
- Fail-closed: Es werden keine Werte erfunden oder aus Forecast/Previous
  abgeleitet.

GitHub:
    python fxblue_services_network_diagnose.py

Voraussetzung:
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

URLS = [
    "https://www.fxblue.com/market-data/economic-calendar",
    "https://publisher2.fxblue.com/",
]

TARGET_YEAR = 2026
TARGET_MONTH = 7

KEYWORDS = (
    "ism",
    "non-manufacturing",
    "services",
    "new orders",
    "employment",
    "prices",
    "prices paid",
    "pmi",
)

MAX_BODY_CHARS = 12000
MAX_WS_MESSAGES = 200
WAIT_MS = 10000


def short(value: Any, limit: int = MAX_BODY_CHARS) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
    except Exception:
        text = repr(value)
    text = text.replace("\x00", " ")
    return text[:limit]


def relevant(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in KEYWORDS)


def month_matches(text: str) -> bool:
    low = text.lower()
    return (
        f"{TARGET_YEAR}-{TARGET_MONTH:02d}" in low
        or f"{TARGET_YEAR}/{TARGET_MONTH:02d}" in low
        or f"{TARGET_MONTH:02d}/{TARGET_YEAR}" in low
        or f"jul {TARGET_YEAR}" in low
        or f"july {TARGET_YEAR}" in low
        or f"jul-{TARGET_YEAR}" in low
        or f"july-{TARGET_YEAR}" in low
    )


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("RESULT=RED")
        print("FEHLER: Playwright ist nicht installiert.")
        print("INSTALL: pip install playwright")
        print("INSTALL_BROWSER: python -m playwright install chromium")
        return 2

    network_events: list[dict[str, Any]] = []
    ws_messages: list[dict[str, Any]] = []
    pages_seen: list[str] = []

    print("=== FXBLUE REAL BROWSER / NETWORK DIAGNOSE ===")
    print(f"TARGET={TARGET_YEAR}-{TARGET_MONTH:02d}")
    print(f"WAIT_MS={WAIT_MS}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        def on_request(req):
            url = req.url
            typ = req.resource_type
            if (
                typ in {"xhr", "fetch", "websocket"}
                or relevant(url)
                or "calendar" in url.lower()
            ):
                item = {
                    "type": "REQUEST",
                    "resource_type": typ,
                    "method": req.method,
                    "url": url,
                    "post_data": short(req.post_data, 4000)
                    if req.post_data else "",
                }
                network_events.append(item)
                print(
                    f"REQUEST [{typ}] {req.method} {url}"
                )

        def on_response(resp):
            req = resp.request
            url = resp.url
            typ = req.resource_type
            if (
                typ in {"xhr", "fetch"}
                or relevant(url)
                or "calendar" in url.lower()
            ):
                item = {
                    "type": "RESPONSE",
                    "resource_type": typ,
                    "status": resp.status,
                    "url": url,
                    "content_type": resp.headers.get("content-type", ""),
                }

                # Nur kleine/JSON/Text-Responses lesen; große Binärdateien
                # werden bewusst nicht angefasst.
                try:
                    ctype = resp.headers.get("content-type", "").lower()
                    if (
                        "json" in ctype
                        or "text" in ctype
                        or "javascript" in ctype
                        or "xml" in ctype
                    ):
                        body = resp.text()
                        item["body"] = short(body)
                        if relevant(body):
                            print(
                                f"RESPONSE_MATCH status={resp.status} "
                                f"url={url}"
                            )
                            print(
                                "BODY_MATCH:",
                                short(body, 4000)
                            )
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"

                network_events.append(item)

        page.on("request", on_request)
        page.on("response", on_response)

        # Playwright exposes WebSocket objects through page.on("websocket").
        def on_websocket(ws):
            print(f"WEBSOCKET OPEN: {ws.url}")

            ws_info = {
                "type": "WEBSOCKET",
                "url": ws.url,
                "messages": [],
            }
            ws_messages.append(ws_info)

            def on_frame_received(payload):
                if len(ws_info["messages"]) >= MAX_WS_MESSAGES:
                    return

                text = payload if isinstance(payload, str) else repr(payload)
                msg = {
                    "direction": "RECEIVED",
                    "payload": short(text, 10000),
                    "relevant": relevant(text),
                    "target_month": month_matches(text),
                }
                ws_info["messages"].append(msg)

                print(
                    f"WS RECEIVED relevant={msg['relevant']} "
                    f"target={msg['target_month']} "
                    f"payload={short(text, 3000)}"
                )

            def on_frame_sent(payload):
                if len(ws_info["messages"]) >= MAX_WS_MESSAGES:
                    return

                text = payload if isinstance(payload, str) else repr(payload)
                msg = {
                    "direction": "SENT",
                    "payload": short(text, 10000),
                    "relevant": relevant(text),
                    "target_month": month_matches(text),
                }
                ws_info["messages"].append(msg)

                if relevant(text):
                    print(
                        f"WS SENT relevant=True "
                        f"payload={short(text, 3000)}"
                    )

            ws.on("framereceived", on_frame_received)
            ws.on("framesent", on_frame_sent)

        page.on("websocket", on_websocket)

        for url in URLS:
            print()
            print("=== NAVIGATION ===")
            print(f"URL={url}")

            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

                print(
                    f"HTTP_STATUS={response.status if response else 'NONE'}"
                )
                print(f"FINAL_URL={page.url}")

                pages_seen.append(page.url)

                # Netzwerk/JS Zeit geben.
                page.wait_for_timeout(WAIT_MS)

                print()
                print("=== RENDERED DOM ===")

                html = page.content()
                print(f"RENDERED_HTML_LENGTH={len(html)}")

                body_text = page.locator("body").inner_text(timeout=5000)
                print(f"BODY_TEXT_LENGTH={len(body_text)}")

                matches = []
                for line in body_text.splitlines():
                    line = " ".join(line.split())
                    if line and relevant(line):
                        matches.append(line)

                seen = set()
                for line in matches:
                    if line not in seen:
                        seen.add(line)
                        print("DOM_CANDIDATE:", line[:1500])

                print(f"DOM_CANDIDATES={len(seen)}")

                print()
                print("=== TARGET MONTH DOM CHECK ===")
                print(
                    f"MONTH_MATCH={month_matches(body_text)}"
                )

                # Tatsächlich gerenderte Actual-Elemente prüfen.
                selectors = [
                    ".MetricsBoxActual",
                    ".MetricsBoxActual .MetricsBoxValue",
                    "[class*='Actual']",
                    "[class*='actual']",
                    "[data-actual]",
                    "[data-actual-value]",
                ]

                print()
                print("=== ACTUAL SELECTOR CHECK ===")

                for selector in selectors:
                    try:
                        count = page.locator(selector).count()
                        print(f"SELECTOR={selector} COUNT={count}")

                        for i in range(min(count, 20)):
                            el = page.locator(selector).nth(i)
                            try:
                                print(
                                    "  ELEMENT:",
                                    short(
                                        {
                                            "text": el.inner_text(),
                                            "html": el.evaluate(
                                                "(e) => e.outerHTML"
                                            ),
                                        },
                                        3000,
                                    ),
                                )
                            except Exception:
                                pass
                    except Exception as exc:
                        print(
                            f"SELECTOR_ERROR={selector}: "
                            f"{type(exc).__name__}: {exc}"
                        )

                # Nur nachweisen, ob JS/DOM überhaupt die vier Begriffe
                # gemeinsam bzw. einzeln sichtbar macht.
                print()
                print("=== FOUR SERVICES COMPONENT CHECK ===")
                for name in (
                    "pmi",
                    "new orders",
                    "employment",
                    "prices",
                    "prices paid",
                ):
                    found = name in body_text.lower()
                    print(f"{name.upper()}_VISIBLE={found}")

            except Exception as exc:
                print(
                    f"NAVIGATION_ERROR={type(exc).__name__}: {exc}"
                )

        browser.close()

    print()
    print("=== NETWORK SUMMARY ===")

    for item in network_events:
        print(
            f"{item.get('type')} "
            f"{item.get('resource_type', '')} "
            f"{item.get('status', '')} "
            f"{item.get('url', '')}"
        )

    print()
    print("=== WEBSOCKET SUMMARY ===")

    total_ws = 0
    relevant_ws = 0
    target_ws = 0

    for ws in ws_messages:
        messages = ws.get("messages", [])
        total_ws += len(messages)

        for msg in messages:
            if msg.get("relevant"):
                relevant_ws += 1
            if msg.get("target_month"):
                target_ws += 1

        print(
            f"WS_URL={ws['url']} "
            f"MESSAGES={len(messages)}"
        )

    print(f"WEBSOCKET_MESSAGES={total_ws}")
    print(f"WEBSOCKET_RELEVANT_MESSAGES={relevant_ws}")
    print(f"WEBSOCKET_TARGET_MONTH_MESSAGES={target_ws}")

    # Persist diagnostics as an artifact-friendly JSON file.
    out = {
        "target": f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
        "pages_seen": pages_seen,
        "network_events": network_events,
        "websockets": ws_messages,
    }

    output = Path("fxblue_network_diagnose.json")
    output.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"DIAGNOSTIC_FILE={output}")

    # Wichtig: Ein Diagnose-Lauf ist nur dann GREEN, wenn ein echter
    # Datenweg nachgewiesen wurde. Das Skript verändert keinerlei
    # Produktivlogik.
    evidence = (
        total_ws > 0
        or any(
            e.get("resource_type") in {"xhr", "fetch"}
            and e.get("status") == 200
            for e in network_events
        )
    )

    print()
    if evidence:
        print("RESULT=GREEN_DIAGNOSTIC_DATA_PATH_FOUND")
        print(
            "NEXT=Analysiere jetzt die konkrete JSON/WebSocket-Struktur; "
            "noch keinen Produktiv-Fix einbauen."
        )
        return 0

    print("RESULT=RED_NO_DYNAMIC_DATA_PATH_PROVEN")
    print(
        "NEXT=FXBlue-Datenweg konnte mit diesem Lauf nicht "
        "nachgewiesen werden."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
