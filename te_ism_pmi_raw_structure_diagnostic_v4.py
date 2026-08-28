#!/usr/bin/env python3
"""
TE ISM Services PMI – isolierter Raw-HTML-Strukturtest v4

Nur Diagnose. Keine Produktionsänderung.

Ziel:
- echten GitHub-Runner-Response von Trading Economics untersuchen
- gezielt den vorhandenen 2026-08-05-Treffer analysieren
- feststellen, ob der PMI-Wert in Script/JSON/HTML-Attributen/anderen
  eingebetteten Strukturen steckt
- NICHT einfach irgendein 54.1 als PMI akzeptieren

Keine Änderungen an:
Gate, Cache, LME, FRED, FXBlue, Manufacturing.
"""

import html
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

TARGET_DATE = "2026-08-05"
TARGET_DATE_ALT = "08/05/2026"
TARGET_REF = "Jul 2026"

PATTERNS = [
    r"2026-08-05",
    r"08/05/2026",
    r"Jul\s+2026",
    r"ISM\s+Services\s+PMI",
    r"United States\s+ISM\s+Services\s+PMI",
    r"54\.1",
    r"54\.10",
    r"Business\s+Activity",
]


def fetch():
    req = Request(
        URL,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=20) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, URL, ""
    except URLError as e:
        print(f"FETCH_ERROR={type(e).__name__}: {e}")
        return None, URL, ""
    except Exception as e:
        print(f"FETCH_ERROR={type(e).__name__}: {e}")
        return None, URL, ""


def clean(s):
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def show_context(text, pattern, radius=1200, limit=6):
    matches = list(re.finditer(pattern, text, re.I))
    print(f"HITS={len(matches)}")
    for i, m in enumerate(matches[:limit]):
        start = max(0, m.start() - radius)
        end = min(len(text), m.end() + radius)
        print(f"--- CONTEXT {i} ---")
        print(clean(text[start:end]))
    return len(matches)


def extract_scripts(raw):
    scripts = re.findall(
        r"<script\b[^>]*>(.*?)</script\s*>",
        raw,
        flags=re.I | re.S,
    )
    print(f"SCRIPT_BLOCKS={len(scripts)}")

    interesting = []
    needles = (
        "2026-08-05",
        "Jul 2026",
        "54.1",
        "ISM Services",
        "non-manufacturing",
        "calendar",
        "actual",
        "forecast",
        "previous",
    )

    for i, script in enumerate(scripts):
        low = script.lower()
        if any(n.lower() in low for n in needles):
            interesting.append((i, script))

    print(f"INTERESTING_SCRIPT_BLOCKS={len(interesting)}")
    for i, script in interesting[:10]:
        print(f"--- SCRIPT[{i}] ---")
        print(clean(script[:8000]))

    return interesting


def inspect_data_attributes(raw):
    attrs = re.findall(
        r"\b(?:data-[\w:-]+)\s*=\s*(['\"])(.*?)\1",
        raw,
        flags=re.I | re.S,
    )
    interesting = []
    for quote, value in attrs:
        if any(
            x in value.lower()
            for x in (
                "2026-08-05",
                "jul 2026",
                "54.1",
                "ism services",
                "actual",
                "forecast",
                "previous",
            )
        ):
            interesting.append(value)

    print(f"DATA_ATTRIBUTES_TOTAL={len(attrs)}")
    print(f"DATA_ATTRIBUTES_INTERESTING={len(interesting)}")
    for i, value in enumerate(interesting[:30]):
        print(f"DATA_ATTR[{i}]={clean(value)[:1500]}")


def inspect_json_like(raw):
    # Nur Diagnose: JSON-artige Bereiche mit Zielbegriffen finden.
    candidates = re.findall(
        r"(?is)(?:\{[^{}]{0,4000}\}|\[[^\[\]]{0,4000}\])",
        raw,
    )
    interesting = []
    for candidate in candidates:
        low = candidate.lower()
        if (
            "2026-08-05" in low
            or "jul 2026" in low
            or "54.1" in low
            or "ism services" in low
        ):
            interesting.append(candidate)

    print(f"JSON_LIKE_CANDIDATES={len(candidates)}")
    print(f"JSON_LIKE_INTERESTING={len(interesting)}")
    for i, candidate in enumerate(interesting[:20]):
        print(f"--- JSON_LIKE[{i}] ---")
        print(clean(candidate)[:5000])


def main():
    print("=== TE ISM SERVICES PMI RAW STRUCTURE DIAGNOSTIC v4 ===")
    print(f"URL={URL}")
    print(f"TARGET_DATE={TARGET_DATE}")
    print(f"TARGET_REFERENCE={TARGET_REF}")
    print("PRODUCTION_CHANGE=NONE")
    print()

    status, final_url, raw = fetch()
    print(f"HTTP_STATUS={status}")
    print(f"FINAL_URL={final_url}")
    print(f"CONTENT_LENGTH={len(raw)}")

    if not raw:
        print("RESULT=RED_NO_HTML")
        return 1

    print()
    print("=== GLOBAL HIT COUNTS ===")
    counts = {}
    for pattern in PATTERNS:
        n = len(re.findall(pattern, raw, re.I))
        counts[pattern] = n
        print(f"{pattern} => {n}")

    print()
    print("=== TARGET DATE CONTEXT ===")
    date_hits = show_context(raw, r"2026-08-05", radius=1800, limit=8)

    print()
    print("=== JULY 2026 CONTEXT ===")
    jul_hits = show_context(raw, r"Jul\s+2026", radius=1800, limit=8)

    print()
    print("=== PMI LABEL CONTEXT ===")
    pmi_hits = show_context(
        raw,
        r"(?:United States\s+)?ISM\s+Services\s+PMI",
        radius=1800,
        limit=8,
    )

    print()
    print("=== 54.1 CONTEXT ===")
    value_hits = show_context(raw, r"\b54\.1(?:0+)?\b", radius=1800, limit=8)

    print()
    print("=== SCRIPT ANALYSIS ===")
    scripts = extract_scripts(raw)

    print()
    print("=== DATA-ATTRIBUTE ANALYSIS ===")
    inspect_data_attributes(raw)

    print()
    print("=== JSON-LIKE ANALYSIS ===")
    inspect_json_like(raw)

    print()
    print("=== FINAL DIAGNOSTIC STATUS ===")

    # Grün bedeutet nur: Wir haben genug Rohdaten, um den nächsten
    # Parser-Fix gezielt zu bauen. Es ist absichtlich KEIN Parser-Erfolg.
    if date_hits and (jul_hits or pmi_hits or value_hits or scripts):
        print("RESULT=GREEN_RAW_STRUCTURE_SUFFICIENT_FOR_TARGETED_FIX")
        print("NEXT_STEP=PATCH_ONLY_THE_EXISTING_TE_PMI_EXTRACTION")
        return 0

    print("RESULT=RED_RAW_STRUCTURE_NOT_SUFFICIENT")
    return 1


if __name__ == "__main__":
    sys.exit(main())
