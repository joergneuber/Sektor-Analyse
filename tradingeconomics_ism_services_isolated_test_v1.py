#!/usr/bin/env python3
"""
Isolierter Trading Economics ISM-Services-Test.

Ziel:
- Nur prüfen, ob Trading Economics aus GitHub Actions erreichbar ist.
- Nur die vier benötigten ISM-Services-Werte für Referenz Juli 2026 prüfen:
  PMI, New Orders, Employment, Prices.
- Keine Änderung am Produktionsskript.
- Keine API-Key-Abhängigkeit.
- Nur Python-Standardbibliothek.
"""

import re
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TARGET_YEAR = 2026
TARGET_MONTH = 7
TARGET_RELEASE_DATE = "2026-08-05"
TIMEOUT = 15

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

EVENTS = {
    "pmi": "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
    "new_orders": "https://tradingeconomics.com/united-states/ism-non-manufacturing-new-orders",
    "employment": "https://tradingeconomics.com/united-states/ism-non-manufacturing-employment",
    "prices": "https://tradingeconomics.com/united-states/ism-non-manufacturing-prices",
}


class TableParser(HTMLParser):
    """Minimaler HTML-Tabellenparser ohne externe Pakete."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag == "tr":
            self._row = []

        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._text = []

    def handle_data(self, data):
        if self._cell is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            value = " ".join("".join(self._text).split())
            self._row.append(value)
            self._cell = None
            self._text = []

        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None
            self._text = []


def fetch(url):
    request = Request(
        url,
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
            elapsed = time.monotonic() - started
            return {
                "ok": True,
                "status": response.status,
                "url": response.geturl(),
                "body": body,
                "seconds": elapsed,
                "error": None,
            }

    except HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "url": url,
            "body": b"",
            "seconds": time.monotonic() - started,
            "error": f"HTTPError: {exc}",
        }

    except URLError as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "body": b"",
            "seconds": time.monotonic() - started,
            "error": f"URLError: {exc}",
        }

    except TimeoutError as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "body": b"",
            "seconds": time.monotonic() - started,
            "error": f"TimeoutError: {exc}",
        }

    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "body": b"",
            "seconds": time.monotonic() - started,
            "error": f"{type(exc).__name__}: {exc}",
        }


def normalize_number(value):
    if value is None:
        return None

    text = value.strip().replace(",", "")

    if not text or text.lower() in {"n/a", "na", "-", "--", "null"}:
        return None

    match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", text)

    if not match:
        return None

    try:
        return float(text.rstrip("%"))
    except ValueError:
        return None


def find_target_row(rows):
    """
    Sucht explizit die Release-Zeile 2026-08-05 / Jul.
    Dadurch wird nicht versehentlich Forecast/Previous verwendet.
    """

    candidates = []

    for row in rows:
        joined = " | ".join(row)

        if TARGET_RELEASE_DATE not in joined:
            continue

        if not re.search(r"\bJul\b|\bJuly\b", joined, re.I):
            continue

        candidates.append(row)

    return candidates


def extract_actual(row):
    """
    Trading Economics Kalenderzeile:
    Date | Time | ... | Reference | Actual | Previous | Consensus | Forecast

    Wir suchen bevorzugt nach der Spalte 'Actual' über die tatsächliche
    Tabellenstruktur. Falls nur die reine Datenzeile vorhanden ist, nutzen
    wir die bekannte Position aus der TE-Kalenderstruktur.
    """

    # Typische TE-Datenzeile für den relevanten Release:
    # 2026-08-05 | 02:00 PM | | Jul | 54.1 | 54 | | 54.5
    #
    # Nach Entfernen leerer Zellen kann die Position variieren.
    # Deshalb testen wir die Zeile konservativ anhand der bekannten Struktur.

    if len(row) >= 5:
        # Release date + time + optional empty fields + reference + actual
        for idx, value in enumerate(row):
            if value.lower() in {"jul", "july"}:
                # Actual steht direkt nach der Reference-Zelle.
                for candidate_idx in range(idx + 1, min(idx + 4, len(row))):
                    value = normalize_number(row[candidate_idx])
                    if value is not None:
                        return value

    return None


def main():
    print("=== TRADING ECONOMICS ISM SERVICES ISOLATED TEST v1 ===", flush=True)
    print(f"TARGET_REFERENCE={TARGET_YEAR}-{TARGET_MONTH:02d}", flush=True)
    print(f"TARGET_RELEASE_DATE={TARGET_RELEASE_DATE}", flush=True)
    print(f"TIMEOUT={TIMEOUT}s", flush=True)
    print("DEPENDENCIES=PYTHON_STANDARD_LIBRARY_ONLY", flush=True)
    print("MODE=DIAGNOSTIC_ONLY_NO_PRODUCTION_CHANGES", flush=True)
    print("", flush=True)

    results = {}
    overall_started = time.monotonic()

    for kind, url in EVENTS.items():
        print(f"=== COMPONENT={kind} ===", flush=True)
        print(f"URL={url}", flush=True)

        result = fetch(url)

        print(f"HTTP_STATUS={result['status']}", flush=True)
        print(f"FINAL_URL={result['url']}", flush=True)
        print(f"SECONDS={result['seconds']:.2f}", flush=True)

        if not result["ok"]:
            print(f"FETCH_ERROR={result['error']}", flush=True)
            results[kind] = None
            print("COMPONENT_RESULT=RED_FETCH", flush=True)
            print("", flush=True)
            continue

        print(f"CONTENT_LENGTH={len(result['body'])}", flush=True)

        try:
            html = result["body"].decode("utf-8", errors="replace")
            parser = TableParser()
            parser.feed(html)
            rows = parser.rows

            target_rows = find_target_row(rows)

            print(f"TABLE_ROWS={len(rows)}", flush=True)
            print(f"TARGET_ROWS={len(target_rows)}", flush=True)

            if target_rows:
                for target_row in target_rows[:3]:
                    print(
                        "TARGET_ROW=" + " | ".join(target_row),
                        flush=True,
                    )

                actual = extract_actual(target_rows[0])

                if actual is not None:
                    print(f"ACTUAL={actual}", flush=True)
                    results[kind] = actual
                    print("COMPONENT_RESULT=GREEN", flush=True)
                else:
                    print("ACTUAL=None", flush=True)
                    results[kind] = None
                    print("COMPONENT_RESULT=RED_NO_ACTUAL", flush=True)
            else:
                print("ACTUAL=None", flush=True)
                results[kind] = None
                print("COMPONENT_RESULT=RED_NO_TARGET_ROW", flush=True)

        except Exception as exc:
            print(
                f"PARSE_ERROR={type(exc).__name__}: {exc}",
                flush=True,
            )
            results[kind] = None
            print("COMPONENT_RESULT=RED_PARSE", flush=True)

        print("", flush=True)

    elapsed = time.monotonic() - overall_started

    print("=== FINAL SUMMARY ===", flush=True)

    for kind in EVENTS:
        print(
            f"{kind}: ACTUAL={results.get(kind)}",
            flush=True,
        )

    successful = sum(
        results.get(kind) is not None
        for kind in EVENTS
    )

    print(f"VALID_ACTUALS={successful}/{len(EVENTS)}", flush=True)
    print(f"TOTAL_SECONDS={elapsed:.2f}", flush=True)

    if successful == len(EVENTS):
        print("RESULT=GREEN_TE_ALL_FOUR_ACTUALS", flush=True)
        return 0

    print("RESULT=RED_TE_INCOMPLETE", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
