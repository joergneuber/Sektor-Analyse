import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()

TESTS = [
    ("DAX", ["DAX", "DAX@EUREX", "GDAXI"]),
    ("EuroStoxx 50", ["STOXX50E", "STX50E", "ESTX50"]),
    ("STOXX Europe 600", ["STOXX600", "STOXX", "SXXP"]),
]

START_DATE = "2026-08-14"
END_DATE = "2026-08-19"

if not API_KEY:
    raise SystemExit("TWELVE_DATA_API_KEY fehlt.")

def request_json(symbol):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "1day",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "apikey": API_KEY,
        "format": "JSON",
        "timezone": "UTC",
    })
    url = f"https://api.twelvedata.com/time_series?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Sektor-Analyse-TwelveData-Test/1.0",
                 "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, json.loads(response.read().decode("utf-8"))

def extract_rows(payload):
    values = payload.get("values", []) if isinstance(payload, dict) else []
    rows = []
    for row in values:
        try:
            if row.get("datetime") and row.get("close") is not None:
                rows.append((row["datetime"], float(row["close"])))
        except (TypeError, ValueError):
            pass
    return sorted(rows)

print("=== TWELVE DATA INDEX DIAGNOSE ===")
print(f"Zeitraum: {START_DATE} bis {END_DATE}\n")

passed = 0

for name, candidates in TESTS:
    print(name)
    found = False
    for symbol in candidates:
        try:
            status, payload = request_json(symbol)
            if payload.get("status") == "error":
                print(f"  {symbol}: HTTP {status}, API-Fehler {payload.get('code','')}: {payload.get('message','')}")
                continue

            rows = extract_rows(payload)
            if not rows:
                print(f"  {symbol}: HTTP {status}, keine EOD-Werte")
                continue

            print(f"  {symbol}: HTTP {status}, {len(rows)} Datensaetze")
            for date, close in rows:
                print(f"    {date}: Close={close}")

            target = [r for r in rows if r[0] == "2026-08-17"]
            if target:
                print(f"  17.08.2026: Close={target[0][1]} -> PASS")
                passed += 1
                found = True
                break
            print(f"  {symbol}: 17.08.2026 fehlt -> naechsten Kandidaten testen")
        except Exception as exc:
            print(f"  {symbol}: FEHLER {type(exc).__name__}: {exc}")

    if not found:
        print(f"  {name}: FAIL")
    print()

print(f"=== ERGEBNIS: {passed}/3 Europa-Indizes liefern einen gueltigen Close fuer 17.08.2026 ===")
raise SystemExit(0 if passed == 3 else 2)
