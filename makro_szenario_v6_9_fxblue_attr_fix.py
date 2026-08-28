#!/usr/bin/env python3
"""
Isolierter FRED-Test für ISM Services – Juli 2026

Zweck:
- NICHTS am Produktivcode ändern.
- FRED als mögliche Ersatzquelle objektiv prüfen.
- Die vier benötigten Reihen separat suchen und testen:
    1) Services PMI
    2) Services New Orders
    3) Services Employment
    4) Services Prices
- Zielbeobachtung: 2026-07-01 / Juli 2026.
- Keine Werte aus Forecast/Previous ableiten.
- Keine Manufacturing-Reihe als Services akzeptieren.
- Wenn eine benötigte Reihe nicht eindeutig gefunden wird: FAIL-CLOSED.

Der Test nutzt:
1. FRED-Suchseite zur Ermittlung möglicher Series IDs.
2. FRED-CSV-Endpunkt für die tatsächlichen Beobachtungen.
Eine FRED-API-Key ist deshalb für diesen Test nicht erforderlich.

Exit:
  0 = GREEN_FRED_ALL_FOUR
  1 = RED_FRED_INCOMPLETE
"""

import csv
import io
import re
import sys
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


TARGET_DATE = "2026-07-01"
TARGET_YEAR = "2026"
TARGET_MONTH = "07"

SEARCH_URL = "https://fred.stlouisfed.org/searchresults"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/csv,text/plain;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Suchbegriffe bewusst streng auf ISM Services begrenzt.
TARGETS = {
    "pmi": [
        "ISM Services PMI",
        "ISM Non-Manufacturing PMI",
        "ISM Services",
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
        "ISM Services Prices",
        "ISM Services Prices Paid",
        "ISM Non-Manufacturing Prices",
        "ISM Non-Manufacturing Prices Paid",
    ],
}

# Bekannte offizielle Juli-2026-Werte dienen ausschließlich als
# Plausibilitäts-/Identitätskontrolle. Sie werden NICHT als Fallback-Daten
# verwendet.
EXPECTED_JULY_2026 = {
    "pmi": 54.1,
    "new_orders": 57.2,
    "employment": 47.4,
    "prices": 70.3,
}


def normalize(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def search_fred(term, session):
    params = {
        "st": term,
        "search_type": "series",
        "order": "search_rank",
    }

    response = session.get(
        SEARCH_URL,
        params=params,
        timeout=20,
    )

    print(f"SEARCH term={term!r}")
    print(f"SEARCH_STATUS={response.status_code}")
    print(f"SEARCH_URL={response.url}")

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []

    # FRED series links haben üblicherweise /series/<ID>.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.fullmatch(r"/series/([A-Za-z0-9_]+)", href)
        if not match:
            continue

        series_id = match.group(1)
        label = normalize(a.get_text(" ", strip=True))

        if series_id and series_id not in {
            item["series_id"] for item in candidates
        }:
            candidates.append(
                {
                    "series_id": series_id,
                    "label": label,
                    "url": urljoin("https://fred.stlouisfed.org", href),
                }
            )

    return candidates[:30]


def fetch_series(series_id, session):
    params = {
        "id": series_id,
    }

    response = session.get(
        CSV_URL,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        return None, f"HTTP_{response.status_code}"

    text = response.text
    if not text.strip():
        return None, "EMPTY"

    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as exc:
        return None, f"CSV_PARSE_ERROR:{type(exc).__name__}"

    for row in rows:
        date = row.get("observation_date")
        value = row.get(series_id)

        if date == TARGET_DATE:
            if value is None:
                return None, "MISSING_VALUE"

            value = value.strip()

            if value in {"", ".", "NA", "N/A", "null", "None"}:
                return None, "NON_NUMERIC_VALUE"

            try:
                return float(value), "OK"
            except ValueError:
                return None, "NON_NUMERIC_VALUE"

    return None, "TARGET_DATE_NOT_FOUND"


def identity_score(kind, series_id, label, term):
    text = f"{series_id} {label} {term}".lower()

    # Services muss explizit vorkommen; Non-Manufacturing ist ebenfalls
    # zulässig, weil ISM Services historisch so bezeichnet wird.
    services = (
        "services" in text
        or "non-manufacturing" in text
        or "non manufacturing" in text
    )

    manufacturing_only = (
        "manufacturing" in text
        and not (
            "non-manufacturing" in text
            or "non manufacturing" in text
            or "services" in text
        )
    )

    if manufacturing_only or not services:
        return -100

    score = 0

    if "ism" in text:
        score += 10

    if kind == "pmi" and "pmi" in text:
        score += 20

    if kind == "new_orders" and "new orders" in text:
        score += 20

    if kind == "employment" and "employment" in text:
        score += 20

    if kind == "prices" and "prices" in text:
        score += 20

    if "manufacturing" in text:
        score -= 50

    return score


def main():
    print("=== FRED ISM SERVICES ISOLATED TEST ===")
    print(f"TARGET_DATE={TARGET_DATE}")
    print("PRODUCTIVE_CODE_CHANGED=False")
    print("FORECAST_PREVIOUS_FALLBACK=False")
    print("MANUFACTURING_AS_SERVICES=False")
    print()

    session = requests.Session()
    session.headers.update(HEADERS)

    selected = {}
    diagnostics = {}

    for kind, terms in TARGETS.items():
        print(f"=== TARGET={kind} ===")

        discovered = []

        for term in terms:
            try:
                found = search_fred(term, session)
            except requests.RequestException as exc:
                print(
                    f"SEARCH_ERROR={type(exc).__name__}: {exc}"
                )
                continue

            for item in found:
                key = item["series_id"]
                if key not in {
                    x["series_id"] for x in discovered
                }:
                    discovered.append(item)

        scored = []

        for item in discovered:
            score = identity_score(
                kind,
                item["series_id"],
                item["label"],
                " ".join(terms),
            )

            if score <= 0:
                continue

            value, status = fetch_series(
                item["series_id"],
                session,
            )

            scored.append(
                {
                    **item,
                    "score": score,
                    "value": value,
                    "status": status,
                }
            )

            print(
                "CANDIDATE "
                f"series={item['series_id']} "
                f"score={score} "
                f"status={status} "
                f"value={value} "
                f"label={item['label']!r}"
            )

        scored.sort(
            key=lambda x: (
                x["value"] is not None,
                x["score"],
            ),
            reverse=True,
        )

        valid = [
            item
            for item in scored
            if item["value"] is not None
        ]

        if valid:
            best = valid[0]
            selected[kind] = best
            print(
                "SELECTED "
                f"series={best['series_id']} "
                f"value={best['value']} "
                f"label={best['label']!r}"
            )
        else:
            diagnostics[kind] = scored
            print("SELECTED=None")

        print()

    print("=== FINAL FRED RESULT ===")

    all_four = True

    for kind in TARGETS:
        item = selected.get(kind)

        if item is None:
            print(f"{kind.upper()}=MISSING")
            all_four = False
            continue

        value = item["value"]
        expected = EXPECTED_JULY_2026[kind]
        delta = abs(value - expected)

        print(
            f"{kind.upper()}="
            f"{value} "
            f"SERIES={item['series_id']} "
            f"EXPECTED_REFERENCE={expected} "
            f"DELTA={delta}"
        )

        if delta > 0.001:
            print(
                f"{kind.upper()}_IDENTITY_CHECK=RED"
            )
            all_four = False
        else:
            print(
                f"{kind.upper()}_IDENTITY_CHECK=GREEN"
            )

    print()

    if all_four:
        print("RESULT=GREEN_FRED_ALL_FOUR")
        print(
            "CONCLUSION="
            "FRED liefert alle vier benötigten ISM-Services-Werte "
            "für Juli 2026 und die Werte stimmen mit der offiziellen "
            "ISM-Referenz überein."
        )
        return 0

    print("RESULT=RED_FRED_INCOMPLETE")
    print(
        "CONCLUSION="
        "FRED ist als alleinige Quelle für alle vier benötigten "
        "ISM-Services-Komponenten nicht nachgewiesen."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
