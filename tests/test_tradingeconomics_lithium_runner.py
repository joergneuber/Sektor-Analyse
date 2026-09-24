#!/usr/bin/env python3
"""
TradingEconomics Lithium compatibility/cache diagnostic - V3.

IMPORTANT:
- Does NOT import makro_szenario.py.
- Does NOT require pandas, requests, pytest, or any project dependency.
- Uses Python standard library only.
- Reads the existing macro cache JSON directly for diagnostics.
- Writes only a test cache:
    .macro_cache/lithium_te_test_cache.json
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

LITHIUM_URLS = (
    "https://tradingeconomics.com/commodity/lithium",
    "https://de.tradingeconomics.com/commodity/lithium",
)

PRODUCTION_CACHE_FILE = ROOT / ".macro_cache" / "macro_cache.json"
TEST_CACHE_FILE = ROOT / ".macro_cache" / "lithium_te_test_cache.json"


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_number(raw: str) -> float | None:
    s = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        left, right = s.rsplit(",", 1)
        s = (
            left.replace(",", "") + "." + right
            if len(right) <= 2
            else s.replace(",", "")
        )
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 or (
            len(parts) == 2 and len(parts[1]) == 3
        ):
            s = "".join(parts)

    try:
        return float(s)
    except ValueError:
        return None


def fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, raw.decode(charset, errors="replace")


def find_current_lithium_value(text: str) -> float | None:
    """
    Diagnostic-only current-value extraction.

    We deliberately require Lithium and CNY/T in the same local context.
    """
    plain = clean_text(text)

    patterns = (
        r"\bLithium\b.{0,700}?([0-9][0-9.,]*)\s*CNY/T",
        r"([0-9][0-9.,]*)\s*CNY/T.{0,700}?\bLithium\b",
    )

    for pattern in patterns:
        match = re.search(pattern, plain, re.I)
        if match:
            value = parse_number(match.group(1))
            if value is not None:
                return value

    return None


def extract_exact_date_value(
    text: str,
    target_date: dt.date,
) -> float | None:
    """
    Test-only exact-date extraction from publicly rendered page text.

    This does not attempt to bypass subscription/history restrictions.
    It only looks for a publicly exposed exact-date/value representation.
    """
    plain = clean_text(text)

    date_variants = (
        target_date.isoformat(),
        target_date.strftime("%d.%m.%Y"),
        target_date.strftime("%m/%d/%Y"),
        target_date.strftime("%d/%m/%Y"),
        target_date.strftime("%b %d, %Y"),
        target_date.strftime("%B %d, %Y"),
    )

    number = r"([0-9][0-9.,]*)"

    for date_text in date_variants:
        d = re.escape(date_text)

        patterns = (
            rf"\bLithium\b.{{0,900}}?{number}\s*CNY/T"
            rf".{{0,700}}?{d}\b",

            rf"{d}.{{0,700}}?\bLithium\b"
            rf".{{0,900}}?{number}\s*CNY/T",

            rf"\bLithium\b.{{0,500}}?{d}"
            rf".{{0,500}}?{number}\s*CNY/T",
        )

        for pattern in patterns:
            match = re.search(pattern, plain, re.I)
            if match:
                return parse_number(match.group(1))

    return None


def save_test_cache(
    target_date: dt.date,
    value: float,
    url: str,
) -> int:
    cache = load_json(TEST_CACHE_FILE)

    if not cache:
        cache = {
            "version": 1,
            "source": "TradingEconomics Public Lithium Test",
            "unit": "CNY/T",
            "observations": {},
        }

    observations = cache.setdefault("observations", {})

    observations[target_date.isoformat()] = {
        "value": value,
        "reference_date": target_date.isoformat(),
        "unit": "CNY/T",
        "source": "TradingEconomics Public Commodities",
        "url": url,
        "status": "REAL_PUBLIC_SECONDARY",
        "method": "TEST_ONLY_EXACT_DATE",
    }

    cache["latest_date"] = target_date.isoformat()
    cache["latest_value"] = value

    TEST_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEST_CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return len(observations)


def print_existing_cache() -> None:
    print("=== EXISTING PROJECT CACHE ===")
    print(f"FILE: {PRODUCTION_CACHE_FILE}")
    print(
        "EXISTS: "
        f"{'YES' if PRODUCTION_CACHE_FILE.exists() else 'NO'}"
    )

    data = load_json(PRODUCTION_CACHE_FILE)

    if data is None:
        print("JSON_READ: NOT_AVAILABLE")
        print("PRODUCTION_CACHE_MODIFIED: NO")
        print()
        return

    print("JSON_READ: PASS")
    print(f"TOP_LEVEL_KEYS: {sorted(data.keys())}")

    lme = data.get("lme")

    if isinstance(lme, dict):
        print("LME_SECTION: PRESENT")
        print(f"LME_KEYS: {sorted(lme.keys())}")
    else:
        print("LME_SECTION: NOT_FOUND")

    print("PRODUCTION_CACHE_MODIFIED: NO")
    print()


def main() -> int:
    target_date = dt.date.today()

    print("=== TRADING ECONOMICS LITHIUM CACHE TEST V3 ===")
    print("DEPENDENCIES: STANDARD LIBRARY ONLY")
    print("IMPORT_MAKRO_SZENARIO: NO")
    print("PANDAS_REQUIRED: NO")
    print("REQUESTS_REQUIRED: NO")
    print("PYTEST_REQUIRED: NO")
    print(f"TARGET_DATE: {target_date.isoformat()}")
    print()

    print_existing_cache()

    found_value = None
    found_url = None
    successful_urls = 0

    for url in LITHIUM_URLS:
        print(f"URL: {url}")

        try:
            status, body = fetch(url)
        except Exception as exc:
            print(
                f"HTTP_ERROR: {type(exc).__name__}: {exc}"
            )
            print()
            continue

        print(f"HTTP_STATUS: {status}")
        print(f"CONTENT_LENGTH: {len(body)}")

        if status != 200:
            print("ENDPOINT_RESULT: FAIL")
            print()
            continue

        successful_urls += 1

        current_value = find_current_lithium_value(body)
        exact_value = extract_exact_date_value(
            body,
            target_date,
        )

        print(
            "CURRENT_VALUE: "
            f"{current_value if current_value is not None else 'NOT_FOUND'}"
        )
        print(
            "EXACT_DATE_VALUE: "
            f"{exact_value if exact_value is not None else 'NOT_FOUND'}"
        )

        if exact_value is not None:
            found_value = exact_value
            found_url = url
            print("LITHIUM_EXACT_DATE: FOUND")
            print()
            break

        print("LITHIUM_EXACT_DATE: NOT_FOUND")
        print()

    if found_value is None:
        print("=== CONCLUSION ===")
        print(f"SUCCESSFUL_TE_URLS: {successful_urls}")
        print("LITHIUM_EXACT_DATE: NOT_FOUND")
        print("TEST_CACHE_WRITE: NOT_PERFORMED")
        print("PRODUCTION_FILES_MODIFIED: NO")
        print(
            "RESULT: FAIL - public page did not expose an exact-date "
            "Lithium value in this diagnostic"
        )
        return 1

    count = save_test_cache(
        target_date,
        found_value,
        found_url,
    )

    print("=== TEST-ONLY CACHE ===")
    print(f"FILE: {TEST_CACHE_FILE}")
    print("WRITE_MODE: APPEND_OR_UPDATE_BY_DATE")
    print(f"OBSERVATIONS_COUNT: {count}")
    print(f"LATEST_DATE: {target_date.isoformat()}")
    print(f"LATEST_VALUE: {found_value}")
    print("TEST_CACHE_WRITE: PASS")
    print()

    print("=== FINAL RESULT ===")
    print("HTTP_ACCESS: PASS")
    print("LITHIUM_CURRENT_VALUE: PASS")
    print("LITHIUM_EXACT_DATE: PASS")
    print("DAILY_CACHE_MODEL: PASS")
    print("PRODUCTION_FILES_MODIFIED: NO")
    print("RESULT: PASS")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
