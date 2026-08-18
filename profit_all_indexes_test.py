import datetime
import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ.get("PROFIT_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("PROFIT_API_KEY fehlt.")

BASE = "https://api.profit.com/data-api"
START = "2026-08-14"
END = "2026-08-19"

# We deliberately resolve the symbols through Profit.com's reference data.
# No Yahoo ticker is assumed to be a Profit ticker.
TARGETS = [
    ("S&P 500", ["GSPC", "SPX", "SP500"], ["S&P 500", "S&P 500 Index"]),
    ("Nasdaq", ["IXIC", "COMP", "NASDAQ"], ["Nasdaq Composite", "NASDAQ Composite"]),
    ("Dow Jones", ["DJI"], ["Dow Jones Industrial Average"]),
    ("Russell 2000", ["RUT.INDX"], ["Russell 2000", "Russell 2000 Index"]),
    ("DAX", ["GDAXI"], ["DAX Index"]),
    ("EuroStoxx 50", ["STOXX50E", "SX5E"], ["Euro Stoxx 50"]),
    ("STOXX Europe 600", ["STOXX"], ["Stoxx Europe 600", "STOXX Europe 600"]),
]

def get_json(path, params):
    p = dict(params)
    p["token"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Sektor-Analyse-Profit-All-Indexes-Test/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, json.loads(response.read().decode("utf-8"))

def resolve_index(symbol):
    status, payload = get_json("/reference/indices", {
        "symbol": symbol,
        "exchange": "INDX",
        "type": "INDEX",
        "available_data": "historical",
        "limit": 1000,
    })
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    return status, items

def historical(ticker):
    return get_json(
        "/market-data/historical/daily/" + urllib.parse.quote(ticker, safe=""),
        {"start_date": START, "end_date": END},
    )

def target_close(rows):
    for row in rows:
        ts = row.get("t")
        close = row.get("c")
        if ts is None or close is None:
            continue
        try:
            dt = datetime.datetime.fromtimestamp(
                int(ts), datetime.timezone.utc
            ).date().isoformat()
            if dt == "2026-08-17":
                return float(close)
        except (TypeError, ValueError, OSError):
            continue
    return None

print("=== PROFIT.COM ALL 7 INDEX DIAGNOSE ===")
print(f"Zeitraum: {START} bis {END}")
print("Ziel: gueltiger Close am 17.08.2026")
print()

passed = 0

for label, symbols, expected_names in TARGETS:
    print(f"### {label}")
    candidates = []

    for symbol in symbols:
        try:
            status, items = resolve_index(symbol)
            print(f"  Reference {symbol}: HTTP {status}, Treffer={len(items)}")
            for item in items:
                print(
                    f"    ticker={item.get('ticker')!r} | "
                    f"symbol={item.get('symbol')!r} | "
                    f"name={item.get('name')!r} | "
                    f"type={item.get('type')!r} | "
                    f"exchange={item.get('exchange')!r} | "
                    f"country={item.get('country')!r}"
                )
                if (
                    str(item.get("type", "")).upper() == "INDEX"
                    and str(item.get("exchange", "")).upper() == "INDX"
                ):
                    candidates.append(item)
        except Exception as exc:
            print(f"  Reference {symbol}: FEHLER {type(exc).__name__}: {exc}")

    # Deduplicate by Profit ticker.
    unique = {}
    for item in candidates:
        if item.get("ticker"):
            unique[item["ticker"]] = item

    found = False
    for ticker, item in unique.items():
        name = str(item.get("name", ""))
        # Exact/strong name check prevents ETF/warrant/sector-index confusion.
        valid_name = any(
            expected.lower() in name.lower() for expected in expected_names
        )
        if not valid_name:
            continue

        try:
            status, payload = historical(ticker)
            rows = payload if isinstance(payload, list) else payload.get("data", [])
            close = target_close(rows)

            print(
                f"  HIST {ticker} ({name}): HTTP {status}, "
                f"Datensaetze={len(rows)}"
            )

            if close is not None:
                print(f"    17.08.2026 Close={close} -> PASS")
                passed += 1
                found = True
                break

            print("    17.08.2026: kein gueltiger Close")
        except Exception as exc:
            print(
                f"  HIST {ticker} ({name}): "
                f"FEHLER {type(exc).__name__}: {exc}"
            )

    if not found:
        print(f"  {label}: FAIL")
    print()

print(f"=== ERGEBNIS: {passed}/7 Indizes mit gueltigem Close am 17.08.2026 ===")

if passed == 7:
    print("PROFIT_ALL_7_TEST: PASS")
    raise SystemExit(0)

print("PROFIT_ALL_7_TEST: NICHT 7/7")
raise SystemExit(2)
