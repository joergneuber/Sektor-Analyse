import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

# --- KONFIGURATION ---
sektoren_map = {
    "XLK": "Technologie", "XLF": "Finanzen", "XLV": "Gesundheit", "XLY": "Zyklischer Konsum",
    "XLP": "Basiskonsum", "XLE": "Energie", "XLI": "Industrie", "XLB": "Rohstoffe",
    "XLU": "Versorger", "XLRE": "Immobilien", "XLC": "Kommunikation"
}

sektoren_aktien = {
    "XLK": ["AAPL", "MSFT", "ORCL", "ADBE", "CRM", "AVGO", "TXN", "NVDA", "CSCO", "INTC"],
    "XLF": ["JPM", "BAC", "GS", "MS", "C", "AXP", "WFC", "SCHW", "BLK", "USB"],
    "XLV": ["UNH", "JNJ", "LLY", "MRK", "PFE", "ABBV", "TMO", "DHR", "AMGN", "GILD"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "TGT", "GM", "F"],
    "XLP": ["PG", "KO", "PEP", "COST", "WMT", "CL", "EL", "MDLZ", "GIS", "K"],
    "XLE": ["XOM", "CVX", "SLB", "COP", "EOG", "PXD", "MPC", "PSX", "VLO", "HAL"],
    "XLI": ["CAT", "GE", "HON", "BA", "UPS", "LMT", "DE", "MMM", "RTX", "UNP"],
    "XLB": ["LIN", "APD", "ECL", "SHW", "FCX", "NEM", "DD", "DOW", "PPG", "VMC"],
    "XLU": ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PEG", "ED", "XEL"],
    "XLRE": ["PLD", "AMT", "EQIX", "PSA", "SPG", "O", "DLR", "WELL", "AVB", "CCI"],
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA"]
}

# --- FUNKTIONEN ---
def get_setup(ticker, sektor):
    try:
        df = yf.download(ticker, period="300d", progress=False)
        if df.empty or len(df) < 200: return None
        
        akt = df['Close'].iloc[-1]
        # Score Berechnung
        e20 = df['Close'].ewm(span=20).mean().iloc[-1]
        e50 = df['Close'].ewm(span=50).mean().iloc[-1]
        e100 = df['Close'].ewm(span=100).mean().iloc[-1]
        e200 = df['Close'].ewm(span=200).mean().iloc[-1]
        
        score = sum([1 for ema in [e20, e50, e100, e200] if akt > ema])
        if score < 2: return None
        
        return {"Ticker": ticker, "Sektor": sektor, "Score": score, "Kurs": round(akt, 2)}
    except: return None

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    
    # Analyse
    for ticker, name in sektoren_map.items():
        aktien = sektoren_aktien.get(ticker, [])
        for aktie in aktien:
            res = get_setup(aktie, name)
            if res: results.append(res)
    
    # Export
    df = pd.DataFrame(results)
    if not df.empty:
        df.to_csv(f"Setups({today}).csv", sep=';', index=False)
        # Briefing
        with open(f"Briefing_{today}.txt", "w") as f:
            f.write(f"MARKT-UPDATE {today}\n\nTOP SETUPS (Score >= 2):\n")
            f.write(df.sort_values("Score", ascending=False).to_string())
        print("Analyse erfolgreich.")
    else:
        print("Keine Setups gefunden.")
