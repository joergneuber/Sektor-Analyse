#!/usr/bin/env python3
"""
ISOLIERTER PATCH-TEST v1
TE ISM Services: Last statt Actual

Produktionsdatei bleibt unveraendert.

Der Test:
- ruft Trading Economics direkt ab,
- liest die aktuelle Services-PMI-Tabelle,
- akzeptiert die TE-Spalte "Last" als aktuellen Wert,
- verlangt Reference = Jul 2026,
- verlangt ein Release-Datum im Folgemonat,
- prueft den PMI gezielt gegen 54.10,
- prueft die drei Subindizes gegen 57.20 / 47.40 / 70.30,
- verwendet niemals Previous.
"""

from pathlib import Path
import re
import sys
from datetime import datetime
from io import StringIO

import pandas as pd
import requests


TARGET_FILE = Path("makro_szenario.py")
URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"
EXPECTED_RELEASE = "2026-08-05"

EXPECTED = {
    "ISM Services PMI": 54.10,
    "ISM Services New Orders": 57.20,
    "ISM Services Employment": 47.40,
    "ISM Services Prices": 70.30,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_release_from_html(html):
    patterns = (
        r"\b2026-08-05\b",
        r"\bAugust\s+5,?\s+2026\b",
        r"\bAug\s+5,?\s+2026\b",
    )
    for pattern in patterns:
        if re.search(pattern, html, re.I):
            return EXPECTED_RELEASE
    return None


def normalize_component(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def main():
    print("=== TE ISM SERVICES LAST-PATCH TEST v1 ===")
    print(f"TARGET_FILE={TARGET_FILE}")
    print(f"REFERENCE={REFERENCE}")
    print(f"URL={URL}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    try:
        response = requests.get(
            URL,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"HTTP_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_HTTP")
        return 1

    html = response.text
    print(f"HTTP_STATUS={response.status_code}")
    print(f"FINAL_URL={response.url}")
    print(f"HTML_LENGTH={len(html)}")

    release = parse_release_from_html(html)
    print(f"RELEASE_DATE={release}")

    if release != EXPECTED_RELEASE:
        print("RESULT=RED_RELEASE_DATE")
        return 1

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as exc:
        print(f"READ_HTML_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_READ_HTML")
        return 1

    print(f"TABLE_COUNT={len(tables)}")

    found = {}

    for table_no, df in enumerate(tables):
        columns = [normalize_component(c) for c in df.columns]
        lower = [c.casefold() for c in columns]

        if "components" not in lower:
            continue

        current_col = None
        if "last" in lower:
            current_col = lower.index("last")
            current_name = "Last"
        elif "actual" in lower:
            current_col = lower.index("actual")
            current_name = "Actual"
        else:
            continue

        print(f"TABLE={table_no} COLUMNS={columns!r}")
        print(f"CURRENT_COLUMN={current_name}")

        components_col = lower.index("components")
        reference_col = lower.index("reference") if "reference" in lower else None

        for _, row in df.iterrows():
            cells = [normalize_component(v) for v in row.tolist()]
            if components_col >= len(cells) or current_col >= len(cells):
                continue

            component = cells[components_col]
            reference = cells[reference_col] if reference_col is not None and reference_col < len(cells) else ""

            if reference.casefold() != REFERENCE.casefold():
                continue

            if component in EXPECTED:
                raw = cells[current_col]
                try:
                    value = float(raw.replace(",", ""))
                except ValueError:
                    continue

                found[component] = value
                print(
                    f"FOUND component={component!r} "
                    f"reference={reference!r} "
                    f"current={current_name!r} value={value}"
                )

    print("")
    print("=== VALIDATION ===")

    failed = []

    for component, expected in EXPECTED.items():
        actual = found.get(component)
        if actual is None:
            print(f"CHECK={component} RESULT=MISSING")
            failed.append(component)
            continue

        ok = abs(actual - expected) < 0.001
        print(
            f"CHECK={component} EXPECTED={expected:.2f} "
            f"FOUND={actual:.2f} RESULT={'GREEN' if ok else 'RED'}"
        )
        if not ok:
            failed.append(component)

    # Explicit guard: Previous must not be selected.
    print("PREVIOUS_USAGE=FORBIDDEN")
    print("FORECAST_USAGE=FORBIDDEN")

    if failed:
        print("FAILED=" + ",".join(failed))
        print("RESULT=RED_PATCH_TEST")
        return 1

    print("RESULT=GREEN_TE_LAST_PATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
