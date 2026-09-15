import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_URL = "https://finnhub.io/api/v1/calendar/economic"
TARGETS = {
    "ADP": ["adp"],
    "NFP": ["non farm", "nonfarm", "payroll"],
    "CPI": ["cpi", "consumer price"],
    "PPI": ["ppi", "producer price"],
    "JOLTS": ["jolts", "job openings"],
    "PCE": ["pce", "personal consumption"],
    "GDP": ["gdp", "gross domestic"],
    "ISM": ["ism", "manufacturing pmi", "services pmi"],
    "FOMC": ["fomc", "federal funds", "fed interest rate"],
}

def get_events(token: str):
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=7)

    query = urlencode({
        "from": today.isoformat(),
        "to": end.isoformat(),
        "token": token,
    })
    req = Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": "Sektor-Analyse-Finnhub-Calendar-Test/1.0"},
    )

    with urlopen(req, timeout=20) as response:
        status = response.status
        raw = response.read()
    return status, today, end, json.loads(raw.decode("utf-8"))

def text_of(event):
    return " ".join(
        str(event.get(k, ""))
        for k in ("event", "category", "name")
    ).lower()

def print_event(event):
    fields = [
        "date", "country", "event", "category", "importance",
        "currency", "unit", "actual", "estimate", "consensus",
        "previous", "revised", "source",
    ]
    values = [f"{k}={event.get(k)!r}" for k in fields if k in event]
    print("  " + " | ".join(values))

def main():
    token = os.environ.get("FINNHUB_API_KEY")
    if not token:
        print("ERROR: FINNHUB_API_KEY is not set.")
        return 2

    print("FINNHUB ECONOMIC CALENDAR TEST")
    print("=" * 72)

    try:
        status, start, end, payload = get_events(token)
    except HTTPError as exc:
        print(f"HTTP_STATUS={exc.code}")
        if exc.code == 429:
            print("RESULT=RATE_LIMIT_429")
        elif exc.code in (401, 403):
            print("RESULT=AUTHENTICATION_OR_PLAN_ERROR")
        else:
            print("RESULT=HTTP_ERROR")
        return 1
    except (URLError, TimeoutError) as exc:
        print(f"RESULT=NETWORK_ERROR: {type(exc).__name__}")
        return 1
    except Exception as exc:
        print(f"RESULT=UNEXPECTED_ERROR: {type(exc).__name__}: {exc}")
        return 1

    print(f"HTTP_STATUS={status}")
    print(f"DATE_RANGE={start}..{end}")
    print(f"RESPONSE_TYPE={type(payload).__name__}")

    if isinstance(payload, dict):
        events = payload.get("economicCalendar", payload.get("data", []))
    elif isinstance(payload, list):
        events = payload
    else:
        events = []

    if not isinstance(events, list):
        print(f"RESULT=UNEXPECTED_PAYLOAD_SHAPE")
        print(f"PAYLOAD_KEYS={list(payload.keys()) if isinstance(payload, dict) else 'n/a'}")
        return 1

    print(f"EVENT_COUNT={len(events)}")

    us_events = [
        e for e in events
        if str(e.get("country", "")).upper() in {"US", "USA", "UNITED STATES"}
    ]
    print(f"US_EVENT_COUNT={len(us_events)}")

    print("\nTARGET EVENT MATCHES")
    print("-" * 72)

    found = {}
    for label, keywords in TARGETS.items():
        matches = [
            e for e in us_events
            if any(keyword in text_of(e) for keyword in keywords)
        ]
        found[label] = matches
        print(f"{label}: {len(matches)}")
        for event in matches[:5]:
            print_event(event)

    print("\nUS EVENTS (first 20)")
    print("-" * 72)
    for event in us_events[:20]:
        print_event(event)

    print("\nFIELD COVERAGE")
    print("-" * 72)
    field_names = [
        "date", "country", "event", "category", "importance",
        "currency", "unit", "actual", "estimate", "consensus",
        "previous", "revised", "source",
    ]
    for field in field_names:
        count = sum(
            1 for e in us_events
            if field in e and e.get(field) not in (None, "")
        )
        print(f"{field}: {count}/{len(us_events)}")

    print("\nRESULT=SUCCESS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
