#!/usr/bin/env python3
"""
Isolierter Regressionstest: Trading Economics ISM Services PMI.

Zweck:
- Prüft ausschließlich die neue PMI-Erkennung.
- Keine Änderung an Gate, Cache, LME, FRED, FXBlue oder Manufacturing.
- Erwarteter Juli-2026-Wert: 54.1.

Bekannte TE-Struktur der PMI-Seite:
  Calendar:
  2026-08-05 | 02:00 PM | | Jul | 54.1 | 54 | | 54.5

  Related:
  ISM Services Business Activity | 59.10 | 55.40 | points | Jul 2026
  ...

Der Parser darf Business Activity (59.10) NICHT als PMI verwechseln.
"""

import re
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
TARGET_RELEASE_DATE = "2026-08-05"
TARGET_REFERENCE = "Jul"
EXPECTED_PMI = 54.1
TIMEOUT = 15

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            self.text = []

    def handle_data(self, data):
        if self.cell is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.row is not None:
            self.row.append(" ".join("".join(self.text).split()))
            self.cell = None
            self.text = []
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
            self.cell = None


def fetch():
    request = Request(
        URL,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        method="GET",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return (
                response.status,
                response.geturl(),
                response.read(),
                time.monotonic() - started,
                None,
            )
    except HTTPError as exc:
        return exc.code, URL, b"", time.monotonic() - started, f"HTTPError: {exc}"
    except URLError as exc:
        return None, URL, b"", time.monotonic() - started, f"URLError: {exc}"
    except TimeoutError as exc:
        return None, URL, b"", time.monotonic() - started, f"TimeoutError: {exc}"
    except Exception as exc:
        return None, URL, b"", time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def number(value):
    value = value.strip().replace(",", "")
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        return float(value)
    return None


def extract_pmi(rows):
    """
    Neue, gezielte Logik:

    Die TE-PMI-Seite hat eine Kalenderzeile mit:
      ... | Jul | 54.1 | 54 | ... | 54.5

    Nur diese Kalenderzeile darf den PMI-Actual liefern.

    Wichtig:
    - 'ISM Services Business Activity' aus der Related-Tabelle ist NICHT PMI.
    - 'Previous', 'Consensus' und 'TEForecast' werden nicht verwendet.
    """

    for row in rows:
        joined = " | ".join(row)

        if TARGET_RELEASE_DATE not in joined:
            continue

        ref_positions = [
            i for i, value in enumerate(row)
            if value.strip().lower() == TARGET_REFERENCE.lower()
        ]

        for pos in ref_positions:
            # In der echten TE-Kalenderstruktur folgt Actual direkt auf Reference.
            if pos + 1 >= len(row):
                continue

            actual = number(row[pos + 1])
            if actual is None:
                continue

            return actual, row

    return None, None


def main():
    print("=== TE ISM SERVICES PMI REGRESSION TEST v2 ===")
    print(f"URL={URL}")
    print(f"TARGET_RELEASE_DATE={TARGET_RELEASE_DATE}")
    print(f"TARGET_REFERENCE={TARGET_REFERENCE}")
    print(f"EXPECTED_PMI={EXPECTED_PMI}")
    print("DEPENDENCIES=PYTHON_STANDARD_LIBRARY_ONLY")
    print("SCOPE=PMI_PARSER_ONLY")
    print("UNCHANGED=GATE,CACHE,LME,FRED,FXBLUE,MANUFACTURING")
    print()

    status, final_url, body, seconds, error = fetch()

    print(f"HTTP_STATUS={status}")
    print(f"FINAL_URL={final_url}")
    print(f"CONTENT_LENGTH={len(body)}")
    print(f"SECONDS={seconds:.2f}")

    if error:
        print(f"FETCH_ERROR={error}")
        print("RESULT=RED_FETCH")
        return 1

    html = body.decode("utf-8", errors="replace")
    parser = TableParser()
    parser.feed(html)

    print(f"TABLE_ROWS={len(parser.rows)}")
    print()

    # Sicherheitsprüfung gegen die bekannte Verwechslungsgefahr.
    business_activity_rows = [
        row for row in parser.rows
        if any("ISM Services Business Activity" in cell for cell in row)
    ]
    print(f"BUSINESS_ACTIVITY_ROWS={len(business_activity_rows)}")
    if business_activity_rows:
        print(
            "BUSINESS_ACTIVITY_SAMPLE="
            + " | ".join(business_activity_rows[0])
        )

    actual, row = extract_pmi(parser.rows)

    print()
    print("=== PMI RESULT ===")
    print(f"ACTUAL={actual}")
    if row:
        print("ACTUAL_SOURCE_ROW=" + " | ".join(row))

    if actual is None:
        print("RESULT=RED_PMI_ACTUAL_NOT_FOUND")
        return 1

    if actual != EXPECTED_PMI:
        print(
            f"RESULT=RED_WRONG_PMI_EXPECTED_{EXPECTED_PMI}_GOT_{actual}"
        )
        return 1

    if business_activity_rows:
        business_value = number(business_activity_rows[0][1])
        if business_value == actual:
            print("RESULT=RED_PARSER_COLLISION_BUSINESS_ACTIVITY")
            return 1

    print("ASSERTION_EXPECTED_54_1=PASS")
    print("ASSERTION_NOT_BUSINESS_ACTIVITY=PASS")
    print("ASSERTION_ACTUAL_FROM_CALENDAR_ROW=PASS")
    print("RESULT=GREEN_PMI_PARSER_FIXED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
