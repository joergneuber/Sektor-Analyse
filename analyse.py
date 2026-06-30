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
    if hist.empty: return "Neutral", "Marktdaten nicht abrufbar"
    current_close = hist['Close'].iloc[-1]
    ema50 = hist['Close'].ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = hist['Close'].ewm(span=200, adjust=False).mean().iloc[-1]
    status = "Bullish" if current_close > ema50 and current_close > ema200 else ("Bearish" if current_close < ema50 and current_close < ema200 else "Neutral")
    details = f"S&P 500 Kurs: {current_close:.2f} | EMA50: {ema50:.2f} | EMA200: {ema200:.2f}"
    return status, details

def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
    last_close = data['Close'].iloc[-1]
    def safe_calc(days): return (last_close / data['Close'].iloc[-days]) - 1 if days < len(data) else 0
    p5, p12, p30, p60 = safe_calc(5), safe_calc(12), safe_calc(30), safe_calc(60)
    return {"Ticker": ticker, "Sektor": name, "5T": round(p5*100, 3), "12T": round(p12*100, 3), "30T": round(p30*100, 3), "60T": round(p60*100, 3), "Rotation-Score": round(((p5*0.7) + (p12*0.3))*100, 3)}

def analyze_a_setup(ticker, sektor, context):
    ticker_obj = yf.Ticker(ticker)
    name = ticker_obj.info.get('longName', ticker)
    hist = ticker_obj.history(period="200d")
    if hist.empty or len(hist) < 200: return None
    hist['EMA50'] = hist['Close'].ewm(span=50).mean()
    hist['EMA200'] = hist['Close'].ewm(span=200).mean()
    hist['RSI'] = 100 - (100 / (1 + (hist['Close'].diff().where(hist['Close'].diff()>0, 0).rolling(14).mean() / (-hist['Close'].diff().where(hist['Close'].diff()<0, 0).rolling(14).mean()))))
    close, ema50, ema200 = hist['Close'].iloc[-1], hist['EMA50'].iloc[-1], hist['EMA200'].iloc[-1]
    low_20, high_20 = hist['Low'].iloc[-20:].min(), hist['High'].iloc[-20:].max()
    einstieg = round(ema50, 2)
    stop
