import yfinance as yf
import pandas as pd
from datetime import datetime
import time

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
def get_live_market_data(ticker):
    df = yf.download(ticker, period="250d", interval="1d", progress=False)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    c = df['Close']
    return {"EMA20": c.ewm(span=20).mean().iloc[-1], "EMA50": c.ewm(span=50).mean().iloc[-1], 
            "EMA100": c.ewm(span=100).mean().iloc[-1], "EMA200": c.ewm(span=200).mean().iloc[-1]}

def get_perf(ticker, name):
    hist = yf.download(ticker, period="120d", progress=False)
    if hist.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
    if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
    last = hist['Close'].iloc[-1]
    def get_p(d): return round(((last / hist['Close'].iloc[-d]) - 1) * 100, 2) if len(hist) >= d else 0
    return {"Ticker": ticker, "Sektor": name, "5T": get_p(5), "12T": get_p(12), "30T": get_p(30), "60T": get_p(60), "Rotation-Score": round((get_p(5) * 0.7 + get_p(12) * 0.3), 3)}

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1) # Verhindert Blocking
    hist = yf.download(ticker, period="250d", progress=False)
    if hist.empty or len(hist) < 200: return None
    if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
    for span in [20, 50, 100, 200]: hist[f'EMA{span}'] = hist['Close'].ewm(span=span).mean()
    atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
    curr, entry, stop = round(hist['Close'].iloc[-1], 2), round(hist['High'].rolling(20).max().iloc[-1], 2), round(hist['Low'].rolling(20).min().iloc[-1], 2)
    risiko = entry - stop
    return {
        "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), "Sektor": sektor,
        "Score": sum([1 for e in [20, 50, 100, 200] if hist['Close'].iloc[-1] > hist[f'EMA{e}'].iloc[-1]]),
        "Kurs": curr, "Einstieg": entry, "Stop": stop, "TP1": round(entry + atr, 2), "TP2": round(entry + (atr * 3), 2),
        "CRV1": round((entry + atr - entry) / risiko, 2) if risiko > 0 else 0.0, 
        "CRV2": round((entry + (atr * 3) - entry) / risiko, 2) if risiko > 0 else 0.0
    }

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    markt_info = {"S&P 500": get_live_market_data("^GSPC"), "Nasdaq 100": get_live_market_data("^NDX")}
    
    # 1. Performance
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    top_2_sektoren = df_perf.head(2)['Ticker'].tolist()
    
    # 2. Setups
    all_setups = []
    for s_ticker in top_2_sektoren:
        for stock in sektoren_aktien.get(s_ticker, []):
            res = analyze_a_setup(stock, sektoren_map[s_ticker])
            if res: all_setups.append(res)
            
    df_s = pd.DataFrame(all_setups).sort_values(by=['CRV1', 'CRV2'], ascending=[False, False]) if all_setups else pd.DataFrame()
    
    # 3. Export
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    with open(f"Briefing_{today}.txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n" + "="*30 + "\n\nGESAMTMARKTFILTER\n")
        for m, vals in markt_info.items():
            if vals: f.write(f"{m}: " + " | ".join([f"{k}:{v:.2f}" for k, v in vals.items()]) + "\n")
        f.write("\nTOP 2 SEKTOR SETUPS\n" + df_s.to_string(index=False))
