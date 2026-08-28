#!/usr/bin/env python3
"""
Isolierter FRED-Test für ISM Services.

WICHTIG:
- Keine Änderung/Verwendung der Produktivlogik.
- Kein Gate, kein Cache, kein LME, kein FXBlue, kein Manufacturing.
- Jeder HTTP-Request hat ein hartes Timeout.
- Jeder Schritt wird sofort geloggt.
"""

import re
import sys
import time
from datetime import date
import requests

TARGET_YEAR = 2026
TARGET_MONTH = 7

# Kandidaten für den isolierten Test.
# Wir testen nur, welche der benötigten ISM-Services-Reihen
# über die frei zugängliche FRED-Weboberfläche gefunden werden.
SERIES_CANDIDATES = {
    "pmi": [
        "ISM Services PMI",
        "ISM Non-Manufacturing PMI",
        "ISM Services Purchasing Managers Index",
    ],
    "new_orders": [
        "ISM Services New Orders",
        "ISM Non-Manufacturing New Orders",
    ],
    "employment": [
        "ISM Services Employment",
        "ISM Non-Manufacturing Employment",
    ],
    "prices": [
        "ISM Services Prices Paid",
        "ISM Non-Manufacturing Prices Paid",
    ],
}

TIMEOUT = 10
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


def get(url, params=None):
    """Ein einziger HTTP-GET mit hartem Timeout."""
    started = time.monotonic()
    print(f"REQUEST START url={url} params={params}", flush=True)
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
            timeout=TIMEOUT,
        )
        elapsed = time.monotonic() - started
        print(
            f"REQUEST END status={response.status_code} "
            f"seconds={elapsed:.2f} bytes={len(response.content)}",
            flush=True,
        )
        return response
    except requests.RequestException as exc:
        elapsed = time.monotonic() - started
        print(
            f"REQUEST ERROR type={type(exc).__name__} "
            f"seconds={elapsed:.2f} error={exc}",
            flush=True,
        )
        return None


def extract_fred_candidates(html):
    """
    Extrahiert FRED-Series-IDs aus Such-/Ergebnis-HTML.
    Es werden nur tatsächlich gefundene FRED-IDs übernommen.
    """
    ids = []
    seen = set()

    # Typische FRED Series-ID-Links.
    patterns = [
        r'href=["\']/series/([A-Za-z0-9_]+)',
        r'/series/([A-Za-z0-9_]+)',
    ]

    for pattern in patterns:
        for sid in re.findall(pattern, html, flags=re.I):
            if sid not in seen:
                seen.add(sid)
                ids.append(sid)

    return ids


def search_fred(query):
    url = "https://fred.stlouisfed.org/searchresults"
    response = get(url, {"st": query})
    if response is None or response.status_code != 200:
        return []

    ids = extract_fred_candidates(response.text)
    print(f"SEARCH RESULT query={query!r} series_ids={ids[:10]}", flush=True)
    return ids[:10]


def inspect_series(series_id):
    """
    Prüft die FRED-Series-Seite und versucht, einen Wert für TARGET_YEAR/MONTH
    aus dem sichtbaren HTML zu erkennen.

    Das ist bewusst nur eine Diagnose. Es wird KEIN API-Key benötigt und
    nichts in die Produktivpipeline geschrieben.
    """
    url = f"https://fred.stlouisfed.org/series/{series_id}"
    response = get(url)

    if response is None or response.status_code != 200:
        return None

    html = response.text

    # Nur diagnostische Suche nach dem Zielmonat.
    # FRED kann Daten clientseitig/als Chart-Daten einbetten; deshalb prüfen wir
    # mehrere verbreitete Datumsdarstellungen.
    target_patterns = [
        rf"{TARGET_YEAR}-{TARGET_MONTH:02d}-\d{{2}}",
        rf"{TARGET_YEAR}/{TARGET_MONTH:02d}/\d{{2}}",
        rf"{TARGET_MONTH:02d}/\d{{2}}/{TARGET_YEAR}",
    ]

    found_dates = []
    for pattern in target_patterns:
        found_dates.extend(re.findall(pattern, html))

    print(
        f"SERIES {series_id}: target_date_occurrences={len(found_dates)}",
        flush=True,
    )

    # Suche nach eingebetteten Beobachtungsdaten in einfachen FRED-Formaten.
    value_patterns = [
        rf"{TARGET_YEAR}-{TARGET_MONTH:02d}-\d{{2}}[^0-9\-]{{0,80}}"
        rf"(-?\d+(?:\.\d+)?)",
        rf"{TARGET_YEAR}/{TARGET_MONTH:02d}/\d{{2}}[^0-9\-]{{0,80}}"
        rf"(-?\d+(?:\.\d+)?)",
    ]

    for pattern in value_patterns:
        match = re.search(pattern, html)
        if match:
            try:
                value = float(match.group(1))
                print(
                    f"SERIES {series_id}: POSSIBLE_TARGET_VALUE={value}",
                    flush=True,
                )
                return value
            except ValueError:
                pass

    return None


def main():
    print("=== FRED ISM SERVICES ISOLATED TEST v2 ===", flush=True)
    print(f"TARGET={TARGET_YEAR}-{TARGET_MONTH:02d}", flush=True)
    print(f"REQUEST_TIMEOUT={TIMEOUT}s", flush=True)
    print("MODE=DIAGNOSTIC_ONLY_NO_PRODUCTION_CHANGES", flush=True)
    print("", flush=True)

    results = {}
    total_start = time.monotonic()

    for kind, queries in SERIES_CANDIDATES.items():
        print(f"=== COMPONENT={kind} ===", flush=True)

        component_ids = []
        for query in queries:
            ids = search_fred(query)
            for sid in ids:
                if sid not in component_ids:
                    component_ids.append(sid)

            # Nicht mehrere langsame Suchvarianten endlos abarbeiten.
            if component_ids:
                break

        value = None
        selected_id = None

        for sid in component_ids[:3]:
            value = inspect_series(sid)
            if value is not None:
                selected_id = sid
                break

        results[kind] = {
            "series_id": selected_id,
            "value": value,
            "candidates": component_ids,
        }

        print(
            f"COMPONENT_RESULT kind={kind} "
            f"series_id={selected_id} value={value} "
            f"candidates={component_ids}",
            flush=True,
        )
        print("", flush=True)

    elapsed = time.monotonic() - total_start

    print("=== FINAL RESULT ===", flush=True)
    for kind, result in results.items():
        print(
            f"{kind}: series_id={result['series_id']} "
            f"value={result['value']}",
            flush=True,
        )

    # Bewusst konservativ:
    # Ein grünes Ergebnis gibt es nur, wenn alle vier Komponenten tatsächlich
    # einen Zielwert liefern. Kandidaten allein reichen NICHT.
    all_four = all(
        results[kind]["value"] is not None
        for kind in ("pmi", "new_orders", "employment", "prices")
    )

    print(f"TOTAL_SECONDS={elapsed:.2f}", flush=True)

    if all_four:
        print("RESULT=GREEN_FRED_ALL_FOUR", flush=True)
        return 0

    print("RESULT=RED_FRED_INCOMPLETE", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
