#!/usr/bin/env python3
"""Isolierter TE ISM Services Production-Patch-Test.

Die Produktionsdatei makro_szenario.py wird nicht veraendert.
"""

import re
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

TARGET_FILE = Path("makro_szenario.py")
URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"
EXPECTED_RELEASE = "2026-08-05"

EXPECTED = {
    "pmi": 54.10,
    "new_orders": 57.20,
    "employment": 47.40,
    "prices": 70.30,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def extract_main_pmi(html):
    patterns = (
        r"United States ISM Services PMI.{0,3000}?"
        r"(?:edged up to|increased to|rose to)\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        r"ISM Services PMI.{0,3000}?"
        r"(?:edged up to|increased to|rose to)\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
        r"Non Manufacturing PMI.{0,3000}?"
        r"(?:increased to|rose to|edged up to)\s*"
        r"([0-9]+(?:\.[0-9]+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            return float(match.group(1))
    return None


def extract_release(html):
    for pattern in (
        r"\b2026-08-05\b",
        r"\bAugust\s+5,?\s+2026\b",
        r"\bAug\s+5,?\s+2026\b",
    ):
        if re.search(pattern, html, re.I):
            return EXPECTED_RELEASE
    return None


def extract_components(html):
    result = {}
    mapping = {
        "ISM Services New Orders": "new_orders",
        "ISM Services Employment": "employment",
        "ISM Services Prices": "prices",
    }

    for table_no, df in enumerate(pd.read_html(StringIO(html))):
        columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
        lower = [c.casefold() for c in columns]

        if "components" not in lower:
            continue
        if "last" not in lower or "reference" not in lower:
            continue

        component_col = lower.index("components")
        last_col = lower.index("last")
        reference_col = lower.index("reference")

        print(f"COMPONENT_TABLE={table_no}")
        print(f"COMPONENT_COLUMNS={columns!r}")

        for _, row in df.iterrows():
            values = [str(v).strip() for v in row.tolist()]
            if max(component_col, last_col, reference_col) >= len(values):
                continue

            if values[reference_col].casefold() != REFERENCE.casefold():
                continue

            key = mapping.get(values[component_col])
            if key is None:
                continue

            try:
                value = float(values[last_col].replace(",", ""))
            except ValueError:
                continue

            result[key] = value
            print(
                f"FOUND {key}={value:.2f} "
                f"component={values[component_col]!r} "
                f"reference={values[reference_col]!r} "
                f"source_column=Last"
            )

    return result


def main():
    print("=== TE ISM SERVICES PRODUCTION PATCH TEST ===")
    print(f"TARGET_FILE={TARGET_FILE}")
    print(f"REFERENCE={REFERENCE}")
    print(f"URL={URL}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    try:
        compile(
            TARGET_FILE.read_text(encoding="utf-8"),
            str(TARGET_FILE),
            "exec",
        )
    except Exception as exc:
        print(f"TARGET_SYNTAX_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_TARGET_SYNTAX")
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
    print(f"HTML_LENGTH={len(html)}")

    pmi = extract_main_pmi(html)
    release = extract_release(html)
    components = extract_components(html)

    print("")
    print("=== PATCH LOGIC RESULT ===")
    print(f"PMI={pmi}")
    print(f"RELEASE_DATE={release}")

    failed = []

    checks = {
        "pmi": pmi,
        "new_orders": components.get("new_orders"),
        "employment": components.get("employment"),
        "prices": components.get("prices"),
    }

    for key, expected in EXPECTED.items():
        actual = checks[key]
        ok = actual is not None and abs(actual - expected) < 0.001
        print(
            f"CHECK={key} EXPECTED={expected:.2f} "
            f"FOUND={actual} RESULT={'GREEN' if ok else 'RED'}"
        )
        if not ok:
            failed.append(key)

    release_ok = release == EXPECTED_RELEASE
    print(
        f"CHECK=release EXPECTED={EXPECTED_RELEASE} "
        f"FOUND={release} RESULT={'GREEN' if release_ok else 'RED'}"
    )
    if not release_ok:
        failed.append("release")

    print("PREVIOUS_USAGE=FORBIDDEN")
    print("FORECAST_USAGE=FORBIDDEN")

    if failed:
        print("FAILED=" + ",".join(failed))
        print("RESULT=RED_PRODUCTION_PATCH")
        return 1

    print("RESULT=GREEN_PRODUCTION_PATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
