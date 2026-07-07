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
    "CLOU": ["SNOW", "CRWD", "OKTA", "ZS", "DDOG", "NET", "SPLK", "MDB", "TEAM", "DOCU"],
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
        if isinstance(hist.columns, pd.MultiIndex): 
            hist.columns = hist.columns.get_level_values(0)
        
        if hist.empty or len(hist) < 200:
            return "Trend S&P 500: Nicht bewertet (Daten unvollständig)"
            
        close = hist['Close']
        aktuell = close.iloc[-1]
        
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        output = (
            f"S&P 500 Kurs: {aktuell:.2f}\n"
            f"EMA 20:  {e20:.2f}\n"
            f"EMA 50:  {e50:.2f}\n"
            f"EMA 100: {e100:.2f}\n"
            f"EMA 200: {e200:.2f}\n"
            f"WMA 200: {w200:.2f}"
        )
        return output
    except Exception as e:
        return f"Trend S&P 500: Fehler bei Berechnung ({str(e)})"

def get_qqq_quote():
    try:
        ticker = yf.Ticker("QQQ")
        data = ticker.history(period="1d")
        if not data.empty:
            kurs = data['Close'].iloc[-1]
            return (f"QQQ (Nasdaq 100) Kurs: {kurs:.2f}\n"
                    f"Trend QQQ (Nasdaq 100): Bullisch (Proxy)")
        return "QQQ Kurs: Daten nicht verfügbar"
    except:
        return "QQQ Kurs: Fehler beim Abruf"

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

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1)
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        if hist.empty or len(hist) < 100 or hist['Volume'].tail(20).mean() < 500_000:
            return None

        closes, highs, lows = hist['Close'], hist['High'], hist['Low']
        atr = (highs - lows).rolling(14).mean().iloc[-1]
        
        # CHARTTECHNISCHE ZONEN
        # Support: Lokales Tief der letzten 60 Tage
        # Resistance: Lokales Hoch der letzten 60 Tage
        support = lows.rolling(60).min().iloc[-1]
        resistance = highs.rolling(60).max().iloc[-1]
        ema20 = closes.ewm(span=20).mean().iloc[-1]

        # STOP-LOSS (Charttechnisch + Puffer)
        # Wir setzen den Stop unter den Support, aber mit ATR-Puffer
        stop_level = round(support - (1.0 * atr), 2)
        
        # TARGETS (Charttechnisch)
        # TP1: Mitte zwischen Einstieg und Widerstand
        # TP2: Widerstand (Resistance-Zone)
        target_1 = round(resistance * 0.95, 2)
        target_2 = round(resistance, 2)

        # CANDIDATES (Individuelle Setups mit individuellen CRVs)
        candidates = [
            {"typ": "Bounce",   "entry": support + (0.5 * atr), "stop": stop_level, "tp1": target_1, "tp2": target_2},
            {"typ": "Re-Test",  "entry": ema20,                 "stop": stop_level, "tp1": target_1, "tp2": target_2},
            {"typ": "Breakout", "entry": resistance * 1.01,     "stop": support,    "tp1": resistance * 1.05, "tp2": resistance * 1.10}
        ]
        
        best_setup = None
        best_crv = 0
        
        for s in candidates:
            risiko = s['entry'] - s['stop']
            if risiko <= 0: continue
            
            # CRV2 basierend auf TP2
            crv = (s['tp2'] - s['entry']) / risiko
            
            if crv > best_crv:
                best_setup = s.copy()
                best_setup['crv'] = round(crv, 2)
                best_setup['tp1'] = round(s['tp1'], 2)
                best_setup['tp2'] = round(s['tp2'], 2)
                best_setup['stop'] = round(s['stop'], 2)
                best_setup['entry'] = round(s['entry'], 2)
                best_crv = crv
        
        if not best_setup or best_setup['crv'] < 1.0:
            return None
            
        # RSI & Trend (für Status)
        rsi = 100 - (100 / (1 + (closes.diff().where(closes.diff() > 0, 0).rolling(14).mean() / 
                                (-closes.diff().where(closes.diff() < 0, 0)).rolling(14).mean()))).iloc[-1]
        exp1, exp2 = closes.ewm(span=12).mean(), closes.ewm(span=26).mean()
        is_bullish = (exp1 - exp2).iloc[-1] > (exp1 - exp2).ewm(span=9).mean().iloc[-1]
        
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        
        return {
            "Ticker": ticker, "Name": info.get('longName', ticker), "Sektor": sektor,
            "Kursziel": info.get('targetMeanPrice', "N/A"), "Setup-Typ": best_setup['typ'],
            "RSI": round(rsi, 2), "MACD-Trend": "Bullish" if is_bullish else "Bearish",
            "Status": "Gelaufen" if closes.iloc[-1] > (best_setup['entry'] * 1.01) else "Beobachten",
            "Kurs": round(closes.iloc[-1], 2), "Einstieg": best_setup['entry'],
            "Stop": best_setup['stop'], "TP1": best_setup['tp1'], "TP2": best_setup['tp2'],
            "CRV2": best_setup['crv']
        }
    except: return None
        
if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    sp500_filter_text = get_sp500_data()
    qqq_text = get_qqq_quote() 
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    all_setups = []
    for _, row in df_perf.head(3).iterrows():
        for s in sektoren_aktien.get(row['Ticker'], []):
            res = analyze_a_setup(s, row['Sektor'])
            if res: all_setups.append(res)
    
    df_s = pd.DataFrame(all_setups)
    if df_s.empty: sys.exit()
    
    # --- FINALE BERECHNUNGEN (Konsistent zu analyze_a_setup) ---
    # Upside basierend auf Fundamentaldaten (Kursziel vs. Einstieg)
    df_s['Upside'] = df_s.apply(lambda r: round(((r['Kursziel'] - r['Einstieg']) / r['Einstieg']) * 100, 1) if isinstance(r['Kursziel'], (int, float)) else 0.0, axis=1)
    
    # Tech_Upside (TP2 vs. Einstieg)
    df_s['Tech_Upside'] = df_s.apply(lambda r: round(((r['TP2'] - r['Einstieg']) / r['Einstieg']) * 100, 1), axis=1)
    
    # CRV1 (TP1 vs. Risiko)
    df_s['CRV1'] = df_s.apply(lambda r: round(((r['TP1'] - r['Einstieg']) / (r['Einstieg'] - r['Stop'])), 2) if (r['Einstieg'] - r['Stop']) != 0 else 0, axis=1)
    
    # STATUS-LOGIK (ATR-S&R konform)
    def determine_status(row):
        if (row['CRV2'] >= 1.5 and row['MACD-Trend'] == 'Bullish' and 30 <= row['RSI'] <= 70 and row['Status'] == "Beobachten"):
            return "VALIDE"
        elif row['RSI'] > 70:
            return "ÜBERHITZT!"
        else:
            return "BEOBACHTEN"
    
    df_s['Status2'] = df_s.apply(determine_status, axis=1)
    
    # Sortierung: VALIDE nach oben, dann CRV2 absteigend
    df_s['sort_col'] = df_s['Status2'].apply(lambda x: 0 if x == "VALIDE" else (1 if x == "BEOBACHTEN" else 2))
    df_s = df_s.sort_values(by=['sort_col', 'CRV2'], ascending=[True, False])
    
    # --- EXPORT ---
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    cols = ['Name', 'Sektor', 'Setup-Typ', 'MACD-Trend', 'RSI', 'Status', 'Status2', 
            'Kursziel', 'Upside', 'Kurs', 'Tech_Upside', 'Einstieg', 'Stop', 'TP1', 'CRV1', 'TP2', 'CRV2']
    
    df_s[cols].to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # --- BRIEFING SCHREIB-BLOCK ---
    # --- KORREKTER BRIEFING SCHREIB-BLOCK ---
    with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n==============================\n\n{sp500_filter_text}\n{qqq_text}\n\n")
        f.write("TRADE-ZUSAMMENFASSUNG (VALIDE TITEL)\n------------------------------\n")
        
        valide = df_s[df_s['Status2'] == "VALIDE"]
        for _, row in valide.iterrows():
            f.write(f"Ticker: {row['Ticker']} | {row['Name']} ({row['Sektor']})\n")
            f.write(f"Setup: {row['Setup-Typ']} | Kurs: {row['Kurs']} | Einstieg: {row['Einstieg']}\n")
            f.write(f"Stop: {row['Stop']} | TP1: {row['TP1']} | TP2: {row['TP2']}\n")
            
            # Hier fügen wir die fehlenden Upside-Daten hinzu
            analyst_info = f"{row['Kursziel']} (Fund. Ziel)" if row['Kursziel'] != "N/A" else "Kein Ziel"
            f.write(f"Analystenziel: {analyst_info} | Upside: {row['Upside']}% | Tech. Upside: {row['Tech_Upside']}%\n")
            
            f.write(f"CRV (TP1): {row['CRV1']} | CRV (TP2): {row['CRV2']}\n")
            f.write(f"RSI: {row['RSI']} | Trend: {row['MACD-Trend']}\n------------------------------\n")
