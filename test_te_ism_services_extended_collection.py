#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import traceback
from io import StringIO
from pathlib import Path

TARGET = Path("makro_szenario.py")
OUT = Path(".te_services_extended")
OUT.mkdir(exist_ok=True)

TE_URLS = {
    "services_pmi": "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
    "services_new_orders": "https://tradingeconomics.com/united-states/ism-non-manufacturing-new-orders",
    "services_employment": "https://tradingeconomics.com/united-states/ism-non-manufacturing-employment",
    "services_prices": "https://tradingeconomics.com/united-states/ism-non-manufacturing-prices",
}

SP_URLS = {
    "releases_de": "https://www.pmi.spglobal.com/Public/Release/PressReleases?language=de",
    "us_services_public": "https://www.pmi.spglobal.com/Public?language=de",
}

SP_TERMS = [
    "S&P Global US Services PMI",
    "Services PMI",
    "Business Activity",
    "New Business",
    "New Export Business",
    "Outstanding Business",
    "Backlogs",
    "Employment",
    "Prices Charged",
    "Input Prices",
    "Future Activity",
    "business expectations",
    "new orders",
    "employment",
    "prices",
    "Jul 2026",
    "July 2026",
    "Jun 2026",
    "June 2026",
    "May 2026",
]

TE_TERMS = [
    "ISM Services PMI",
    "Services PMI",
    "Business Activity",
    "New Orders",
    "Employment",
    "Prices",
    "Supplier Deliveries",
    "Backlog",
    "Outstanding Business",
    "Inventories",
    "Inventory Sentiment",
    "Imports",
    "Exports",
    "New Export Orders",
    "Actual",
    "Previous",
    "Forecast",
    "Consensus",
    "Jul 2026",
    "July 2026",
    "2026-08-05",
]


def safe(label, fn):
    try:
        return fn()
    except Exception as exc:
        print(f"{label}=RED | {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2)
        return None


def fetch(url, label):
    import requests

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/131 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
        allow_redirects=True,
    )

    print(f"HTTP_{label}={response.status_code}")
    print(f"FINAL_URL_{label}={response.url}")
    print(f"BYTES_{label}={len(response.content)}")

    (OUT / f"{label}.html").write_text(
        response.text,
        encoding="utf-8",
    )

    return response.text


def inventory_html(html, terms, label):
    lower = html.lower()

    for term in terms:
        positions = [
            match.start()
            for match in re.finditer(
                re.escape(term.lower()),
                lower,
            )
        ]

        print(
            f"TERM_{label}={term}|COUNT={len(positions)}"
        )

        for position in positions[:5]:
            snippet = re.sub(
                r"\s+",
                " ",
                html[
                    max(0, position - 450):
                    position + 1800
                ],
            )

            print(
                f"CONTEXT_{label}={term}|"
                f"{snippet[:2200]}"
            )


def parse_tables(html, label):
    import pandas as pd

    tables = pd.read_html(StringIO(html))

    print(f"TABLE_COUNT_{label}={len(tables)}")

    for index, dataframe in enumerate(tables):
        table_text = dataframe.astype(str).to_string(
            index=False
        )

        if re.search(
            r"actual|previous|forecast|consensus|services|"
            r"business activity|new business|new orders|"
            r"employment|prices|outstanding|backlog|"
            r"input prices|prices charged|future activity|"
            r"jul|june|may",
            table_text,
            re.I,
        ):
            print(
                f"TABLE_CANDIDATE_{label}="
                f"{index}|SHAPE={dataframe.shape}"
            )

            print(
                f"COLUMNS_{label}_{index}="
                f"{list(map(str, dataframe.columns))}"
            )

            print(table_text[:12000])


def main():
    print(
        "=== VERY LARGE TE + S&P GLOBAL "
        "SERVICES COLLECTION ==="
    )

    print(
        "RULE=diagnostic only; "
        "no production writes; never abort"
    )

    print(f"TARGET={TARGET}")

    if TARGET.exists():
        print("TARGET_EXISTS=GREEN")

        safe(
            "TARGET_SYNTAX",
            lambda: compile(
                TARGET.read_text(encoding="utf-8"),
                str(TARGET),
                "exec",
            ),
        )
    else:
        print("TARGET_EXISTS=RED")

    pages = {}

    # --------------------------------------------------
    # Trading Economics
    # --------------------------------------------------

    for label, url in TE_URLS.items():
        html = safe(
            f"FETCH_TE_{label}",
            lambda url=url, label=label:
                fetch(url, f"te_{label}"),
        )

        if html:
            pages[f"TE_{label}"] = html

            safe(
                f"INVENTORY_TE_{label}",
                lambda html=html:
                    inventory_html(
                        html,
                        TE_TERMS,
                        "TE",
                    ),
            )

            safe(
                f"TABLES_TE_{label}",
                lambda html=html, label=label:
                    parse_tables(
                        html,
                        f"TE_{label}",
                    ),
            )

    # --------------------------------------------------
    # S&P Global
    # --------------------------------------------------

    for label, url in SP_URLS.items():
        html = safe(
            f"FETCH_SP_{label}",
            lambda url=url, label=label:
                fetch(url, f"sp_{label}"),
        )

        if html:
            pages[f"SP_{label}"] = html

            safe(
                f"INVENTORY_SP_{label}",
                lambda html=html:
                    inventory_html(
                        html,
                        SP_TERMS,
                        "SP",
                    ),
            )

            safe(
                f"TABLES_SP_{label}",
                lambda html=html, label=label:
                    parse_tables(
                        html,
                        f"SP_{label}",
                    ),
            )

    # --------------------------------------------------
    # Broad numeric-context search
    # --------------------------------------------------

    fields = [
        "Business Activity",
        "New Business",
        "New Export Business",
        "Outstanding Business",
        "Employment",
        "Prices Charged",
        "Input Prices",
        "Future Activity",
        "New Orders",
        "Prices",
    ]

    for label, html in pages.items():
        text = re.sub(
            r"\s+",
            " ",
            re.sub(r"<[^>]+>", " ", html),
        )

        for field in fields:
            pattern = re.compile(
                rf"(?i).{{0,220}}"
                rf"{re.escape(field)}"
                rf".{{0,500}}"
                rf"(?<!\d)"
                rf"(?:\d{{1,3}}(?:[.,]\d{{1,2}})?)"
                rf"(?!\d)"
            )

            matches = pattern.findall(text)

            if matches:
                print(
                    f"NUMERIC_CONTEXT_{label}="
                    f"{field}|COUNT={len(matches)}"
                )

                for match in matches[:20]:
                    print(
                        "NUMERIC_CONTEXT=",
                        match[:1200],
                    )

    # --------------------------------------------------
    # Final result -- deliberately always exit 0
    # --------------------------------------------------

    print("=== COLLECTION SUMMARY ===")
    print(f"SOURCES_COLLECTED={len(pages)}")
    print(f"ARTIFACT_DIR={OUT}")
    print("RESULT=COLLECTION_COMPLETE")
    print("EXIT_POLICY=0")

    sys.exit(0)


if __name__ == "__main__":
    main()
