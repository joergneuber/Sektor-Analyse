import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import time

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
    "SMH": ["NVDA", "TSM", "ASML", "AVGO", "QCOM", "TXN", "AMAT", "AMD", "LRCX", "MU"],
    "IGV": ["MSFT", "ADBE", "CRM", "ORCL", "SNOW", "PANW", "WDAY", "INTU", "NOW", "ADSK"],
    "XBI": ["AMGN", "GILD", "BIIB", "VRTX", "REGN", "ILMN", "SGEN", "EXAS", "MRNA", "TECH"],
    "KRE": ["FITB", "HBAN", "CFG", "KEY", "ZION", "RF", "CMA", "PBCT", "SNV", "HBAP"],
    "HACK": ["PANW", "CRWD", "FTNT", "OKTA", "ZS", "CYBR", "QLYS", "TENB", "VRSN", "CHKP"],
    "CLOU": ["SNOW", "CRWD", "OKTA", "ZS", "DDOG", "NET", "SPLK", "MDB", "TEAM", "DOCU"],
    "AIQ": ["NVDA", "MSFT", "GOOGL", "META", "AAPL", "AMD", "TSM", "ORCL", "ADBE", "CRM"],
    "BOTZ": ["NVDA", "ABB", "ISRG", "ROK", "TER", "ITW", "PTC", "FLIR", "TYL", "AMRC"],
    "IHI": ["ABT", "DHR", "MDT", "BSX", "SYK", "ZBH", "EW", "BAX", "RMD", "ALGN"],
    "PAVE": ["DE", "CAT", "ETN", "JCI", "PH", "IR", "CMI", "XYL", "ITW", "EMR"],
    "XRT": ["AMZN", "HD", "LOW", "TGT", "COST", "WMT", "BBY", "TJX", "ROST", "ULTA"]
}

# --- FUNKTIONEN ---
def get_sp500_data():
    try:
        hist = yf.download("^GSPC", period="300d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        if hist.empty: return None, "Fehler S&P 500"
        
        close = hist['Close']
        m = {
            "aktuell": close.iloc[-1],
            "e20": close.ewm(span=20, adjust=False).mean().iloc[-1],
            "e50": close.ewm(span=50, adjust=False).mean().iloc[-1],
            "e200": close.ewm(span=200, adjust=False).mean().iloc[-1]
        }
        text = f"S&P 500 Kurs: {m['aktuell']:.2f} | EMA 50: {m['e50']:.2f} | EMA 200: {m['e200']:.2f}"
        return m, text
    except: return None, "Fehler S&P 500"

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1)
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        if hist.empty or len(hist) < 200: return None
            
        c = hist['Close']
        highs, lows = hist['High'], hist['Low']
        akt = c.iloc[-1]
        
        # Scoring
        score = sum([1 for ema in [c.ewm(span=20).mean().iloc[-1], c.ewm(span=50).mean().iloc[-1], 
                                   c.ewm(span=100).mean().iloc[-1], c.ewm(span=200).mean().iloc[-1]] if akt > ema])
        if score < 2: return None
            
        atr = (highs - lows).rolling(14).mean().iloc[-1]
        entry = highs.rolling(20).max().iloc[-1]
        stop = lows.rolling(20).min().iloc[-1]
        risiko = entry - stop
        
        return {
            "Ticker": ticker, "Sektor": sektor, "Kurs": round(akt, 2), "Score": score,
            "Einstieg": round(entry, 2), "Stop": round(stop, 2), "CRV1": round(((entry + atr) - entry) / risiko, 2)
        }
    except: return None

# --- MAIN ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    sp500_m, sp500_txt = get_sp500_data()
    
    # Performance-Analyse
    perf_data = []
    for t, n in sektoren_map.items():
        hist = yf.download(t, period="60d", progress=False)
        if not hist.empty:
            close = hist['Close'].iloc[:, 0] if isinstance(hist['Close'], pd.DataFrame) else hist['Close']
            perf_data.append({"Ticker": t, "Sektor": n, "Score": round(((close.iloc[-1] / close.iloc[0]) - 1) * 100, 2)})
    
    df_perf = pd.DataFrame(perf_data).sort_values("Score", ascending=False)
    
    # Setups
    all_setups = []
    for _, row in df_perf.head(3).iterrows():
        for s in sektoren_aktien.get(row['Ticker'], []):
            res = analyze_a_setup(s, row['Sektor'])
            if res: all_setups.append(res)
    
    df_s = pd.DataFrame(all_setups)
    df_perf.to_csv(f"Performance({today}).csv", sep=';')
    df_s.to_csv(f"Setups({today}).csv", sep=';')
    
    print("Analyse erfolgreich abgeschlossen.")
