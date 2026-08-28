#!/usr/bin/env python3
"""Isolierter TE-Tabellen-Test: zeigt exakt, was pandas.read_html sieht."""

from pathlib import Path
import ast
import sys
import requests
import pandas as pd
from io import StringIO

TARGET_FILE = Path("makro_szenario.py")
URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def main():
    print("=== TE ISM SERVICES TABLE DIAGNOSE v4 ===")
    print(f"TARGET_FILE={TARGET_FILE}")
    print(f"REFERENCE={REFERENCE}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    html = requests.get(URL, headers=HEADERS, timeout=30).text
    print(f"HTTP_HTML_LENGTH={len(html)}")

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as exc:
        print(f"READ_HTML_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_READ_HTML")
        return 1

    print(f"TABLE_COUNT={len(tables)}")

    target_rows = 0

    for i, df in enumerate(tables):
        print("")
        print(f"=== TABLE {i} ===")
        print(f"SHAPE={df.shape}")
        print(f"COLUMNS_RAW={df.columns.tolist()!r}")

        cols = [str(c).strip() for c in df.columns]
        print(f"COLUMNS_STRIPPED={cols!r}")
        print(f"ACTUAL_EXACT={any(c.casefold() == 'actual' for c in cols)}")

        # Print rows containing relevant terms/values, plus first 5 rows.
        shown = set()
        for ridx, row in df.iterrows():
            joined = " | ".join(str(v) for v in row.tolist())
            low = joined.casefold()
            if (
                "pmi" in low
                or "services" in low
                or "non manufacturing" in low
                or "54.1" in low
                or "57.2" in low
                or "47.4" in low
                or "70.3" in low
                or "jul 2026" in low
            ):
                if ridx not in shown:
                    print(f"TARGET_ROW_INDEX={ridx}")
                    print(f"TARGET_ROW={joined}")
                    shown.add(ridx)
                    target_rows += 1

        if len(df):
            print("FIRST_ROWS:")
            for ridx, row in df.head(5).iterrows():
                print(f"ROW_{ridx}=" + " | ".join(str(v) for v in row.tolist()))

    print("")
    print("=== DIAGNOSTIC CONCLUSION ===")
    print(f"TARGET_ROWS={target_rows}")

    # Independently verify the exact expected current TE values in HTML.
    expected = ("54.10", "57.20", "47.40", "70.30", "2026-08-05")
    for value in expected:
        print(f"HTML_MARKER_{value}={html.count(value)}")

    print("RESULT=GREEN_TABLE_DIAGNOSTIC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
