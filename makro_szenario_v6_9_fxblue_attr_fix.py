#!/usr/bin/env python3
"""
FX Blue – isolierter Event-Discovery-Test v1

ZWECK
-----
Dieser Test beantwortet ausschließlich eine Frage:

    Liefert FX Blue im aktuell erreichbaren HTML überhaupt Events,
    die als ISM Services / ISM Non-Manufacturing erkennbar sind?

WICHTIG
-------
- Keine Wertinterpretation.
- Kein Actual-/Forecast-/Previous-Parsing.
- Keine Änderung an makro_szenario.py.
- Keine Änderung an Cache, Gate, LME, FRED oder Manufacturing.
- Kein "Raten" anhand numerischer Tokens.
- Ein gefundenes Manufacturing-Event wird NICHT als Services akzeptiert.

Exit:
  0 = SERVICES_EVENT_FOUND
  1 = NO_SERVICES_EVENT_FOUND / Quelle nicht verwertbar
"""

import re
import sys
from collections import Counter
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


URLS = [
    "https://www.fxblue.com/market-data/economic-calendar",
    "https://publisher2.fxblue.com/",
]

TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


SERVICE_TERMS = (
    "ism services",
    "ism non-manufacturing",
    "ism non manufacturing",
    "non-manufacturing pmi",
    "non manufacturing pmi",
    "services pmi",
    "services business activity",
)

COMPONENT_TERMS = (
    "services new orders",
    "services employment",
    "services prices",
    "services prices paid",
    "non-manufacturing new orders",
    "non-manufacturing employment",
    "non-manufacturing prices",
)

MANUFACTURING_TERMS = (
    "ism manufacturing",
    "manufacturing pmi",
    "manufacturing prices paid",
    "manufacturing new orders",
    "manufacturing employment",
)


def normalize(text):
    text = text or ""
    return re.sub(r"\s+", " ", text).strip()


def classify(text):
    low = normalize(text).lower()

    if any(term in low for term in SERVICE_TERMS):
        return "SERVICES"

    if any(term in low for term in COMPONENT_TERMS):
        return "SERVICES_COMPONENT"

    if any(term in low for term in MANUFACTURING_TERMS):
        return "MANUFACTURING"

    if "ism" in low:
        return "ISM_OTHER"

    return None


def collect_event_like_strings(soup):
    """
    Sammelt nur Text aus typischen Event-/Tabellen-Containern.
    Keine Zahlenextraktion.
    """
    candidates = []

    for element in soup.find_all(
        ["tr", "td", "th", "li", "div", "span", "a"]
    ):
        text = normalize(element.get_text(" ", strip=True))
        if not text:
            continue

        low = text.lower()

        if (
            "ism" in low
            or "non-manufacturing" in low
            or "non manufacturing" in low
            or "services" in low
            or "manufacturing" in low
        ):
            candidates.append(text)

    # Deduplicate, aber Reihenfolge erhalten.
    seen = set()
    result = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            result.append(item)

    return result


def collect_script_hints(soup, base_url):
    """
    Sucht ausschließlich nach textuellen Hinweisen in Script-/Inline-Inhalten.
    Keine Ausführung von JavaScript.
    """
    hits = []

    for script in soup.find_all("script"):
        content = script.string or script.get_text() or ""
        low = content.lower()

        if any(
            term in low
            for term in (
                "calendar",
                "ism services",
                "non-manufacturing",
                "services pmi",
                "eventtype",
                "calendarws",
                "getitems",
            )
        ):
            hits.append(normalize(content)[:3000])

    # Zusätzlich externe JS-Dateien nur auflisten, nicht laden.
    scripts = []
    for tag in soup.find_all("script", src=True):
        scripts.append(urljoin(base_url, tag.get("src")))

    return hits, scripts


def run():
    print("=== FXBLUE ISM SERVICES EVENT DISCOVERY ===")
    print("MODE=READ_ONLY_EVENT_NAME_DISCOVERY")
    print("VALUE_PARSING=False")
    print("ACTUAL_PARSING=False")
    print()

    session = requests.Session()
    session.headers.update(HEADERS)

    all_event_strings = []
    script_hints = []
    external_scripts = []
    successful_pages = 0

    for url in URLS:
        print(f"REQUEST={url}")

        try:
            response = session.get(
                url,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            print(f"HTTP_STATUS={response.status_code}")
            print(f"FINAL_URL={response.url}")
            print(f"CONTENT_LENGTH={len(response.text)}")

            if response.history:
                chain = [
                    str(item.status_code)
                    for item in response.history
                ]
                chain.append(str(response.status_code))
                print("REDIRECT_CHAIN=" + " -> ".join(chain))

            if response.status_code != 200:
                print("PAGE_RESULT=NOT_USABLE")
                print()
                continue

            successful_pages += 1

            soup = BeautifulSoup(response.text, "html.parser")

            events = collect_event_like_strings(soup)
            all_event_strings.extend(events)

            hints, scripts = collect_script_hints(
                soup,
                response.url,
            )
            script_hints.extend(hints)
            external_scripts.extend(scripts)

            print(f"EVENT_LIKE_TEXT_COUNT={len(events)}")
            print()

        except requests.RequestException as exc:
            print(f"REQUEST_ERROR={type(exc).__name__}: {exc}")
            print()

    # Globale Deduplication.
    seen = set()
    unique_events = []

    for item in all_event_strings:
        if item not in seen:
            seen.add(item)
            unique_events.append(item)

    print("=== CLASSIFIED EVENT NAMES ===")

    counts = Counter()
    service_hits = []
    component_hits = []
    manufacturing_hits = []
    ism_other_hits = []

    for item in unique_events:
        category = classify(item)

        if category:
            counts[category] += 1

        if category == "SERVICES":
            service_hits.append(item)
        elif category == "SERVICES_COMPONENT":
            component_hits.append(item)
        elif category == "MANUFACTURING":
            manufacturing_hits.append(item)
        elif category == "ISM_OTHER":
            ism_other_hits.append(item)

    print(f"TOTAL_UNIQUE_EVENT_LIKE_TEXT={len(unique_events)}")
    print(f"SERVICES_MATCHES={len(service_hits)}")
    print(f"SERVICES_COMPONENT_MATCHES={len(component_hits)}")
    print(f"MANUFACTURING_MATCHES={len(manufacturing_hits)}")
    print(f"ISM_OTHER_MATCHES={len(ism_other_hits)}")
    print()

    print("--- SERVICES ---")
    for item in service_hits:
        print("SERVICES_EVENT:", item[:1000])

    print()
    print("--- SERVICES COMPONENTS ---")
    for item in component_hits:
        print("SERVICES_COMPONENT_EVENT:", item[:1000])

    print()
    print("--- MANUFACTURING (CONTROL GROUP) ---")
    for item in manufacturing_hits:
        print("MANUFACTURING_EVENT:", item[:1000])

    print()
    print("--- OTHER ISM ---")
    for item in ism_other_hits:
        print("ISM_OTHER_EVENT:", item[:1000])

    print()
    print("=== STATIC SCRIPT HINTS ===")
    print(f"INLINE_SCRIPT_HINTS={len(script_hints)}")

    for index, hint in enumerate(script_hints[:20], 1):
        print(f"SCRIPT_HINT_{index}={hint}")

    print()
    unique_scripts = []
    seen_scripts = set()

    for script in external_scripts:
        if script not in seen_scripts:
            seen_scripts.add(script)
            unique_scripts.append(script)

    print(f"EXTERNAL_SCRIPT_COUNT={len(unique_scripts)}")

    for script in unique_scripts[:50]:
        print("SCRIPT_SRC:", script)

    print()
    print("=== DECISION ===")

    if successful_pages == 0:
        print("RESULT=RED_NO_USABLE_FXBLUE_PAGE")
        return 1

    if service_hits or component_hits:
        print("RESULT=GREEN_SERVICES_EVENT_FOUND")
        print("NEXT_STEP=INSPECT_EXACT_EVENT_STRUCTURE_BEFORE_ANY_VALUE_PARSING")
        return 0

    print("RESULT=RED_NO_SERVICES_EVENT_FOUND")
    print("NEXT_STEP=DO_NOT_MODIFY_PRODUCTIVE_PARSER")
    return 1


if __name__ == "__main__":
    sys.exit(run())
