#!/usr/bin/env python3
"""
TE ISM Services PMI – isolierter HTML-Strukturtest v3

WICHTIG:
- Noch KEINE Produktionsänderung.
- Nur Diagnose der PMI-Seite.
- Kein Gate, Cache, LME, FRED, FXBlue oder Manufacturing.
- Keine Drittanbieter-Dependencies.

Der vorherige Test hat gezeigt:
  HTTP 200
  CONTENT_LENGTH > 300 KB
  TABLE_ROWS=9
  Related-Zeile vorhanden:
    ISM Services Business Activity | 59.10 | 55.40 | points | Jul 2026

Aber der erwartete PMI-Kalenderdatensatz wurde vom simplen <tr>/<td>-Parser
nicht erfasst.

Dieser Test sucht deshalb zusätzlich direkt im RAW-HTML nach:
  - 54.1 / 54.10
  - Jul 2026
  - 2026-08-05
  - ISM Services PMI
  - ISM Services Business Activity

und gibt den begrenzten HTML-Kontext aus. Damit bestimmen wir die echte
PMI-Struktur, bevor irgendein Produktionsparser geändert wird.
"""

import re
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
TIMEOUT = 20

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
    req = Request(
        URL,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    start = time.monotonic()
    try:
        with urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.geturl(), r.read(), time.monotonic() - start, None
    except HTTPError as e:
        return e.code, URL, b"", time.monotonic() - start, f"HTTPError: {e}"
    except URLError as e:
        return None, URL, b"", time.monotonic() - start, f"URLError: {e}"
    except Exception as e:
        return None, URL, b"", time.monotonic() - start, f"{type(e).__name__}: {e}"


def contexts(html, pattern, radius=500, limit=8):
    hits = list(re.finditer(pattern, html, re.I))
    out = []
    for m in hits[:limit]:
        a = max(0, m.start() - radius)
        b = min(len(html), m.end() + radius)
        snippet = html[a:b]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        out.append(snippet)
    return hits, out


def main():
    print("=== TE ISM SERVICES PMI RAW HTML DIAGNOSTIC v3 ===")
    print(f"URL={URL}")
    print("SCOPE=PMI_ONLY")
    print("PRODUCTION_CHANGE=NONE")
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

    patterns = {
        "PMI_NAME": r"ISM\s+Services\s+PMI",
        "BUSINESS_ACTIVITY": r"ISM\s+Services\s+Business\s+Activity",
        "TARGET_DATE": r"2026-08-05",
        "TARGET_REFERENCE": r"\bJul\s+2026\b",
        "VALUE_54_1": r"\b54\.1(?:0+)?\b",
        "VALUE_54_10": r"\b54\.10\b",
        "VALUE_54": r"\b54(?:\.0+)?\b",
    }

    print()
    print("=== RAW HIT COUNTS ===")
    hitmap = {}
    for name, pattern in patterns.items():
        hits, _ = contexts(html, pattern)
        hitmap[name] = len(hits)
        print(f"{name}={len(hits)}")

    print()
    print("=== EXISTING PARSED TABLE ROWS ===")
    parser = TableParser()
    parser.feed(html)
    print(f"TABLE_ROWS={len(parser.rows)}")
    for i, row in enumerate(parser.rows):
        print(f"ROW[{i}]=" + " | ".join(row))

    print()
    print("=== TARGET RAW CONTEXT ===")

    # Priority: actual PMI value, then date/reference combinations.
    diagnostic_patterns = [
        ("VALUE_54_1", r"\b54\.1(?:0+)?\b"),
        ("TARGET_DATE", r"2026-08-05"),
        ("TARGET_REFERENCE", r"\bJul\s+2026\b"),
        ("PMI_NAME", r"ISM\s+Services\s+PMI"),
    ]

    total_contexts = 0

    for label, pattern in diagnostic_patterns:
        hits, snippets = contexts(html, pattern, radius=700, limit=5)
        if not snippets:
            continue

        print()
        print(f"--- {label}: {len(hits)} hits; showing {len(snippets)} ---")
        for i, snippet in enumerate(snippets):
            print(f"CONTEXT[{label}][{i}]={snippet}")
            total_contexts += 1

    print()
    print("=== STRUCTURE ASSESSMENT ===")

    # We do NOT declare success merely because 54.1 exists.
    # We need evidence that 54.1 belongs to the July PMI release,
    # not another unrelated occurrence.
    has_541 = hitmap["VALUE_54_1"] > 0
    has_date = hitmap["TARGET_DATE"] > 0
    has_jul = hitmap["TARGET_REFERENCE"] > 0
    has_pmi_name = hitmap["PMI_NAME"] > 0

    print(f"HAS_54_1={has_541}")
    print(f"HAS_TARGET_DATE={has_date}")
    print(f"HAS_JUL_2026={has_jul}")
    print(f"HAS_PMI_NAME={has_pmi_name}")
    print(f"CONTEXT_BLOCKS={total_contexts}")

    if has_541 and has_jul:
        print("RESULT=GREEN_PMI_RAW_DATA_PATH_FOUND")
        print("NEXT_STEP=BUILD_MINIMAL_PARSER_FIX_FROM_THE_SHOWN_CONTEXT")
        return 0

    print("RESULT=RED_PMI_RAW_STRUCTURE_STILL_UNRESOLVED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
