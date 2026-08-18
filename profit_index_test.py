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

def get(path, params):
    p = dict(params)
    p["token"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Sektor-Analyse-Profit-Test/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))

def search_indices(label):
    # Profit's documented reference endpoint supports symbol/name filters;
    # request a broad index list first so we do not guess exchange suffixes.
    status, payload = get("/reference/indices", {
        "limit": 1000,
        "available_data": "historical",
        "type": "INDEX",
    })
    items = payload if isinstance(payload, list) else payload.get("data", [])
    print(f"  Reference HTTP: {status}; Treffer gesamt: {len(items)}")

    terms = {
        "DAX": ["dax"],
        "EuroStoxx 50": ["euro stoxx 50", "eurostoxx 50", "euro stoxx", "estx"],
        "STOXX Europe 600": ["stoxx europe 600", "stoxx 600", "europe 600"],
    }[label]

    matches = []
    for x in items:
        text = " ".join(str(x.get(k, "")) for k in
                         ("symbol", "name", "description", "exchange", "country")).lower()
        if any(t in text for t in terms):
            matches.append(x)
    return matches

def historical(ticker):
    return get(f"/market-data/historical/daily/{urllib.parse.quote(ticker, safe='')}", {
        "start_date": START,
        "end_date": END,
    })

print("=== PROFIT.COM INDEX DIAGNOSE ===")
print(f"Zeitraum: {START} bis {END}")
print()

total_pass = 0

for label in ["DAX", "EuroStoxx 50", "STOXX Europe 600"]:
    print(f"### {label}")
    try:
        matches = search_indices(label)
        print(f"  passende Referenztreffer: {len(matches)}")

        for i, x in enumerate(matches[:30], 1):
            print(
                f"  [{i}] symbol={x.get('symbol')!r} | "
                f"name={x.get('name')!r} | exchange={x.get('exchange')!r} | "
                f"country={x.get('country')!r}"
            )

        found = False
        for x in matches:
            symbol = x.get("symbol")
            exchange = x.get("exchange")
            if not symbol:
                continue

            # Profit tickers are composed from symbol + exchange code.
            ticker = f"{symbol}.{exchange}" if exchange and "." not in str(symbol) else str(symbol)

            try:
                status, payload = historical(ticker)
                rows = payload if isinstance(payload, list) else payload.get("data", [])
                print(f"  TEST {ticker}: HTTP {status}, Datensaetze={len(rows)}")

                target = None
                for row in rows:
                    if row.get("t") is None:
                        continue
                    # Daily timestamps are Unix seconds; avoid timezone assumptions
                    # by comparing the UTC date.
                    import datetime
                    dt = datetime.datetime.fromtimestamp(int(row["t"]), datetime.timezone.utc)
                    if dt.date().isoformat() == "2026-08-17" and row.get("c") is not None:
                        target = float(row["c"])
                        break

                if target is not None:
                    print(f"    17.08.2026 Close={target} -> PASS")
                    total_pass += 1
                    found = True
                    break
                else:
                    print("    17.08.2026: kein gueltiger Close")
            except Exception as exc:
                print(f"    TEST-FEHLER {type(exc).__name__}: {exc}")

        if not found:
            print(f"  {label}: FAIL")
    except Exception as exc:
        print(f"  REFERENZ-FEHLER {type(exc).__name__}: {exc}")
    print()

print(f"=== ERGEBNIS: {total_pass}/3 Europa-Indizes mit gueltigem Close am 17.08.2026 ===")
raise SystemExit(0 if total_pass == 3 else 2)
