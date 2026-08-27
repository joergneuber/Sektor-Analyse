import os
import requests

SERIES_ID = "GSCPI"
URL = "https://api.stlouisfed.org/fred/series/observations"


def main():
    api_key = os.getenv("FRED_API_KEY")

    print("FRED API TEST: GSCPI")

    if not api_key:
        print("STATUS: FEHLER")
        print("FRED_API_KEY ist nicht gesetzt")
        raise SystemExit(1)

    params = {
        "api_key": api_key,
        "file_type": "json",
        "series_id": SERIES_ID,
    }

    try:
        response = requests.get(URL, params=params, timeout=15)

        print(f"HTTP-Status: {response.status_code}")

        try:
            data = response.json()
        except ValueError:
            print("FRED-Antwort ist kein gültiges JSON")
            print(f"Antwort: {response.text[:1000]}")
            raise SystemExit(1)

        if response.status_code != 200:
            print(f"FRED error_code: {data.get('error_code')}")
            print(f"FRED error_message: {data.get('error_message')}")
            print("STATUS: FEHLER")
            raise SystemExit(1)

        observations = data.get("observations", [])

        if not observations:
            print("Keine Beobachtungen zurückgegeben")
            print("STATUS: FEHLER")
            raise SystemExit(1)

        valid = [
            x for x in observations
            if x.get("value") not in (None, "", ".")
        ]

        if not valid:
            print("Keine gültigen Werte vorhanden")
            print("STATUS: FEHLER")
            raise SystemExit(1)

        last = valid[-1]

        print(f"Datenstand: {last.get('date')}")
        print(f"Letzter Wert: {last.get('value')}")
        print("STATUS: OK")

    except requests.RequestException as e:
        print(f"HTTP-Fehler: {e}")
        print("STATUS: FEHLER")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
