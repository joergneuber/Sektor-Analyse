import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ.get("ALPACA_API_KEY", "").strip()
API_SECRET = os.environ.get("ALPACA_API_SECRET", "").strip()

if not API_KEY or not API_SECRET:
    raise SystemExit("ALPACA_API_KEY und ALPACA_API_SECRET fehlen.")

CANDIDATES = {
    "S&P 500": ["SPX", "SPXW"],
    "Nasdaq 100": ["NDX"],
    "Nasdaq Composite": ["COMP"],
    "Dow Jones": ["DJI", "DJIA"],
    "Russell 2000": ["RUT"],
    "DAX": ["DAX", "GDAXI"],
    "EuroStoxx 50": ["SX5E", "STOXX50E"],
    "STOXX Europe 600": ["SXXP", "STOXX"],
}

START = "2026-08-14T00:00:00Z"
END = "2026-08-19T00:00:00Z"
BASE = "https://data.alpaca.markets/v1beta1/indices/values"

def fetch(symbol):
    params = urllib.parse.urlencode({
        "index_symbols": symbol,
        "start": START,
        "end": END,
        "limit": 100,
    })
    req = urllib.request.Request(
        f"{BASE}?{params}",
        headers={
            "APCA-API-KEY-ID": API_KEY,
            "APCA-API-SECRET-KEY": API_SECRET,
            "Accept": "application/json",
            "User-Agent": "Sektor-Analyse-Alpaca-Test/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, json.loads(response.read().decode("utf-8"))

print("=== ALPACA INDEX DIAGNOSE ===")
print(f"Zeitraum: {START} bis {END}")
print()

for name, symbols in CANDIDATES.items():
    print(name)
    found = False
    for symbol in symbols:
        try:
            status, payload = fetch(symbol)
            print(f"  Kandidat {symbol}: HTTP {status}")
            if isinstance(payload, dict):
                rows = payload.get("index_values", payload.get("values", []))
                if not rows:
                    print(f"    Antwort: {payload}")
                    continue
            else:
                rows = payload
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                print(f"    {row}")
            for row in rows:
                date = str(row.get("t", row.get("timestamp", row.get("date", ""))))[:10]
                value = row.get("v", row.get("value", row.get("close")))
                if date == "2026-08-17" and value is not None:
                    print(f"    17.08.2026: value={value} -> PASS")
                    found = True
        except Exception as exc:
            print(f"  Kandidat {symbol}: FEHLER {type(exc).__name__}: {exc}")
    if not found:
        print("  RESULTAT: kein verifizierter 17.08.2026-Wert")
    print()

print("=== ENDE ALPACA INDEX DIAGNOSE ===")
