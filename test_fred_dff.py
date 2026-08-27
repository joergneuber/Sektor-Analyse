import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES_ID = "DFF"
API_KEY = os.environ.get("FRED_API_KEY")

if not API_KEY:
    print("FRED API TEST: FEHLER")
    print("FRED_API_KEY ist nicht gesetzt.")
    sys.exit(1)

params = {
    "api_key": API_KEY,
    "file_type": "json",
    "series_id": SERIES_ID,
    "sort_order": "desc",
    "limit": 1,
}

url = API_URL + "?" + urlencode(params)
request = Request(url, headers={"User-Agent": "Sektor-Analyse-FRED-Test/1.0"})

try:
    with urlopen(request, timeout=15) as response:
        status = response.status
        data = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    print("FRED API TEST: FEHLER")
    print(f"Fehlertyp: {type(exc).__name__}")
    print(f"Fehler: {exc}")
    sys.exit(1)

if status != 200:
    print("FRED API TEST: FEHLER")
    print(f"HTTP-Status: {status}")
    sys.exit(1)

observations = data.get("observations", [])
if not observations:
    print("FRED API TEST: FEHLER")
    print("Keine Beobachtung für DFF zurückgegeben.")
    sys.exit(1)

observation = observations[0]
print("FRED API TEST: DFF")
print(f"HTTP-Status: {status}")
print(f"Datenstand: {observation.get('date')}")
print(f"Letzter Wert: {observation.get('value')}")
print("STATUS: OK")
