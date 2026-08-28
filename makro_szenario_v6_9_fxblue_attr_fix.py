#!/usr/bin/env python3
"""
FX Blue ISM Services – isolierter REAL-HTML-Parser-Test v3

Zweck:
- KEINE Produktivdatei ändern.
- KEIN Gate/Cache/LME/FRED/Manufacturing.
- Prüft ausschließlich, ob die von FX Blue tatsächlich gelieferten
  HTML-Seiten die vier Services-Actual-Werte sauber enthalten.
- Nutzt bewusst den bereits im GitHub-Log nachgewiesenen DOM-Bereich
  "Most recent" / MetricsBoxActual, statt hypothetische API-Endpunkte
  oder WebSockets zu erraten.

Zielmonat:
    2026-07 (ISM-Bericht Juli, veröffentlicht am 05.08.2026)

Erwartung:
    PMI             -> Actual vorhanden
    New Orders      -> Actual vorhanden
    Employment      -> Actual vorhanden
    Prices Paid     -> Actual vorhanden

Fail-closed:
- kein Actual => FAIL
- "-" / N/A / null / leer => FAIL
- Forecast/Previous allein => FAIL
- data-value allein => FAIL
"""

import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


TARGET_YEAR = 2026
TARGET_MONTH = 7

BASE = "https://publisher2.fxblue.com/calendar/item/"

EVENTS = {
    "pmi": "ISM_Services_PMI_US",
    "new_orders": "ISM_Services_New_Orders_Index_US",
    "employment": "ISM_Services_Employment_Index_US",
    "prices": "ISM_Services_Prices_Paid_US",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_actual(raw):
    """Strikt: akzeptiert nur einen einzelnen numerischen Actual-Wert."""
    if raw is None:
        return None

    if isinstance(raw, dict):
        for key in ("actual", "actualValue", "value", "text"):
            if key in raw:
                raw = raw[key]
                break
        else:
            return None

    s = str(raw).strip()
    if not s or s.lower() in {"null", "none", "n/a", "na", "-", "--"}:
        return None

    # Keine freie Zahlensuche in beliebigem Text:
    # "Forecast 52 Previous 51" darf niemals als Actual gelten.
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%?", s):
        return None

    try:
        return float(s.rstrip("%"))
    except ValueError:
        return None


def parse_most_recent(html):
    soup = BeautifulSoup(html, "html.parser")

    # Der reale FX-Blue-Response enthält:
    # <h2>Most recent - <span class="CalendarDate" ts="...">...</span></h2>
    # direkt gefolgt von .MetricsBar mit Previous/Revised/Forecast/Actual.
    date_node = soup.select_one("span.CalendarDate")
    date_text = date_node.get_text(" ", strip=True) if date_node else None

    actual_node = soup.select_one(".MetricsBoxActual .MetricsBoxValue")
    forecast_node = soup.select_one(".MetricsBoxForecast .MetricsBoxValue")
    previous_node = soup.select_one(".MetricsBoxPrevious .MetricsBoxValue")

    actual_text = actual_node.get_text(" ", strip=True) if actual_node else None
    forecast_text = forecast_node.get_text(" ", strip=True) if forecast_node else None
    previous_text = previous_node.get_text(" ", strip=True) if previous_node else None

    actual = clean_actual(actual_text)

    return {
        "date": date_text,
        "actual_raw": actual_text,
        "actual": actual,
        "forecast_raw": forecast_text,
        "previous_raw": previous_text,
    }


def date_matches_target_release(date_text):
    if not date_text:
        return False

    # Erwartet für Juli 2026: Veröffentlichung am 05.08.2026.
    # Wir akzeptieren das reale Release-Datum, aber NICHT blind irgendeinen
    # August-Eintrag: Monat/Jahr werden aus dem Zielmonat abgeleitet.
    expected_release_year = TARGET_YEAR
    expected_release_month = TARGET_MONTH + 1

    if expected_release_month == 13:
        expected_release_year += 1
        expected_release_month = 1

    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
        date_text,
    )
    if not m:
        return False

    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))

    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month = month_map.get(month_name)
    return (
        year == expected_release_year
        and month == expected_release_month
        and day > 0
    )


def fetch(kind, event_id):
    url = urljoin(BASE, event_id)
    print(f"\n=== {kind.upper()} ===")
    print(f"URL={url}")

    r = requests.get(url, headers=HEADERS, timeout=20)
    print(f"HTTP_STATUS={r.status_code}")
    print(f"FINAL_URL={r.url}")
    print(f"CONTENT_LENGTH={len(r.text)}")

    if r.status_code != 200:
        return False, {"error": f"HTTP {r.status_code}"}

    parsed = parse_most_recent(r.text)

    print(f"MOST_RECENT={parsed['date']}")
    print(f"PREVIOUS={parsed['previous_raw']!r}")
    print(f"FORECAST={parsed['forecast_raw']!r}")
    print(f"ACTUAL_RAW={parsed['actual_raw']!r}")
    print(f"ACTUAL_PARSED={parsed['actual']!r}")
    print(f"RELEASE_MATCH_TARGET_2026_07={date_matches_target_release(parsed['date'])}")

    ok = (
        date_matches_target_release(parsed["date"])
        and parsed["actual"] is not None
    )

    print("RESULT=GREEN" if ok else "RESULT=RED")
    return ok, parsed


def synthetic_fail_closed_tests():
    print("\n=== SYNTHETISCHE FAIL-CLOSED TESTS ===")
    cases = {
        "Actual: 52.4": ("52.4", 52.4),
        "Actual: +52.4": ("+52.4", 52.4),
        "Actual: -52.4": ("-52.4", -52.4),
        "Actual: 0": ("0", 0.0),
        "Actual: null": ("null", None),
        "Actual: None": ("None", None),
        "data-value=52.4": ("52.4 Forecast 51 Previous 50", None),
        "Forecast/Previous ohne Actual": ("Forecast: 52.0 | Previous: 51.7", None),
        "53.4* strikt": ("53.4*", None),
        "N/A": ("N/A", None),
        "-": ("-", None),
    }

    all_ok = True
    for name, (raw, expected) in cases.items():
        got = clean_actual(raw)
        ok = got == expected
        all_ok &= ok
        print(f"{name:35} got={got!r:8} expected={expected!r:8} "
              f"{'GREEN' if ok else 'RED'}")

    return all_ok


def main():
    print("=== FXBLUE ISM SERVICES REAL-HTML TEST v3 ===")
    print(f"TARGET_REFERENCE={TARGET_YEAR:04d}-{TARGET_MONTH:02d}")
    print("MODE=ISOLATED / NO PRODUCTIVE LOGIC")

    synthetic_ok = synthetic_fail_closed_tests()

    real_results = {}
    for kind, event_id in EVENTS.items():
        try:
            ok, data = fetch(kind, event_id)
        except Exception as exc:
            ok, data = False, {"error": f"{type(exc).__name__}: {exc}"}
            print(f"RESULT=RED ERROR={data['error']}")
        real_results[kind] = ok

    print("\n=== GESAMTERGEBNIS ===")
    print(f"SYNTHETIC_FAIL_CLOSED={'GREEN' if synthetic_ok else 'RED'}")
    for kind, ok in real_results.items():
        print(f"REAL_{kind.upper()}={'GREEN' if ok else 'RED'}")

    all_ok = synthetic_ok and all(real_results.values())
    print(f"FINAL_RESULT={'GREEN' if all_ok else 'RED'}")

    # Kein künstliches Erfolgssignal: Exit 1 bei irgendeinem Fehlschlag.
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
