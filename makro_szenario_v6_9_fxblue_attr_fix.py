import os
import pandas as pd
from fredapi import Fred

# 1. API-Key festlegen
FRED_API_KEY = os.getenv("FRED_API_KEY", "DEIN_API_KEY_HIER_EINTRAGEN")
fred = Fred(api_key=FRED_API_KEY)

# 2. FRED-Serien-IDs für ISM Services & Subindizes
ISM_SERVICES_SERIES = {
    "Services_PMI": "NMFPCI",           # ISM Non-Manufacturing NMI (Hauptindex)
    "Business_Activity": "NMFBAI",      # Geschäftsaktivität
    "New_Orders": "NMFNOI",             # Auftragseingang
    "Employment": "NMFEMI",             # Beschäftigung
    "Supplier_Deliveries": "NMFSDI",   # Lieferzeiten
    "Inventories": "NMFIINI",           # Lagerbestände
    "Prices_Paid": "NMFPPI",            # Gezahlte Preise
}

def fetch_ism_services_full(start_date="2024-01-01"):
    series_dataframes = []

    for name, series_id in ISM_SERVICES_SERIES.items():
        try:
            # Daten einzeln abrufen
            series = fred.get_series(series_id, observation_start=start_date)
            df_temp = pd.DataFrame(series, columns=[name])
            series_dataframes.append(df_temp)
            print(f" Erfolgreich geladen: {name} ({series_id})")
        except Exception as e:
            print(f" Fehler bei {name} ({series_id}): {e}")

    # Alle DataFrames über das Datum zusammenführen (Outer Join)
    if series_dataframes:
        combined_df = pd.concat(series_dataframes, axis=1)
        combined_df.index.name = "Date"
        return combined_df
    
    return None

if __name__ == "__main__":
    print("=== ISM Services PMI & Subindizes Abruf (FRED API) ===")
    df_result = fetch_ism_services_full(start_date="2024-01-01")

    if df_result is not None:
        print("\n--- Neueste Monatsdaten ---")
        print(df_result.tail(12))

        # Optional: Speichern als CSV
        # df_result.to_csv("ism_services_fred_data.csv")
