import yfinance as yf
import pandas as pd
import os
from datetime import datetime

# ... (sektoren_map und sektoren_aktien bleiben gleich) ...

def get_market_status():
    return "Bullish", "Details..."

def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty:
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
    
    last_close = data['Close'].iloc[-1]
    
    def safe_perf(days):
        if days >= len(data): return (last_close / data['Close'].iloc[0]) - 1
        return (last_close / data['Close'].iloc[-days]) - 1

    # Berechnung als Dezimalzahl
    p5 = safe_perf(5)
    p12 = safe_perf(12)
    p30 = safe_perf(30)
    p60 = safe_perf(60)
    rs = (p5 * 0.7) + (p12 * 0.3)
    
    # HIER: Multiplikation mit 100 für die gewünschte Darstellung (6,526 statt 0,06526)
    # round(..., 3) sorgt für genau drei Nachkommastellen
    return {
        "Ticker": ticker, "Sektor": name, 
        "5T": round(p5 * 100, 3), 
        "12T": round(p12 * 100, 3), 
        "30T": round(p30 * 100, 3), 
        "60T": round(p60 * 100, 3), 
        "Rotation-Score": round(rs * 100, 3)
    }

def analyze_a_setup(ticker, sektor, context):
    return {"Ticker": ticker, "Sektor": sektor, "Status": "Check"}

# 3. Hauptlogik
print("Starte Analyse...")
market_context, market_details = get_market_status()

perf_list = [get_perf(t, n) for t, n in sektoren_map.items()]
df_perf = pd.DataFrame(perf_list).sort_values("Rotation-Score", ascending=False)

setups = []
for _, row in df_perf.head(2).iterrows():
    for t in sektoren_aktien.get(row['Ticker'], [])[:3]:
        setups.append(analyze_a_setup(t, row['Sektor'], market_context))

df_setups = pd.DataFrame(setups)

# 4. Speichern
base_path = os.getcwd()
df_perf.to_csv(os.path.join(base_path, "Performance.csv"), index=False)
df_setups.to_csv(os.path.join(base_path, "Setups.csv"), index=False)

print(f"Dateien erfolgreich gespeichert unter: {base_path}")
