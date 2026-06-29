import yfinance as yf

import pandas as pd

import os

from datetime import datetime


# 1. Konfiguration

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


# 2. Hilfsfunktionen (Beispielstruktur)

def get_market_status():

    return "Bullish", "Details..."


def get_perf(ticker, name):
    # Lade Daten mit Puffer
    data = yf.Ticker(ticker).history(period="120d")
    
    if data.empty:
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}

    last_close = data['Close'].iloc[-1]
    
    # Hier der Clou: Wenn ein Zeitraum (z.B. -60) außerhalb der verfügbaren 
    # Daten liegt, nehmen wir den ältesten verfügbaren Kurs (data.iloc[0]) 
    # anstatt einer 0 oder eines Fehlers.
    def safe_perf(days):
        index = -days
        if abs(index) >= len(data): 
            # Nutze den ältesten verfügbaren Kurs als Basis, wenn Historie zu kurz
            return (last_close / data['Close'].iloc[0]) - 1
        return (last_close / data['Close'].iloc[index]) - 1

    perf_5t = safe_perf(5)
    perf_12t = safe_perf(12)
    perf_30t = safe_perf(30)
    perf_60t = safe_perf(60)
    
    rotation_score = (perf_5t * 0.7) + (perf_12t * 0.3)
    
    return {
        "Ticker": ticker, "Sektor": name, 
        "5T": perf_5t, "12T": perf_12t, 
        "30T": perf_30t, "60T": perf_60t, 
        "Rotation-Score": rotation_score
    }


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
