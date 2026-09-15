import json
import re
import urllib.request
from datetime import datetime, timezone

SOURCES = {
    "ECONOMIC_CALENDAR_ICS": "https://joaoalonsocasella.github.io/Economic_Calendar/USA.ics",
    "FOREXFACTORY_JSON": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
}

TARGETS = {
    "ADP": ["adp"],
    "NFP": ["non farm", "nonfarm", "payroll"],
    "CPI": ["cpi", "consumer price"],
    "PPI": ["ppi", "producer price"],
    "JOLTS": ["jolts", "job openings"],
    "PCE": ["pce", "personal consumption"],
    "GDP": ["gdp", "gross domestic"],
    "ISM": ["ism", "manufacturing pmi", "services pmi"],
    "FOMC": ["fomc", "federal funds", "fomc statement"],
}

def fetch(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Sektor-Analyse-Free-Calendar-Test/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("content-type", ""), r.read()

def parse_ics(text):
    events = []
    blocks = re.split(r"\r?\n\r?\n", text)
    for block in blocks:
        if "BEGIN:VEVENT" not in block:
            continue
        event = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.split(";", 1)[0].upper()
            if key in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "UID"}:
                event[key] = value.strip()
        if event:
            events.append(event)
    return events

def test_ics():
    name = "ECONOMIC_CALENDAR_ICS"
    print(f"\n=== {name} ===")
    try:
        status, ctype, body = fetch(SOURCES[name])
        text = body.decode("utf-8", errors="replace")
        events = parse_ics(text)
        print(f"HTTP_STATUS={status}")
        print(f"CONTENT_TYPE={ctype}")
        print(f"BYTES={len(body)}")
        print(f"ICS_EVENT_COUNT={len(events)}")

        joined = "\n".join(
            f"{e.get('DTSTART','')} {e.get('SUMMARY','')}"
            for e in events
        ).lower()

        for label, words in TARGETS.items():
            hits = [e for e in events if any(
                w in (e.get("SUMMARY", "") + " " + e.get("DESCRIPTION", "")).lower()
                for w in words
            )]
            print(f"{label}={len(hits)}")
            for e in hits[:3]:
                print(f"  {e.get('DTSTART','')} | {e.get('SUMMARY','')}")

        print("RESULT=SUCCESS" if events else "RESULT=EMPTY")
    except Exception as exc:
        print("RESULT=ERROR")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print(f"ERROR={exc}")

def test_forexfactory():
    name = "FOREXFACTORY_JSON"
    print(f"\n=== {name} ===")
    try:
        status, ctype, body = fetch(SOURCES[name])
        payload = json.loads(body.decode("utf-8"))
        print(f"HTTP_STATUS={status}")
        print(f"CONTENT_TYPE={ctype}")
        print(f"BYTES={len(body)}")
        print(f"JSON_TYPE={type(payload).__name__}")
        print(f"EVENT_COUNT={len(payload) if isinstance(payload, list) else 'n/a'}")

        events = payload if isinstance(payload, list) else []
        us = [
            e for e in events
            if str(e.get("country", "")).upper() in {"USD", "US", "USA", "UNITED STATES"}
        ]
        print(f"US_EVENT_COUNT={len(us)}")

        for label, words in TARGETS.items():
            hits = [
                e for e in us
                if any(w in str(e.get("title", "")).lower() for w in words)
            ]
            print(f"{label}={len(hits)}")
            for e in hits[:5]:
                print(
                    f"  {e.get('date','')} | {e.get('country','')} | "
                    f"{e.get('impact','')} | {e.get('title','')} | "
                    f"forecast={e.get('forecast','')} | previous={e.get('previous','')}"
                )

        print("\nFIRST_US_EVENTS")
        for e in us[:20]:
            print(
                f"  {e.get('date','')} | {e.get('impact','')} | "
                f"{e.get('title','')} | forecast={e.get('forecast','')} | "
                f"previous={e.get('previous','')}"
            )

        print("RESULT=SUCCESS")
    except Exception as exc:
        print("RESULT=ERROR")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print(f"ERROR={exc}")

def main():
    print("FREE MACRO CALENDAR CANDIDATE TEST")
    print("=" * 72)
    print(f"UTC_NOW={datetime.now(timezone.utc).isoformat()}")
    print("No API key. No production files changed.")
    test_ics()
    test_forexfactory()
    return 0

raise SystemExit(main())
