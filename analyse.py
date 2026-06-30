import yfinance as yf
import pandas as pd
import numpy as np
import os
from datetime import datetime

# --- 1. KONFIGURATION ---
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

# --- 2. HILFSFUNKTIONEN ---
def get_market_status():
    market = yf.Ticker("^GSPC")
    hist = market.history(period="250d") 
    if hist.empty: return "Neutral", "Keine Marktdaten"
    c = hist['Close'].iloc[-1]
    e50 = hist['Close'].ewm(span=50).mean().iloc[-1]
    e200 = hist['Close'].ewm(span=200).mean().iloc[-1]
    status = "Bullish" if c > e50 and c > e200 else ("Bearish" if c < e50 and c < e200 else "Neutral")
    return status, f"S&P 500: {c:.2f} | EMA50: {e50:.2f} | EMA200: {e200:.2f}"

def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "Rotation-Score": 0}
    last = data['Close'].iloc[-1]
    p5 = (last / data['Close'].iloc[-5]) - 1
    return {"Ticker": ticker, "Sektor": name, "Rotation-Score": round(p5*100, 3)}

def analyze_a_setup(ticker, sektor, context):
    hist = yf.Ticker(ticker).history(period="250d")
    if hist.empty or len(hist) < 200: return None
    
    hist['EMA20'] = hist['Close'].ewm(span=20).mean()
    hist['EMA50'] = hist['Close'].ewm(span=50).mean()
    hist['EMA100'] = hist['Close'].ewm(span=100).mean()
    hist['EMA200'] = hist['Close'].ewm(span=200).mean()
    
    # ATR Berechnung
    tr = pd.concat([hist['High']-hist['Low'], abs(hist['High']-hist['Close'].shift()), abs(hist['Low']-hist['Close'].shift())], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    
    close = hist['Close'].iloc[-1]
    c_ema20, c_ema50, c_ema100, c_ema200 = (1 if close > hist[e].iloc[-1] else 0 for e in ['EMA20','EMA50','EMA100','EMA200'])
    c_mom = 1 if hist['EMA20'].iloc[-1] > hist['EMA50'].iloc[-1] else 0
    c_imp = 1 if close > hist['Close'].iloc[-5] else 0
    
    delta = hist['Close'].diff()
    rsi = 100 - (100 / (1 + (delta.where(delta > 0, 0).rolling(14).mean() / (-delta.where(delta < 0, 0).rolling(14).mean()))))
    c_rsi = -1 if rsi.iloc[-1] > 75 else 0
    
    return {
        "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), "Sektor": sektor,
        "Score": c_ema20 + c_ema50 + c_ema100 + c_ema200 + c_mom + c_imp + c_rsi,
        "EMA20": c_ema20, "EMA50": c_ema50, "EMA100": c_ema100, "EMA200": c_ema200,
        "Momentum": c_mom, "Impuls": c_imp, "RSI_Warn": c_rsi,
        "Einstieg": round(close, 2), "Stop": round(close - (atr * 2), 2),
        "CRV_TP1": 1.0, "CRV_TP2": 2.5, "Markt_Trend": context
    }

# --- 3. HAUPTTEIL ---
markt_status, markt_details = get_market_status()
df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
setups = []
for index, row in df_perf.head(2).iterrows():
    for t in sektoren_aktien.get(row['Ticker'], [])[:3]:
        res = analyze_a_setup(t, row['Sektor'], f"{markt_status} - {markt_details}")
        if res: setups.append(res)

if setups:
    df_s = pd.DataFrame(setups).sort_values(by=['Score'], ascending=False)
    today = datetime.now().strftime("%Y-%m-%d")
    for f in ["Setups.csv", f"Setups({today}).csv"]:
        df_s.to_csv(os.path.join(os.getcwd(), f), index=False, sep=';', encoding='utf-8-sig')
    print(f"Analyse fertig: {len(df_s)} Aktien analysiert.")
