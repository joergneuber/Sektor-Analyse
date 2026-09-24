#!/usr/bin/env python3
"""
TradingEconomics Lithium compatibility/cache diagnostic.

Production code is not modified. This test imports makro_szenario only to
inspect its existing cache environment and then performs a TEST-ONLY Lithium
exact-date extraction plus a separate test cache write.
"""

from __future__ import annotations
import datetime as dt
import json
import re
import sys
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import makro_szenario as macro

LITHIUM_URLS = (
    "https://tradingeconomics.com/commodity/lithium",
    "https://de.tradingeconomics.com/commodity/lithium",
)
TEST_CACHE_FILE = ROOT / ".macro_cache" / "lithium_te_test_cache.json"

def clean_text(text):
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def parse_number(raw):
    s = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        left, right = s.rsplit(",", 1)
        s = left.replace(",", "") + "." + right if len(right) <= 2 else s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
            s = "".join(parts)
    try:
        return float(s)
    except ValueError:
        return None

def load_test_cache():
    if TEST_CACHE_FILE.exists():
        try:
            data = json.loads(TEST_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"version": 1, "source": "TradingEconomics Public Lithium Test", "observations": {}}

def save_test_cache(data):
    TEST_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEST_CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def existing_cache_diagnostic():
    print("=== EXISTING PROJECT CACHE DIAGNOSTIC ===")
    print(f"MACRO_CACHE_FILE: {macro.MACRO_CACHE_FILE}")
    print(f"MACRO_CACHE_EXISTS: {'YES' if macro.MACRO_CACHE_FILE.exists() else 'NO'}")
    try:
        cache = macro._cache_load()
    except Exception as exc:
        print(f"MACRO_CACHE_LOAD: ERROR {type(exc).__name__}: {exc}")
        return
    print(f"MACRO_CACHE_TOP_LEVEL_KEYS: {sorted(cache.keys())}")
    lme = cache.get("lme")
    print(f"LME_CACHE_PRESENT: {'YES' if isinstance(lme, dict) else 'NO'}")
    if isinstance(lme, dict):
        print(f"LME_CACHE_KEYS: {sorted(lme.keys())}")
        if isinstance(lme.get("data"), dict):
            print(f"LME_CACHE_DATA_METALS: {sorted(lme['data'].keys())}")
        direct = [k for k, v in lme.items() if isinstance(v, dict) and isinstance(v.get("data"), dict)]
        if direct:
            print(f"LME_CACHE_DIRECT_METALS: {sorted(direct)}")
    print("PRODUCTION_CACHE_MODIFIED_BY_THIS_TEST: NO")
    print()

def fetch(url):
    response = requests.get(
        url, timeout=20,
        headers={**getattr(macro, "REQUEST_HEADERS", {}),
                 "Accept": "text/html,application/xhtml+xml",
                 "Accept-Language": "en-US,en;q=0.9,de;q=0.8"},
        allow_redirects=True)
    return response.status_code, response.text

def extract_lithium_exact_date(html_text, target_date):
    """Test-only equivalent of the existing TE public exact-date pattern."""
    plain = clean_text(html_text)
    dates = (
        target_date.strftime("%B %d, %Y"),
        target_date.strftime("%b %d, %Y"),
        target_date.strftime("%d.%m.%Y"),
        target_date.isoformat(),
    )
    number = r"([0-9][0-9.,]*)"
    for date_text in dates:
        d = re.escape(date_text)
        patterns = (
            rf"\bLithium\b.{{0,900}}?{number}\s*CNY/T.{{0,500}}?\b(?:on|as of|for)\s+{d}\b",
            rf"\bLithium\b.{{0,900}}?{number}\s*CNY/T.{{0,500}}?{d}\b",
            rf"{d}.{{0,500}}?\bLithium\b.{{0,900}}?{number}\s*CNY/T",
        )
        for pattern in patterns:
            m = re.search(pattern, plain, re.I)
            if m:
                value = parse_number(m.group(1))
                if value is not None:
                    return {
                        "value": value, "reference_date": target_date.isoformat(),
                        "status": "REAL_PUBLIC_SECONDARY",
                        "source": "TradingEconomics Public Commodities",
                        "datatype": "TE_PUBLIC_LITHIUM",
                        "method": "TEST_ONLY_NARRATIVE_EXACT_DATE",
                        "unit": "CNY/T",
                    }
    try:
        import pandas as pd
        from io import StringIO
        frames = pd.read_html(StringIO(html_text))
    except Exception:
        frames = []
    markers = {
        target_date.isoformat(), target_date.strftime("%d.%m.%Y"),
        target_date.strftime("%m/%d/%Y"), target_date.strftime("%d/%m/%Y"),
        target_date.strftime("%b %d, %Y"), target_date.strftime("%B %d, %Y")
    }
    for ti, frame in enumerate(frames):
        if frame.empty:
            continue
        columns = [str(c).strip().lower() for c in frame.columns]
        price_idx = next((i for i,c in enumerate(columns) if c == "price" or c.endswith("| price")), None)
        for ri, row in frame.fillna("").astype(str).iterrows():
            cells = [str(x).strip() for x in row.tolist()]
            row_text = " | ".join(cells)
            if not re.search(r"\blithium\b", row_text, re.I):
                continue
            if not any(x.lower() in row_text.lower() for x in markers):
                continue
            value = parse_number(cells[price_idx]) if price_idx is not None and price_idx < len(cells) else None
            if value is None:
                nums = [parse_number(c) for c in cells[1:]]
                nums = [n for n in nums if n is not None and 1000 <= n <= 500000]
                if len(nums) == 1:
                    value = nums[0]
            if value is not None:
                return {
                    "value": value, "reference_date": target_date.isoformat(),
                    "status": "REAL_PUBLIC_SECONDARY",
                    "source": "TradingEconomics Public Commodities",
                    "datatype": "TE_PUBLIC_LITHIUM",
                    "method": "TEST_ONLY_PANDAS_READ_HTML_EXACT_DATE",
                    "table_index": ti, "row_index": int(ri),
                    "row": cells, "unit": "CNY/T",
                }
    return None

def main():
    target = dt.date.today()
    print("=== TRADING ECONOMICS LITHIUM CACHE COMPATIBILITY TEST ===")
    print(f"TARGET_DATE: {target.isoformat()}")
    print()
    existing_cache_diagnostic()

    found = None
    successful = 0
    for url in LITHIUM_URLS:
        print(f"URL: {url}")
        try:
            status, body = fetch(url)
        except Exception as exc:
            print(f"HTTP_ERROR: {type(exc).__name__}: {exc}")
            continue
        print(f"HTTP_STATUS: {status}")
        print(f"CONTENT_LENGTH: {len(body)}")
        if status != 200:
            print("SKIP: endpoint did not return HTTP 200")
            print()
            continue
        successful += 1
        result = extract_lithium_exact_date(body, target)
        if result:
            result["url"] = url
            found = result
            print("LITHIUM_EXACT_DATE: FOUND")
            print(f"VALUE: {result['value']}")
            print(f"UNIT: {result['unit']}")
            print(f"METHOD: {result['method']}")
            print()
            break
        print("LITHIUM_EXACT_DATE: NOT_FOUND")
        print()

    if found:
        cache = load_test_cache()
        observations = cache.setdefault("observations", {})
        observations[target.isoformat()] = found
        cache["latest_date"] = target.isoformat()
        cache["latest_value"] = found["value"]
        cache["unit"] = found["unit"]
        save_test_cache(cache)
        print("=== TEST-ONLY LITHIUM CACHE ===")
        print(f"TEST_CACHE_FILE: {TEST_CACHE_FILE}")
        print("WRITE_MODE: APPEND_OR_UPDATE_BY_DATE")
        print(f"OBSERVATIONS_COUNT: {len(observations)}")
        print(f"LATEST_DATE: {cache['latest_date']}")
        print(f"LATEST_VALUE: {cache['latest_value']}")
        print("CACHE_WRITE: PASS")
        print()
        print("=== CONCLUSION ===")
        print("EXISTING_TE_PUBLIC_PATTERN: COMPATIBLE")
        print("LITHIUM_EXACT_DATE: PASS")
        print("DAILY_CACHE_MODEL: PASS")
        print("PRODUCTION_FILES_MODIFIED: NO")
        print("RESULT: PASS")
        return 0

    print("=== CONCLUSION ===")
    print("EXISTING_TE_PUBLIC_PATTERN: AVAILABLE")
    print("LITHIUM_EXACT_DATE: NOT_FOUND")
    print("DAILY_CACHE_MODEL: NOT_TESTED")
    print("PRODUCTION_FILES_MODIFIED: NO")
    print(f"SUCCESSFUL_TE_URLS: {successful}")
    print("RESULT: FAIL - no exact-date Lithium observation found")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
