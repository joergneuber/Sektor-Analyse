import yfinance as yf
import pandas as pd
from datetime import datetime
import time

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

def get_perf(ticker, name):
    try:
        hist = yf.download(ticker, period="60d", progress=False)
        # Fix: Extrahiere nur die Close-Werte und entferne MultiIndex
        if isinstance(hist.columns, pd.MultiIndex):
            hist = hist['Close']
        if hist.empty: return {"Ticker": ticker, "Sektor": name, "Rotation-Score": 0.0}
        
        close = hist.iloc[:, 0] if isinstance(hist, pd.DataFrame) else hist
        last = close.iloc[-1]
        p5 = ((last / close.iloc[-5]) - 1) * 100
        p12 = ((last / close.iloc[-12]) - 1) * 100
        return {"Ticker": ticker, "Sektor": name, "Rotation-Score": round((p5 * 0.7 + p12 * 0.3), 3)}
    except: return {"Ticker": ticker, "Sektor": name, "Rotation-Score": 0.0}

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1)
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        if hist.empty or len(hist) < 200: return None
        
        atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
        entry = hist['High'].rolling(20).max().iloc[-1]
        stop = hist['Low'].rolling(20).min().iloc[-1]
        risiko = entry - stop
        return {
            "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), 
            "Sektor": sektor, "CRV1": round((entry + atr - entry) / risiko, 2) if risiko > 0 else 0
        }
    except: return None

if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Sicherstellen, dass die Daten als Liste von Dicts korrekt konvertiert werden
    perf_list = [get_perf(t, n) for t, n in sektoren_map.items()]
    df_perf = pd.DataFrame(perf_list)
    df_perf = df_perf.sort_values("Rotation-Score", ascending=False)
    top_2 = df_perf.head(2)
    
    final_setups = []
    for _, row in top_2.iterrows():
        s_ticker = row['Ticker']
        sector_results = []
        for stock in sektoren_aktien.get(s_ticker, []):
            res = analyze_a_setup(stock, sektoren_map[s_ticker])
            if res: sector_results.append(res)
        final_setups.extend(sorted(sector_results, key=lambda x: x['CRV1'], reverse=True)[:5])
    
    df_s = pd.DataFrame(final_setups)
    
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    print("Export erfolgreich.")
