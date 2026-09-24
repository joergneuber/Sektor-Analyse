#!/usr/bin/env python3
"""Diagnostic-only test for public TradingEconomics Lithium data.

No project production code is modified or imported.
No TradingEconomics API key is used.
Exit code 0 means the diagnostic completed. Current-page access is reported
as PASS/FAIL; historical availability is reported separately and does not
silently become a production assumption.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

URLS = [
    "https://de.tradingeconomics.com/commodity/lithium",
    "https://tradingeconomics.com/commodity/lithium",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Sektor-Analyse-Lithium-Diagnostic/1.0)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
TIMEOUT = 20

# Dates chosen to answer the actual history question without assuming that
# every day must exist (weekends/holidays are not trading observations).
PROBE_YEARS = list(range(2017, dt.date.today().year + 1))


def fetch(url: str) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, raw.decode(charset, errors="replace"), response.geturl()


def clean_text(source: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", source, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def extract_current_value(text: str) -> dict:
    # Keep this deliberately diagnostic. We require Lithium and a plausible
    # CNY/T token in the same local area instead of grabbing an arbitrary number.
    patterns = [
        r"Lithium.{0,500}?([0-9][0-9.,]*)\s*CNY\s*/?\s*T",
        r"Lithium.{0,500}?([0-9][0-9.,]*)\s*CNY/T",
        r"([0-9][0-9.,]*)\s*CNY\s*/?\s*T.{0,500}?Lithium",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            raw = m.group(1)
            return {"value_raw": raw, "unit": "CNY/T", "method": "local_text_pattern"}
    return {}


def find_year_evidence(source: str, text: str) -> dict[int, dict]:
    """Find evidence that a historical year occurs in the public page.

    This is NOT claimed to be a complete historical series. It is deliberately
    diagnostic: a year is only marked as present when the source contains a
    year token near Lithium/price/chart-related content.
    """
    result = {}
    source_lower = source.lower()
    for year in PROBE_YEARS:
        token = str(year)
        candidates = []
        for m in re.finditer(re.escape(token), source_lower):
            start = max(0, m.start() - 900)
            end = min(len(source_lower), m.end() + 900)
            chunk = source_lower[start:end]
            if "lithium" in chunk or "commodity/lithium" in chunk or "series" in chunk:
                candidates.append(m.start())
                if len(candidates) >= 3:
                    break
        if candidates:
            result[year] = {
                "evidence": "year token near lithium/series content",
                "matches": len(candidates),
            }
    return result


def extract_iso_dates(source: str) -> list[str]:
    dates = set(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", source))
    return sorted(dates)


def main() -> int:
    print("=== TRADING ECONOMICS LITHIUM DIAGNOSTIC ===")
    print("Mode: public website only; NO API key; NO production code changes")
    print(f"Today (runner UTC): {dt.datetime.now(dt.timezone.utc).date().isoformat()}")
    print()

    successful = []
    all_years: dict[int, dict] = {}
    iso_dates: set[str] = set()
    current_hits = []

    for url in URLS:
        print(f"URL: {url}")
        try:
            status, source, final_url = fetch(url)
            print(f"HTTP_STATUS: {status}")
            print(f"FINAL_URL: {final_url}")
            print(f"CONTENT_LENGTH: {len(source)}")
            if status != 200:
                print("RESULT: FAIL (HTTP)")
                print()
                continue

            successful.append(url)
            text = clean_text(source)
            print(f"LITHIUM_TEXT_PRESENT: {'YES' if 'lithium' in text.lower() else 'NO'}")
            current = extract_current_value(text)
            if current:
                current_hits.append((url, current))
                print(f"CURRENT_VALUE_RAW: {current['value_raw']}")
                print(f"CURRENT_UNIT: {current['unit']}")
                print(f"CURRENT_METHOD: {current['method']}")
            else:
                print("CURRENT_VALUE: NOT_FOUND_WITH_SAFE_PATTERN")

            years = find_year_evidence(source, text)
            for year, evidence in years.items():
                all_years.setdefault(year, {"urls": []})["urls"].append(url)
                all_years[year]["evidence"] = evidence["evidence"]

            dates = extract_iso_dates(source)
            iso_dates.update(dates)
            print(f"ISO_DATES_FOUND_IN_SOURCE: {len(dates)}")
            if dates:
                print(f"ISO_DATE_RANGE_IN_SOURCE: {dates[0]} -> {dates[-1]}")
            print("RESULT: PASS (page reachable)")
        except urllib.error.HTTPError as exc:
            print(f"RESULT: FAIL (HTTPError {exc.code})")
        except urllib.error.URLError as exc:
            print(f"RESULT: FAIL (URL/network: {exc.reason})")
        except Exception as exc:
            print(f"RESULT: FAIL ({type(exc).__name__}: {exc})")
        print()

    print("=== HISTORICAL DIAGNOSTIC ===")
    if all_years:
        years = sorted(all_years)
        print("YEARS_WITH_PAGE_EVIDENCE:", ", ".join(map(str, years)))
        print(f"OLDEST_YEAR_WITH_PAGE_EVIDENCE: {years[0]}")
        print(f"NEWEST_YEAR_WITH_PAGE_EVIDENCE: {years[-1]}")
    else:
        print("YEARS_WITH_PAGE_EVIDENCE: NONE")

    if iso_dates:
        dates = sorted(iso_dates)
        print(f"ISO_DATE_RANGE: {dates[0]} -> {dates[-1]}")
    else:
        print("ISO_DATE_RANGE: NONE_FOUND")

    print()
    print("=== INTERPRETATION ===")
    if current_hits:
        print("CURRENT_PRICE: FOUND")
    else:
        print("CURRENT_PRICE: NOT_CONFIRMED")
    if all_years:
        print("HISTORICAL_ACCESS: PAGE_CONTAINS_HISTORICAL_YEAR_EVIDENCE")
        print("IMPORTANT: This does NOT prove a complete daily history.")
    else:
        print("HISTORICAL_ACCESS: NOT_PROVEN_BY_PUBLIC_PAGE")
    print("API_KEY_REQUIRED_FOR_THIS_TEST: NO")
    print("PRODUCTION_FILES_CHANGED: NO")

    report = {
        "urls_successful": successful,
        "current_hits": [{"url": u, **v} for u, v in current_hits],
        "years_with_page_evidence": sorted(all_years),
        "iso_date_count": len(iso_dates),
        "iso_date_range": [min(iso_dates), max(iso_dates)] if iso_dates else None,
        "api_key_used": False,
        "production_files_changed": False,
    }
    out = Path("lithium_tradingeconomics_diagnostic.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT_FILE: {out}")

    # Only infrastructure reachability is a hard failure. Historical coverage
    # is intentionally diagnostic because the page may expose chart data in JS
    # without exposing a complete public HTML table.
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
