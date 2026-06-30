import yfinance as yf
import pandas as pd
import os
from datetime import datetime

# 1. Konfiguration - MUSS GANZ OBEN STEHEN
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

# 2. Hilfsfunktionen - MÜSSEN AM LINKEN RAND STEHEN
def get_market_status():
    # ^GSPC ist das offizielle Ticker-Symbol für den S&P 500 bei Yahoo Finance
    market = yf.Ticker("^GSPC")
    hist = market.history(period="250d") 
    
    if hist.empty:
        return "Neutral", "Marktdaten aktuell nicht abrufbar"
    
    current_close = hist['Close'].iloc[-1]
    ema50 = hist['Close'].ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = hist['Close'].ewm(span=200, adjust=False).mean().iloc[-1]
    
    # Einordnung
    if current_close > ema50 and current_close > ema200:
        status = "Bullish"
    elif current_close < ema50 and current_close < ema200:
        status = "Bearish"
    else:
        status = "Neutral (Korrekturphase oder Seitwärts)"
        
    details = f"S&P 500 Kurs: {current_close:.2f} | EMA50: {ema50:.2f} | EMA200: {ema200:.2f}"
    return status, details
def get_perf(ticker, name):
    # Historie laden (120 Tage für Sicherheit)
    data = yf.Ticker(ticker).history(period="120d")
    if data.empty:
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
    
    last_close = data['Close'].iloc[-1]
    
    # Hilfsfunktion für sichere Berechnung
    def safe_calc(days):
        if days >= len(data): return (last_close / data['Close'].iloc[0]) - 1
        return (last_close / data['Close'].iloc[-days]) - 1

    p5 = safe_calc(5)
    p12 = safe_calc(12)
    p30 = safe_calc(30)
    p60 = safe_calc(60)
    rs = (p5 * 0.7) + (p12 * 0.3)
    
    # Ergebnisse in Prozent (x100) mit 3 Nachkommastellen
    return {
        "Ticker": ticker, "Sektor": name, 
        "5T": round(p5 * 100, 3), "12T": round(p12 * 100, 3), 
        "30T": round(p30 * 100, 3), "60T": round(p60 * 100, 3), 
        "Rotation-Score": round(rs * 100, 3)
    }

import yfinance as yf
import pandas as pd
import numpy as np

def analyze_a_setup(ticker, sektor, context):
    ticker_obj = yf.Ticker(ticker)
    name = ticker_obj.info.get('longName', ticker)
    hist = ticker_obj.history(period="200d")
    
    if hist.empty or len(hist) < 200:
        return None

    # --- 1. Indikatoren berechnen ---
    hist['EMA50'] = hist['Close'].ewm(span=50).mean()
    hist['EMA200'] = hist['Close'].ewm(span=200).mean()
    
    # RSI & BB für Signale
    delta = hist['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    hist['RSI'] = 100 - (100 / (1 + (gain / loss)))
    
    sma20 = hist['Close'].rolling(window=20).mean()
    std20 = hist['Close'].rolling(window=20).std()
    hist['BB_Width'] = (sma20 + (std20 * 2)) - (sma20 - (std20 * 2))

    # --- 2. Charttechnische Werte ---
    close = hist['Close'].iloc[-1]
    ema50 = hist['EMA50'].iloc[-1]
    ema200 = hist['EMA200'].iloc[-1]
    low_20 = hist['Low'].iloc[-20:].min()
    high_20 = hist['High'].iloc[-20:].max()
    
    einstieg = round(ema50, 2)
    support_level = min(ema200, low_20)
    stop_loss = round(support_level * 0.98, 2)
    risiko = einstieg - stop_loss
    
    tp1 = round(high_20, 2)
    tp2 = round(max(einstieg + (risiko * 2.5), high_20 * 1.05), 2)
    
    # --- 3. Scoring & Signale ---
    is_golden_cross = (hist['EMA50'].iloc[-2] < hist['EMA200'].iloc[-2]) & (hist['EMA50'].iloc[-1] > hist['EMA200'].iloc[-1])
    is_breakout = close > high_20
    is_oversold = hist['RSI'].iloc[-1] < 40
    score = (2 if is_golden_cross else 0) + (1 if is_breakout else 0) + (1 if is_oversold else 0)
    
    # --- 4. Markt-Kontext ---
    markt_trend = "Bullish" if close > ema200 else "Bearish"
    markt_details = f"Kurs: {round(close, 2)} | EMA50: {round(ema50, 2)} | EMA200: {round(ema200, 2)}"

    return {
        "Ticker": ticker, "Name": name, "Sektor": sektor,
        "Score": score, "Setup": "Breakout" if is_breakout else "Neutral",
        "Einstieg": einstieg, "Stop": stop_loss,
        "CRV_TP1": round((tp1 - einstieg) / risiko, 1) if risiko > 0 else 0,
        "CRV_TP2": round((tp2 - einstieg) / risiko, 1) if risiko > 0 else 0,
        "Markt_Trend": markt_trend, "Markt_Details": markt_details
    }

# --- HIER IST DIE KORREKTE REIHENFOLGE ---

print("Starte Analyse...")
setups = []  # 1. Variable HIER initialisieren

# 2. Schleife zum Füllen der Liste
for index, row in df_perf.head(2).iterrows():
    sektor_name = row['Sektor']
    ticker_key = row['Ticker']
    
    for t in sektoren_aktien.get(ticker_key, [])[:3]:
        res = analyze_a_setup(t, sektor_name, market_context)
        if res:
            setups.append(res)
        else:
            print(f"DEBUG: Keine Daten für {t}")

# 3. JETZT ist 'setups' bekannt und kann benutzt werden
print(f"DEBUG: Anzahl der gesammelten Setups: {len(setups)}")

# 4. Erst jetzt wird das DataFrame erzeugt
df_setups = pd.DataFrame(setups)

# ... weiter mit deinem Speicher-Code
if not df_setups.empty:
    # Filter: Entferne Rauschen (Score >= 1)
    df_setups = df_setups[df_setups['Score'] >= 1]
    
    # Sortieren
    df_setups = df_setups.sort_values(by=['Score', 'CRV_TP2'], ascending=[False, False])
    
    # Markt-Details hinzufügen
    df_setups['Markt_Trend'] = markt_status
    df_setups['Markt_Details'] = markt_details

# 5. Speichern
today = datetime.now().strftime("%Y-%m-%d")
setups_path = os.path.join(os.getcwd(), f"Setups({today}).csv")
df_setups.to_csv(setups_path, index=False, sep=';', encoding='utf-8-sig')

print(f"Analyse fertig. {len(df_setups)} Setups in {setups_path} gespeichert.")
