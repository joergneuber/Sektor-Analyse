import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import time

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
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA"],
    "SOXX": ["NVDA", "AVGO", "TXN", "QCOM", "INTC", "AMD", "MU", "ADI", "LRCX", "AMAT"],
    "SMH": ["NVDA", "TSM", "ASML", "AVGO", "QCOM", "TXN", "AMAT", "AMD", "LRCX", "MU"],
    "IGV": ["MSFT", "ADBE", "CRM", "ORCL", "SNOW", "PANW", "WDAY", "INTU", "NOW", "ADSK"],
    "XBI": ["AMGN", "GILD", "BIIB", "VRTX", "REGN", "ILMN", "SGEN", "EXAS", "MRNA", "TECH"],
    "KRE": ["FITB", "HBAN", "CFG", "KEY", "ZION", "RF", "CMA", "PBCT", "SNV", "HBAP"],
    "HACK": ["PANW", "CRWD", "FTNT", "OKTA", "ZS", "CYBR", "QLYS", "TENB", "VRSN", "CHKP"],
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

import datetime
import pandas as pd
import yfinance as yf

def get_perf(ticker, name):
    try:
        # Zeitraum auf 1 Jahr erhöhen, um YTD abdecken zu können
        hist = yf.download(ticker, period="1y", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist = hist['Close']
        if hist.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}
        
        close = hist.iloc[:, 0] if isinstance(hist, pd.DataFrame) else hist
        last = close.iloc[-1]
        
        # Berechnung relative Performance
        def p(d): return round(((last / close.iloc[-d]) - 1) * 100, 2)
        
        # YTD Berechnung
        current_year = datetime.datetime.now().year
        ytd_data = close[close.index.year == current_year]
        if not ytd_data.empty:
            ytd_perf = round(((last / ytd_data.iloc[0]) - 1) * 100, 2)
        else:
            ytd_perf = 0
            
        res = {
            "Ticker": ticker, 
            "Sektor": name, 
            "5T": p(5), 
            "12T": p(12), 
            "30T": p(30), 
            "60T": p(60), 
            "YTD": ytd_perf
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
    
    # Primär: EMA 20 & 50
    primary = [val for val in [ema20, ema50] if val < breakout_level]
    # Sekundär: EMA 100, 200
    secondary = [val for val in [ema100, ema200] if val < breakout_level]
    
    if primary: return round(max(primary), 2)
    if secondary: return round(max(secondary), 2)
    return round(breakout_level * 0.98, 2) # Fallback

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1)
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): 
            hist.columns = hist.columns.get_level_values(0)
        if hist.empty or len(hist) < 200: 
            return None
            
        # ... innerhalb von analyze_a_setup ...
        highs = hist['High']
        lows = hist['Low']
        closes = hist['Close']
        
        atr = (highs - lows).rolling(14).mean().iloc[-1]
        breakout_level = highs.rolling(20).max().iloc[-1]
        
        # NEU: Re-Test Einstieg berechnen und den alten 'entry' überschreiben
        entry = calculate_retest_entry(hist, breakout_level)
        
        stop = lows.rolling(20).min().iloc[-1]
        
        # Sicherheitsprüfung: Wenn der EMA-Einstieg unter dem Stop liegt, 
        # nehmen wir das Ausbruchsniveau als Einstieg, um keine unsinnigen Trades zu machen.
        if entry <= stop: 
            entry = breakout_level
            
        risiko = entry - stop
        
        # TP1 und TP2 basieren jetzt auf dem neuen, tieferen Einstieg
        tp1 = entry + atr
        tp2 = entry + (atr * 3)
        
        crv1 = ((tp1 - entry) / risiko) if risiko > 0 else 0
        crv2 = ((tp2 - entry) / risiko) if risiko > 0 else 0
        # ... (Rest der Funktion bleibt wie gehabt)        
        return {
            "Ticker": ticker, 
            "Name": yf.Ticker(ticker).info.get('longName', ticker), 
            "Sektor": sektor,
            "Kurs": round(closes.iloc[-1], 2), 
            "Einstieg": round(entry, 2), 
            "Stop": round(stop, 2),
            "TP1": round(tp1, 2), 
            "TP2": round(tp2, 2),
            "CRV1": round(crv1, 2),
            "CRV2": round(crv2, 2)
        }
    except Exception: 
        return None

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # S&P 500 Berechnung ausführen
    sp500_filter_text = get_sp500_data()
    
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    all_setups = []
    # Iteration über die Top 2 Sektoren
    for _, row in df_perf.head(2).iterrows():
        # Hier wurde "[:5]" entfernt, um alle Aktien des Sektors zu verarbeiten
        sector_results = [analyze_a_setup(s, row['Sektor']) for s in sektoren_aktien.get(row['Ticker'], [])]
        # Ergebnisse sortieren und komplett anhängen
        all_setups.extend(sorted([r for r in sector_results if r], key=lambda x: x['CRV1'], reverse=True))
    
    df_s = pd.DataFrame(all_setups)
    
    # HIER: Sortierung des GESAMTEN DataFrames nach CRV2 absteigend
    if not df_s.empty:
        df_s = df_s.sort_values(by='CRV2', ascending=False)
    
    # CSV Exporte
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # Briefing schreiben
    with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n")
        f.write("==============================\n\n")
        f.write("GESAMTMARKTFILTER\n")
        f.write(sp500_filter_text + "\n\n")
        f.write("PERFORMANCE\n")
        f.write(df_perf.to_string(index=False) + "\n\n")
        f.write("TOP SETUPS\n")
        f.write(df_s.to_string(index=False))

    print("Briefing-Dateien erfolgreich geschrieben.")
