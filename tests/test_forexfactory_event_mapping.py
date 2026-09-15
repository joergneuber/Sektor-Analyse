import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

EVENT_ALIASES = {
    "ADP": [
        "adp",
        "adp employment",
        "adp weekly employment",
    ],
    "NFP": [
        "non-farm",
        "non farm",
        "nonfarm",
        "payroll",
        "employment situation",
    ],
    "CPI": [
        "consumer price index",
        "cpi",
    ],
    "PPI": [
        "producer price index",
        "ppi",
    ],
    "JOLTS": [
        "jolts",
        "job openings",
    ],
    "PCE": [
        "personal consumption expenditures",
        "personal consumption",
        "pce price",
        "pce",
    ],
    "GDP": [
        "gross domestic product",
        "gdp",
    ],
    "ISM": [
        "ism manufacturing",
        "ism services",
        "ism manufacturing pmi",
        "ism services pmi",
        "ism",
    ],
    "JOBLESS_CLAIMS": [
        "initial jobless claims",
        "jobless claims",
        "continuing claims",
    ],
    "FOMC": [
        "fomc",
        "federal funds",
        "fed interest rate",
        "fomc statement",
        "fomc press conference",
        "economic projections",
    ],
}

# Deliberate exclusions to avoid false positives.
EXCLUSIONS = {
    "NFP": [
        "adp",
    ],
    "CPI": [
        "cpi expectations",
    ],
    "PPI": [
        "ppi expectations",
    ],
    "PCE": [
        "consumer confidence",
    ],
    "GDP": [
        "gdpnow",
    ],
}


def fetch():
    request = urllib.request.Request(
        URL,
        headers={
            "User-Agent": (
                "Sektor-Analyse-ForexFactory-Mapping-Test/1.0"
            )
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
        return response.status, response.headers.get("content-type", ""), body


def norm(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def event_text(event):
    return norm(" ".join(
        str(event.get(field, ""))
        for field in (
            "title",
            "event",
            "category",
            "name",
            "description",
        )
        if event.get(field) is not None
    ))


def is_us(event):
    country = norm(event.get("country"))
    return country in {
        "usd",
        "us",
        "usa",
        "united states",
        "united states of america",
    }


def matches(event, aliases, exclusions):
    text = event_text(event)

    if any(alias in text for alias in aliases):
        if any(exclusion in text for exclusion in exclusions):
            return False
        return True

    return False


def print_event(event):
    print(
        "  "
        f"date={event.get('date', '')!r} | "
        f"time={event.get('time', '')!r} | "
        f"country={event.get('country', '')!r} | "
        f"impact={event.get('impact', '')!r} | "
        f"title={event.get('title', event.get('event', ''))!r} | "
        f"forecast={event.get('forecast', '')!r} | "
        f"previous={event.get('previous', '')!r} | "
        f"actual={event.get('actual', '')!r}"
    )


def main():
    print("FOREXFACTORY SEMANTIC EVENT-MAPPING TEST")
    print("=" * 84)
    print(
        "UTC_NOW="
        f"{datetime.now(timezone.utc).isoformat()}"
    )
    print(f"URL={URL}")
    print("API_KEY=NONE")

    try:
        status, content_type, body = fetch()
    except Exception as exc:
        print("RESULT=ERROR")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print(f"ERROR={exc}")
        return 1

    print(f"HTTP_STATUS={status}")
    print(f"CONTENT_TYPE={content_type}")
    print(f"BYTES={len(body)}")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        print("RESULT=INVALID_JSON")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print(f"ERROR={exc}")
        return 1

    if not isinstance(payload, list):
        print("RESULT=UNEXPECTED_PAYLOAD")
        print(f"PAYLOAD_TYPE={type(payload).__name__}")
        return 1

    events = [event for event in payload if isinstance(event, dict)]
    us_events = [event for event in events if is_us(event)]

    print()
    print("=== FEED ===")
    print(f"TOTAL_EVENTS={len(events)}")
    print(f"US_EVENTS={len(us_events)}")

    dates = [
        str(event.get("date"))
        for event in events
        if event.get("date")
    ]
    if dates:
        print(f"MIN_DATE={min(dates)}")
        print(f"MAX_DATE={max(dates)}")

    print()
    print("=== SEMANTIC TARGET MAPPING ===")

    mapping = {}
    for canonical, aliases in EVENT_ALIASES.items():
        found = [
            event
            for event in us_events
            if matches(
                event,
                aliases,
                EXCLUSIONS.get(canonical, []),
            )
        ]
        mapping[canonical] = found

        print()
        print(f"{canonical}={len(found)}")
        for event in found:
            print_event(event)

    print()
    print("=== ALL USD EVENTS ===")
    for event in us_events:
        print_event(event)

    print()
    print("=== REQUIRED FIELD COVERAGE ===")
    fields = [
        "date",
        "time",
        "country",
        "title",
        "impact",
        "forecast",
        "previous",
        "actual",
    ]

    for field in fields:
        present = sum(
            1
            for event in us_events
            if event.get(field) not in (None, "")
        )
        print(f"{field}={present}/{len(us_events)}")

    print()
    print("=== IMPACT DISTRIBUTION ===")
    impact = {}
    for event in us_events:
        value = norm(event.get("impact")) or "unknown"
        impact[value] = impact.get(value, 0) + 1

    for key in sorted(impact):
        print(f"{key.upper()}={impact[key]}")

    print()
    print("=== MAPPING DECISION ===")
    found_targets = sum(
        1 for matches_ in mapping.values()
        if matches_
    )
    print(
        f"TARGETS_FOUND={found_targets}/"
        f"{len(EVENT_ALIASES)}"
    )

    # This test is diagnostic, not a production gate.
    # HTTP/payload success is the pass criterion; coverage is reported.
    print("RESULT=SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
