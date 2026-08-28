#!/usr/bin/env python3
"""
Isolierter Trading Economics PMI-Diagnosetest.

NUR Diagnose:
- keine Produktionsdateien
- kein Gate
- kein Cache
- kein LME
- kein FRED
- kein FXBlue

Ziel: die tatsächlichen HTML-Tabellenzeilen der TE-PMI-Seite sichtbar machen
und den Juli-2026-Actual 54.1 eindeutig identifizieren.
"""

import re
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
TARGET_DATE = "2026-08-05"
TARGET_REF = "Jul"
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
            value = " ".join("".join(self.text).split())
            self.row.append(value)
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
            body = response.read()
            return response.status, response.geturl(), body, time.monotonic() - started, None

    except HTTPError as exc:
        return exc.code, URL, b"", time.monotonic() - started, f"HTTPError: {exc}"

    except URLError as exc:
        return None, URL, b"", time.monotonic() - started, f"URLError: {exc}"

    except TimeoutError as exc:
        return None, URL, b"", time.monotonic() - started, f"TimeoutError: {exc}"

    except Exception as exc:
        return None, URL, b"", time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def is_number(value):
    return re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.replace(",", "")) is not None


def main():
    print("=== TRADING ECONOMICS PMI DEEP DIAGNOSTIC v1 ===", flush=True)
    print(f"URL={URL}", flush=True)
    print(f"TARGET_DATE={TARGET_DATE}", flush=True)
    print(f"TARGET_REFERENCE={TARGET_REF}", flush=True)
    print(f"TIMEOUT={TIMEOUT}s", flush=True)
    print("MODE=DIAGNOSTIC_ONLY", flush=True)
    print("", flush=True)

    status, final_url, body, seconds, error = fetch()

    print(f"HTTP_STATUS={status}", flush=True)
    print(f"FINAL_URL={final_url}", flush=True)
    print(f"CONTENT_LENGTH={len(body)}", flush=True)
    print(f"SECONDS={seconds:.2f}", flush=True)

    if error:
        print(f"FETCH_ERROR={error}", flush=True)
        print("RESULT=RED_FETCH", flush=True)
        return 1

    html = body.decode("utf-8", errors="replace")

    print("", flush=True)
    print("=== RAW HTML TARGET CHECK ===", flush=True)
    print(f"DATE_OCCURRENCES={html.count(TARGET_DATE)}", flush=True)
    jul_occurrences = len(re.findall(r"\\bJul\\b", html, re.I))
    print(f"JUL_OCCURRENCES={jul_occurrences}", flush=True)
    print(f"54_1_OCCURRENCES={len(re.findall(r'\\b54\\.1\\b', html))}", flush=True)

    parser = TableParser()
    parser.feed(html)

    print("", flush=True)
    print("=== ALL PARSED TABLE ROWS ===", flush=True)
    print(f"TABLE_ROWS={len(parser.rows)}", flush=True)

    for index, row in enumerate(parser.rows):
        print(
            f"ROW[{index}]=" + " | ".join(row),
            flush=True,
        )

    print("", flush=True)
    print("=== TARGET CANDIDATES ===", flush=True)

    candidates = []

    for index, row in enumerate(parser.rows):
        joined = " | ".join(row)

        if TARGET_DATE in joined or re.search(r"\bJul\b", joined, re.I):
            candidates.append((index, row))
            print(
                f"CANDIDATE_ROW[{index}]=" + " | ".join(row),
                flush=True,
            )

    print(f"TARGET_CANDIDATES={len(candidates)}", flush=True)

    print("", flush=True)
    print("=== ACTUAL DETECTION ===", flush=True)

    actual = None
    actual_row = None

    for index, row in candidates:
        # TE calendar structure is typically:
        # date | time | empty | reference | actual | previous | consensus | forecast
        #
        # We deliberately require the July reference and the release date.
        if TARGET_DATE not in " | ".join(row):
            continue

        for pos, value in enumerate(row):
            if value.lower() == TARGET_REF.lower():
                following = row[pos + 1:pos + 4]

                print(
                    f"REFERENCE_FOUND row={index} pos={pos} "
                    f"following={following}",
                    flush=True,
                )

                for candidate in following:
                    if is_number(candidate):
                        actual = float(candidate)
                        actual_row = index
                        break

                if actual is not None:
                    break

        if actual is not None:
            break

    if actual is not None:
        print(f"ACTUAL={actual}", flush=True)
        print(f"ACTUAL_ROW={actual_row}", flush=True)

        if actual == 54.1:
            print("ASSERTION=PASS_EXPECTED_54_1", flush=True)
            print("RESULT=GREEN_PMI_ACTUAL_CONFIRMED", flush=True)
            return 0

        print("ASSERTION=FAIL_UNEXPECTED_ACTUAL", flush=True)
        print("RESULT=RED_WRONG_ACTUAL", flush=True)
        return 1

    print("ACTUAL=None", flush=True)
    print("RESULT=RED_PMI_STRUCTURE_UNRESOLVED", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
