#!/usr/bin/env python3
"""
Trading Economics Lithium diagnostic test - phase 2.

Purpose:
- Keep this test completely separate from production code.
- Verify public Trading Economics access without an API key.
- Inspect the public Lithium page for actual historical date/value
  representations in HTML/JavaScript.
- Do NOT modify makro_szenario.py or any production cache.

No pytest is required.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import urllib.request
from pathlib import Path


URLS = (
    "https://de.tradingeconomics.com/commodity/lithium",
    "https://tradingeconomics.com/commodity/lithium",
)

OUTFILE = Path("lithium_tradingeconomics_diagnostic.json")

DATE_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
ISO_DATETIME_RE = re.compile(
    r"\b(20\d{2})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"
)

# Common numeric forms seen in chart/JSON payloads.
NUMBER_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?(?![\w.])")


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, body.decode(charset, errors="replace")


def normalize_number(value: str) -> float | None:
    s = value.strip().replace("\u00a0", "")
    if not s:
        return None

    # 1.234,56 -> 1234.56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    # 134400,5 -> 134400.5
    elif "," in s:
        left, right = s.rsplit(",", 1)
        if len(right) <= 3:
            s = left.replace(",", "") + "." + right
        else:
            s = s.replace(",", "")
    else:
        # 134,400 -> 134400
        if s.count(".") > 1:
            s = s.replace(".", "")
        elif s.count(".") == 1:
            left, right = s.split(".", 1)
            if len(right) == 3 and left.isdigit():
                s = left + right

    try:
        return float(s)
    except ValueError:
        return None


def date_candidates(text: str) -> list[str]:
    found: set[str] = set()

    for y, m, d in DATE_RE.findall(text):
        try:
            value = dt.date(int(y), int(m), int(d))
        except ValueError:
            continue
        found.add(value.isoformat())

    for y, m, d, hh, mm, ss in ISO_DATETIME_RE.findall(text):
        try:
            value = dt.date(int(y), int(m), int(d))
        except ValueError:
            continue
        found.add(value.isoformat())

    return sorted(found)


def context_samples(text: str, needle: str, limit: int = 5) -> list[str]:
    samples = []
    start = 0
    while len(samples) < limit:
        pos = text.lower().find(needle.lower(), start)
        if pos < 0:
            break
        a = max(0, pos - 220)
        b = min(len(text), pos + 420)
        samples.append(re.sub(r"\s+", " ", html.unescape(text[a:b])))
        start = pos + len(needle)
    return samples


def extract_date_value_candidates(text: str) -> list[dict]:
    """
    Look for likely date/value pairs in compact JSON/JavaScript fragments.

    This is deliberately diagnostic rather than a production parser. We record
    candidates only when a date and a numeric value occur close together.
    """
    compact = re.sub(r"\s+", " ", html.unescape(text))
    candidates: list[dict] = []

    # Object-like forms:
    # {"date":"2026-09-24","value":134400}
    # {"Date":"2026-09-24","Value":134400}
    object_patterns = [
        re.compile(
            r'["\'](?:date|Date|datetime|Datetime|timestamp|Timestamp)["\']\s*:\s*'
            r'["\'](20\d{2}-\d{2}-\d{2})(?:T[^"\']*)?["\'][^{}]{0,500}?'
            r'["\'](?:value|Value|price|Price|close|Close)["\']\s*:\s*'
            r'["\']?(-?\d[\d.,]*)["\']?',
            re.I,
        ),
        re.compile(
            r'["\'](?:value|Value|price|Price|close|Close)["\']\s*:\s*'
            r'["\']?(-?\d[\d.,]*)["\']?[^{}]{0,500}?'
            r'["\'](?:date|Date|datetime|Datetime|timestamp|Timestamp)["\']\s*:\s*'
            r'["\'](20\d{2}-\d{2}-\d{2})(?:T[^"\']*)?["\']',
            re.I,
        ),
    ]

    for pattern in object_patterns:
        for match in pattern.finditer(compact):
            groups = match.groups()
            if len(groups) != 2:
                continue
            if groups[0].startswith("20"):
                date_s, number_s = groups
            else:
                number_s, date_s = groups
            number = normalize_number(number_s)
            if number is None:
                continue
            candidates.append(
                {"date": date_s, "value": number, "raw_value": number_s}
            )

    # Array-like forms: ["2026-09-24", 134400]
    array_pattern = re.compile(
        r'[\["\'](20\d{2}-\d{2}-\d{2})(?:T[^"\']*)?["\']\s*,\s*'
        r'["\']?(-?\d[\d.,]*)["\']?[\]"\' ]',
        re.I,
    )
    for match in array_pattern.finditer(compact):
        number = normalize_number(match.group(2))
        if number is not None:
            candidates.append(
                {
                    "date": match.group(1),
                    "value": number,
                    "raw_value": match.group(2),
                }
            )

    # Deduplicate.
    unique = {}
    for item in candidates:
        unique[(item["date"], item["value"])] = item

    return sorted(unique.values(), key=lambda x: (x["date"], x["value"]))


def main() -> int:
    print("=== TRADING ECONOMICS LITHIUM DIAGNOSTIC - PHASE 2 ===")
    print("Purpose: inspect public HTML/JavaScript for historical Lithium data")
    print("API KEY: NOT USED")
    print()

    results = []

    for url in URLS:
        print(f"URL: {url}")

        try:
            status, body = fetch(url)
        except Exception as exc:
            print(f"HTTP_ERROR: {type(exc).__name__}: {exc}")
            results.append(
                {"url": url, "status": None, "error": str(exc)}
            )
            print()
            continue

        print(f"HTTP_STATUS: {status}")
        print(f"CONTENT_LENGTH: {len(body)}")

        decoded = html.unescape(body)
        lower = decoded.lower()

        lithium_present = "lithium" in lower
        unit_present = "cny/t" in lower or "cny / t" in lower

        print(f"LITHIUM_TEXT_PRESENT: {'YES' if lithium_present else 'NO'}")
        print(f"CNY/T_PRESENT: {'YES' if unit_present else 'NO'}")

        dates = date_candidates(decoded)
        pairs = extract_date_value_candidates(decoded)

        print(f"ISO_DATE_COUNT: {len(dates)}")
        if dates:
            print(f"ISO_DATE_RANGE: {dates[0]} -> {dates[-1]}")
        else:
            print("ISO_DATE_RANGE: NONE")

        print(f"DATE_VALUE_CANDIDATES: {len(pairs)}")

        if pairs:
            print(f"PAIR_DATE_RANGE: {pairs[0]['date']} -> {pairs[-1]['date']}")
            print("OLDEST_5_PAIRS:")
            for item in pairs[:5]:
                print(f"  {item['date']} | {item['value']}")
            print("NEWEST_5_PAIRS:")
            for item in pairs[-5:]:
                print(f"  {item['date']} | {item['value']}")
        else:
            print("NO_DIRECT_DATE_VALUE_PAIRS_FOUND")

        samples = {
            "lithium": context_samples(decoded, "lithium", 3),
            "cny_t": context_samples(decoded, "CNY/T", 3),
            "cny_slash_t": context_samples(decoded, "CNY / T", 3),
        }

        results.append(
            {
                "url": url,
                "status": status,
                "content_length": len(body),
                "lithium_present": lithium_present,
                "cny_t_present": unit_present,
                "iso_dates_count": len(dates),
                "iso_date_min": dates[0] if dates else None,
                "iso_date_max": dates[-1] if dates else None,
                "date_value_candidates_count": len(pairs),
                "date_value_candidates": pairs[:100],
                "context_samples": samples,
            }
        )
        print()

    payload = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "api_key_used": False,
        "production_files_modified": False,
        "results": results,
    }

    OUTFILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"DIAGNOSTIC_FILE: {OUTFILE}")

    successful = [r for r in results if r.get("status") == 200]
    pair_found = any(r.get("date_value_candidates_count", 0) > 0 for r in results)

    if not successful:
        print("RESULT: FAIL - no Trading Economics endpoint returned HTTP 200")
        return 1

    print("RESULT: PASS - public endpoint reachable")
    print(
        "HISTORICAL_PAIR_EXTRACTION: "
        + ("FOUND" if pair_found else "NOT_FOUND")
    )
    print("NOTE: NOT_FOUND does not prove that historical data are unavailable;")
    print("      it means this diagnostic did not yet identify date/value pairs")
    print("      in the public HTML/JavaScript representation.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
