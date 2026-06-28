import yfinance as yf
import pandas as pd
import os
from datetime import datetime

# 1. Konfiguration
# [Hier deine sektoren_map und sektoren_aktien einfügen]
sektoren_map = {
    # Beispiel: "XLK": "Technologie"
}
sektoren_aktien = {
    # Beispiel: "XLK": ["AAPL", "MSFT"]
}

# 2. Hilfsfunktionen (Beispielstruktur)
def get_market_status():
    return "Bullish", "Details..."

def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="1mo")
    score = (data['Close'].iloc[-1] / data['Close'].iloc[0]) - 1
    return {"Ticker": ticker, "Sektor": name, "Rotation-Score": score}

def analyze_a_setup(ticker, sektor, context):
    return {"Ticker": ticker, "Sektor": sektor, "Status": "Check"}

# 3. Hauptlogik
print("Starte Analyse...")
market_context, market_details = get_market_status()

# Analyse durchführen
perf_list = [get_perf(t, n) for t, n in sektoren_map.items()]
df_perf = pd.DataFrame(perf_list).sort_values("Rotation-Score", ascending=False)

setups = []
for _, row in df_perf.head(2).iterrows():
    for t in sektoren_aktien.get(row['Ticker'], [])[:3]:
        setups.append(analyze_a_setup(t, row['Sektor'], market_context))
df_setups = pd.DataFrame(setups)

# 4. Absolutes Speichern (GitHub-safe)
# Hier wird die Datei garantiert im Arbeitsverzeichnis gespeichert
base_path = os.getcwd()
perf_path = os.path.join(base_path, "Performance.csv")
setups_path = os.path.join(base_path, "Setups.csv")

df_perf.to_csv(perf_path, index=False)
df_setups.to_csv(setups_path, index=False)

print(f"Dateien erfolgreich gespeichert unter: {base_path}")
