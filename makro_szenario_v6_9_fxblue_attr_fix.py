import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

URLS = {
    "pmi": "https://publisher2.fxblue.com/calendar/item/ISM_Services_PMI_US",
    "new_orders": "https://publisher2.fxblue.com/calendar/item/ISM_Services_New_Orders_Index_US",
    "employment": "https://publisher2.fxblue.com/calendar/item/ISM_Services_Employment_Index_US",
    "prices": "https://publisher2.fxblue.com/calendar/item/ISM_Services_Prices_Paid_US",
}

TARGET_YEAR = 2026
TARGET_MONTH = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://publisher2.fxblue.com/",
}


def clean_actual(value):
    if value is None:
        return None

    s = str(value).strip()

    if s.lower() in {
        "", "n/a", "na", "null", "none",
        "-", "--", "—", "–"
    }:
        return None

    # Strikt: der komplette Wert muss numerisch sein.
    if not re.fullmatch(r"[+-]?\s*\d+(?:[.,]\d+)?%?", s):
        return None

    s = s.replace(" ", "").replace("%", "").replace(",", ".")

    try:
        return float(s)
    except ValueError:
        return None


def parse_fxblue_page(html):
    soup = BeautifulSoup(html, "html.parser")

    matches = []

    for row in soup.select(".PastEventRow"):
        # Header-Zeile überspringen
        if "PastEventHeader" in row.get("class", []):
            continue

        date_node = row.select_one(".PastEventDate")
        actual_node = row.select_one(".PastEventActual")

        if not date_node or not actual_node:
            continue

        date_text = date_node.get_text(" ", strip=True)
        raw_actual = actual_node.get_text(" ", strip=True)

        actual = clean_actual(raw_actual)

        matches.append({
            "date": date_text,
            "raw_actual": raw_actual,
            "actual": actual,
        })

    return matches


print("=" * 80)
print("FXBLUE DIRECT DOM PARSER TEST")
print("=" * 80)
print(f"TARGET={TARGET_YEAR}-{TARGET_MONTH:02d}")
print()

session = requests.Session()
session.headers.update(HEADERS)

failures = 0
successful_values = {}

for kind, url in URLS.items():

    print("-" * 80)
    print(f"KIND={kind}")
    print(f"URL={url}")
    print("-" * 80)

    try:
        r = session.get(url, timeout=30)

        print(f"HTTP_STATUS={r.status_code}")
        print(f"FINAL_URL={r.url}")
        print(f"CONTENT_LENGTH={len(r.text)}")

        if r.status_code != 200:
            print("RESULT=FAIL_HTTP")
            failures += 1
            continue

        events = parse_fxblue_page(r.text)

        print(f"PAST_EVENT_ROWS={len(events)}")

        target_events = []

        for event in events:
            print(
                f"EVENT date={event['date']!r} "
                f"raw_actual={event['raw_actual']!r} "
                f"actual={event['actual']!r}"
            )

            # Für unseren Test genügt die Datumsprüfung über
            # Monat/Jahr im Datumstext.
            low = event["date"].lower()

            if (
                str(TARGET_YEAR) in low
                and (
                    "august" in low
                    or "aug" in low
                )
            ):
                target_events.append(event)

        if not target_events:
            print("TARGET_EVENT=NOT_FOUND")
            failures += 1
            continue

        valid = [
            e for e in target_events
            if e["actual"] is not None
        ]

        if not valid:
            print("TARGET_ACTUAL=INVALID_OR_MISSING")
            failures += 1
            continue

        actual = valid[0]["actual"]
        successful_values[kind] = actual

        print(f"TARGET_ACTUAL={actual}")
        print("RESULT=PASS")

    except Exception as exc:
        print(
            f"RESULT=FAIL_EXCEPTION "
            f"{type(exc).__name__}: {exc}"
        )
        failures += 1

    print()

print("=" * 80)
print("RESULT SUMMARY")
print("=" * 80)

for kind in URLS:
    print(
        f"{kind}: "
        f"{successful_values.get(kind)!r}"
    )

print()

if set(successful_values) == set(URLS) and failures == 0:
    print("FINAL_RESULT=GREEN")
else:
    print(f"FINAL_RESULT=RED FAILURES={failures}")
    raise SystemExit(1)
