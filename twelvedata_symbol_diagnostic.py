import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("TWELVE_DATA_API_KEY fehlt.")

SEARCHES = [
    ("DAX", ["DAX", "DAX 40", "DAX Index", "Germany 40"]),
    ("EuroStoxx 50", ["Euro Stoxx 50", "EURO STOXX 50", "ESTX 50", "STOXX 50"]),
    ("STOXX Europe 600", ["STOXX Europe 600", "STOXX 600", "Europe 600", "SXXP"]),
]

def get_json(path, params):
    params = dict(params)
    params["apikey"] = API_KEY
    url = "https://api.twelvedata.com/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Sektor-Analyse-TwelveData-Diagnostic/2.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))

def search(term):
    status, payload = get_json("symbol_search", {
        "symbol": term,
        "outputsize": 30,
        "show_plan": "true",
    })
    return status, payload

def timeseries(symbol, exchange=None, mic=None):
    p = {
        "symbol": symbol,
        "interval": "1day",
        "start_date": "2026-08-14",
        "end_date": "2026-08-19",
        "outputsize": 10,
    }
    if exchange:
        p["exchange"] = exchange
    if mic:
        p["mic_code"] = mic
    return get_json("time_series", p)

print("=== TWELVE DATA SYMBOL-DIAGNOSE v2 ===")
print("Ziel: echten Index identifizieren und 17.08.2026 Close pruefen.")
print("Der vorherige DAX=47.1686 Treffer wird NICHT als DAX akzeptiert.")
print()

for label, terms in SEARCHES:
    print(f"### {label}")
    candidates = []
    seen = set()

    for term in terms:
        try:
            status, payload = search(term)
            data = payload.get("data", []) if isinstance(payload, dict) else []
            print(f"SEARCH '{term}': HTTP {status}, {len(data)} Treffer")

            for item in data:
                key = (
                    item.get("symbol"),
                    item.get("instrument_name"),
                    item.get("exchange"),
                    item.get("mic_code"),
                    item.get("instrument_type"),
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(item)

        except Exception as exc:
            print(f"SEARCH '{term}': FEHLER {type(exc).__name__}: {exc}")

    print(f"Einzigartige Kandidaten: {len(candidates)}")

    # Show all candidates; this is the important diagnostic output.
    for i, item in enumerate(candidates, 1):
        access = item.get("access", {})
        print(
            f"  [{i}] symbol={item.get('symbol')!r} | "
            f"name={item.get('instrument_name')!r} | "
            f"exchange={item.get('exchange')!r} | "
            f"MIC={item.get('mic_code')!r} | "
            f"type={item.get('instrument_type')!r} | "
            f"country={item.get('country')!r} | "
            f"plan={access.get('plan')!r}"
        )

    # Do not guess. Only test candidates whose name/type strongly indicate
    # the requested index and whose plan is Basic/Global/available.
    print("Historien-Test:")
    tested = 0
    found = False

    for item in candidates:
        name = str(item.get("instrument_name") or "").lower()
        typ = str(item.get("instrument_type") or "").lower()
        symbol = item.get("symbol")
        plan = str((item.get("access") or {}).get("plan") or "").lower()

        if not symbol:
            continue

        if "index" not in typ and "index" not in name:
            continue

        if label == "DAX" and not ("dax" in name or "dax" in str(symbol).lower()):
            continue
        if label == "EuroStoxx 50" and not ("euro stoxx" in name or "eurostoxx" in name or "stoxx 50" in name or "estx" in str(symbol).lower()):
            continue
        if label == "STOXX Europe 600" and not ("stoxx europe 600" in name or "stoxx 600" in name or "sxxp" in str(symbol).lower()):
            continue

        tested += 1
        try:
            status, payload = timeseries(
                symbol,
                exchange=item.get("exchange"),
                mic=item.get("mic_code"),
            )
            values = payload.get("values", []) if isinstance(payload, dict) else []
            print(
                f"  TEST symbol={symbol!r} name={item.get('instrument_name')!r}: "
                f"HTTP {status}, {len(values)} Werte, plan={plan!r}"
            )

            target = None
            for row in values:
                if row.get("datetime") == "2026-08-17" and row.get("close") is not None:
                    target = row["close"]
                    break

            if target is not None:
                print(f"    17.08.2026 Close={target} -> PASS")
                found = True
                break
            else:
                print("    17.08.2026: kein gueltiger Close")
        except Exception as exc:
            print(f"    TEST-FEHLER: {type(exc).__name__}: {exc}")

    if not found:
        print(f"  {label}: NO VALID PASS (Kandidaten getestet: {tested})")
    print()

print("=== ENDE DER DIAGNOSE ===")
print("Noch keine bestehende Analyse-/Cache-Datei wurde veraendert.")
