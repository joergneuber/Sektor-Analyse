import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()

INDEXES = [
    ("S&P 500", "^GSPC"),
    ("Nasdaq", "^IXIC"),
    ("Dow Jones", "^DJI"),
    ("Russell 2000", "^RUT"),
    ("DAX", "^GDAXI"),
    ("EuroStoxx 50", "^STOXX50E"),
    ("STOXX Europe 600", "^STOXX"),
]

FROM_DATE = "2026-08-14"
TO_DATE = "2026-08-19"

if not FMP_API_KEY:
    raise SystemExit("FMP_API_KEY fehlt in den GitHub Actions Secrets/Environment Variables.")

def fetch(symbol):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "from": FROM_DATE,
        "to": TO_DATE,
        "apikey": FMP_API_KEY,
    })
    url = f"https://financialmodelingprep.com/stable/historical-price-eod/light?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Sektor-Analyse-FMP-Test/1.0",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        status = response.status
        raw = response.read().decode("utf-8")
    return status, json.loads(raw)

print("=== FMP INDEX DIAGNOSE ===")
print(f"Zeitraum: {FROM_DATE} bis {TO_DATE}")
print()

passed = 0

for name, symbol in INDEXES:
    try:
        status, payload = fetch(symbol)

        if isinstance(payload, dict):
            print(f"{name} ({symbol})")
            print(f"  HTTP: {status}")
            print(f"  FMP-Antwort: {payload}")
            print("  STATUS: FAIL")
            print()
            continue

        rows = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            date = row.get("date")
            close = row.get("price", row.get("close"))
            if date and close is not None:
                try:
                    close_num = float(close)
                    rows.append((date, close_num))
                except (TypeError, ValueError):
                    pass

        rows.sort(key=lambda x: x[0])

        print(f"{name} ({symbol})")
        print(f"  HTTP: {status}")
        print(f"  Datensaetze: {len(rows)}")

        for date, close in rows:
            print(f"  {date}: Close={close}")

        target = [r for r in rows if r[0] == "2026-08-17"]
        if target:
            print(f"  17.08.2026: Close={target[0][1]}  -> PASS")
            passed += 1
        else:
            print("  17.08.2026: kein gueltiger Close -> FAIL")

        print()

    except Exception as exc:
        print(f"{name} ({symbol})")
        print(f"  FEHLER: {type(exc).__name__}: {exc}")
        print("  STATUS: FAIL")
        print()

print(f"=== ERGEBNIS: {passed}/{len(INDEXES)} Indizes liefern einen gueltigen Close fuer 17.08.2026 ===")

if passed == len(INDEXES):
    print("FMP_TEST: PASS")
    raise SystemExit(0)
else:
    print("FMP_TEST: NICHT 7/7")
    raise SystemExit(2)
