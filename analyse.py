import yfinance as yf
import pandas as pd
import os
from datetime import datetime

# 1. Konfiguration - MUSS GANZ OBEN STEHEN
sektoren_map = {
    "XLK": "Technologie", "XLF": "Finanzen", "XLV": "Gesundheit", "XLY": "Zyklischer Konsum",
    "XLP": "Basiskonsum", "XLE": "Energie", "XLI": "Industrie", "XLB": "Rohstoffe",
    "XLU": "Versorger", "XLRE": "Immobilien", "XLC": "Kommunikation",
    "SOXX": "Halbleiter", "SMH": "Halbleiter (Global)", "IGV": "Software", 
    "XBI": "Biotechnologie", "KRE": "Regionalbanken", "HACK": "Cybersecurity", 
    "CLOU": "Cloud Computing", "AIQ": "Künstliche Intelligenz",
    "BOTZ": "Robotik", "IHI": "Medical Devices", "PAVE": "Infrastruktur", "XRT": "Einzelhandel"
}

sektoren_aktien = {
    "XLK": ["AAPL", "MSFT", "ORCL"], "XLF": ["JPM", "BAC", "GS"], "XLV": ["UNH", "JNJ", "LLY"],
    "XLY": ["AMZN", "TSLA", "HD"], "XLP": ["PG", "KO", "PEP"], "XLE": ["XOM", "CVX", "SLB"],
    "XLI": ["CAT", "GE", "HON"], "XLB": ["LIN", "APD", "ECL"], "XLU": ["NEE", "DUK", "SO"],
    "XLRE": ["PLD", "AMT", "EQIX"], "XLC": ["META", "GOOGL", "NFLX"],
    "SOXX": ["NVDA", "AVGO", "TXN"], "SMH": ["NVDA", "TSM", "ASML"], "IGV": ["ADBE", "CRM", "SAP"],
    "XBI": ["AMGN", "VRTX", "GILD"], "KRE": ["FITB", "HBAN", "CFG"], "HACK": ["PANW", "CRWD", "FTNT"],
    "CLOU": ["NOW", "SNOW", "WDAY"], "AIQ": ["NVDA", "MSFT", "GOOGL"], "BOTZ": ["ISRG", "ABB", "ROK"],
    "IHI": ["MDT", "BSX", "ZBH"], "PAVE": ["DE", "ETN", "CAT"], "XRT": ["AMZN", "HD", "LOW"]
}

# 2. Hilfsfunktionen - MÜSSEN AM LINKEN RAND STEHEN
def get_market_status():
    return "Bullish", "Details..."

def get_perf(ticker, name):
    # Historie laden (120 Tage für Sicherheit)
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty:
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
    
    last_close = data['Close'].iloc[-1]
    
    # Hilfsfunktion für sichere Berechnung
    def safe_calc(days):
        if days >= len(data): return (last_close / data['Close'].iloc[0]) - 1
        return (last_close / data['Close'].iloc[-days]) - 1

    p5 = safe_calc(5)
    p12 = safe_calc(12)
    p30 = safe_calc(30)
    p60 = safe_calc(60)
    rs = (p5 * 0.7) + (p12 * 0.3)
    
    # Ergebnisse in Prozent (x100) mit 3 Nachkommastellen
    return {
        "Ticker": ticker, "Sektor": name, 
        "5T": round(p5 * 100, 3), "12T": round(p12 * 100, 3), 
        "30T": round(p30 * 100, 3), "60T": round(p60 * 100, 3), 
        "Rotation-Score": round(rs * 100, 3)
    }

def analyze_a_setup(ticker, sektor, context):
    return {"Ticker": ticker, "Sektor": sektor, "Status": "Check"}

# 3. Hauptlogik - WIRD ERST HIER AUSGEFÜHRT
print("Starte Analyse...")
market_context, market_details = get_market_status()

# Jetzt sind sektoren_map und get_perf bereits bekannt
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
