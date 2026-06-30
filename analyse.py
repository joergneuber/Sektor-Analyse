import yfinance as yf
import pandas as pd
from datetime import datetime

# --- KONFIGURATION ---
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

# --- FUNKTIONEN ---
def get_live_market_data(ticker):
    df = yf.download(ticker, period="250d", interval="1d", progress=False)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    c = df['Close']
    return {
        "EMA20": c.ewm(span=20).mean().iloc[-1],
        "EMA50": c.ewm(span=50).mean().iloc[-1],
        "EMA100": c.ewm(span=100).mean().iloc[-1],
        "EMA200": c.ewm(span=200).mean().iloc[-1]
    }

def analyze_a_setup(ticker, sektor):
    hist = yf.download(ticker, period="250d", progress=False)
    if hist.empty or len(hist) < 200: return None
    if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
    
    for span in [20, 50, 100, 200]: hist[f'EMA{span}'] = hist['Close'].ewm(span=span).mean()
    atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
    
    curr = round(hist['Close'].iloc[-1], 2)
    entry = round(hist['High'].rolling(20).max().iloc[-1], 2)
    stop = round(hist['Low'].rolling(20).min().iloc[-1], 2)
    risiko = entry - stop
    
    return {
        "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), 
        "Score": sum([1 for e in [20, 50, 100, 200] if hist['Close'].iloc[-1] > hist[f'EMA{e}'].iloc[-1]]),
        "Kurs": curr, "Einstieg": entry, "Stop": stop, 
        "TP1": round(entry + atr, 2), "TP2": round(entry + (atr * 3), 2),
        "CRV1": round((entry + atr - entry) / risiko, 2) if risiko > 0 else 0,
        "CRV2": round((entry + (atr * 3) - entry) / risiko, 2) if risiko > 0 else 0
    }

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 1. Marktfilter
    markt_info = {"S&P 500": get_live_market_data("^GSPC"), "Nasdaq 100": get_live_market_data("^NDX")}
    
    # 2. Setups (Beispiel für Top 2 Sektoren)
    all_setups = []
    for sector_ticker in list(sektoren_map.keys())[:2]:
        for stock in sektoren_aktien.get(sector_ticker, []):
            res = analyze_a_setup(stock, sektoren_map[sector_ticker])
            if res and res['Score'] >= 2: all_setups.append(res)
            
    # 3. Export
    with open(f"Briefing_{today}.txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n" + "="*30 + "\n\nGESAMTMARKTFILTER\n")
        for m, vals in markt_info.items():
            if vals: f.write(f"{m}: " + " | ".join([f"{k}:{v:.2f}" for k, v in vals.items()]) + "\n")
        
        f.write("\nVALIDE SETUPS (Score >= 2)\n")
        f.write(pd.DataFrame(all_setups).to_string(index=False))

print("Briefing erfolgreich generiert.")
