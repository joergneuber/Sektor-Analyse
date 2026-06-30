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
def check_marktfilter():
    markt_status = {}
    for ticker in ["SPY", "QQQ"]:
        try:
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            c = df['Close']
            ema20, ema50 = c.ewm(span=20).mean().iloc[-1], c.ewm(span=50).mean().iloc[-1]
            ema100, ema200 = c.ewm(span=100).mean().iloc[-1], c.ewm(span=200).mean().iloc[-1]
            markt_status[ticker] = f"E20:{ema20:.2f} | E50:{ema50:.2f} | E100:{ema100:.2f} | E200:{ema200:.2f}"
        except:
            markt_status[ticker] = "Daten aktuell nicht berechenbar"
    return markt_status

def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
    last = data['Close'].iloc[-1]
    def get_p(d): return round(((last / data['Close'].iloc[-d]) - 1) * 100, 2) if len(data) >= d else 0
    p5, p12, p30, p60 = get_p(5), get_p(12), get_p(30), get_p(60)
    return {"Ticker": ticker, "Sektor": name, "5T": p5, "12T": p12, "30T": p30, "60T": p60, "Rotation-Score": round((p5 * 0.7 + p12 * 0.3), 3)}

def analyze_a_setup(ticker, sektor, context):
    hist = yf.Ticker(ticker).history(period="250d")
    if hist.empty or len(hist) < 200: return None
    for span in [20, 50, 100, 200]: hist[f'EMA{span}'] = hist['Close'].ewm(span=span).mean()
    
    hist['TR'] = hist['High'] - hist['Low']
    atr = hist['TR'].rolling(14).mean().iloc[-1]
    
    current_price = round(hist['Close'].iloc[-1], 2)
    low_20 = hist['Low'].rolling(20).min().iloc[-1]
    
    entry = round(hist['High'].rolling(20).max().iloc[-1], 2)
    tp1 = round(entry + atr, 2)
    tp2 = round(entry + (atr * 3), 2)
    stop_loss = round(low_20, 2)
    risiko = entry - stop_loss
    
    crv1 = round((tp1 - entry) / risiko, 2) if risiko > 0 else 0.0
    crv2 = round((tp2 - entry) / risiko, 2) if risiko > 0 else 0.0
    
    score = sum([1 for e in [20, 50, 100, 200] if hist['Close'].iloc[-1] > hist[f'EMA{e}'].iloc[-1]])
    
    return {
        "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), "Sektor": sektor,
        "Score": score, "Kurs": current_price, "Einstieg": entry, "Stop": stop_loss, 
        "TP1": tp1, "TP2": tp2, "CRV1": crv1, "CRV2": crv2, "Trend": context
    }

# --- HAUPTTEIL ---
if __name__ == "__main__":
    markt = check_marktfilter()
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    all_setups = [analyze_a_setup(t, row['Sektor'], "Trend aktiv") for _, row in df_perf.head(2).iterrows() for t in sektoren_aktien.get(row['Ticker'], [])]
    
    if (setups := [s for s in all_setups if s]):
        today = datetime.now().strftime("%Y-%m-%d")
        df_s = pd.DataFrame(setups).sort_values(by=['Score'], ascending=False)
        
        df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
        df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
        
        with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
            f.write(f"Markt-Update {today}\n" + "="*30 + "\n\n")
            f.write("MARKTFILTER (EMA-WERTE)\n")
            for m, status in markt.items(): f.write(f"{m}: {status}\n")
            f.write("\n" + df_s[["Ticker", "Name", "Score", "Kurs", "Einstieg", "Stop", "TP1", "TP2", "CRV1", "CRV2"]].to_string(index=False))
            f.write("\n\n--- Performance-Daten Übersicht ---\n\n")
            f.write(df_perf.to_string(index=False))
