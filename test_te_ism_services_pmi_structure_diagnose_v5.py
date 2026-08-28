#!/usr/bin/env python3
"""ISOLIERTE TE-ISM-SERVICES-PMI-STRUKTURDIAGNOSE v5."""

import re
import sys
from io import StringIO

import pandas as pd
import requests

URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def norm(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def main():
    print("=== TE ISM SERVICES PMI STRUCTURE DIAGNOSE v5 ===")
    print(f"URL={URL}")
    print(f"REFERENCE={REFERENCE}")

    try:
        response = requests.get(
            URL, headers=HEADERS, timeout=30, allow_redirects=True
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"HTTP_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_HTTP")
        return 1

    html = response.text
    print(f"HTTP_STATUS={response.status_code}")
    print(f"HTML_LENGTH={len(html)}")

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as exc:
        print(f"READ_HTML_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_READ_HTML")
        return 1

    print(f"TABLE_COUNT={len(tables)}")

    hits = 0

    for table_no, df in enumerate(tables):
        columns = [norm(c) for c in df.columns]

        print("")
        print(f"=== TABLE {table_no} ===")
        print(f"SHAPE={df.shape}")
        print(f"COLUMNS={columns!r}")

        for row_index, row in df.iterrows():
            values = [norm(v) for v in row.tolist()]
            joined = " | ".join(values)
            low = joined.casefold()

            # Haupt-PMI und alle moeglichen Schreibweisen gezielt suchen.
            is_hit = (
                "54.10" in joined
                or "54.1" in joined
                or "services pmi" in low
                or "non manufacturing pmi" in low
                or ("pmi" in low and "services" in low)
                or ("pmi" in low and "ism" in low)
            )

            if is_hit:
                hits += 1
                print("")
                print(f"*** PMI HIT #{hits} ***")
                print(f"ROW_INDEX={row_index}")
                print(f"ROW_VALUES={values!r}")

                for col, value in zip(columns, values):
                    print(f"CELL[{col}]={value!r}")

                # Bei HTML-Strukturen mit verstecktem/zusammengesetztem Text
                # zusaetzlich die gesamte Zeile als String ausgeben.
                print(f"ROW_JOINED={joined}")

    print("")
    print("=== RAW HTML 54.1 CONTEXT ===")

    positions = [m.start() for m in re.finditer(r"54\.1", html, re.I)]
    print(f"RAW_54_1_HITS={len(positions)}")

    for i, pos in enumerate(positions[:20], 1):
        context = re.sub(r"\s+", " ", html[max(0, pos - 500):pos + 1000])
        print(f"RAW_CONTEXT_{i}={context}")

    print("")
    print("=== RAW HTML PMI CONTEXT ===")

    for needle in (
        "ISM Services PMI",
        "Services PMI",
        "Non Manufacturing PMI",
    ):
        positions = [m.start() for m in re.finditer(re.escape(needle), html, re.I)]
        print(f"RAW_{needle!r}_HITS={len(positions)}")
        for i, pos in enumerate(positions[:5], 1):
            context = re.sub(r"\s+", " ", html[max(0, pos - 400):pos + 900])
            print(f"RAW_{needle!r}_{i}={context}")

    print("")
    print(f"TOTAL_PMI_TABLE_HITS={hits}")

    if hits:
        print("RESULT=GREEN_PMI_STRUCTURE_FOUND")
        return 0

    print("RESULT=RED_PMI_STRUCTURE_NOT_FOUND")
    return 1


if __name__ == "__main__":
    sys.exit(main())
