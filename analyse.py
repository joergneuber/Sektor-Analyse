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

def analyze_a_setup(ticker, sektor, context):
    ticker_obj = yf.Ticker(ticker)
    name = ticker_obj.info.get('longName', ticker)
    hist = ticker_obj.history(period="60d")
    
    if hist.empty:
        return {"Ticker": ticker, "Name": name, "Sektor": sektor, "Einstieg": "N/A", "Stop": "N/A", "TP1": "N/A", "TP2": "N/A", "CRV": "N/A"}

    last_close = hist['Close'].iloc[-1]
    low_60 = hist['Low'].min()
    atr = (hist['High'] - hist['Low']).rolling(window=14).mean().iloc[-1]
    
    # Kalkulationen
    einstieg = round(last_close, 2)
    stop_loss = round(min(last_close - (atr * 2), low_60 * 0.98), 2)
    
    # TP1 bleibt konservativ für Teilgewinne
    tp1 = round(einstieg + (einstieg - stop_loss), 2)
    
    # TP2 wird dynamisch: Das Maximum aus 2x Risiko ODER 60-Tage-Hoch
    high_60 = hist['High'].max()
    tp2 = round(max(einstieg + ((einstieg - stop_loss) * 2), high_60), 2)
    
    # CRV Dynamisch berechnen
    risiko = einstieg - stop_loss
    gewinn = tp2 - einstieg
    
    if risiko != 0:
        crv_wert = round(gewinn / risiko, 1)
        crv_string = f"1:{crv_wert}"
    else:
        crv_string = "N/A"
    
    # CRV Dynamisch berechnen
    risiko = einstieg - stop_loss
    gewinn = tp2 - einstieg
    
    if risiko != 0:
        crv_wert = round(gewinn / risiko, 1)
        crv_string = f"1:{crv_wert}"
    else:
        crv_string = "N/A"
    
    # Einziges Return-Statement am Ende
    return {
        "Ticker": ticker, 
        "Name": name, 
        "Sektor": sektor, 
        "Einstieg": einstieg, 
        "Begründung_Einstieg": "Einstieg zum aktuellen Schlusskurs",
        "Stop": stop_loss, 
        "Begründung_Stop": "Absicherung unter 60T-Tief und ATR-Puffer",
        "TP1": tp1, 
        "Begründung_TP1": "Teilgewinnmitnahme bei 1:1 Chance-Risiko-Verhältnis",
        "TP2": tp2, 
        "Begründung_TP2": "Vollständiger Ausstieg bei 1:2 Chance-Risiko-Verhältnis",
        "CRV": crv_string
    }
    
# 1. Marktstatus abrufen
markt_status, markt_details = get_market_status()
market_context = f"{markt_status} - {markt_details}"

# 2. Performance berechnen
perf_list = [get_perf(t, n) for t, n in sektoren_map.items()]
df_perf = pd.DataFrame(perf_list).sort_values("Rotation-Score", ascending=False)

# 3. Hauptlogik - Analyse der Top-Sektoren
print("Starte Analyse...")
setups = []

# Wir gehen die Top-Sektoren durch
for index, row in df_perf.head(2).iterrows():
    sektor_name = row['Sektor']
    ticker_key = row['Ticker']
    
    # HIER ist die innere Schleife, die 't' definiert:
    for t in sektoren_aktien.get(ticker_key, [])[:3]:
        # Jetzt kennt Python 't' und kann es an analyze_a_setup übergeben
        setups.append(analyze_a_setup(t, sektor_name, market_context))

df_setups = pd.DataFrame(setups)

# Marktstatus in das Setup-Log oder den Report einfügen
print(f"--- Marktstatus ---")
print(f"Trend: {markt_status}")
print(f"Details: {markt_details}")

# 4. Marktbericht speichern
with open("Marktbericht.txt", "w") as f:
    f.write(f"Trend SPY: {markt_status}\n")
    f.write(f"Details: {markt_details}\n")

# 4. Speichern mit aktuellem Datum
base_path = os.getcwd()
today = datetime.now().strftime("%Y-%m-%d")

# Hier werden die Markt-Daten in den DataFrame geschrieben, 
# damit sie in der CSV landen:
df_setups['Markt_Trend'] = markt_status
df_setups['Markt_Details'] = markt_details

# Dateinamen dynamisch erstellen
perf_filename = f"Performance({today}).csv"
setups_filename = f"Setups({today}).csv"

perf_path = os.path.join(base_path, perf_filename)
setups_path = os.path.join(base_path, setups_filename)

df_perf.to_csv(perf_path, index=False)
df_setups.to_csv(setups_path, index=False)

print(f"Dateien erfolgreich gespeichert unter: {base_path}")
print(f"Dateinamen: {perf_filename} und {setups_filename}")
