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
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA"],
    "SOXX": ["NVDA", "AVGO", "TXN", "QCOM", "INTC", "AMD", "MU", "ADI", "LRCX", "AMAT"],
    "SMH": ["NVDA", "TSM", "ASML", "AVGO", "QCOM", "TXN", "AMAT", "AMD", "LRCX", "MU"]
}

# --- FUNKTIONEN ---
def get_live_market_data(ticker):
    try:
        df = yf.download(ticker, period="250d", interval="1d", progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        c = df['Close']
        return {"EMA20": c.ewm(span=20).mean().iloc[-1], "EMA50": c.ewm(span=50).mean().iloc[-1], 
                "EMA100": c.ewm(span=100).mean().iloc[-1], "EMA200": c.ewm(span=200).mean().iloc[-1]}
    except: return None

def get_perf(ticker, name):
    try:
        hist = yf.download(ticker, period="120d", progress=False)
        if hist.empty: return {"Ticker": ticker, "Sektor": name, "Rotation-Score": 0.0}
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        last = hist['Close'].iloc[-1]
        p5 = round(((last / hist['Close'].iloc[-5]) - 1) * 100, 2)
        p12 = round(((last / hist['Close'].iloc[-12]) - 1) * 100, 2)
        return {"Ticker": ticker, "Sektor": name, "Rotation-Score": round((p5 * 0.7 + p12 * 0.3), 3)}
    except: return {"Ticker": ticker, "Sektor": name, "Rotation-Score": 0.0}

def analyze_a_setup(ticker, sektor):
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if hist.empty or len(hist) < 2
