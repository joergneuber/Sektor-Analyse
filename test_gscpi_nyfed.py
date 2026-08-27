import io
import pandas as pd
import requests

URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx"


def main():
    print("GSCPI TEST: New York Fed")

    try:
        response = requests.get(URL, timeout=20)
        print(f"HTTP-Status: {response.status_code}")
        response.raise_for_status()

        data = pd.read_excel(
            io.BytesIO(response.content),
            sheet_name="GSCPI Monthly Data",
        )

        print("Download: OK")
        print(f"Spalten: {list(data.columns)}")

        date_col = next(
            (c for c in data.columns if str(c).strip().lower() == "date"),
            None,
        )
        value_col = next(
            (c for c in data.columns if str(c).strip().lower() == "gscpi"),
            None,
        )

        if date_col is None or value_col is None:
            print("STATUS: FEHLER")
            print("Erwartete Spalten 'Date' und 'GSCPI' nicht gefunden")
            raise SystemExit(1)

        data[date_col] = pd.to_datetime(data[date_col], errors="coerce")
        data[value_col] = pd.to_numeric(data[value_col], errors="coerce")
        data = data.dropna(subset=[date_col, value_col]).sort_values(date_col)

        if data.empty:
            print("STATUS: FEHLER")
            print("Keine gültigen GSCPI-Daten gefunden")
            raise SystemExit(1)

        last = data.iloc[-1]

        print(f"Datenstand: {last[date_col].date()}")
        print(f"Letzter Wert: {last[value_col]}")
        print("STATUS: OK")

    except requests.RequestException as e:
        print(f"HTTP-Fehler: {e}")
        print("STATUS: FEHLER")
        raise SystemExit(1)
    except Exception as e:
        print(f"Fehler beim Einlesen der GSCPI-Datei: {e}")
        print("STATUS: FEHLER")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
