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
        "CRV1": round((entry + atr - entry) / risiko, 2) if risiko > 0 else 0, "CRV2": round((entry + (atr * 3) - entry) / risiko, 2) if risiko > 0 else 0
    }

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    markt_info = {"S&P 500": get_live_market_data("^GSPC"), "Nasdaq 100": get_live_market_data("^NDX")}
    
    # Daten generieren
    all_perf = [get_perf(t, n) for t, n in sektoren_map.items()]
    all_setups = [analyze_a_setup(stock, sektoren_map[s_ticker]) for s_ticker, stocks in sektoren_aktien.items() for stock in stocks]
    
    df_perf = pd.DataFrame(all_perf).sort_values("Rotation-Score", ascending=False)
    df_s = pd.DataFrame([s for s in all_setups if s and s['Score'] >= 2]).sort_values(by=['Score'], ascending=False)
    
    # Exporte
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    with open(f"Briefing_{today}.txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n" + "="*30 + "\n\nGESAMTMARKTFILTER\n")
        for m, vals in markt_info.items():
            if vals: f.write(f"{m}: " + " | ".join([f"{k}:{v:.2f}" for k, v in vals.items()]) + "\n")
        f.write("\nVALIDE SETUPS (Score >= 2)\n" + df_s.to_string(index=False))
