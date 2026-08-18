import datetime
import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ.get("PROFIT_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("PROFIT_API_KEY fehlt.")

BASE = "https://api.profit.com/data-api"
RUT = "RUT.INDX"

def get(path, params):
    p = dict(params)
    p["token"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Sektor-Analyse-Profit-RUT-Test/2.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))

def find_close(rows):
    for row in rows:
        try:
            ts = row.get("t")
            close = row.get("c")
            if ts is None or close is None:
                continue
            d = datetime.datetime.fromtimestamp(
                int(ts), datetime.timezone.utc
            ).date().isoformat()
            if d == "2026-08-17":
                return float(close), row
        except (TypeError, ValueError, OSError):
            pass
    return None, None

print("=== PROFIT.COM DIRECT RUT.INDX TEST ===")
print("Ziel: Russell 2000, 17.08.2026")
print()

# First verify the exact instrument.
try:
    status, payload = get("/reference/indices", {
        "symbol": "RUT",
        "exchange": "INDX",
        "type": "INDEX",
        "limit": 100,
    })
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    print(f"REFERENCE RUT: HTTP {status}, Treffer={len(items)}")
    for item in items:
        print(
            f"  ticker={item.get('ticker')!r} | "
            f"symbol={item.get('symbol')!r} | "
            f"name={item.get('name')!r} | "
            f"type={item.get('type')!r} | "
            f"exchange={item.get('exchange')!r}"
        )
except Exception as exc:
    print(f"REFERENCE FEHLER: {type(exc).__name__}: {exc}")

# Try the exact ticker with progressively broader windows.
windows = [
    ("target-window", "2026-08-14", "2026-08-19"),
    ("30-day-window", "2026-07-20", "2026-08-19"),
    ("90-day-window", "2026-05-20", "2026-08-19"),
    ("1-year-window", "2025-08-18", "2026-08-19"),
]

found = False

for label, start, end in windows:
    try:
        status, payload = get(
            "/market-data/historical/daily/" + urllib.parse.quote(RUT, safe=""),
            {
                "start_date": start,
                "end_date": end,
            },
        )
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        close, raw = find_close(rows)

        print(f"{label}: HTTP {status}, Datensaetze={len(rows)}")

        if rows:
            # Show only a few boundary records, never huge output.
            for row in rows[:2] + ([] if len(rows) <= 4 else ["..."]) + rows[-2:]:
                if row == "...":
                    print("  ...")
                else:
                    print(f"  row={row}")

        if close is not None:
            print(f"17.08.2026 Close={close} -> PASS")
            print("PROFIT_RUT_TEST: PASS")
            found = True
            break
        else:
            print("17.08.2026: kein Close in diesem Fenster")

    except Exception as exc:
        print(f"{label}: FEHLER {type(exc).__name__}: {exc}")

print()
if not found:
    print("PROFIT_RUT_TEST: FAIL")
    raise SystemExit(2)
