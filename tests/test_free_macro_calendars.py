import json
import ssl
import urllib.request
from datetime import datetime, timezone

SOURCES = {
    "FRED": "https://fred.stlouisfed.org/releases/calendar",
    "BLS_ICS": "https://www.bls.gov/schedule/news_release/bls.ics",
    "FED_FOMC": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "ECB": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html",
}

def fetch(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Sektor-Analyse-Free-Macro-Calendar-Test/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, r.headers.get("content-type", ""), body

def report(name, url):
    print(f"\n=== {name} ===")
    print(f"URL={url}")
    try:
        status, content_type, body = fetch(url)
        print(f"HTTP_STATUS={status}")
        print(f"CONTENT_TYPE={content_type}")
        print(f"BYTES={len(body)}")
        text = body.decode("utf-8", errors="replace")
        low = text.lower()

        if name == "BLS_ICS":
            markers = [
                ("Employment Situation", "employment situation"),
                ("Consumer Price Index", "consumer price index"),
                ("Producer Price Index", "producer price index"),
                ("Job Openings", "job openings"),
            ]
        elif name == "FED_FOMC":
            markers = [
                ("2026", "2026"),
                ("September", "september"),
                ("October", "october"),
                ("December", "december"),
            ]
        elif name == "ECB":
            markers = [
                ("2026", "2026"),
                ("monetary policy", "monetary policy"),
            ]
        else:
            markers = [
                ("calendar", "calendar"),
                ("release", "release"),
            ]

        for label, marker in markers:
            print(f"{label.upper().replace(' ', '_')}_PRESENT={'YES' if marker in low else 'NO'}")

        print("RESULT=SUCCESS")
    except Exception as exc:
        print(f"RESULT=ERROR")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print(f"ERROR={exc}")
    return

def main():
    print("FREE MACRO CALENDAR SOURCE TEST")
    print("=" * 72)
    print(f"UTC_NOW={datetime.now(timezone.utc).isoformat()}")

    # Three logical source groups:
    # 1) FRED release calendars
    # 2) BLS official ICS calendar
    # 3) official central-bank calendars (Fed + ECB)
    for name in ("FRED", "BLS_ICS", "FED_FOMC", "ECB"):
        report(name, SOURCES[name])

    print("\n=== INTERPRETATION ===")
    print("FRED = release-calendar source")
    print("BLS_ICS = official BLS publication calendar")
    print("FED_FOMC + ECB = official central-bank calendars")
    print("No API key is used by this test.")
    print("No production project file is changed.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
