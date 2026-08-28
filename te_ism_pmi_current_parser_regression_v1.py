#!/usr/bin/env python3
"""
Isolierter Regressionstest für den AKTUELLEN TE-ISM-PMI-Parser.

WICHTIG:
- Die Produktionsdatei wird NICHT verändert.
- Getestet wird die vorhandene Funktion _te_calendar_actual().
- Zusätzlich wird die reale Trading-Economics-Seite geladen.
- Keine Änderung an Gate, Cache, LME, FRED, FXBlue oder Manufacturing.

Ziel:
1. feststellen, was der aktuelle Parser mit echter TE-Antwort liefert;
2. die bekannten HTML-/Tabellenvarianten synthetisch prüfen;
3. bei Erfolg den erwarteten PMI 54.1 bestätigen;
4. Business Activity 59.10 darf niemals als PMI zurückkommen.

Die Produktionsdatei muss im Repository-Root liegen.
"""

import importlib.util
import re
import sys
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import pandas as pd


PROD_CANDIDATES = [
    "makro_szenario_v6_8_actual_parser_fixed.py",
    "makro_szenario.py",
    "makro_szenario_v6_11_fxblue_real_html_fix.py",
]

URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
EXPECTED_REF = "Jul 2026"
EXPECTED_VALUE = 54.1
EXPECTED_RELEASE_DATE = "2026-08-05"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def find_production_file():
    for name in PROD_CANDIDATES:
        path = Path(name)
        if path.is_file():
            return path
    return None


def load_production_module(path):
    spec = importlib.util.spec_from_file_location("macro_production_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Modul konnte nicht geladen werden: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_real_html():
    request = Request(
        URL,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.geturl(), response.read().decode(
                "utf-8", errors="replace"
            )
    except HTTPError as exc:
        return exc.code, URL, ""
    except URLError as exc:
        return None, URL, ""
    except Exception as exc:
        print(f"REAL_FETCH_EXCEPTION={type(exc).__name__}: {exc}")
        return None, URL, ""


def synthetic_cases():
    # Diese Fälle bilden die Struktur ab, die der Parser tatsächlich können muss.
    # Sie testen ausschließlich _te_calendar_actual().
    return [
        (
            "standard_actual_column",
            """
            <table>
              <tr><th>Date</th><th>Reference</th><th>Actual</th><th>Forecast</th><th>Previous</th></tr>
              <tr><td>2026-08-05</td><td>Jul 2026</td><td>54.1</td><td>54</td><td>54.5</td></tr>
            </table>
            """,
            54.1,
        ),
        (
            "date_reference_combined",
            """
            <table>
              <tr><th>Date</th><th>Actual</th><th>Forecast</th><th>Previous</th></tr>
              <tr><td>Aug 5, 2026</td><td>54.1</td><td>54</td><td>54.5</td></tr>
            </table>
            """,
            54.1,
        ),
        (
            "business_activity_must_not_collide",
            """
            <table>
              <tr><th>Components</th><th>Last</th><th>Previous</th><th>Unit</th><th>Reference</th></tr>
              <tr><td>ISM Services Business Activity</td><td>59.10</td><td>55.40</td><td>points</td><td>Jul 2026</td></tr>
            </table>
            """,
            None,
        ),
        (
            "forecast_previous_without_actual",
            """
            <table>
              <tr><th>Date</th><th>Reference</th><th>Forecast</th><th>Previous</th></tr>
              <tr><td>2026-08-05</td><td>Jul 2026</td><td>54</td><td>54.5</td></tr>
            </table>
            """,
            None,
        ),
    ]


def run_synthetic(parser):
    print()
    print("=== SYNTHETIC REGRESSION ===")
    failures = 0

    for name, html, expected in synthetic_cases():
        try:
            actual, release_date = parser(html, EXPECTED_REF)
        except Exception as exc:
            print(f"CASE={name} RESULT=ERROR {type(exc).__name__}: {exc}")
            failures += 1
            continue

        ok = actual == expected
        if expected is not None:
            ok = ok and release_date == EXPECTED_RELEASE_DATE

        print(
            f"CASE={name} ACTUAL={actual} RELEASE_DATE={release_date} "
            f"EXPECTED={expected} RESULT={'PASS' if ok else 'FAIL'}"
        )

        if not ok:
            failures += 1

    return failures


def run_real(parser):
    print()
    print("=== REAL TRADING ECONOMICS REGRESSION ===")
    status, final_url, html = fetch_real_html()

    print(f"HTTP_STATUS={status}")
    print(f"FINAL_URL={final_url}")
    print(f"CONTENT_LENGTH={len(html)}")

    if not html:
        print("REAL_RESULT=RED_NO_HTML")
        return 1

    # Nachweis, dass die bekannte TE-Seite überhaupt die relevanten Werte enthält.
    def hit(pattern):
        return len(re.findall(pattern, html, flags=re.I))

    print(f"RAW_54_1_HITS={hit(r'\\b54\\.1(?:0+)?\\b')}")
    print(f"RAW_JUL_2026_HITS={hit(r'\\bJul\\s+2026\\b')}")
    print(f"RAW_TARGET_DATE_HITS={hit(r'2026-08-05')}")
    print(
        "RAW_BUSINESS_ACTIVITY_HITS="
        + str(hit(r'ISM\\s+Services\\s+Business\\s+Activity'))
    )

    try:
        actual, release_date = parser(html, EXPECTED_REF)
    except Exception as exc:
        print(f"REAL_PARSER_EXCEPTION={type(exc).__name__}: {exc}")
        print("REAL_RESULT=RED_PARSER_EXCEPTION")
        return 1

    print(f"PARSER_ACTUAL={actual}")
    print(f"PARSER_RELEASE_DATE={release_date}")

    if actual == EXPECTED_VALUE and release_date == EXPECTED_RELEASE_DATE:
        print("ASSERTION_PMI_54_1=PASS")
        print("ASSERTION_RELEASE_DATE=PASS")
        print("ASSERTION_BUSINESS_ACTIVITY_NOT_USED=MANUAL_CHECK_REQUIRED")
        print("REAL_RESULT=GREEN_CURRENT_PARSER_CONFIRMED")
        return 0

    print(
        "REAL_RESULT=RED_CURRENT_PARSER_NOT_CONFIRMED"
        f" EXPECTED={EXPECTED_VALUE}/{EXPECTED_RELEASE_DATE}"
    )
    return 1


def main():
    print("=== CURRENT TE PMI PARSER REGRESSION TEST v1 ===")
    print(f"TARGET={EXPECTED_REF}")
    print(f"EXPECTED={EXPECTED_VALUE}")
    print(f"EXPECTED_RELEASE_DATE={EXPECTED_RELEASE_DATE}")
    print("PRODUCTION_CHANGE=NONE")
    print()

    prod = find_production_file()
    if prod is None:
        print(
            "RESULT=RED_PRODUCTION_FILE_NOT_FOUND "
            f"EXPECTED_ONE_OF={','.join(PROD_CANDIDATES)}"
        )
        return 1

    print(f"PRODUCTION_FILE={prod}")

    try:
        module = load_production_module(prod)
    except Exception as exc:
        print(f"IMPORT_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_PRODUCTION_IMPORT")
        return 1

    parser = getattr(module, "_te_calendar_actual", None)
    if parser is None:
        print("RESULT=RED_FUNCTION_NOT_FOUND")
        return 1

    synthetic_failures = run_synthetic(parser)
    real_result = run_real(parser)

    print()
    print("=== FINAL ===")
    print(f"SYNTHETIC_FAILURES={synthetic_failures}")
    print(f"REAL_EXIT={real_result}")

    if synthetic_failures == 0 and real_result == 0:
        print("RESULT=GREEN_CURRENT_PARSER_FULL_REGRESSION")
        return 0

    print("RESULT=RED_CURRENT_PARSER_NEEDS_MINIMAL_FIX")
    return 1


if __name__ == "__main__":
    sys.exit(main())
