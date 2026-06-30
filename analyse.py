import yfinance as yf
import pandas as pd
import numpy as np
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
def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty: return {"Ticker": ticker, "Sektor": name, "Rotation-Score": 0}
    last = data['Close'].iloc[-1]
    def get_p(d): return round(((last / data['Close'].iloc[-d]) - 1) * 100, 2) if len(data) >= d else 0
    p5, p12, p30, p60 = get_p(5), get_p(12), get_p(30), get_p(60)
    return {"Ticker": ticker, "Sektor": name, "5T": p5, "12T": p12, "30T": p30, "60T": p60, "Rotation-Score": round((p5 * 0.7 + p12 * 0.3), 3)}

def analyze_a_setup(ticker, sektor, context):
    hist = yf.Ticker(ticker).history(period="250d")
    if hist.empty or len(hist) < 200: return None
    
    for span in [20, 50, 100, 200]: hist[f'EMA{span}'] = hist['Close'].ewm(span=span).mean()
    
    close = hist['Close'].iloc[-1]
    low_20 = hist['Low'].rolling(20).min().iloc[-1]
    high_20 = hist['High'].rolling(20).max().iloc[-1]
    resistance_50 = hist['High'].rolling(50).max().iloc[-1]
    
    score = sum([1 for e in [20, 50, 100, 200] if close > hist[f'EMA{e}'].iloc[-1]])
    
    entry = round(max(close
