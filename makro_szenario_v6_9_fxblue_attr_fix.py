#!/usr/bin/env python3
"""
FX Blue – isolierter Endpoint/JavaScript-Diagnosetest, Stufe 2

ZIEL
----
Wir haben aus dem letzten GitHub-Lauf jetzt die echten FX-Blue-Event-Seiten:

  https://publisher2.fxblue.com/calendar/item/ISM_Services_PMI_US
  https://publisher2.fxblue.com/calendar/item/ISM_Services_New_Orders_Index_US
  https://publisher2.fxblue.com/calendar/item/ISM_Services_Employment_Index_US
  https://publisher2.fxblue.com/calendar/item/ISM_Services_Prices_Paid_US

Diese Testdatei untersucht ausschließlich:
1. die vier Event-Seiten,
2. die tatsächlich eingebundenen JavaScript-Dateien,
3. insbesondere /calendar/item.js,
4. eingebettete Variablen wie eventType/authKey/calendarWSEndpoint,
5. AJAX/fetch/XHR/WebSocket/API-Muster,
6. vorhandene Daten-/Event-Strukturen im HTML und JavaScript,
7. ob sich daraus ein reproduzierbarer Datenabruf ableiten lässt.

KEINE Produktivlogik:
- kein Gate
- kein Cache
- kein LME
- kein FRED
- kein Manufacturing
- kein Actual-Parser-Fix
- keine Änderung an makro_szenario.py

Wichtig:
Der Test versucht NICHT, Werte zu erfinden oder Forecast/Previous als Actual
zu verwenden. Er diagnostiziert nur die reale FX-Blue-Technik.

Zielmonat:
    2026-07
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


TARGET_YEAR = 2026
TARGET_MONTH = 7
TARGET_REFERENCE = f"{TARGET_YEAR}-{TARGET_MONTH:02d}"

BASE = "https://publisher2.fxblue.com"

EVENTS = {
    "pmi": "ISM_Services_PMI_US",
    "new_orders": "ISM_Services_New_Orders_Index_US",
    "employment": "ISM_Services_Employment_Index_US",
    "prices": "ISM_Services_Prices_Paid_US",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

TIMEOUT = 30


def section(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def unique(items):
    out = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def get_text(soup: BeautifulSoup) -> str:
    return soup.get_text(" ", strip=True)


def extract_script_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    urls = []
    for script in soup.find_all("script"):
        src = script.get("src")
        if src:
            urls.append(urljoin(page_url, src))
    return unique(urls)


def extract_inline_variables(html: str) -> dict[str, str]:
    result = {}

    patterns = {
        "eventType": r"""document\.eventType\s*=\s*["']([^"']+)["']""",
        "authKey": r"""document\.authKey\s*=\s*["']([^"']+)["']""",
        "calendarWSEndpoint": (
            r"""document\.calendarWSEndpoint\s*=\s*["']([^"']+)["']"""
        ),
        "countryName": r"""document\.countryName\s*=\s*["']([^"']+)["']""",
        "minimalPage": r"""document\.minimalPage\s*=\s*([^;]+)""",
        "_loggedInUser": r"""document\._loggedInUser\s*=\s*["']([^"']*)["']""",
    }

    for name, pattern in patterns.items():
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            result[name] = match.group(1).strip()

    return result


def relevant_lines(text: str, keywords, limit: int = 80) -> list[str]:
    result = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        low = clean.lower()
        if any(k.lower() in low for k in keywords):
            result.append(clean[:3000])
            if len(result) >= limit:
                break
    return result


def extract_api_candidates(text: str) -> list[str]:
    found = set()

    # Absolute URLs
    for value in re.findall(r"""https?://[^"'<>\\\s]+""", text, re.I):
        found.add(value)

    # Quoted relative endpoints/paths containing likely API terms.
    for value in re.findall(
        r"""["']([^"']{1,500})["']""",
        text,
        re.I,
    ):
        low = value.lower()
        if any(
            term in low
            for term in (
                "/api/",
                "api/",
                "ajax",
                "calendar",
                "event",
                "item",
                "history",
                "data",
                "json",
                "getitems",
                "getevents",
                "economic",
                "websocket",
                "ws://",
                "wss://",
            )
        ):
            found.add(value)

    return sorted(found)


def inspect_actual_like_markup(soup: BeautifulSoup) -> None:
    section("HTML: ACTUAL / FORECAST / PREVIOUS / VALUE")

    hits = 0

    for element in soup.find_all(True):
        attrs = element.attrs
        interesting = {}

        for key, value in attrs.items():
            low = key.lower()
            if any(
                term in low
                for term in (
                    "actual",
                    "forecast",
                    "previous",
                    "value",
                    "date",
                    "event",
                )
            ):
                interesting[key] = value

        classes = " ".join(element.get("class", []))
        class_low = classes.lower()

        if (
            interesting
            or any(
                term in class_low
                for term in (
                    "actual",
                    "forecas",
                    "previous",
                    "pastevent",
                    "value",
                )
            )
        ):
            hits += 1
            print(
                f"HIT {hits}: TAG={element.name} "
                f"CLASS={classes!r} "
                f"ATTRS={interesting!r} "
                f"TEXT={element.get_text(' ', strip=True)[:600]!r}"
            )

            if hits >= 150:
                break

    print(f"ACTUAL_LIKE_MARKUP_HITS={hits}")


def inspect_target_month_markup(soup: BeautifulSoup) -> None:
    section(f"HTML: ZIELMONAT {TARGET_REFERENCE}")

    month_names = {
        7: ("jul", "july"),
    }

    short_name, long_name = month_names[TARGET_MONTH]
    hits = 0

    for element in soup.find_all(["tr", "div", "li", "td", "span"]):
        text = element.get_text(" ", strip=True)
        low = text.lower()

        if str(TARGET_YEAR) in low and (
            f"{TARGET_YEAR}-{TARGET_MONTH:02d}" in low
            or short_name in low
            or long_name in low
        ):
            # Nur kompakte Kandidaten ausgeben.
            if len(text) <= 1500:
                hits += 1
                print(
                    f"TARGET_{hits}: {re.sub(r'\\s+', ' ', text)[:1500]}"
                )
                if hits >= 100:
                    break

    print(f"TARGET_MONTH_MARKUP_HITS={hits}")


def inspect_tables(soup: BeautifulSoup) -> None:
    section("HTML: TABELLEN / 3-SPALTEN-FALLBACK")

    tables = soup.find_all("table")
    print(f"TABLE_COUNT={len(tables)}")

    for idx, table in enumerate(tables[:30], 1):
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            values = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                for c in cells
            ]
            if values:
                rows.append(values)

        print(f"TABLE_{idx}_ROWS={len(rows)}")

        for row in rows[:20]:
            print("  ROW:", row)


def inspect_javascript(
    session: requests.Session,
    script_urls: list[str],
) -> dict[str, str]:
    section("JAVASCRIPT-DATEIEN")

    downloaded = {}

    for script_url in script_urls:
        print(f"SCRIPT_URL={script_url}")

        try:
            response = session.get(
                script_url,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            print(
                f"SCRIPT_REQUEST_ERROR={type(exc).__name__}: {exc}"
            )
            continue

        print(f"SCRIPT_HTTP_STATUS={response.status_code}")
        print(f"SCRIPT_FINAL_URL={response.url}")
        print(f"SCRIPT_CONTENT_TYPE={response.headers.get('Content-Type', '')}")
        print(f"SCRIPT_LENGTH={len(response.text)}")

        if response.status_code != 200:
            continue

        downloaded[response.url] = response.text

        # Besonders wichtig: item.js und alle Scripts, die
        # Daten-/WebSocket-/AJAX-Code enthalten.
        matches = relevant_lines(
            response.text,
            [
                "websocket",
                "WebSocket",
                "calendarWSEndpoint",
                "authKey",
                "eventType",
                "fetch(",
                "$.ajax",
                "$.get",
                "$.post",
                "XMLHttpRequest",
                "getitems",
                "getevents",
                "calendar",
                "actual",
                "previous",
                "forecast",
                "PastEvent",
                "MetricsBox",
                "pako",
                "inflate",
                "decompress",
            ],
            limit=120,
        )

        if matches:
            print("RELEVANT_JS_LINES_BEGIN")
            for line in matches:
                print("JS:", line)
            print("RELEVANT_JS_LINES_END")
        else:
            print("NO_RELEVANT_JS_LINES_FOUND")

        api_candidates = extract_api_candidates(response.text)

        if api_candidates:
            print("JS_ENDPOINT_CANDIDATES_BEGIN")
            for candidate in api_candidates[:200]:
                print("ENDPOINT_CANDIDATE:", candidate[:1500])
            print("JS_ENDPOINT_CANDIDATES_END")

    return downloaded


def inspect_inline_scripts(soup: BeautifulSoup) -> None:
    section("INLINE-JAVASCRIPT / DATEN-HINWEISE")

    count = 0

    for script in soup.find_all("script"):
        if script.get("src"):
            continue

        text = script.string or script.get_text()
        if not text.strip():
            continue

        low = text.lower()

        if not any(
            term in low
            for term in (
                "eventtype",
                "authkey",
                "calendarwsendpoint",
                "actual",
                "forecast",
                "previous",
                "websocket",
                "calendar",
                "data",
            )
        ):
            continue

        count += 1
        print(f"INLINE_SCRIPT_{count}_LENGTH={len(text)}")

        for line in relevant_lines(
            text,
            [
                "eventType",
                "authKey",
                "calendarWSEndpoint",
                "actual",
                "forecast",
                "previous",
                "WebSocket",
                "websocket",
                "calendar",
                "data",
            ],
            limit=100,
        ):
            print("INLINE:", line)

    print(f"RELEVANT_INLINE_SCRIPTS={count}")


def try_optional_websocket_module():
    """
    Nur feststellen, ob websocket-client verfügbar ist.
    Noch KEIN WebSocket-Connect: Der nächste Schritt soll erst nach
    Analyse des tatsächlich verwendeten Protokolls erfolgen.
    """
    section("OPTIONALE WEBSOCKET-UNTERSTÜTZUNG")

    try:
        import websocket  # type: ignore

        print("WEBSOCKET_CLIENT_AVAILABLE=YES")
        print(f"WEBSOCKET_CLIENT_VERSION={getattr(websocket, '__version__', 'unknown')}")
        return True
    except ImportError:
        print("WEBSOCKET_CLIENT_AVAILABLE=NO")
        print(
            "HINWEIS=Kein Problem: Für diesen Diagnoselauf wird "
            "kein WebSocket-Client benötigt."
        )
        return False


def main() -> int:
    print("=" * 88)
    print("FX BLUE ISM SERVICES – ENDPOINT/JAVASCRIPT DIAGNOSE STUFE 2")
    print("=" * 88)
    print(f"TARGET_YEAR={TARGET_YEAR}")
    print(f"TARGET_MONTH={TARGET_MONTH}")
    print(f"TARGET_REFERENCE={TARGET_REFERENCE}")
    print("PRODUCTIVLOGIK=UNVERAENDERT")

    session = requests.Session()
    session.headers.update(HEADERS)

    successful_pages = 0
    all_script_urls = set()

    for kind, event_id in EVENTS.items():
        page_url = f"{BASE}/calendar/item/{event_id}"

        section(f"EVENT {kind.upper()} – {event_id}")
        print(f"PAGE_URL={page_url}")

        try:
            response = session.get(
                page_url,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            print(f"HTTP_STATUS={response.status_code}")
            print(f"FINAL_URL={response.url}")
            print(f"CONTENT_TYPE={response.headers.get('Content-Type', '')}")
            print(f"CONTENT_LENGTH={len(response.text)}")

            if response.status_code != 200:
                print("PAGE_RESULT=HTTP_ERROR")
                continue

            successful_pages += 1

            soup = BeautifulSoup(response.text, "html.parser")

            title = (
                soup.title.get_text(" ", strip=True)
                if soup.title
                else ""
            )
            print(f"PAGE_TITLE={title}")

            variables = extract_inline_variables(response.text)
            print(f"INLINE_VARIABLES={variables}")

            if variables.get("calendarWSEndpoint"):
                print(
                    "CALENDAR_WS_ENDPOINT="
                    f"{variables['calendarWSEndpoint']}"
                )

            if variables.get("eventType"):
                print(
                    f"EVENT_TYPE_FROM_PAGE={variables['eventType']}"
                )

            if variables.get("authKey"):
                # Der Schlüssel wird nur als vorhanden gemeldet;
                # kein vollständiger Auth-Key im Log.
                print("AUTH_KEY_PRESENT=YES")
            else:
                print("AUTH_KEY_PRESENT=NO")

            script_urls = extract_script_urls(
                soup,
                response.url,
            )

            print(f"SCRIPT_COUNT={len(script_urls)}")

            for script_url in script_urls:
                all_script_urls.add(script_url)

            inspect_actual_like_markup(soup)
            inspect_target_month_markup(soup)
            inspect_tables(soup)
            inspect_inline_scripts(soup)

        except requests.RequestException as exc:
            print(
                f"PAGE_REQUEST_ERROR={type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            print(
                f"PAGE_DIAGNOSE_ERROR={type(exc).__name__}: {exc}"
            )

    section("GEMEINSAME JAVASCRIPT-DATEIEN")
    for url in sorted(all_script_urls):
        print("SCRIPT:", url)

    # item.js zuerst untersuchen, danach die übrigen gemeinsamen Scripts.
    prioritized = sorted(
        all_script_urls,
        key=lambda u: (
            0 if "/calendar/item.js" in u.lower() else 1,
            u,
        ),
    )

    downloaded = inspect_javascript(session, prioritized)

    try_optional_websocket_module()

    section("ABSCHLUSSBEFUND")

    print(f"SUCCESSFUL_EVENT_PAGES={successful_pages}")
    print(f"DISCOVERED_SCRIPT_URLS={len(all_script_urls)}")
    print(f"DOWNLOADED_SCRIPTS={len(downloaded)}")

    item_js = [
        url for url in downloaded
        if "/calendar/item.js" in url.lower()
    ]

    print(f"ITEM_JS_DOWNLOADED={len(item_js)}")

    if item_js:
        print(
            "RESULT=ITEM_JS_REACHED"
        )
        print(
            "NEXT_STEP=Aus dem item.js-Log den echten Datenkanal "
            "(AJAX/JSON/WebSocket/Kompression) bestimmen."
        )
    elif successful_pages:
        print(
            "RESULT=PAGES_REACHED_BUT_ITEM_JS_NOT_REACHED"
        )
        print(
            "NEXT_STEP=Script-URL/Request-Blockierung untersuchen."
        )
    else:
        print(
            "RESULT=NO_EVENT_PAGE_REACHED"
        )

    print(
        "IMPORTANT=Kein Produktiv-Fix, keine Gate-Aenderung, "
        "kein Cache-/LME-/FRED-/Manufacturing-Eingriff."
    )

    # Ein technischer Diagnosejob darf bei fehlenden Daten nicht
    # künstlich einen Parser-Erfolg behaupten. Gleichzeitig sollen
    # HTTP-Blockaden als Befund im Log sichtbar bleiben.
    return 0


if __name__ == "__main__":
    sys.exit(main())
