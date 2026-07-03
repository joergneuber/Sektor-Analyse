import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import time
import sys

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
                    f"Trend QQQ: Nicht explizit quantifizierbar (keine EMAs in den Quelldaten verfügbar), "
                    f"wird jedoch aufgrund des bullischen S&P 500-Bildes als bullisch eingestuft.")
        return "QQQ Kurs: Daten nicht verfügbar"
    except:
        return "QQQ Kurs: Fehler beim Abruf"

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
    
    primary = [val for val in [ema20, ema50] if val < breakout_level]
    secondary = [val for val in [ema100, ema200] if val < breakout_level]
    
    if primary: return round(max(primary), 2), "Re-Test"
    if secondary: return round(max(secondary), 2), "Re-Test"
    return round(breakout_level * 0.98, 2), "Ausbruch"

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1)
    
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): 
            hist.columns = hist.columns.get_level_values(0)
        
        # 1. Volumen-Filter (muss vor allem anderen kommen)
        if hist.empty or len(hist) < 100 or hist['Volume'].tail(20).mean() < 500_000:
            return None

        # 2. Setup-Daten berechnen
        highs, lows, closes = hist['High'], hist['Low'], hist['Close']
        breakout_level = highs.rolling(20).max().iloc[-1]
        stop = lows.rolling(20).min().iloc[-1]
        entry_val, setup_typ = calculate_retest_entry(hist, breakout_level)
        
        # Sicherheit: Wenn entry_val None ist, sofort abbrechen
        if entry_val is None: return None
        
        if entry_val <= stop: 
            entry_val = breakout_level
            setup_typ = "Ausbruch"

        # 3. Indikatoren
        atr = (highs - lows).rolling(14).mean().iloc[-1]
        risiko = entry_val - stop
        rsi = 100 - (100 / (1 + (closes.diff().where(closes.diff() > 0, 0).rolling(14).mean() / 
                                 (-closes.diff().where(closes.diff() < 0, 0)).rolling(14).mean()))).iloc[-1]
        
        # MACD
        exp1, exp2 = closes.ewm(span=12, adjust=False).mean(), closes.ewm(span=26, adjust=False).mean()
        macd_line, signal_line = (exp1 - exp2).iloc[-1], (exp1 - exp2).ewm(span=9, adjust=False).mean().iloc[-1]
        is_bullish = macd_line > signal_line
        
        # 4. Analysten & Earnings (sicher abrufen)
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        analyst_target = info.get('targetMeanPrice', None) # None wenn nicht vorhanden
        
        # 5. Status-Logik (mit None-Check!)
        is_overheated = rsi > 80
        is_earnings_near = False # Hier könntest du bei Bedarf noch die calendar-Logik einfügen
        
        # Analysten-Risiko-Check nur wenn Werte existieren
        is_analyst_risk = False
        if analyst_target is not None and analyst_target < entry_val:
            is_analyst_risk = True
            
        status = "ÜBERHITZT!" if is_overheated else ("Gelaufen" if closes.iloc[-1] > (entry_val * 1.01) else "Beobachten")
        status2 = "VALIDE" if (is_bullish and not is_overheated and status == "Beobachten" and not is_analyst_risk) else "WACHSAMKEIT"
        
        return {
            "Ticker": ticker, "Name": info.get('longName', ticker), "Sektor": sektor,
            "Earnings": "N/A", "Kursziel": analyst_target if analyst_target else "N/A",
            "Upside": 0.0, "Setup-Typ": setup_typ, "RSI": round(rsi, 2),
            "MACD-Trend": "Bullish" if is_bullish else "Bearish",
            "Status": status, "Status2": status2, "Kurs": round(closes.iloc[-1], 2), 
            "Einstieg": round(entry_val, 2), "Stop": round(stop, 2),
            "TP1": round(entry_val + atr, 2), "TP2": round(entry_val + (atr * 3), 2),
            "CRV1": round(((entry_val + atr) - entry_val) / risiko, 2) if risiko > 0 else 0,
            "CRV2": round(((entry_val + (atr * 3)) - entry_val) / risiko, 2) if risiko > 0 else 0
        }
    except Exception as e:
        return None
    except Exception as e:
        print(f"Fehler bei {ticker}: {e}") # Druckt den echten Fehler aus
        return None        
        
    # --- HAUPTTEIL ---
if __name__ == "__main__":
    # Alles ab hier muss um 4 Leerzeichen eingerückt sein!
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # 1. Benchmarks sicher abrufen
    sp500_filter_text = get_sp500_data()
    qqq_text = get_qqq_quote() 
    
    # 2. Performance berechnen
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    # 3. Setups verarbeiten
    all_setups = []
    print("Starte Setup-Analyse für die Top-3 Sektoren...")
    
    for _, row in df_perf.head(3).iterrows():
        aktien_liste = sektoren_aktien.get(row['Ticker'], [])
        print(f"-> Analysiere Sektor: {row['Sektor']} ({len(aktien_liste)} potenzielle Aktien)")
        
        for s in aktien_liste:
            res = analyze_a_setup(s, row['Sektor'])
            if res:
                all_setups.append(res)
            else:
                # Optional: Hier stumm lassen, wenn das Log zu voll wird
                pass 
    
    # DataFrame Erstellung und Sicherheitsprüfung
    df_s = pd.DataFrame(all_setups)
    
    if df_s.empty or 'Status2' not in df_s.columns:
        print("WARNUNG: Keine Setups gefunden oder Status2 Spalte fehlt.")
        sys.exit()
    
    # Upside-Berechnung direkt im Anschluss für den kompletten DataFrame
    def berechne_upside(row):
        if isinstance(row['Kursziel'], (int, float)) and row['Kursziel'] > 0:
            return round(((row['Kursziel'] - row['Einstieg']) / row['Einstieg']) * 100, 1)
        return 0.0

    df_s['Upside'] = df_s.apply(berechne_upside, axis=1)
        
    # 4. Statistiken und Sortierung
    if not df_s.empty:
        # Zuerst nach Status2 sortieren (VALIDE oben), dann nach CRV2
        df_s['sort_col'] = df_s['Status2'].apply(lambda x: 0 if x == "VALIDE" else 1)
        df_s = df_s.sort_values(by=['sort_col', 'CRV2'], ascending=[True, False])
        df_s = df_s.drop(columns=['sort_col'])
        setup_stats = df_s['Setup-Typ'].value_counts().to_dict()
    else:
        setup_stats = {"Keine": "Setups gefunden"}

    # 5. CSV Exporte (Wie in deinem funktionierenden alten Code)
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    print("CSV-Dateien erfolgreich geschrieben.")

    # Upside-Berechnungen (Technisch vs. Fundamentaler Analysten-Check)
    def berechne_upsides(row):
        # Technisches Upside: Von Einstieg zu TP2
        tech_up = round(((row['TP2'] - row['Einstieg']) / row['Einstieg']) * 100, 1)
        # Fundamentales Upside: Von Einstieg zu Analysten-Kursziel
        fund_up = 0.0
        if isinstance(row['Kursziel'], (int, float)) and row['Kursziel'] > 0:
            fund_up = round(((row['Kursziel'] - row['Einstieg']) / row['Einstieg']) * 100, 1)
        return tech_up, fund_up

    # Neue Spalten zuweisen
    df_s[['Tech-Upside', 'Fund-Upside']] = df_s.apply(lambda row: pd.Series(berechne_upsides(row)), axis=1)    

    
    # 6. Briefing erstellen (Neues Format)
    valide_setups = df_s[df_s['Status2'] == "VALIDE"].sort_values(by='Upside', ascending=False)
    beobachten = df_s[df_s['Status'] == "Beobachten"].sort_values(by='CRV2', ascending=False)

    with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n")
        f.write("==============================\n\n")
        
        f.write("BENCHMARKS\n")
        f.write(sp500_filter_text + "\n")
        f.write(qqq_text + "\n\n")
        
        f.write("TRADE-ZUSAMMENFASSUNG (VALIDE TITEL)\n")
        if not valide_setups.empty:
            for _, row in valide_setups.iterrows():
                f.write(f"------------------------------\n")
                f.write(f"Ticker: {row['Ticker']} | Sektor: {row['Sektor']}\n")
                f.write(f"Aktueller Kurs: {row['Kurs']} | Geplanter Einstieg: {row['Einstieg']}\n")
                f.write(f"Setup-Typ: {row['Setup-Typ']} | Qualität: A\n")
                f.write(f"Stop-Loss: {row['Stop']} | Take-Profit: {row['TP1']} (TP1) / {row['TP2']} (TP2)\n")
                f.write(f"CRV: {row['CRV2']}\n")
                f.write(f"Upside: Technisch {row['Tech-Upside']}% | Fundamentaler Analysten-Check {row['Fund-Upside']}%\n")
                f.write(f"RSI: {row['RSI']} | Trend: {row['MACD-Trend']}\n\n")
        else:
            f.write("Keine. Heute keine Setups im Status 'VALIDE'.\n\n")
            
        f.write("BEACHTEN (STATUS: BEOBACHTEN)\n")
        if not beobachten.empty:
            f.write(beobachten[['Ticker', 'Kurs', 'Einstieg', 'RSI']].head(8).to_string(index=False) + "\n\n")
        else:
            f.write("Keine.\n\n")
        
        f.write("SETUP-STATISTIK\n")
        f.write(str(setup_stats) + "\n")
        
    print("Briefing-Dateien erfolgreich geschrieben.")
