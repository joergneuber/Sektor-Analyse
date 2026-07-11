import pandas as pd
import numpy as np
import yfinance as yf
import datetime
import time
import sys
import os
from groq import Groq

# Importe für Alpaca
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from concurrent.futures import ThreadPoolExecutor

# Initialisierung des Clients direkt beim Start
# Wir nutzen os.getenv, um die Keys sicher aus deinen GitHub-Secrets zu lesen
alpaca_client = StockHistoricalDataClient(os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'))


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
    "XLP": ["PG", "KO", "PEP", "COST", "WMT", "CL", "EL", "MDLZ", "GIS", "KLG"],
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

def get_analyst_target(ticker):
    try:
        stock = yf.Ticker(ticker)
        data = stock.info
        # Hol dir den Wert. Wenn None, wird target automatisch None
        target = data.get('targetMeanPrice') 
        
        # DEBUG: Damit siehst du in der Konsole, was ankommt
        print(f"DEBUG: {ticker} | Gefundenes Analysten-Ziel: {target}")

        # Rückgabe: Wenn target existiert und > 0, gib es zurück. Sonst None.
        if target and target > 0:
            return target
        return None
        
    except Exception as e:
        print(f"ERROR: Fehler bei {ticker}: {e}")
        return None

# --- FUNKTIONEN ---
def update_status_logic(row):
    # Standardwerte
    status = "VALIDE"
    grund = "Alles ok"

    if row['RSI'] > 70:
        status, grund = "ACHTUNG", "RSI überkauft (>70)"
    elif row['Pattern'] != "Kein" and row['Vol_Ratio'] < 0.5:
        status, grund = "ACHTUNG", f"Schwaches Volumen ({row['Vol_Ratio']}x SMA20)"
    elif row['MACD_Trend'] == "Bärisch" and row['Pattern'] != "Kein":
        status, grund = "ACHTUNG", "Bärischer MACD-Trend"
    elif row['Kurs'] >= row['TP1']:
        status, grund = "GELAUFEN", "Kursziel erreicht"
    
    return pd.Series([status, grund])

# --- FUNKTIONEN ---
def get_sp500_data():
    try:
        start_date = datetime.datetime.now() - datetime.timedelta(days=300)
        request = StockBarsRequest(symbol_or_symbols=["SPY"], start=start_date, timeframe=TimeFrame.Day)
        bars = alpaca_client.get_stock_bars(request)
        hist = bars.df
        
        if hist.empty or len(hist) < 200:
            return "S&P 500: Daten unvollständig"
            
        hist = hist.reset_index(level=0, drop=True)
        if 'close' in hist.columns:
            hist = hist.rename(columns={'close': 'Close'})
            
        # WICHTIG: Erst berechnen, dann returnen
        close = hist['Close']
        last_close = close.iloc[-1]
        
        # Indikatoren berechnen
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        return (f"S&P 500: {last_close:.2f} | EMA20: {e20:.0f} | EMA50: {e50:.0f} | "
                f"EMA200: {e200:.0f} | WMA200: {w200:.0f}")
                
    except Exception as e:
        return f"S&P 500: Fehler beim Abruf ({e})"
        
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
        # Zeitraum für 300 Tage
        start_date = datetime.datetime.now() - datetime.timedelta(days=300)
        
        # Alpaca Anfrage für QQQ
        request = StockBarsRequest(
            symbol_or_symbols=["QQQ"],
            start=start_date,
            timeframe=TimeFrame.Day
        )
        
        bars = alpaca_client.get_stock_bars(request)
        hist = bars.df
        
        # Daten prüfen
        if hist.empty or len(hist) < 200:
            return "Nasdaq: Nicht bewertet (Daten unvollständig)"
            
        # Index bereinigen
        hist = hist.reset_index(level=0, drop=True)
        if 'close' in hist.columns:
            hist = hist.rename(columns={'close': 'Close'})
            
        # Daten für Berechnungen extrahieren
        close = hist['Close']
        last_close = close.iloc[-1]
        
        # Indikatoren berechnen
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        
        # WMA200 Berechnung
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        # Rückgabe des formatierten Strings
        return (f"Nasdaq: {last_close:.2f} | EMA20: {e20:.0f} | EMA50: {e50:.0f} | "
                f"EMA200: {e200:.0f} | WMA200: {w200:.0f}")
                
    except Exception as e:
        print(f"FEHLER beim Abruf von QQQ: {e}")
        return f"Nasdaq: Fehler beim Datenabruf ({e})"        
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
        # Zeitraum für 1 Jahr (ca. 260 Handelstage reichen für 60T Performance)
        start_date = datetime.datetime.now() - datetime.timedelta(days=365)
        
        request = StockBarsRequest(
            symbol_or_symbols=[ticker],
            start=start_date,
            timeframe=TimeFrame.Day
        )
        
        bars = alpaca_client.get_stock_bars(request)
        hist = bars.df
        
        if hist.empty:
            return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}
        
        # Index bereinigen und sicherstellen, dass 'close' vorhanden ist
        hist = hist.reset_index(level=0, drop=True)
        if 'close' in hist.columns:
            hist = hist.rename(columns={'close': 'Close'})
            
        close = hist['Close']
        last = close.iloc[-1]
        
        # Hilfsfunktion für prozentuale Performance
        # Sicherstellen, dass wir nicht über das Ende hinaus greifen
        def p(d): 
            if len(close) > d:
                return round(((last / close.iloc[-d]) - 1) * 100, 2)
            return 0
        
        # YTD Performance berechnen
        current_year = datetime.datetime.now().year
        # Wir nutzen den Index (Timestamp) um YTD zu filtern
        ytd_data = close[hist.index.year == current_year]
        ytd_perf = round(((last / ytd_data.iloc[0]) - 1) * 100, 2) if not ytd_data.empty else 0
            
        res = {
            "Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60), "YTD": ytd_perf
        }
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res
        
    except Exception as e:
        print(f"FEHLER bei Performance-Berechnung für {ticker}: {e}")
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
    
    # Extension-Level für techn. Kursziele (über dem aktuellen Kurs)
    fib_0618 = swing_low + (span * 1.618)
    fib_1000 = swing_low + (span * 2.0)
    
    return fib_0618, fib_1000

def clean_num(val, default=0.0):
    # Alles hier drunter muss um 4 Leerzeichen eingerückt sein!
    try:
        if val is None:
            return None
        return float(val)
    except Exception as e:
        print(f"DEBUG: Konvertierungsfehler bei Wert: {val} | Fehler: {e}")
        return default

def get_ideal_delta(upside_prozent):
    # Einfache Heuristik:
    # Bei kleinem Upside brauchen wir hohes Delta für direkte Reaktion
    # Bei großem Upside reicht moderates Delta für Hebel
    if upside_prozent < 5:
        return 0.70  # Aggressiv, tief im Geld
    elif upside_prozent < 15:
        return 0.55  # Der "Sweet Spot"
    else:
        return 0.40  # Mehr Hebel, weniger Delta-Risiko

def analyze_a_setup(ticker, sektor):
    upside_potenzial = None
    # Firmennamen abrufen
    try:
        ticker_obj = yf.Ticker(ticker)
        # Wir versuchen den longName zu laden, falls nicht verfügbar, nehmen wir den Ticker
        info = ticker_obj.info
        firma_name = info.get('longName', ticker)
        # Fallback, falls longName ein leeres String ist
        if not firma_name or firma_name == "":
            firma_name = ticker
    except:
        firma_name = ticker
    
    # ... hier folgt dein restlicher Code ...
    # 0. Initialisierung
    setup_typ = "Kein"
    pattern = "Kein"
    tp1 = 0
    
    try:
        # Kursdaten über Alpaca laden
        start_date = datetime.datetime.now() - datetime.timedelta(days=365)
        request = StockBarsRequest(
            symbol_or_symbols=[ticker],
            start=start_date,
            timeframe=TimeFrame.Day
        )
        
        bars = alpaca_client.get_stock_bars(request)
        data = bars.df
        
        if data.empty:
            print(f"DEBUG: {ticker} -> Daten von Alpaca leer.")
            return None
            
        # Index und Spalten bereinigen
        data = data.reset_index(level=0, drop=True)
        if 'close' in data.columns:
            data = data.rename(columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'volume': 'Volume'})

        # 1. Indikatoren berechnen
        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        data['Vol_SMA20'] = data['Volume'].rolling(20).mean()
        
        # RSI Berechnung direkt als Spalte
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        
        # Hier wird 'RSI' als Spalte gespeichert
        data['RSI'] = 100 - (100 / (1 + (gain / loss)))
        data['RSI'] = data['RSI'].fillna(50) # Wichtig: NaN durch 50 ersetzen
        
        # Vol_Ratio direkt als Spalte speichern
        data['Vol_Ratio'] = data['Volume'] / data['Vol_SMA20']
        data['Vol_Ratio'] = data['Vol_Ratio'].fillna(0)
        
        # MACD Berechnung
        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        # 2. Trend-Status
        # Jetzt muss der Kurs über WMA200 UND über EMA200 liegen
        trend_status = "Unter WMA200/EMA200" if (data['Close'].iloc[-1] < data['WMA200'].iloc[-1] or data['Close'].iloc[-1] < data['EMA200'].iloc[-1]) else "OK"

        # 3. Candlestick-Muster bestimmen
        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        if lower_wick > (2 * body): 
            pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        # 4. EMA-Ausbruch
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       (data['EMA8'].iloc[-2] <= data['EMA20'].iloc[-2]) and \
                       (data['Volume'].iloc[-1] > data['Vol_SMA20'].iloc[-1])
        
        # 5. Setup-Typ Definition
        if ema_breakout and pattern != "Kein":
            setup_typ = f"Kombi: {pattern} + Ausbruch"
        elif ema_breakout:
            setup_typ = "Trend-Ausbruch"
        elif pattern != "Kein":
            setup_typ = f"Reines Pattern: {pattern}"
        else:
            return None # Keine Filterbedingung erfüllt

       # 6. Metriken & Ziele
        entry = data['Close'].iloc[-1]
        entry2 = round(data['EMA20'].iloc[-1], 2)
        stop = data['Low'].rolling(10).min().iloc[-1]
        
        ema20 = data['EMA20'].iloc[-1]
        ema50 = data['EMA50'].iloc[-1]
        ema100 = data['EMA100'].iloc[-1]
        ema200 = data['EMA200'].iloc[-1]
        wma200 = data['WMA200'].iloc[-1]
        
        # Ziele (TP1/TP2) festlegen
        fib1, fib2 = get_fib_levels(data)
        potenzial_targets = sorted([ema20, ema50, ema100, ema200, wma200, fib1, fib2])
        targets_above = [t for t in potenzial_targets if t > entry]

        tp1 = targets_above[0] if targets_above else entry * 1.08
        tp2 = targets_above[1] if len(targets_above) >= 2 else tp1 * 1.05

        # --- SICHERE BERECHNUNG ANALYSTEN-ZIEL & UPSIDE ---
        analysten_ziel = get_analyst_target(ticker)
        
        # Sicherstellen, dass analysten_ziel eine Zahl ist (0.0 statt None)
        if analysten_ziel is None:
            analysten_ziel = 0.0
            
        # Target bestimmen (Analysten-Ziel bevorzugt, sonst technisches TP1)
        target_value = analysten_ziel if analysten_ziel > 0 else tp1
        
        # Upside-Potenzial berechnen
        if entry > 0:
            upside_potenzial = round(((target_value - entry) / entry) * 100, 2)
        else:
            upside_potenzial = 0.0
            
        # Risiko prüfen
        risiko = entry - stop
        if risiko <= 0: return None
        
        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        # --- HIER EINFÜGEN: CRV-FILTER ---
        # Verwirft Setups, die ein schlechtes Chance-Risiko-Verhältnis haben
        if crv1 < 1.0 or crv2 < 1.0:
            return None 
        # ---------------------------------
        
        vol_ratio = round(data['Volume'].iloc[-1] / data['Vol_SMA20'].iloc[-1], 2)
        risk_perc = round(((entry - stop) / entry) * 100, 2)

        # Nutze data['RSI'] anstatt der alten Variable rsi
        status_val = "ACHTUNG" if data['RSI'].iloc[-1] > 70 else "VALIDE"
        grund_val = "RSI zu hoch" if status_val == "ACHTUNG" else "Alles ok"
             
        # Nur zur Kontrolle – das hilft dir den Fehler in 1 Sekunde zu finden
        print(f"DEBUG: Ticker={ticker}, Name={firma_name}, Sektor={sektor}")

        # --- VOR DER RÜCKGABE EINFÜGEN ---
        # Plausibilitäts-Check: Wenn EMA20 > 2 * Kurs, ist die Berechnung vermutlich falsch
        current_price = entry
        ema20_val = data['EMA20'].iloc[-1]


        
        # Datenprüfung
        if len(data) < 200: 
            return None
                
        # 1. Eindeutiger Zugriff auf die aktuelle Zeile (verhindert Index-Verschiebung)
        last_row = data.iloc[-1]
        
        # 2. Plausibilitäts-Check
        # EMA20 darf nicht extrem weit vom Kurs entfernt sein (hier Faktor 2 als Limit)
        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG: Plausibilitätsfehler bei {ticker}. EMA20 ({last_row['EMA20']:.2f}) vs Kurs ({last_row['Close']:.2f})")
            return None
           
        # Berechnung des Upside-Potenzials
        if analysten_ziel > 0:
            upside_potenzial = round(((analysten_ziel - data['Close'].iloc[-1]) / data['Close'].iloc[-1]) * 100, 2)
        else:
            # Fallback auf TP1, falls kein Analysten-Ziel vorhanden ist
            # TP1 wird hier durch die Fib-Logik etwas später definiert, 
            # daher nutzen wir hier den Platzhalter oder berechnen es später.
            # Da tp1 hier noch nicht existiert, setzen wir es auf 0 und korrigieren es im Return
            upside_potenzial = None 

        # Das 'if' steht am linken Rand (bzw. auf gleicher Ebene wie der restliche Code)
        if upside_potenzial is None:
            # Diese Zeile muss zwingend mit 4 Leerzeichen eingerückt sein
            upside_potenzial = 0

        # Sicherstellen, dass last_row definiert ist
        last_row = data.iloc[-1]

        # Plausibilitäts-Check (korrekt eingerückt)
        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG: Plausibilitätsfehler bei {ticker}. EMA20 ({last_row['EMA20']:.2f}) vs Kurs ({last_row['Close']:.2f})")
            return None
            
        #DEBUG: Überprüfe, was wirklich in das Dictionary geht
        print(f"DEBUG: Ticker {ticker} -> Analysten-Ziel im Dictionary: {analysten_ziel}")
        
        return {
            "Ticker": str(ticker),
            "Name": str(firma_name),
            "Sektor": str(sektor),
            "Trend": str(trend_status),
            "Setup_Typ": str(setup_typ),
            "Pattern": str(pattern),
            "Tech-Kursziel": clean_num(tp1),
            # Ändere das kurzzeitig so:
            "Analysten-Kursziel": analysten_ziel,
            "Upside-Potenzial%": upside_potenzial,
            "Status2": str(status_val),
            "Status_Grund": str(grund_val),
            "RSI": clean_num(last_row['RSI']),
            "MACD_Trend": str(macd_trend),
            "CRV1": clean_num(crv1),
            "CRV2": clean_num(crv2),
            "Kurs": round(last_row['Close'], 2),
            "Einstieg": round(last_row['Close'], 2),
            "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
            "Stop": clean_num(stop),
            "Risk_Perc": clean_num(risk_perc),
            "TP1": clean_num(tp1),
            "TP2": clean_num(tp2),
            "Vol_Ratio": clean_num(last_row['Vol_Ratio']),
            "Ideales_Delta": clean_num(0)
        }

    # Das 'except' MUSS auf der gleichen Einrückungsebene wie das 'try' stehen!
    except Exception as e:
        print(f"FEHLER: Bei der Analyse von {ticker} ist ein Problem aufgetreten: {e}")
        return None

if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # 1. Benchmarks sicher abrufen
    sp500_filter_text = get_sp500_data()
    qqq_text = get_qqq_quote() 
    
    # 2. Performance berechnen
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    # 3. Setups verarbeiten (PARALLEL)
    print("Starte Setup-Analyse...")
    blacklist = ["SPLK"] 
    
    # Aufgabenliste erstellen
    tasks = []
    for _, row in df_perf.head(10).iterrows():
        aktien_liste = sektoren_aktien.get(row['Ticker'], [])
        for s in aktien_liste:
            if s not in blacklist:
                tasks.append((s, row['Sektor']))
    
    # Parallel mit max_workers=10 ausführen
    all_setups = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        # Führt analyze_a_setup für alle Tasks gleichzeitig aus
        results = list(executor.map(lambda p: analyze_a_setup(*p), tasks))
        
    # Ergebnisse filtern (None-Werte entfernen)
    all_setups = [r for r in results if r is not None]
    print(f"Analyse beendet. {len(all_setups)} Setups gefunden.")
    
    # Deine Liste/Spalten und Reihenfolge in setup-Datei (HIER EINGERÜCKT!)
    cols = ['Ticker', 'Name', 'Sektor', 'Trend', 'Setup_Typ', 'Pattern', 'Tech-Kursziel', 
            'Analysten-Kursziel', 'Upside-Potenzial%', 'Status2', 'Status_Grund', 
            'RSI', 'MACD_Trend', 'CRV1', 'CRV2', 'Kurs', 'Einstieg', 'Einstieg2(EMA 20)', 
            'Stop', 'Risk_Perc', 'TP1', 'TP2', 'Vol_Ratio', 'Ideales_Delta']

    if not all_setups:
        print("Keine Setups gefunden.")
        df_s = pd.DataFrame(columns=cols)
    else:
        # Hier erzwingst du die Spaltenreihenfolge!
        # Auch wenn in einem Dictionary mal ein Wert fehlt, 
        # bleibt die Struktur durch 'columns=cols' stabil.
        df_s = pd.DataFrame(all_setups, columns=cols)
        
        # Duplikate entfernen (auf Basis der Ticker-Spalte)
        df_s = df_s.drop_duplicates(subset=['Ticker'])
        
        # Jetzt erst den Index setzen
        df_s = df_s.set_index('Ticker')
        
        # B) Status-Logik anwenden
        df_s[['Status2', 'Status_Grund']] = df_s.apply(update_status_logic, axis=1)

    # 5. FILTERN
    # 5. FILTERN (Erweitert um Trend-Check)
    if not df_s.empty:
        top_5_sektoren = df_perf.nlargest(5, 'Rotation-Score')['Sektor'].tolist()
        
        # NEU: Nur Sektoren-Treffer UND nur Aktien, die im Aufwärtstrend (über WMA200) sind
        df_s = df_s[
            (df_s['Sektor'].isin(top_5_sektoren)) & 
            (df_s['Trend'] == 'OK')
        ].copy()
        
        print(f"DEBUG: Setups nach Sektor-Filter & Trend-Check: {len(df_s)}")

    # 6. KONVERTIERUNG & SORTIEREN
        if not df_s.empty:
            cols_to_num = ['CRV1', 'Risk_Perc', 'Upside-Potenzial%'] # 'Upside-Potenzial%' hier hinzufügen
            for col in cols_to_num:
                df_s[col] = pd.to_numeric(df_s[col], errors='coerce').fillna(0)

            df_s['Status_Order'] = df_s['Status2'].map({'VALIDE': 0, 'ACHTUNG': 1}).fillna(2)
            
            # HIER ÄNDERN: Verwende den ursprünglichen Namen 'Upside-Potenzial%'
            df_s = df_s.sort_values(
                by=['Status_Order', 'Upside-Potenzial%', 'CRV1'], 
                ascending=[True, False, False]
            )
            df_s = df_s.drop(columns=['Status_Order'])

    # 7. BEREINIGUNG & FORMATIERUNG
    df_clean = df_s.copy()
    
    # 1. ZUERST umbenennen
    df_clean = df_clean.rename(columns={'Upside-Potenzial%': 'Upside_%_vs_Aktuell'})
    
    # 2. DANN das Delta berechnen (da der Name jetzt existiert)
    # Stelle sicher, dass die Funktion 'get_ideal_delta' weiter oben im Skript definiert ist
    df_clean['Ideales_Delta'] = df_clean['Upside_%_vs_Aktuell'].apply(get_ideal_delta)
    
    # 3. DANN Runden
    cols_to_round = [
        'Tech-Kursziel', 'Analysten-Kursziel', 'Upside_%_vs_Aktuell', 
        'RSI', 'CRV1', 'CRV2', 'Kurs', 'Einstieg', 'Einstieg2(EMA 20)', 
        'Stop', 'Risk_Perc', 'TP1', 'TP2', 'Vol_Ratio'
    ]
    df_clean[cols_to_round] = df_clean[cols_to_round].round(2)
    
    # 4. Leere Spalte entfernen (falls nötig)
    if 'Ideales_Delta' in df_clean.columns:
        # Hier optional noch Delta auf 2 Stellen runden, falls es eine Fließkommazahl ist
        pass
    
    # 8. EXPORT
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # Hier exportierst du jetzt zwei Versionen (falls das so gewollt ist)
    df_clean.to_csv("setup_liste.csv", index=False)
    df_clean.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')

    relevante_setups = df_clean[df_clean['Status2'] != "GELAUFEN"]
    valide_setups = relevante_setups[relevante_setups['Status2'] == "VALIDE"]
    achtung_setups = relevante_setups[relevante_setups['Status2'] == "ACHTUNG"]
    
with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
    f.write(f"MARKT-UPDATE {today}\n==============================\n\n")
    f.write(f"BENCHMARKS\n{sp500_filter_text}\n{qqq_text}\n\n")
    
    # 1. TOP-CHANCEN (VALIDE)
    f.write("\n" + "="*50 + "\n")
    f.write("TRADE-ZUSAMMENFASSUNG (Valide Setups)\n")
    f.write("="*50 + "\n")
    
    for ticker_val, row in valide_setups.iterrows():
        f.write(f"\n>>> {ticker_val} | {row['Name']} <<<\n")
        f.write(f"Sektor: {row['Sektor']} | Status: {row['Status2']} | Grund: {row['Status_Grund']}\n")
        f.write(f"Pattern: {row['Pattern']} ({row['Setup_Typ']})\n")
        f.write("-" * 40 + "\n")
        f.write(f"Kurs: {row['Kurs']} | RSI: {row['RSI']} | MACD: {row['MACD_Trend']}\n")
        f.write(f"Einstieg: {row['Einstieg']} | Stop: {row['Stop']} | Risiko: {row['Risk_Perc']}%\n")
        # Ändere die Zeile im Briefing-Teil so:
        f.write(f"Einstieg: {row['Einstieg']} | EMA20: {row['Einstieg2(EMA 20)']} | Stop: {row['Stop']} | Risiko: {row['Risk_Perc']}%\n")
        f.write(f"TP1: {row['TP1']} | TP2: {row['TP2']} | CRV1: {row['CRV1']} | CRV2: {row['CRV2']}\n")
        f.write(f"Vol-Ratio: {row['Vol_Ratio']}x | Ideales Delta: {row['Ideales_Delta']}\n")
        f.write(f"Suche: Hebelprodukt auf {ticker_val} (Ziel: {row['TP1']})\n")
        f.write("\n")

    # 2. WATCHLIST (ACHTUNG)
    f.write("\n" + "="*50 + "\n")
    f.write("WATCHLIST (ACHTUNG - Manuelle Prüfung erforderlich)\n")
    f.write("="*50 + "\n")
    
    # HIER MUSS 'achtung_setups' stehen, NICHT 'watchlist'
    for ticker_val, row in achtung_setups.iterrows():
        upside_val = row.get('Upside_%_vs_Aktuell') 
        # Sicherstellen, dass der Wert vorhanden ist
        if upside_val is not None:
            upside_text = f"{upside_val:.2f}%"
        else:
            upside_text = "Kein Ziel"

        f.write(f"Ticker: {ticker_val} | Grund: {row['Status_Grund']} | Kurs: {row['Kurs']}\n")
        f.write(f"Upside: Technisch {row['Tech-Kursziel']} | Potenzial: {upside_text}\n")
        f.write("-" * 30 + "\n")
            
    f.write(f"\nScan-Statistik: {len(df_clean)} Ticker analysiert, davon {len(valide_setups)} valide Setups gefunden.\n")
