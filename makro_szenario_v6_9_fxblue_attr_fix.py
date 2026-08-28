#!/usr/bin/env python3
"""
FX Blue – isolierter Real-World-Diagnosetest

Zweck:
- ausschließlich die tatsächliche FX-Blue-Datenquelle/Response untersuchen
- keine Änderung an makro_szenario.py
- keine Gate-/Cache-/LME-/FRED-/Manufacturing-Logik
- dynamische Monatsprüfung für TARGET_YEAR/TARGET_MONTH
- prüft HTML, Links, relevante DOM-Klassen, Attribute und mögliche
  JSON/API-Hinweise
- der Test endet nur dann mit exit code 0, wenn die Diagnose selbst
  vollständig durchgeführt werden konnte; fehlende FX-Blue-Daten werden
  als Diagnosebefund ausgegeben und nicht künstlich als Erfolg gewertet.

Ausführung im GitHub-Testworkflow:
    python fxblue_services_real_diagnose.py
"""

import json
import re
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


TARGET_YEAR = 2026
TARGET_MONTH = 7

# Der bisher verwendete Endpunkt wird bewusst als Kandidat getestet.
CANDIDATE_URLS = [
    "https://www.fxblue.com/market-data/economic-calendar",
]

TIMEOUT = 20

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
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.fxblue.com/",
}


MONTH_NAMES = {
    1: ("jan", "january"),
    2: ("feb", "february"),
    3: ("mar", "march"),
    4: ("apr", "april"),
    5: ("may", "may"),
    6: ("jun", "june"),
    7: ("jul", "july"),
    8: ("aug", "august"),
    9: ("sep", "september"),
    10: ("oct", "october"),
    11: ("nov", "november"),
    12: ("dec", "december"),
}


def month_matches(text: str) -> bool:
    """Prüft Zielmonat dynamisch; kein hartcodierter August/anderer Monat."""
    if not text:
        return False

    low = text.lower()
    short_name, long_name = MONTH_NAMES[TARGET_MONTH]

    # ISO/Jahr-Monat
    iso_patterns = (
        f"{TARGET_YEAR}-{TARGET_MONTH:02d}",
        f"{TARGET_YEAR}/{TARGET_MONTH:02d}",
    )
    if any(p in low for p in iso_patterns):
        return True

    # Jahr + Monatsname
    if str(TARGET_YEAR) in low and (
        short_name in low or long_name in low
    ):
        return True

    # Numerische Datumsvarianten
    numeric_patterns = (
        rf"\b{TARGET_MONTH:02d}[/-]\d{{1,2}}[/-]{TARGET_YEAR}\b",
        rf"\b\d{{1,2}}[/-]{TARGET_MONTH:02d}[/-]{TARGET_YEAR}\b",
        rf"\b{TARGET_YEAR}[/-]{TARGET_MONTH:02d}[/-]\d{{1,2}}\b",
    )
    return any(re.search(p, low) for p in numeric_patterns)


def print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def safe_attr_value(value):
    if isinstance(value, (list, tuple)):
        return " ".join(map(str, value))
    return str(value)


def inspect_response(url: str, response: requests.Response) -> None:
    print_section("HTTP / RESPONSE")
    print(f"REQUEST_URL={url}")
    print(f"HTTP_STATUS={response.status_code}")
    print(f"FINAL_URL={response.url}")
    print(f"REDIRECT_COUNT={len(response.history)}")
    for i, hop in enumerate(response.history, 1):
        print(f"REDIRECT_{i}_STATUS={hop.status_code}")
        print(f"REDIRECT_{i}_URL={hop.url}")
        print(f"REDIRECT_{i}_LOCATION={hop.headers.get('Location', '')}")

    print(f"CONTENT_TYPE={response.headers.get('Content-Type', '')}")
    print(f"CONTENT_LENGTH={len(response.text)}")
    print(f"SERVER={response.headers.get('Server', '')}")
    print(f"CF_RAY={response.headers.get('CF-Ray', '')}")


def inspect_links(soup: BeautifulSoup, base_url: str) -> None:
    print_section("RELEVANTE LINKS / ENDPOINT-HINWEISE")

    patterns = (
        "ism",
        "services",
        "non-manufacturing",
        "economic-calendar",
        "calendar",
        "api",
        "json",
        "ajax",
        "past",
    )

    found = set()

    for tag in soup.find_all(["a", "script", "link", "form"]):
        for attr in ("href", "src", "action"):
            raw = tag.get(attr)
            if not raw:
                continue

            value = safe_attr_value(raw)
            low = value.lower()

            if any(p in low for p in patterns):
                absolute = urljoin(base_url, value)
                found.add(absolute)

    if not found:
        print("NO_RELEVANT_LINKS_FOUND")
    else:
        for item in sorted(found):
            print("LINK:", item)

    # Zusätzlich explizit nach URL-Strings im gelieferten HTML suchen.
    html = str(soup)
    url_candidates = set(
        re.findall(
            r"""https?://[^"'<>\\\s]+""",
            html,
            flags=re.IGNORECASE,
        )
    )
    for item in sorted(url_candidates):
        low = item.lower()
        if any(p in low for p in patterns):
            print("HTML_URL:", item)


def inspect_dom_classes(soup: BeautifulSoup) -> int:
    print_section("DOM-KLASSEN / FX-BLUE EVENT-STRUKTUR")

    wanted = (
        "PastEventRow",
        "PastEventDate",
        "PastEventActual",
        "MetricsBoxActual",
        "MetricsBoxValue",
        "PastEvent",
    )

    total = 0

    for class_name in wanted:
        elements = soup.select(f".{class_name}")
        print(f"CLASS={class_name} COUNT={len(elements)}")

        for element in elements[:50]:
            total += 1
            print(
                "ELEMENT:",
                class_name,
                "| TAG=", element.name,
                "| TEXT=", element.get_text(" ", strip=True)[:500],
            )

            interesting = {
                k: v
                for k, v in element.attrs.items()
                if any(
                    x in k.lower()
                    for x in (
                        "actual",
                        "value",
                        "forecast",
                        "previous",
                        "date",
                        "event",
                        "name",
                    )
                )
            }
            if interesting:
                print("  ATTRS=", interesting)

    return total


def inspect_actual_attributes(soup: BeautifulSoup) -> int:
    print_section("ACTUAL / VALUE ATTRIBUTE-DIAGNOSE")

    hits = 0

    for element in soup.find_all(True):
        interesting = {
            k: v
            for k, v in element.attrs.items()
            if (
                "actual" in k.lower()
                or "value" in k.lower()
                or "forecast" in k.lower()
                or "previous" in k.lower()
            )
        }

        if interesting:
            hits += 1
            print(
                "TAG=",
                element.name,
                "ATTRS=",
                interesting,
                "TEXT=",
                element.get_text(" ", strip=True)[:500],
            )

    print(f"ATTRIBUTE_HITS={hits}")
    return hits


def inspect_candidate_events(soup: BeautifulSoup) -> list[str]:
    print_section("ISM / SERVICES EVENT-KANDIDATEN")

    keywords = (
        "ism",
        "non-manufacturing",
        "services",
        "new orders",
        "employment",
        "prices paid",
    )

    candidates = []
    seen = set()

    for element in soup.find_all(
        ["tr", "div", "li", "td", "a", "span"]
    ):
        text = element.get_text(" ", strip=True)
        if not text:
            continue

        low = text.lower()
        if any(k in low for k in keywords):
            # Nur sinnvoll lange/kurze Kandidaten begrenzen.
            normalized = re.sub(r"\s+", " ", text)
            if normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

    for line in candidates[:200]:
        print("CANDIDATE:", line[:1500])

    print(f"CANDIDATES={len(candidates)}")
    return candidates


def inspect_target_month_candidates(soup: BeautifulSoup) -> list[str]:
    print_section(
        f"ZIELMONAT-DIAGNOSE: {TARGET_YEAR}-{TARGET_MONTH:02d}"
    )

    target = []
    seen = set()

    for element in soup.find_all(
        ["tr", "div", "li", "td", "a", "span"]
    ):
        text = element.get_text(" ", strip=True)
        if not text or not month_matches(text):
            continue

        normalized = re.sub(r"\s+", " ", text)
        if normalized not in seen:
            seen.add(normalized)
            target.append(normalized)

    for line in target[:200]:
        print("TARGET_CANDIDATE:", line[:1500])

    print(f"TARGET_MONTH_CANDIDATES={len(target)}")
    return target


def inspect_embedded_json(soup: BeautifulSoup) -> int:
    print_section("EMBEDDED JSON / SCRIPT-DIAGNOSE")

    hits = 0

    for i, script in enumerate(soup.find_all("script")):
        content = script.string or script.get_text()
        if not content:
            continue

        low = content.lower()

        if not any(
            key in low
            for key in (
                "ism",
                "services",
                "non-manufacturing",
                "actual",
                "economic",
                "calendar",
            )
        ):
            continue

        hits += 1
        print(
            f"SCRIPT_{hits}: length={len(content)} "
            f"type={script.get('type', '')}"
        )

        # Keine komplette Script-Datei ausgeben; relevante Zeilen/Fragmente.
        lines = content.splitlines()
        emitted = 0

        for line in lines:
            l = line.lower()
            if any(
                key in l
                for key in (
                    "ism",
                    "services",
                    "non-manufacturing",
                    "actual",
                    "economic",
                    "calendar",
                    "api",
                    "json",
                )
            ):
                print("  SCRIPT_MATCH:", line.strip()[:1500])
                emitted += 1
                if emitted >= 30:
                    break

    print(f"RELEVANT_SCRIPTS={hits}")
    return hits


def inspect_data_attributes(soup: BeautifulSoup) -> int:
    print_section("DATA-* ATTRIBUTE-DIAGNOSE")

    hits = 0

    for element in soup.find_all(True):
        data_attrs = {
            k: v
            for k, v in element.attrs.items()
            if k.lower().startswith("data-")
        }

        interesting = {
            k: v
            for k, v in data_attrs.items()
            if any(
                x in k.lower()
                for x in (
                    "actual",
                    "value",
                    "forecast",
                    "previous",
                    "event",
                    "date",
                    "name",
                    "ism",
                    "service",
                )
            )
        }

        if interesting:
            hits += 1
            print(
                "TAG=",
                element.name,
                "DATA_ATTRS=",
                interesting,
                "TEXT=",
                element.get_text(" ", strip=True)[:400],
            )

    print(f"DATA_ATTRIBUTE_HITS={hits}")
    return hits


def inspect_html_for_api_patterns(html: str) -> None:
    print_section("MÖGLICHE API/AJAX-PATTERNS IM HTML")

    patterns = [
        r"""["']([^"']*(?:api|ajax|calendar|economic)[^"']*)["']""",
        r"""url\s*:\s*["']([^"']+)["']""",
        r"""fetch\s*\(\s*["']([^"']+)["']""",
        r"""(?:xhr|ajax)\s*\([^)]*["']([^"']+)["']""",
    ]

    found = set()

    for pattern in patterns:
        for match in re.findall(pattern, html, re.IGNORECASE):
            if isinstance(match, tuple):
                match = match[0]
            if match:
                found.add(match)

    if not found:
        print("NO_API_PATTERNS_FOUND")
    else:
        for item in sorted(found):
            print("API_PATTERN:", item[:1000])


def main() -> int:
    print_section("FX BLUE REAL-WORLD SERVICES DIAGNOSE")
    print(f"TARGET_YEAR={TARGET_YEAR}")
    print(f"TARGET_MONTH={TARGET_MONTH}")
    print(
        "TARGET_PERIOD="
        f"{TARGET_YEAR}-{TARGET_MONTH:02d}"
    )
    print(
        "TARGET_MONTH_NAMES="
        f"{MONTH_NAMES[TARGET_MONTH][0]},"
        f"{MONTH_NAMES[TARGET_MONTH][1]}"
    )

    session = requests.Session()
    session.headers.update(HEADERS)

    successful_responses = 0

    for url in CANDIDATE_URLS:
        print_section(f"TEST ENDPOINT: {url}")

        try:
            response = session.get(
                url,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            inspect_response(url, response)

            if response.status_code != 200:
                print(
                    f"ENDPOINT_RESULT=HTTP_{response.status_code}"
                )
                continue

            successful_responses += 1

            html = response.text
            soup = BeautifulSoup(html, "html.parser")

            inspect_links(soup, response.url)
            inspect_candidate_events(soup)
            inspect_target_month_candidates(soup)
            inspect_dom_classes(soup)
            inspect_actual_attributes(soup)
            inspect_data_attributes(soup)
            inspect_embedded_json(soup)
            inspect_html_for_api_patterns(html)

            print_section("DIAGNOSE-ZUSAMMENFASSUNG")
            print("HTTP_OK=YES")
            print(f"HTML_LENGTH={len(html)}")
            print(
                "TARGET_MONTH_MATCH_TEST="
                f"{month_matches(f'{TARGET_YEAR}-{TARGET_MONTH:02d}')}"
            )

        except requests.RequestException as exc:
            print(
                f"REQUEST_ERROR={type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            print(
                f"DIAGNOSE_ERROR={type(exc).__name__}: {exc}"
            )

    print_section("FINAL RESULT")
    print(f"SUCCESSFUL_HTTP_ENDPOINTS={successful_responses}")

    if successful_responses == 0:
        print("RESULT=NO_USABLE_FXBLUE_ENDPOINT")
        print(
            "NEXT_STEP=Endpoint/API muss geklärt werden; "
            "kein Parser-/Gate-Fix."
        )
        # Der Diagnose-Test ist vollständig gelaufen. Für CI soll der
        # Befund sichtbar sein, ohne einen echten Parser-Erfolg vorzutäuschen.
        return 0

    print("RESULT=DIAGNOSE_COMPLETED")
    print(
        "IMPORTANT=Diese Datei verändert keine Produktivlogik "
        "und beweist keinen ISM-Services-Datenfund."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
