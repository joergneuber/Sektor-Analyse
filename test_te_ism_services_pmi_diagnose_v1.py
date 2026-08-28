#!/usr/bin/env python3
"""
ISOLIERTE DIAGNOSE:
Trading Economics -> ISM Services PMI

Zweck:
- NICHT den Produktionscode veraendern.
- NICHT Forecast/Previous als Actual verwenden.
- Den von Trading Economics gelieferten PMI-HTML-Inhalt untersuchen.
- Alle plausiblen Release-Date-Kandidaten ausgeben.
- Zeigen, was der bestehende Parser _te_calendar_actual() fuer PMI liefert.

Diese Datei ist absichtlich eigenstaendig.
"""

from pathlib import Path
import ast
import importlib.util
import re
import sys

import requests


TARGET_FILE = Path("makro_szenario.py")
PMI_URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tradingeconomics.com/",
}


def clean_html(html):
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_date_candidates(html):
    text = clean_html(html)

    patterns = [
        r"\b20\d{2}-\d{2}-\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}\b",
    ]

    candidates = []
    seen = set()

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            value = match.group(0)
            key = value.lower()
            if key not in seen:
                seen.add(key)
                start = max(0, match.start() - 180)
                end = min(len(text), match.end() + 180)
                candidates.append((value, text[start:end]))

    return candidates


def find_pmi_contexts(html):
    text = clean_html(html)
    needles = [
        "ISM Services PMI",
        "ISM Non Manufacturing PMI",
        "Non Manufacturing PMI",
        "Services PMI",
        "PMI",
        "54.1",
        "54.10",
    ]

    results = []
    for needle in needles:
        pos = 0
        while True:
            pos = text.lower().find(needle.lower(), pos)
            if pos < 0:
                break
            results.append(
                (
                    needle,
                    text[max(0, pos - 250):min(len(text), pos + 450)],
                )
            )
            pos += len(needle)

    # Deduplicate identical snippets.
    unique = []
    seen = set()
    for needle, context in results:
        key = context
        if key not in seen:
            seen.add(key)
            unique.append((needle, context))
    return unique[:20]


def main():
    print("=== TE ISM SERVICES PMI RELEASE-DATE DIAGNOSE ===")
    print(f"TARGET_FILE={TARGET_FILE}")
    print(f"URL={PMI_URL}")
    print(f"REFERENCE={REFERENCE}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    try:
        source = TARGET_FILE.read_text(encoding="utf-8")
        ast.parse(source)
    except Exception as exc:
        print(f"SOURCE_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_SOURCE")
        return 1

    try:
        response = requests.get(
            PMI_URL,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )
    except Exception as exc:
        print(f"HTTP_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_HTTP")
        return 1

    print(f"HTTP_STATUS={response.status_code}")
    print(f"FINAL_URL={response.url}")
    print(f"CONTENT_TYPE={response.headers.get('content-type', '')}")
    print(f"CONTENT_LENGTH={len(response.text)}")

    html = response.text
    text = clean_html(html)

    print(f"TEXT_LENGTH={len(text)}")
    print(f"REFERENCE_VISIBLE={REFERENCE.lower() in text.lower()}")

    print("")
    print("=== DATE CANDIDATES ===")

    candidates = find_date_candidates(html)

    if not candidates:
        print("DATE_CANDIDATES=0")
    else:
        print(f"DATE_CANDIDATES={len(candidates)}")
        for index, (value, context) in enumerate(candidates[:80], 1):
            print(f"DATE_{index}={value}")
            print(f"CONTEXT_{index}={context}")

    print("")
    print("=== PMI CONTEXTS ===")

    contexts = find_pmi_contexts(html)
    print(f"PMI_CONTEXTS={len(contexts)}")

    for index, (needle, context) in enumerate(contexts, 1):
        print(f"PMI_CONTEXT_{index}_MATCH={needle}")
        print(f"PMI_CONTEXT_{index}={context}")

    print("")
    print("=== EXISTING PRODUCTION PARSER ===")

    spec = importlib.util.spec_from_file_location(
        "macro_under_test",
        TARGET_FILE,
    )

    if spec is None or spec.loader is None:
        print("RESULT=RED_IMPORT_SPEC")
        return 1

    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"IMPORT_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_IMPORT")
        return 1

    parser = getattr(module, "_te_calendar_actual", None)

    if parser is None:
        print("TE_CALENDAR_ACTUAL_FUNCTION=NOT_FOUND")
    else:
        print("TE_CALENDAR_ACTUAL_FUNCTION=FOUND")
        try:
            parsed_value, parsed_release = parser(html, REFERENCE)
            print(f"PRODUCTION_PARSER_VALUE={parsed_value}")
            print(f"PRODUCTION_PARSER_RELEASE_DATE={parsed_release}")
        except Exception as exc:
            print(
                "PRODUCTION_PARSER_ERROR="
                f"{type(exc).__name__}: {exc}"
            )

    print("")
    print("=== RAW HTML PMI/DATE PROBES ===")

    raw_patterns = [
        r"2026-08-05",
        r"2026/08/05",
        r"08/05/2026",
        r"August\s+5,?\s+2026",
        r"5\s+August\s+2026",
        r"54\.1",
        r"54\.10",
    ]

    for pattern in raw_patterns:
        hits = re.findall(pattern, html, flags=re.I)
        print(f"RAW_PATTERN={pattern} HITS={len(hits)}")

    print("")
    print("RESULT=GREEN_DIAGNOSTIC_COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
