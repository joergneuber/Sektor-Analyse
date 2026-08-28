import pandas as pd
from fredapi import Fred

# 1. API-Key eintragen
FRED_API_KEY = "DEIN_API_KEY_HIER_EINTRAGEN"
fred = Fred(api_key=FRED_API_KEY)

# 2. FRED-Serien-IDs definieren
# Beispiele für beliebte Makro-Indikatoren:
series_ids = {
    "ISM_Services": "NMFPCI",        # ISM Non-Manufacturing / Services PMI
    "ISM_Manufacturing": "MANEMP",   # ISM Manufacturing Index (oder verwandter Subindex)
    "US_Leitzins": "FEDFUNDS",       # Federal Funds Effective Rate
    "US_Inflation": "CPIAUCSL",      # Verbraucherpreisindex (CPI)
}

def fetch_fred_data(series_dict, start_date="2023-01-01"):
    dataframes = []
    
    for name, series_id in series_dict.items():
        try:
            # Daten abfragen
            series_data = fred.get_series(series_id, observation_start=start_date)
            df = pd.DataFrame(series_data, columns=[name])
            dataframes.append(df)
            print(f" Erfolgreich geladen: {name} ({series_id})")
        except Exception as e:
            print(f" Fehler bei {name} ({series_id}): {e}")
            
    # Zusammenführen in eine Tabelle über das Datum
    if dataframes:
        combined_df = pd.concat(dataframes, axis=1)
        return combined_df
    return None

# 3. Ausführung
if __name__ == "__main__":
    df_result = fetch_fred_data(series_ids, start_date="2024-01-01")
    
    print("\n--- Neueste Datenpunkte ---")
    print(df_result.tail(10))
