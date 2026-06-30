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
def get_market_status():
    market = yf.Ticker("^GSPC")
    hist = market.history(period="250d") 
    if hist.empty: return "Neutral", "Keine Marktdaten"
    c, e50, e200 = hist['Close'].iloc[-1], hist['Close'].ewm(span=50).mean().iloc[-1], hist['Close'].ewm(span=200).mean().iloc[-1]
    status = "Bullish" if c > e50 and c > e200 else ("Bearish" if c < e50 and c < e200 else "Neutral")
    return status, f"S&P 500: {c:.2f} | EMA50: {e50:.2f} | EMA200: {e200:.2f}"

def get_perf(ticker, name):
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty: return {"Ticker": ticker, "Sektor": name, "Rotation-Score": 0}
    last = data['Close'].iloc[-1]
    def get_p(d): return round(((last / data['Close'].iloc[-d]) - 1) * 100, 2) if len(data) >= d else 0
    p5, p12 = get_p(5), get_p(12)
    return {"Ticker": ticker, "Sektor": name, "Rotation-Score": round((p5 * 0.7 + p12 * 0.3), 3)}

def analyze_a_setup(ticker, sektor, context):
    hist = yf.Ticker(ticker).history(period="250d")
    if hist.empty or len(hist) < 200: return None
    
    # Charttechnische Anker
    low_20 = hist['Low'].rolling(20).min().iloc[-1]
    high_20 = hist['High'].rolling(20).max().iloc[-1]
    ema200 = hist['Close'].ewm(span=200).mean().iloc[-1]
    
    # 1. Einstieg: Bestätigung am Pullback-Niveau (z.B. Fib 61.8% des letzten Swings)
    entry = round(max(hist['Close'].iloc[-1], (high_20 - (high_20 - low_20) * 0.382)), 2)
    
    # 2. Stop-Loss: Unter signifikantes Tief
    stop_loss = round(min(low_20, ema200 * 0.97), 2)
    risiko = entry - stop_loss
    
    # 3. TP1: Konservatives Ziel (Nächstes Widerstandsniveau/Verlaufshoch)
    tp1 = round(high_20, 2)
    
    # 4. TP2: Erweitertes Ziel (Fib-Extension 161.8% des Risikos)
    tp2 = round(entry + (risiko * 1.618), 2)
    
    # Dynamisches CRV
    crv1 = round((tp1 - entry) / risiko, 2) if risiko > 0 else 0.0
    crv2 = round((tp2 - entry) / risiko, 2) if risiko > 0 else 0.0
    
    return {
        "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), "Sektor": sektor,
        "Einstieg": entry, "Stop": stop_loss, "TP1": tp1, "TP2": tp2, "CRV1": crv1, "CRV2": crv2, "Trend": context
    }

# --- HAUPTTEIL ---
if __name__ == "__main__":
    markt_status, markt_details = get_market_status()
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    all_setups = [
        analyze_a_setup(t, row['Sektor'], markt_status) 
        for _, row in df_perf.head(2).iterrows() 
        for t in sektoren_aktien.get(row['Ticker'], [])
    ]
    
    if (setups := [s for s in all_setups if s]):
        today = datetime.now().strftime("%Y-%m-%d")
        df_s = pd.DataFrame(setups)
        df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
        with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
            f.write(f"Markt-Update {today}: {markt_details}\n\n" + df_s.to_string(index=False))
