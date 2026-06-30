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
        hist = yf.download(ticker, period="120d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist = hist['Close']
        if hist.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
        close = hist.iloc[:, 0] if isinstance(hist, pd.DataFrame) else hist
        last = close.iloc[-1]
        def p(d): return round(((last / close.iloc[-d]) - 1) * 100, 2)
        res = {"Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60)}
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res
    except: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}

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
        tp1 = entry + atr
        tp2 = entry + (atr * 3)
        return {
            "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), "Sektor": sektor,
            "Kurs": round(hist['Close'].iloc[-1], 2), "Einstieg": round(entry, 2), "Stop": round(stop, 2),
            "TP1": round(tp1, 2), "TP2": round(tp2, 2),
            "CRV1": round((tp1 - entry) / risiko, 2) if risiko > 0 else 0,
            "CRV2": round((tp2 - entry) / risiko, 2) if risiko > 0 else 0
        }
    except: return None

if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    all_setups = []
    for _, row in df_perf.head(2).iterrows():
        sector_results = [analyze_a_setup(s, row['Sektor']) for s in sektoren_aktien.get(row['Ticker'], [])]
        all_setups.extend(sorted([r for r in sector_results if r], key=lambda x: x['CRV1'], reverse=True)[:5])
    
    df_s = pd.DataFrame(all_setups)
    
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    with open(f"Briefing_{today}.txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n" + "="*30 + "\n\nPERFORMANCE\n" + df_perf.to_string(index=False))
        f.write("\n\nTOP SETUPS\n" + df_s.to_string(index=False))
