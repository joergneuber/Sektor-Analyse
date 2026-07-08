import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import time
import sys
import os
from groq import Groq

# --- KONFIGURATION ---
# Initialisiere den Groq Client
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

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
    "XLE": ["XOM", "CVX", "SLB", "COP", "EOG", "MPC", "PSX", "VLO", "HAL", "OXY"],
    "XLI": ["CAT", "GE", "HON", "BA", "UPS", "LMT", "DE", "MMM", "RTX", "UNP"],
    "XLB": ["LIN", "APD", "ECL", "SHW", "FCX", "NEM", "DD", "DOW", "PPG", "VMC"],
    "XLU": ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PEG", "ED", "XEL"],
    "XLRE": ["PLD", "AMT", "EQIX", "PSA", "SPG", "O", "DLR", "WELL", "AVB", "CCI"],
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA"],
    "SOXX": ["NVDA", "AVGO", "TXN", "QCOM", "INTC", "AMD", "MU", "ADI", "LRCX", "AMAT"],
    "SMH": ["NVDA", "TSM", "ASML", "AVGO", "QCOM", "TXN", "AMAT", "AMD", "LRCX", "MU"],
    "IGV": ["MSFT", "ADBE", "CRM", "ORCL", "SNOW", "PANW", "WDAY", "INTU", "NOW", "ADSK"],
    "XBI": ["AMGN", "GILD", "BIIB", "VRTX", "REGN", "ILMN", "TECH", "MRNA", "IBB"],
    "KRE": ["FITB", "HBAN", "CFG", "KEY", "ZION", "RF", "CMA", "SNV", "NYCB", "WBS"],
    "HACK": ["PANW", "CRWD", "FTNT", "OKTA", "ZS", "CHKP", "QLYS", "TENB", "VRSN"],
    "CLOU": ["SNOW", "CRWD", "OKTA", "ZS", "DDOG", "NET", "MDB", "TEAM", "DOCU"],
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
        # Fix für MultiIndex-Spalten falls nötig
        if isinstance(hist.columns, pd.MultiIndex): 
            hist.columns = hist.columns.get_level_values(0)
            
        if hist.empty or len(hist) < 200:
            return "S&P 500: Nicht bewertet (Daten unvollständig)"
        
        close = hist['Close']
        last_close = close.iloc[-1]
        
        # Indikatoren berechnen
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        return (f"S&P 500: {last_close:.2f} | EMA20: {e20:.0f} | EMA50: {e50:.0f} | "
                f"EMA200: {e200:.0f} | WMA200: {w200:.0f}")
    except Exception as e:
        return f"S&P 500: Fehler beim Abruf ({e})"

def get_qqq_quote():
    try:
        hist = yf.download("QQQ", period="300d", progress=False)
        # Fix für MultiIndex-Spalten falls nötig
        if isinstance(hist.columns, pd.MultiIndex): 
            hist.columns = hist.columns.get_level_values(0)
            
        if hist.empty or len(hist) < 200:
            return "Nasdaq: Nicht bewertet (Daten unvollständig)"
            
        close = hist['Close']
        last_close = close.iloc[-1]
        
        # Indikatoren berechnen
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        return (f"Nasdaq: {last_close:.2f} | EMA20: {e20:.0f} | EMA50: {e50:.0f} | "
                f"EMA200: {e200:.0f} | WMA200: {w200:.0f}")
    except Exception as e:
        return f"Nasdaq: Fehler beim Abruf ({e})"

def get_perf(ticker, name):
    try:
        hist = yf.download(ticker, period="1y", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist = hist['Close']
        if hist.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}
        
        close = hist.iloc[:, 0] if isinstance(hist, pd.DataFrame) else hist
        last = close.iloc[-1]
        
        def p(d): return round(((last / close.iloc[-d]) - 1) * 100, 2)
        
        current_year = datetime.datetime.now().year
        ytd_data = close[close.index.year == current_year]
        ytd_perf = round(((last / ytd_data.iloc[0]) - 1) * 100, 2) if not ytd_data.empty else 0
            
        res = {
            "Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60), "YTD": ytd_perf
        }
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res
    except: 
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

def calculate_retest_entry(hist, breakout_level):
    close = hist['Close']
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
    
    primary = [val for val in [ema20, ema50] if val < breakout_level]
    secondary = [val for val in [ema100, ema200] if val < breakout_level]
    
    if primary: return round(max(primary), 2), "Re-Test"
    if secondary: return round(max(secondary), 2), "Re-Test"
    return round(breakout_level * 0.98, 2), "Ausbruch"

def check_bullish_confirmation(df):
    """Prüft die letzte Kerze auf bullische Umkehr mit erhöhter Sicherheit."""
    # Wir brauchen mindestens 3 Kerzen, um die Dynamik (bärisch -> bullisch) zu sehen
    if len(df) < 3: return None
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    body = abs(last['Close'] - last['Open'])
    lower_wick = min(last['Open'], last['Close']) - last['Low']
    upper_wick = last['High'] - max(last['Open'], last['Close'])
    
    # 1. HAMMER-Check (klassisch)
    if lower_wick > (2 * body) and upper_wick < body:
        return "Hammer"
        
    # 2. ERWEITERTES BULLISH ENGULFING: 
    # Vorletzte Kerze war rot (bärisch), letzte ist grün (bullisch) und umschließt den Körper
    is_prev_bearish = prev['Close'] < prev['Open']
    is_last_bullish = last['Close'] > last['Open']
    engulfs = last['Close'] > prev['Open'] and last['Open'] < prev['Close']
    
    if is_prev_bearish and is_last_bullish and engulfs:
        return "Engulfing"
        
    return None
    
def get_fib_levels(data):
    """Berechnet die 0.618 und 1.618 Extension Level basierend auf den letzten 60 Tagen."""
    recent_data = data.iloc[-60:]
    swing_high = recent_data['High'].max()
    swing_low = recent_data['Low'].min()
    span = swing_high - swing_low
    
    # Extension-Level für Kursziele (über dem aktuellen Kurs)
    fib_0618 = swing_low + (span * 1.618)
    fib_1000 = swing_low + (span * 2.0)
    
    return fib_0618, fib_1000

def analyze_a_setup(ticker, sektor):
    try:
        # 1. Daten laden
        t = yf.Ticker(ticker)
        data = t.history(period="1y")
        if isinstance(data.columns, pd.MultiIndex): 
            data.columns = data.columns.get_level_values(0)
        if data.empty or len(data) < 200: return None

        # 2. Indikatoren berechnen
        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean() # NEU
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean() # NEU
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        data['Vol_SMA20'] = data['Volume'].rolling(20).mean()
        
        # RSI Berechnung
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        # MACD Berechnung
        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        # 3. Vorab-Filter: Trend
        if data['Close'].iloc[-1] < data['WMA200'].iloc[-1]: return None

        # 4. Candlestick-Muster bestimmen
        c1, c2 = data.iloc[-1], data.iloc[-2]
        pattern = "Kein"
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        if lower_wick > (2 * body): pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        # 5. EMA-Ausbruch & Kombi-Logik bestimmen
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       (data['EMA8'].iloc[-2] <= data['EMA20'].iloc[-2]) and \
                       (data['Volume'].iloc[-1] > data['Vol_SMA20'].iloc[-1])
        
        # 6. Setup-Typ Definition
        if ema_breakout and pattern != "Kein":
            setup_typ = f"Kombi: {pattern} + Ausbruch"
        elif ema_breakout:
            setup_typ = "Trend-Ausbruch"
        elif pattern != "Kein":
            setup_typ = f"Reines Pattern: {pattern}"
        else:
            return None # Keine Filterbedingung erfüllt

        # 7. Metriken (Dynamische CRV Berechnung)
        info = t.info
        entry = data['Close'].iloc[-1]
        # 1. STOP-LOSS
        stop = data['Low'].rolling(10).min().iloc[-1]
        
        # 2. ALLE INDIKATOREN FÜR ZIELE BERECHNEN (EMA20, EMA50, EMA100, WMA200)
        # Stelle sicher, dass diese Spalten in 'data' existieren
        ema20 = data['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = data['EMA50'].iloc[-1] if 'EMA50' in data.columns else entry * 1.05
        ema100 = data['Close'].ewm(span=100, adjust=False).mean().iloc[-1]
        wma200 = data['WMA200'].iloc[-1]
        
        # FIBONACCI-LEVELS
        fib1, fib2 = get_fib_levels(data)
        
        # 3. ZIELE IN POTENTIAL_TARGETS LISTE AUFNEHMEN
        # Wir sortieren jetzt alle relevanten Chart-Widerstände
        potential_targets = sorted([ema20, ema50, ema100, wma200, fib1, fib2])
        
        # Logik: TP1 ist der erste Widerstand über dem Einstieg, TP2 der zweite
        targets_above = [t for t in potential_targets if t > entry]
        
        if len(targets_above) >= 2:
            tp1 = targets_above[0]
            tp2 = targets_above[1]
        elif len(targets_above) == 1:
            tp1 = targets_above[0]
            tp2 = targets_above[0] * 1.05 # TP2 etwas höher setzen
        else:
            tp1 = entry * 1.08  # Fallback
            tp2 = entry * 1.15  # Fallback
            
        # 4. DYNAMISCHES CRV
        risiko = entry - stop
        if risiko <= 0: return None
        
        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
    except Exception as e:
        print(f"Fehler bei Analyse von {ticker}: {e}")
        return None
    except Exception as e:
        print(f"Fehler bei Analyse von {ticker}: {e}")
        return None
        
if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # 1. Benchmarks sicher abrufen
    sp500_filter_text = get_sp500_data()
    qqq_text = get_qqq_quote() 
    
    # 2. Performance berechnen
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    # 3. Setups verarbeiten
    all_setups = []
    print("Starte Setup-Analyse...")
    blacklist = ["SPLK"] 
    
    for _, row in df_perf.head(10).iterrows():
        aktien_liste = sektoren_aktien.get(row['Ticker'], [])
        print(f"Prüfe Sektor: {row['Sektor']} ({len(aktien_liste)} Aktien)")
        
        for s in aktien_liste:
            if s in blacklist: continue
            try:
                res = analyze_a_setup(s, row['Sektor'])
                if res:
                    all_setups.append(res)
                    print(f" -> Setup gefunden: {s}")
            except Exception as e:
                print(f"Überspringe {s} aufgrund eines Fehlers: {e}")
                continue 
    
    # 4. Spalten-Reihenfolge (Setup-Datei)
    cols = ['Ticker', 'Name', 'Sektor', 'Setup_Typ', 'Pattern', 'Kursziel', 'RSI', 'MACD_Trend', 
            'Status2', 'CRV1', 'CRV2', 'Kurs', 'Einstieg', 'Stop', 'TP1', 'TP2', 
            'Vol_Ratio', 'Risk_Perc', 'Ideales_Delta']

    if not all_setups:
        print("Keine Setups gefunden.")
        df_s = pd.DataFrame(columns=cols)
    else:
        df_s = pd.DataFrame(all_setups)
        df_s = df_s.drop_duplicates(subset=['Ticker'], keep='first')
        df_s = df_s.reindex(columns=cols)
        
        # Status-Logik anwenden
        def update_status_logic(row):
            # 1. Bedingung: Überkaufter RSI (über 70) -> ACHTUNG
            if row['RSI'] > 70:
                return "ACHTUNG"
            
            # 2. Bedingung: Bärischer MACD bei bullischem Signal -> ACHTUNG
            if row['MACD_Trend'] == "Bärisch" and row['Pattern'] != "Kein":
                return "ACHTUNG"
            
            # 3. Standard-Logik
            if row['Pattern'] != "Kein" and row['Kurs'] < row['TP1']:
                return "VALIDE"
            elif row['Kurs'] >= row['TP1']:
                return "GELAUFEN"
            
            return "ACHTUNG"
            
        df_s['Status2'] = df_s.apply(update_status_logic, axis=1)

   # 5. FILTERN, SORTIEREN & EXPORTIEREN
    
    # 1. Top-3 Sektoren Filtern
    top_3_sektoren = df_perf.nlargest(3, 'Rotation-Score')['Sektor'].tolist()
    df_s = df_s[df_s['Sektor'].isin(top_3_sektoren)].copy()

    # 2. Numerische Konvertierung & Sortieren
    cols_to_num = ['CRV1', 'Risk_Perc']
    for col in cols_to_num:
        df_s[col] = pd.to_numeric(df_s[col], errors='coerce').fillna(0)

    df_s['Status_Order'] = df_s['Status2'].map({'VALIDE': 0, 'ACHTUNG': 1}).fillna(2)
    df_s = df_s.sort_values(by=['Status_Order', 'CRV1', 'Risk_Perc'], ascending=[True, False, True])
    df_s = df_s.drop(columns=['Status_Order'])

    # 3. CSV Speichern
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv("setup_liste.csv", index=False)
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # 6. Briefing erstellen
    relevante_setups = df_s[df_s['Status2'] != "GELAUFEN"].copy()
    
    with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n==============================\n\n")
        f.write(f"BENCHMARKS\n{sp500_filter_text}\n{qqq_text}\n\n")
        f.write("TRADE-ZUSAMMENFASSUNG (Relevante Setups)\n")
        
        if not relevante_setups.empty:
            for _, row in relevante_setups.iterrows():
                f.write(f"\nTicker: {row['Ticker']} | {row['Name']}\n")
                f.write(f"Sektor: {row['Sektor']} | Status: {row['Status2']}\n")
                f.write(f"Setup-Qualität: {row['Setup_Typ']}\n")
                f.write(f"Kurs: {row['Kurs']} | RSI: {row['RSI']} | MACD: {row['MACD_Trend']}\n")
                f.write(f"TP1: {row['TP1']} | CRV1: {row['CRV1']}\n")
                f.write(f"Risiko: {row['Risk_Perc']}% | Vol-Ratio: {row['Vol_Ratio']}x\n")
                f.write(f"Suche: Hebelprodukt auf {row['Ticker']} (Ziel: {row['TP1']})\n")
                f.write("-" * 30 + "\n")
        else:
            f.write("Keine validen Setups oder ACHTUNG-Kandidaten gefunden.\n")
        
        f.write(f"\nScan-Statistik: {len(df_s)} Ticker analysiert.\n")
