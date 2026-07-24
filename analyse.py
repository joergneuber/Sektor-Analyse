import pandas as pd
import numpy as np
import yfinance as yf
import datetime
import time
import sys
import os
from scipy.signal import argrelextrema
from groq import Groq

# Importe für Alpaca
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
# News-API (Benzinga-Feed, in den bestehenden Alpaca-Keys enthalten) - defensiv
# importiert, damit ein Versions-Problem der Bibliothek nie den Lauf stoppt
try:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    NEWS_VERFUEGBAR = True
except Exception:
    NEWS_VERFUEGBAR = False
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
    "BOTZ": "Robotik", "IHI": "Medical Devices", "PAVE": "Infrastruktur", "XRT": "Einzelhandel",
    "ITA": "Rüstung/Aerospace", "XME": "Minen/Metalle", "GDX": "Gold-Miner",
    "OIH": "Öl-Services", "TAN": "Solar/Clean Energy"
}

sektoren_aktien = {
    "XLK": ["AAPL", "MSFT", "ORCL", "ADBE", "CRM", "AVGO", "TXN", "NVDA", "CSCO", "INTC",
            "MRVL", "KLAC", "SNPS", "CDNS", "PYPL", "EA", "INTU", "NOW"],
    "XLF": ["JPM", "BAC", "GS", "MS", "C", "AXP", "WFC", "SCHW", "BLK", "USB", "PNC", "TFC", "COF"],
    "XLV": ["UNH", "JNJ", "LLY", "MRK", "PFE", "ABBV", "TMO", "DHR", "AMGN", "GILD", "ISRG", "BMY", "CVS"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "TGT", "GM", "F", "BKNG", "CMG", "ORLY"],
    "XLP": ["PG", "KO", "PEP", "COST", "WMT", "CL", "EL", "MDLZ", "GIS", "KLG", "KMB", "KHC", "SYY"],
    "XLE": ["XOM", "CVX", "SLB", "COP", "EOG", "MPC", "PSX", "VLO", "HAL", "OXY", "DVN", "FANG", "WMB"],
    "XLI": ["CAT", "GE", "HON", "BA", "UPS", "LMT", "DE", "MMM", "RTX", "UNP", "ETN", "CSX", "WM"],
    "XLB": ["LIN", "APD", "ECL", "SHW", "FCX", "NEM", "DD", "DOW", "PPG", "VMC", "NUE", "MLM", "IFF"],
    "XLU": ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PEG", "ED", "XEL", "WEC", "ES", "AWK"],
    "XLRE": ["PLD", "AMT", "EQIX", "PSA", "SPG", "O", "DLR", "WELL", "AVB", "CCI", "VICI", "EXR", "IRM"],
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA", "TTWO", "LYV", "OMC"],
    "SOXX": ["NVDA", "AVGO", "TXN", "QCOM", "INTC", "AMD", "MU", "ADI", "LRCX", "AMAT",
             "KLAC", "MRVL", "MPWR", "SWKS", "ON", "MCHP", "TER", "ENTG"],
    "SMH": ["NVDA", "TSM", "ASML", "AVGO", "QCOM", "TXN", "AMAT", "AMD", "LRCX", "MU",
            "KLAC", "MRVL", "MPWR", "ON", "MCHP"],
    "IGV": ["MSFT", "ADBE", "CRM", "ORCL", "SNOW", "PANW", "WDAY", "INTU", "NOW", "ADSK",
            "CRWD", "ZS", "DDOG", "TEAM", "HUBS", "VEEV", "PTC", "BSY"],
    "XBI": ["AMGN", "GILD", "BIIB", "VRTX", "REGN", "ILMN", "TECH", "MRNA", "IBB",
            "INCY", "EXEL", "NBIX", "BMRN", "UTHR"],
    "KRE": ["FITB", "HBAN", "CFG", "KEY", "ZION", "RF", "CMA", "SNV", "FLG", "WBS", "EWBC", "PNFP", "WAL"],
    "HACK": ["PANW", "CRWD", "FTNT", "OKTA", "ZS", "CHKP", "QLYS", "TENB", "VRSN",
             "S", "NET", "RPD", "VRNS", "FFIV"],
    "CLOU": ["SNOW", "CRWD", "OKTA", "ZS", "DDOG", "NET", "MDB", "TEAM", "DOCU",
             "TWLO", "HUBS", "BILL", "PATH", "FSLY", "ESTC"],
    "AIQ": ["NVDA", "MSFT", "GOOGL", "META", "AAPL", "AMD", "TSM", "ORCL", "ADBE", "CRM",
            "PLTR", "SNOW", "NOW", "CRWD", "MRVL"],
    "BOTZ": ["NVDA", "ABB", "ISRG", "ROK", "TER", "ITW", "PTC", "FLIR", "TYL", "AMRC",
             "CGNX", "SYM"],
    "IHI": ["ABT", "DHR", "MDT", "BSX", "SYK", "ZBH", "EW", "BAX", "RMD", "ALGN", "PODD", "DXCM", "GEHC"],
    "PAVE": ["DE", "CAT", "ETN", "JCI", "PH", "IR", "CMI", "XYL", "ITW", "EMR", "PWR", "MLM", "URI"],
    "XRT": ["AMZN", "HD", "LOW", "TGT", "COST", "WMT", "BBY", "TJX", "ROST", "ULTA", "DKS", "BURL", "FIVE"],
    "ITA": ["RTX", "LMT", "NOC", "GD", "BA", "LHX", "HWM", "TDG", "HEI", "AXON", "TXT", "HII"],
    "XME": ["FCX", "NUE", "STLD", "CLF", "AA", "X", "RS", "CMC", "ATI", "MP", "HL", "CRS"],
    "GDX": ["NEM", "GOLD", "AEM", "WPM", "FNV", "GFI", "KGC", "AU", "RGLD", "PAAS", "HMY", "EGO"],
    "OIH": ["SLB", "HAL", "BKR", "FTI", "NOV", "CHX", "WFRD", "RIG", "HP", "PTEN", "LBRT", "VAL"],
    "TAN": ["FSLR", "ENPH", "SEDG", "RUN", "NXT", "ARRY", "SHLS", "CSIQ", "JKS", "DQ", "MAXN", "FLNC"]
}

# --- STOXX EUROPE 600 / DAX (Xetra, via yfinance) ---
# Sektor-Rotation läuft über STOXX-Europe-600-Sektor-ETFs (breiterer Referenzrahmen),
# die Kandidaten stammen aus DAX40, MDAX und Eurozonen-Large-Caps (nur EUR-Börsen).
# Hinweis: Nur 3 der 7 ETF-Ticker (Banken, Versicherungen, Versorger) wurden einzeln
# verifiziert; die übrigen folgen dem etablierten iShares-Namensschema, sollten aber
# einmalig gegengeprüft werden. Falls ein Ticker falsch ist, liefert get_perf_yf()
# einfach eine Performance von 0 für diesen Sektor (kein Absturz, siehe Try/Except).
eu_sektoren_etf = {
    "EXV1.DE": "Banken",
    "EXH5.DE": "Versicherungen",
    "EXV3.DE": "Technologie",
    "EXV4.DE": "Gesundheit",
    "EXV6.DE": "Industrie",
    "EXH9.DE": "Versorger",
    "EXV5.DE": "Automobil",
}

eu_benchmark_ticker = "EXSA.DE"  # iShares STOXX Europe 600 UCITS ETF (DE) - EU-Referenzindex für RS

# EU-Ticker nach Sektor (Stand: Juli 2026;
# die Zusammensetzung wird von der Deutschen Börse zweimal jährlich überprüft, daher
# gelegentlich gegenchecken)
dax_aktien = {
    # EU-Kandidaten (nicht mehr nur DAX): DAX40 + MDAX + Eurozonen-Large-Caps.
    # Bewusst NUR Börsen mit EUR-Notierung (.DE Xetra, .PA Paris, .AS Amsterdam,
    # .MI Mailand, .MC Madrid) - keine .CO/.SW/.L-Titel, da die EU-Pipeline
    # durchgängig EUR als Währung annimmt.
    "Banken": ["DBK.DE", "CBK.DE", "BNP.PA", "ACA.PA", "GLE.PA", "INGA.AS", "ISP.MI", "UCG.MI", "SAN.MC", "BBVA.MC"],
    "Versicherungen": ["ALV.DE", "MUV2.DE", "HNR1.DE", "TLX.DE", "CS.PA", "G.MI"],
    "Technologie": ["SAP.DE", "IFX.DE", "NEM.DE", "AIXA.DE", "BC8.DE", "ASML.AS", "ADYEN.AS", "BESI.AS", "CAP.PA", "STMPA.PA", "PRX.AS"],
    "Gesundheit": ["BAYN.DE", "MRK.DE", "FRE.DE", "FME.DE", "SRT3.DE", "QIA.DE", "SHL.DE", "EVT.DE", "SAN.PA", "PHIA.AS"],
    "Industrie": ["SIE.DE", "AIR.DE", "MTX.DE", "RHM.DE", "CON.DE", "DHL.DE", "G1A.DE", "DTG.DE", "BAS.DE", "KGX.DE", "NDX1.DE", "SU.PA", "SAF.PA", "AI.PA", "PIA.MI"],
    "Versorger": ["EOAN.DE", "RWE.DE", "ENEL.MI", "IBE.MC", "ENGI.PA", "VIE.PA"],
    "Automobil": ["VOW3.DE", "BMW.DE", "MBG.DE", "P911.DE", "RNO.PA", "STLAM.MI", "ML.PA", "PIRC.MI"],
}

def berechne_indikatoren(df):
    # 1. MultiIndex entfernen (wichtig für yfinance-Struktur)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 2. Prüfen, ob 'Close' existiert (Sicherheitsprüfung)
    if 'Close' not in df.columns:
        return df 

    # 3. RSI berechnen (ersetzt die alten, fehlerhaften Zeilen 74-93)
    # get_safe_rsi kümmert sich intern um die Prüfung der Länge und Division durch Null
    df['RSI'] = get_safe_rsi(df)
    
    return df
    
def get_analyst_target(ticker, retries=3):
    """Holt Analysten-Daten mit Retry-Logik."""
    for i in range(retries):
        try:
            stock = yf.Ticker(ticker)
            data = stock.info
            target = data.get('targetMeanPrice')
            
            if target and target > 0:
                return target
            return None
            
        except Exception as e:
            print(f"Versuch {i+1} für {ticker} fehlgeschlagen: {e}. Warte 2s...")
            time.sleep(2)
    return None

def get_safe_rsi(df, period=14):
    """Berechnet RSI und gibt immer eine saubere Series zurück."""
    if 'Close' not in df.columns or len(df) < period:
        return pd.Series([50.0] * len(df), index=df.index)
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    # Division durch Null verhindern
    rs = gain / loss.replace(0, 1e-9) 
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


# --- FUNKTIONEN ---
def update_status_logic(row):
    # ... (deine Variablen-Extraktion bleibt gleich)
    rsi = row.get('RSI', 50) # Standard 50, falls was schiefgeht
    pattern = row.get('Pattern', "Kein")
    vol_ratio = row.get('Vol_Ratio', 1.0) # Standard 1.0, damit kein Fehler bei < 0.5
    macd_trend = row.get('MACD_Trend', "Neutral")
    kurs = row.get('Kurs', 0)
    tp1 = row.get('TP1', float('inf')) # Unendlich, falls kein TP1 existiert
    divergenz = row.get('Divergenz', "Keine")

    # Logik mit den sicheren Variablen
    if rsi > 70:
        return pd.Series(["ACHTUNG", "RSI überkauft (>70)"])
    # NEU: Dein RSI-Check für unter 30
    elif rsi < 30:
        return pd.Series(["VALIDE", "RSI überverkauft (<30) - Kaufsignal"])   
    elif pattern != "Kein" and vol_ratio < 0.5:
        return pd.Series(["ACHTUNG", f"Schwaches Volumen ({round(float(vol_ratio), 2)}x SMA20)"])
    elif macd_trend == "Bärisch":
        return pd.Series(["ACHTUNG", "Bärischer MACD-Trend"])
    elif kurs >= tp1:
        return pd.Series(["GELAUFEN", "Kursziel erreicht"])
    elif divergenz == "Bullisch":
        return pd.Series(["VALIDE", "Bullische Divergenz (Signal)"])
    
    # 3. STANDARDFALL
    else:
        return pd.Series(["VALIDE", "Alles ok"])

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
            
        close = hist['Close']
        last_close = close.iloc[-1]
        
        # Indikatoren berechnen
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1] # Neu
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        return (f"S&P 500: {last_close:.2f} | EMA20: {e20:.0f} | EMA50: {e50:.0f} | "
                f"EMA100: {e100:.0f} | EMA200: {e200:.0f} | WMA200: {w200:.0f}")
                
    except Exception as e:
        return f"S&P 500: Fehler beim Abruf ({e})"

def get_earnings_warnung(ticker, warn_tage=7):
    """Prüft per yfinance, ob der nächste Earnings-Termin innerhalb der
    nächsten warn_tage liegt. Gibt einen Warntext zurück (z.B.
    '⚠ Earnings in 3 Tagen (21.07.2026)') oder None. Earnings-Gaps sind
    das größte Über-Nacht-Risiko für Swing-Positionen - ein Stop schützt
    nicht vor einem Gap unter den Stop-Kurs. Defensiv: jeder Fehler
    (kein Termin verfügbar, API-Aussetzer) führt still zu None."""
    try:
        kalender = yf.Ticker(ticker).calendar
        termine = None
        if isinstance(kalender, dict):
            termine = kalender.get('Earnings Date')
        if not termine:
            print(f"DEBUG-EARNINGS: {ticker} -> kein Termin im Kalender hinterlegt")
            return None
        naechster = termine[0] if isinstance(termine, (list, tuple)) else termine
        heute = datetime.date.today()
        if hasattr(naechster, 'date'):
            naechster = naechster.date()
        delta = (naechster - heute).days
        if 0 <= delta <= warn_tage:
            tage_text = "HEUTE" if delta == 0 else f"in {delta} Tag{'en' if delta != 1 else ''}"
            print(f"DEBUG-EARNINGS: {ticker} -> Termin {naechster.strftime('%d.%m.%Y')} (in {delta} Tagen) -> WARNUNG")
            return f"⚠ Earnings {tage_text} ({naechster.strftime('%d.%m.%Y')})"
        print(f"DEBUG-EARNINGS: {ticker} -> nächster Termin {naechster.strftime('%d.%m.%Y')} (in {delta} Tagen, außerhalb Warnfenster)")
        return None
    except Exception as e:
        print(f"DEBUG-EARNINGS: {ticker} -> kein Termin ermittelbar ({type(e).__name__})")
        return None


_news_client = None

def get_news_headlines(ticker, max_n=3):
    """Holt die jüngsten Schlagzeilen zu einem US-Ticker über die Alpaca-
    News-API (Benzinga-Feed, in den bestehenden Keys enthalten). Gibt eine
    Liste 'TT.MM.: Titel' zurück (max. max_n). Nur für suffixlose US-Ticker -
    für EU-Titel liefert der Feed nichts, dann leere Liste. Defensiv: jeder
    Fehler führt still zu leerer Liste, News sind reiner Zusatz-Kontext."""
    global _news_client
    if not NEWS_VERFUEGBAR:
        print(f"DEBUG-NEWS: {ticker} -> übersprungen (News-API in alpaca-py nicht verfügbar)")
        return []
    if '.' in str(ticker):
        print(f"DEBUG-NEWS: {ticker} -> übersprungen (EU-Ticker, kein US-News-Feed)")
        return []
    try:
        if _news_client is None:
            _news_client = NewsClient(os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'))
        req = NewsRequest(symbols=str(ticker), limit=max_n)
        antwort = _news_client.get_news(req)
        # Versionssichere Extraktion: je nach alpaca-py-Version liegen die
        # Artikel unter antwort.news ODER unter antwort.data['news'] (NewsSet)
        artikel_liste = getattr(antwort, 'news', None)
        if artikel_liste is None:
            daten = getattr(antwort, 'data', None)
            artikel_liste = daten.get('news', []) if isinstance(daten, dict) else []
        headlines = []
        for artikel in list(artikel_liste)[:max_n]:
            datum = artikel.created_at.strftime('%d.%m.') if getattr(artikel, 'created_at', None) else ''
            titel = getattr(artikel, 'headline', '') or ''
            if titel:
                headlines.append(f"{datum}: {titel}")
        print(f"DEBUG-NEWS: {ticker} -> {len(headlines)} Schlagzeile(n) gefunden")
        return headlines
    except Exception as e:
        print(f"DEBUG: News für {ticker} nicht abrufbar ({e})")
        return []


def get_zins_warner():
    """NEU (22.07.2026): reiner Kontext-Indikator (wie VIX/Lithium-Proxy),
    KEINE Setup-Quelle, KEINE Abwertungsgrundlage. ^TYX = CBOE 30-Year
    Treasury Yield Index (Rendite 30-jaehriger US-Staatsanleihen in Prozent,
    z.B. 4.85 = 4,85%). Steigende Langfristrenditen gelten klassisch als
    Belastung fuer Aktienbewertungen (v.a. Wachstums-/Tech-Werte) - anhaltend
    hohe/steigende Werte (insbesondere neue Mehrjahres-Hochs) sind ein
    Warnsignal fuer den Gesamtmarkt, sinkende Renditen eher entlastend.
    2 Nachkommastellen (nicht .0f wie bei Index-Benchmarks), da Renditen im
    niedrigen einstelligen Prozentbereich liegen - .0f wuerde die
    Aussagekraft komplett zerstoeren."""
    try:
        hist = yf.Ticker("^TYX").history(period="300d")
        hist = hist.dropna(subset=['Close'])
        if hist.empty or len(hist) < 200:
            return "Zins-Warner (30J-US-Rendite): Daten unvollständig"

        close = hist['Close']
        last_close = close.iloc[-1]
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]

        return (f"Zins-Warner (30J-US-Staatsanleihenrendite, ^TYX): {last_close:.2f}% | "
                f"EMA20: {e20:.2f}% | EMA50: {e50:.2f}%")
    except Exception as e:
        print(f"DEBUG: Zins-Warner nicht verfügbar ({e}).")
        return "Zins-Warner (30J-US-Rendite): Daten unvollständig"


def get_10j_rendite():
    """NEU (24.07.2026): Ergaenzung zu get_zins_warner (30J) - die 10-jaehrige
    US-Staatsanleihenrendite (^TNX) ist die eigentliche Standard-Referenz, an
    der Aktienbewertungen/KGVs ueblicherweise gemessen werden (anders als die
    30J-Rendite, die eher als Langfrist-Warnsignal dient). Auf Yahoo Finance
    wird ^TNX bereits direkt als Prozentwert gefuehrt (z.B. 4.66 = 4,66%),
    keine Skalierung noetig - identisches Format wie get_zins_warner. Reiner
    Kontext-Indikator, KEINE Setup-Quelle, KEINE Abwertungsgrundlage."""
    try:
        hist = yf.Ticker("^TNX").history(period="300d")
        hist = hist.dropna(subset=['Close'])
        if hist.empty or len(hist) < 200:
            return "10J-US-Staatsanleihenrendite (^TNX): Daten unvollständig"

        close = hist['Close']
        last_close = close.iloc[-1]
        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]

        return (f"10J-US-Staatsanleihenrendite (^TNX): {last_close:.2f}% | "
                f"EMA20: {e20:.2f}% | EMA50: {e50:.2f}%")
    except Exception as e:
        print(f"DEBUG: 10J-Rendite nicht verfügbar ({e}).")
        return "10J-US-Staatsanleihenrendite (^TNX): Daten unvollständig"


def get_index_benchmark_yf(ticker, label):
    """Generische Benchmark-Funktion für Indizes, die nicht über Alpaca verfügbar
    sind (z.B. DAX, EuroStoxx50) - lädt Kursdaten via yfinance, identisches
    Kennzahlen-Format wie get_sp500_data()."""
    try:
        hist = yf.Ticker(ticker).history(period="300d")

        if hist.empty:
            return f"{label}: Daten unvollständig"

        # NaN-Platzhalterzeilen entfernen (yfinance legt vor Börsenöffnung teils
        # eine leere Zeile für den aktuellen Tag an - sonst zeigt iloc[-1] auf NaN
        # statt auf den letzten echten Schlusskurs)
        hist = hist.dropna(subset=['Close'])

        if hist.empty or len(hist) < 200:
            return f"{label}: Daten unvollständig"

        close = hist['Close']
        last_close = close.iloc[-1]

        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]

        return (f"{label}: {last_close:.2f} | EMA20: {e20:.0f} | EMA50: {e50:.0f} | "
                f"EMA100: {e100:.0f} | EMA200: {e200:.0f} | WMA200: {w200:.0f}")

    except Exception as e:
        return f"{label}: Fehler beim Abruf ({e})"

def get_benchmark_close():
    """Lädt die rohen SPY-Schlusskurse (ca. 1 Jahr) als Series für die
    Relative-Stärke-Berechnung einzelner Aktien gegenüber dem Gesamtmarkt."""
    try:
        start_date = datetime.datetime.now() - datetime.timedelta(days=365)
        request = StockBarsRequest(symbol_or_symbols=["SPY"], start=start_date, timeframe=TimeFrame.Day)
        bars = alpaca_client.get_stock_bars(request)
        hist = bars.df

        if hist.empty:
            print("DEBUG: SPY-Benchmark leer, Relative Stärke wird übersprungen.")
            return None

        hist = hist.reset_index(level=0, drop=True)
        if 'close' in hist.columns:
            hist = hist.rename(columns={'close': 'Close'})

        return hist['Close']

    except Exception as e:
        print(f"FEHLER beim Laden der SPY-Benchmark: {e}")
        return None

# --- EU-SPEZIFISCHE DATENFUNKTIONEN (yfinance, da Alpaca keine STOXX-600-Werte abdeckt) ---

def get_eu_benchmark_close():
    """Lädt die rohen Schlusskurse des STOXX-Europe-600-ETF (EXSA.DE) für die
    Relative-Stärke-Berechnung der DAX-Werte gegenüber dem europäischen Markt."""
    try:
        hist = yf.Ticker(eu_benchmark_ticker).history(period="1y")
        if hist.empty:
            print("DEBUG: EU-Benchmark (EXSA.DE) leer, Relative Stärke EU wird übersprungen.")
            return None
        # NaN-Platzhalterzeilen entfernen (siehe get_index_benchmark_yf)
        hist = hist.dropna(subset=['Close'])
        if hist.empty:
            print("DEBUG: EU-Benchmark (EXSA.DE) nach NaN-Bereinigung leer, Relative Stärke EU wird übersprungen.")
            return None
        return hist['Close']
    except Exception as e:
        print(f"FEHLER beim Laden der EU-Benchmark: {e}")
        return None

def get_perf_yf(ticker, name):
    """yfinance-Äquivalent zu get_perf() für die STOXX-Europe-600-Sektor-ETFs,
    da diese nicht über Alpaca verfügbar sind. Gleiche Kennzahlen/Formel wie US-Version."""
    try:
        hist = yf.Ticker(ticker).history(period="1y")

        if hist.empty:
            return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

        # NaN-Platzhalterzeilen entfernen (siehe get_index_benchmark_yf)
        hist = hist.dropna(subset=['Close'])

        if hist.empty:
            return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

        close = hist['Close']
        last = close.iloc[-1]

        def p(d):
            if len(close) > d:
                return round(((last / close.iloc[-d]) - 1) * 100, 2)
            return 0

        current_year = datetime.datetime.now().year
        ytd_data = close[close.index.year == current_year]
        ytd_perf = round(((last / ytd_data.iloc[0]) - 1) * 100, 2) if not ytd_data.empty else 0

        res = {
            "Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60), "YTD": ytd_perf
        }
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res

    except Exception as e:
        print(f"FEHLER bei EU-Performance-Berechnung für {ticker}: {e}")
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

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
                f"EMA100: {e100:.0f} | EMA200: {e200:.0f} | WMA200: {w200:.0f}")
                
    except Exception as e:
        print(f"FEHLER beim Abruf von QQQ: {e}")
        return f"Nasdaq: Fehler beim Datenabruf ({e})"

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

def check_rsi_divergence(data):
    """Prüft auf RSI-Divergenz in den letzten 40 Tagen."""
    # Wir schauen auf die letzten 40 Tage für die Minima/Maxima
    df = data.tail(40)
    
    # Lokale Extrema finden (order=5 bedeutet: min 5 Kerzen Abstand für einen Peak)
    ilocs_min = argrelextrema(df['Close'].values, np.less_equal, order=5)[0]
    ilocs_max = argrelextrema(df['Close'].values, np.greater_equal, order=5)[0]
    
    # Brauchen mindestens 2 Punkte für einen Vergleich
    if len(ilocs_min) < 2 or len(ilocs_max) < 2:
        return None

    # Bullische Divergenz (Preis tiefer, RSI höher)
    if (df['Close'].iloc[ilocs_min[-1]] < df['Close'].iloc[ilocs_min[-2]]) and \
       (df['RSI'].iloc[ilocs_min[-1]] > df['RSI'].iloc[ilocs_min[-2]]):
        return "Bullisch"
        
    # Bärische Divergenz (Preis höher, RSI tiefer)
    if (df['Close'].iloc[ilocs_max[-1]] > df['Close'].iloc[ilocs_max[-2]]) and \
       (df['RSI'].iloc[ilocs_max[-1]] < df['RSI'].iloc[ilocs_max[-2]]):
        return "Bärisch"
        
    return None

def check_trendline_breakout(data, lookback=120, order=5, touch_tolerance=0.01):
    """
    Sucht eine fallende Widerstands-Trendlinie durch mindestens 3 Swing-Highs
    (Toleranz: 1% Abstand zur Linie) in den letzten `lookback` Handelstagen
    und prüft, ob der Kurs innerhalb der letzten 3 Kerzen mit über-
    durchschnittlichem Volumen darüber ausgebrochen ist.
    Nur Long-Ausbrüche (fallende Linie nach oben durchbrochen) - ein Bruch
    einer STEIGENDEN Linie nach unten wird bewusst nicht erfasst, da die
    Strategie ausschließlich Long-Setups handelt.
    Gibt (ausbruch: bool, linien_level_heute: float|None) zurück.
    """
    fenster = data.iloc[-lookback:] if len(data) > lookback else data.copy()
    if len(fenster) < 10:
        return False, None

    # Ausbruchskerzen selbst (letzte 3) von der Linienbildung ausschließen,
    # damit die Linie nicht durch den möglichen Ausbruch selbst verzerrt wird
    suchbereich = fenster.iloc[:-3]
    if len(suchbereich) < 10:
        return False, None

    highs = suchbereich['High'].values
    idx_swings = argrelextrema(highs, np.greater_equal, order=order)[0]

    if len(idx_swings) < 3:
        return False, None

    x = idx_swings.astype(float)
    y = highs[idx_swings]
    slope, intercept = np.polyfit(x, y, 1)

    # Nur fallende Trendlinien relevant (Ausbruch nach oben = Long-Signal)
    if slope >= 0:
        return False, None

    # Berührungspunkte innerhalb der Toleranz zählen (mind. 3 gefordert)
    linie_bei_punkten = slope * x + intercept
    beruehrungen = int(np.sum(np.abs(y - linie_bei_punkten) <= (linie_bei_punkten * touch_tolerance)))
    if beruehrungen < 3:
        return False, None

    # Linie bis heute projizieren und Ausbruch prüfen: Kreuzung innerhalb der
    # letzten 3 Kerzen (analog zum EMA-Breakout-Fenster), aktuell darüber,
    # plus Volumen-Bestätigung an einem der letzten 3 Tage (nicht zwingend heute -
    # der eigentliche Ausbruchstag mit dem Volumen-Spike kann auch 1-2 Tage
    # zurückliegen, während der Kurs seitdem über der Linie hält)
    heute_pos = len(fenster) - 1
    linie_heute = slope * heute_pos + intercept
    close_heute = fenster['Close'].iloc[-1]

    crossover_kuerzlich = any(
        fenster['Close'].iloc[-1 - i] <= (slope * (heute_pos - i) + intercept)
        for i in range(1, 4)
    )

    volumen_ok = any(
        fenster['Volume'].iloc[-1 - i] > fenster['Vol_SMA20'].iloc[-1 - i]
        for i in range(0, 3)
    )

    ausbruch = bool(close_heute > linie_heute) and crossover_kuerzlich and bool(volumen_ok)
    return ausbruch, (float(linie_heute) if ausbruch else None)

def check_kumo_breakout(data):
    """
    Prüft einen echten Kumo-Ausbruch (Ichimoku-Wolke): Der Kurs muss die
    KOMPLETTE Wolke von unten nach oben durchbrochen haben - also über BEIDEN
    Grenzen (Senkou Span A und B) stehen, nicht nur über einer (sonst befindet
    sich der Kurs noch innerhalb der Wolke, kein echter Ausbruch).
    Der Ausbruch selbst darf innerhalb der letzten 3 Tage liegen (analog zum
    Crossover-Fenster bei EMA-Breakout/Trendlinie), aktuell muss der Kurs
    weiterhin oberhalb stehen. Pflicht-Volumen an einem der letzten 3 Tage
    (nicht zwingend heute - der Ausbruchstag mit Volumen-Spike kann auch
    1-2 Tage zurückliegen, während der Kurs seitdem über der Wolke hält).
    Gibt (ausbruch: bool, wolken_obergrenze_heute: float|None) zurück.
    """
    if len(data) < 5 or 'SenkouA' not in data.columns or 'SenkouB' not in data.columns:
        return False, None

    kumo_ober = data[['SenkouA', 'SenkouB']].max(axis=1)
    heute_ober = kumo_ober.iloc[-1]
    close_heute = data['Close'].iloc[-1]

    if pd.isna(heute_ober):
        return False, None

    ueber_wolke_heute = close_heute > heute_ober
    if not ueber_wolke_heute:
        return False, None

    # War innerhalb der letzten 3 Tage noch NICHT (vollständig) über der Wolke
    # - frischer Ausbruch, kein bereits seit langem etabliertes "über der Wolke"
    frischer_ausbruch = any(
        pd.notna(kumo_ober.iloc[-1 - i]) and data['Close'].iloc[-1 - i] <= kumo_ober.iloc[-1 - i]
        for i in range(1, 4)
    )

    volumen_ok = any(
        data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i]
        for i in range(0, 3)
    )

    ausbruch = bool(ueber_wolke_heute) and frischer_ausbruch and bool(volumen_ok)
    return ausbruch, (float(heute_ober) if ausbruch else None)

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

def get_golden_cross_status(data, tage=10):
    """NEU (21.07.2026): rein informativer Kommentar, KEIN Filter- oder
    Bewertungskriterium - taucht nur als Zusatzinfo im Briefing auf, hat
    keinerlei Einfluss auf Setup-Erkennung oder Setup-Qualität. Prüft, ob
    EMA50 die EMA200 innerhalb der letzten `tage` Handelstage gekreuzt hat:
    Golden Cross (EMA50 von unten nach oben, klassisch positiv gedeutet)
    oder Death Cross (EMA50 von oben nach unten, klassisch negativ gedeutet).
    Kein frischer Cross -> aktuelle Struktur (EMA50 über/unter EMA200) als
    schwächere Zusatzinfo."""
    if len(data) < 210 or 'EMA50' not in data.columns or 'EMA200' not in data.columns:
        return "N/A (zu wenig Kurshistorie)"
    ema50, ema200 = data['EMA50'], data['EMA200']
    for i in range(0, tage + 1):
        idx, idx_prev = -1 - i, -2 - i
        if abs(idx_prev) > len(data):
            break
        if pd.isna(ema50.iloc[idx_prev]) or pd.isna(ema200.iloc[idx_prev]):
            continue
        if ema50.iloc[idx] > ema200.iloc[idx] and ema50.iloc[idx_prev] <= ema200.iloc[idx_prev]:
            return f"GOLDEN CROSS vor {i} Handelstag(en) (EMA50 kreuzt EMA200 nach oben)"
        if ema50.iloc[idx] < ema200.iloc[idx] and ema50.iloc[idx_prev] >= ema200.iloc[idx_prev]:
            return f"DEATH CROSS vor {i} Handelstag(en) (EMA50 kreuzt EMA200 nach unten)"
    if ema50.iloc[-1] > ema200.iloc[-1]:
        return "Kein frischer Cross (EMA50 > EMA200, langfristig bullische Struktur)"
    return "Kein frischer Cross (EMA50 < EMA200, langfristig bärische Struktur)"


_sektor_kgv_cache = {}  # NEU (23.07.2026): Cache pro Skriptlauf (Key: (Markt, Sektor)) -
                         # verhindert, dass derselbe Sektor mehrfach abgefragt wird, nur
                         # weil mehrere validierte Setups im selben Sektor liegen. Wird bei
                         # jedem Skriptstart neu geleert (kein persistenter Cache).

_sektoren_map_rev = {name: etf for etf, name in sektoren_map.items()}  # Sektor-Name -> ETF-Ticker


def _sektor_median_kgv(sektor, markt, eigener_ticker):
    """NEU (23.07.2026): ermittelt den Median-KGV der Sektor-Peers (aus den
    bereits vorhandenen sektoren_aktien/dax_aktien-Listen - KEINE zusaetzliche
    Ticker-Recherche noetig) fuer einen fairen, sektor-relativen Vergleich statt
    einer pauschalen 15/30-Grenze (Halbleiter und Minen/Metalle haben strukturell
    unterschiedliche KGV-Niveaus). API-schonend durch zwei Massnahmen: (1) Cache
    pro Sektor - wird ein Sektor schon im selben Lauf abgefragt, kommt das
    Ergebnis aus dem Cache, kein erneuter Fetch; (2) einzelne fehlgeschlagene
    Peer-Abfragen brechen die Berechnung nicht ab, sie werden einfach
    uebersprungen. Gibt None zurueck, wenn kein Sektor bekannt ist oder zu wenige
    Peers auswertbar sind (< 3) - Aufrufer faellt dann auf die alte feste
    15/30-Grenze zurueck (Sicherheitsnetz, kein Abbruch)."""
    cache_key = (markt, sektor)
    if cache_key in _sektor_kgv_cache:
        return _sektor_kgv_cache[cache_key]

    if markt == "EU":
        peers = dax_aktien.get(sektor, [])
    else:
        peers = sektoren_aktien.get(_sektoren_map_rev.get(sektor), [])

    kgv_werte = []
    for peer in peers:
        if peer == eigener_ticker:
            continue  # der zu bewertende Titel selbst zaehlt nicht als eigener Peer
        try:
            peer_kgv = yf.Ticker(peer).info.get("trailingPE")
            if peer_kgv and peer_kgv > 0:
                kgv_werte.append(peer_kgv)
        except Exception:
            continue  # einzelner Peer-Fehler soll den Sektor-Median nicht verhindern

    median = round(pd.Series(kgv_werte).median(), 1) if len(kgv_werte) >= 3 else None
    _sektor_kgv_cache[cache_key] = median
    return median


def berechne_fundamental_ampel(ticker, sektor=None, markt=None):
    """GEAENDERT (23.07.2026): KGV wird jetzt relativ zum Sektor-Median bewertet
    statt an einer pauschalen 15/30-Grenze - Halbleiter/Software (strukturell
    hohe KGVs) und Minen/Banken (strukturell niedrige KGVs) waren bei der alten
    festen Grenze nicht fair vergleichbar (z.B. Broadcom vs. Freeport-McMoRan).
    GUENSTIG: KGV < 80% des Sektor-Median | TEUER: KGV > 130% des Sektor-Median |
    dazwischen NEUTRAL. Ohne sektor/markt-Angabe (oder falls kein Sektor-Median
    ermittelbar, siehe _sektor_median_kgv) faellt die Funktion auf die alte feste
    15/30-Grenze zurueck - reines Sicherheitsnetz, kein Funktionsverlust.
    Wird nur für die bereits gefilterte, kleine Setup-Liste aufgerufen
    (nicht für das ganze ~370er-Universum) - hält die zusätzliche API-Last
    gering."""
    try:
        info = yf.Ticker(ticker).info
        kgv = info.get("trailingPE")
        if kgv is None or kgv <= 0:
            return "N/A", "Kein KGV verfügbar (z. B. Verlust-Unternehmen) - keine Bewertungsaussage möglich."

        sektor_median = _sektor_median_kgv(sektor, markt, ticker) if sektor and markt else None

        if sektor_median is None:
            if kgv < 15:
                return "GUENSTIG", f"KGV {round(kgv, 1)} - unterhalb der groben 15er-Hausnummer (kein Sektor-Vergleich möglich)."
            elif kgv > 30:
                return "TEUER", f"KGV {round(kgv, 1)} - oberhalb der groben 30er-Hausnummer (kein Sektor-Vergleich möglich)."
            else:
                return "NEUTRAL", f"KGV {round(kgv, 1)} - im üblichen Rahmen (kein Sektor-Vergleich möglich)."

        rel = kgv / sektor_median
        if rel < 0.8:
            return "GUENSTIG", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - {round((1 - rel) * 100)}% günstiger als der Sektor."
        elif rel > 1.3:
            return "TEUER", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - {round((rel - 1) * 100)}% teurer als der Sektor."
        else:
            return "NEUTRAL", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - im üblichen Rahmen für den Sektor."
    except Exception as e:
        print(f"DEBUG: Fundamental-Ampel für {ticker} nicht verfügbar ({e}).")
        return "N/A", "Fundamentaldaten aktuell nicht abrufbar."



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

def analyze_a_setup(ticker, sektor, spy_close=None):
    upside_potenzial = None
    # Firmennamen abrufen
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        firma_name = info.get('longName', ticker)
        if not firma_name or firma_name == "":
            firma_name = ticker
    except:
        firma_name = ticker

    # 0. Initialisierung
    setup_typ = "Kein"
    pattern = "Kein"
    tp1 = 0

    # Start des Haupt-Blocks
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
       
        # Vor der Berechnung des RSI:
        if len(data) < 15: # Puffer für 14 Perioden + 1
            print(f"Zu wenig Daten für {ticker}: {len(data)} Zeilen")
            return None
            
        # RSI Berechnung
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.000001)
        data['RSI'] = 100 - (100 / (1 + rs))
        data['RSI'] = data['RSI'].fillna(50)
        divergenz = check_rsi_divergence(data)

        # 1. Indikatoren berechnen
        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        data['Vol_SMA20'] = data['Volume'].rolling(20).mean()

        # Ichimoku-Basiswerte (NEU): Tenkan-sen/Kijun-sen als Hoch-Tief-
        # Mittelpunkte (andere Berechnungsgrundlage als die EMAs oben), Senkou
        # Span A/B als projizierte Kumo-Grenzen. Werden unten als zusätzliche
        # TP-Kandidaten (Kumo) bzw. zusätzliches Pullback-Level (Kijun-sen)
        # genutzt - erscheinen NICHT als eigene Briefing-Felder, sondern
        # fließen nur in die bestehenden TP1/TP2/Setup-Typ-Werte mit ein.
        data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
        data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
        data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)  # wie im Chart: 26 Perioden Vorlauf
        data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

        entry = data['Close'].iloc[-1]
        stop = data['Low'].rolling(10).min().iloc[-1]

        # --- NEU: Stochastik & Marktstruktur ---
        # Stochastik (14,3,3)
        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
        data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()
        
        # Marktstruktur (einfacher Higher-Low Check: Low[last] > Low[prev])
        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        
        # Danach direkt prüfen:
        if 'RSI' not in data.columns:
            print(f"RSI-Berechnung fehlgeschlagen für {ticker}")
            return None
        
        data['Vol_Ratio'] = data['Volume'] / data['Vol_SMA20']
        data['Vol_Ratio'] = data['Vol_Ratio'].fillna(0)

        # MACD Berechnung
        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        # 2. Trend-Status
        trend_status = "Unter WMA200/EMA200" if (data['Close'].iloc[-1] < data['WMA200'].iloc[-1] or data['Close'].iloc[-1] < data['EMA200'].iloc[-1]) else "OK"

        # 3. Candlestick-Muster
        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        if lower_wick > (2 * body): 
            pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        # 4. EMA-Ausbruch (Crossover darf innerhalb der letzten 3 Kerzen liegen, nicht nur gestern)
        crossover_kuerzlich = any(
            data['EMA8'].iloc[-1 - i] <= data['EMA20'].iloc[-1 - i] for i in range(1, 4)
        )
        # Volumen-Bestätigung an einem der letzten 3 Tage (nicht zwingend heute -
        # der eigentliche Ausbruchstag mit dem Volumen-Spike kann auch 1-2 Tage
        # zurückliegen, während der Kurs seitdem über der EMA20 hält)
        volumen_kuerzlich = any(
            data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i] for i in range(0, 3)
        )
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       crossover_kuerzlich and \
                       volumen_kuerzlich
       
        # --- 5. Setup-Typ mit Pro-Check Filter ---
        
        # Berechnungen für den Filter
        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        stoch_k = 100 * ((data['Close'].iloc[-1] - low_min.iloc[-1]) / (high_max.iloc[-1] - low_min.iloc[-1] + 1e-9))
        
        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        buffer = 0.01  # 1.0% Puffer für Zone (vorher 0.3%)
        
        # Prüfung: Kurs in der EMA-Zone? Richtungsabhängig (NEU): Ein reiner
        # Bruch nach unten (Kurs dauerhaft unter der EMA) zählt nicht mehr als
        # Pullback-Test, auch wenn er innerhalb der Toleranz liegt - das wäre
        # ein Unterstützungsbruch, keine Bestätigung. Es zählt nur, wenn der
        # Kurs aktuell nah an der EMA liegt UND innerhalb der letzten 3 Tage
        # mindestens einmal auf/über der EMA stand (frischer Reclaim erlaubt,
        # analog zum 3-Tage-Fenster beim EMA-Breakout).
        price = data['Close'].iloc[-1]

        def ema_pullback_test(ema_series):
            ema_heute = ema_series.iloc[-1]
            nah_dran = abs(price - ema_heute) < (price * buffer)
            war_ueber_ema_kuerzlich = any(
                data['Close'].iloc[-1 - i] >= ema_series.iloc[-1 - i] for i in range(0, 3)
            )
            return nah_dran and war_ueber_ema_kuerzlich

        in_ema_zone = any(ema_pullback_test(ema_series) for ema_series in [data['EMA20'], data['EMA50'], data['Kijun']])

        # Dritter, eigenständiger Setup-Typ: Ausbruch aus einer fallenden
        # Trendlinie (mind. 3 Berührungspunkte, 1% Toleranz, Pflicht-Volumen)
        trendlinien_ausbruch, tl_level = check_trendline_breakout(data)

        # Vierter, eigenständiger Setup-Typ: echter Kumo-Ausbruch (Ichimoku-
        # Wolke komplett von unten nach oben durchbrochen, Pflicht-Volumen)
        kumo_ausbruch, kumo_level = check_kumo_breakout(data)

        # 5. Setup-Typ mit Pro-Check Filter
        # --- DEBUG-LOGGING ---
        # Dieser Print zeigt dir im Log genau, warum ein Setup abgelehnt wird
        print(f"DEBUG: {ticker} | Breakout: {ema_breakout} | InZone: {in_ema_zone} | "
              f"HL: {is_higher_low} | Stoch: {stoch_k:.1f} | TL-Ausbruch: {trendlinien_ausbruch} | Kumo-Ausbruch: {kumo_ausbruch}")

        # --- Filter-Logik ---
        # 1. Der Haupt-Filter (muss mit 'if' beginnen)
        if (ema_breakout or (in_ema_zone and is_higher_low) or trendlinien_ausbruch or kumo_ausbruch) and stoch_k < 90:
            
            # Setup-Typ: ALLE zutreffenden Pfade auflisten, nicht nur den ersten
            # Treffer (sonst geht z.B. ein gleichzeitiger Kumo-Ausbruch neben
            # einem Trendlinien-Ausbruch stillschweigend verloren)
            pfade = []
            if trendlinien_ausbruch:
                pfade.append("Trendlinien-Ausbruch")
            if kumo_ausbruch:
                pfade.append("Kumo-Ausbruch")
            if ema_breakout:
                pfade.append("EMA-Breakout")
            if in_ema_zone and is_higher_low:
                pfade.append("Pullback-Zone")
            basis_label = " + ".join(pfade)
            setup_typ = basis_label  # NEU: nur die Basis-Pfade, Pattern (Hammer/Engulfing) bleibt in eigener Spalte, wird NICHT mehr an Setup_Typ angehaengt
            
        # 2. Das 'else' MUSS genau unter dem 'if' stehen (gleiche Einrückung)
        else:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Haupt-Filter nicht erfüllt (Breakout={ema_breakout}, InZone={in_ema_zone}, HL={is_higher_low}, TL-Ausbruch={trendlinien_ausbruch}, Kumo-Ausbruch={kumo_ausbruch}, Stoch={stoch_k:.1f})")
            return None

        # --- Momentum-Zusatzkriterien: Relative Stärke & 52-Wochen-Hoch-Nähe ---
        # Klassische Momentum-Bausteine (u.a. CANSLIM): Eine Aktie sollte sich
        # stärker entwickeln als der breite Markt (Relative Stärke) und in der
        # Nähe ihres 52-Wochen-Hochs notieren statt nahe am Tief.

        # Relative Stärke vs. SPY (60-Tage-Performance im Vergleich zum Index)
        rel_staerke = None
        if spy_close is not None and len(spy_close) > 60 and len(data) > 60:
            stock_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
            spy_perf_60 = ((spy_close.iloc[-1] / spy_close.iloc[-60]) - 1) * 100
            rel_staerke = round(stock_perf_60 - spy_perf_60, 2)

            if rel_staerke <= -10:
                print(f"DEBUG-VERWORFEN: {ticker} | Grund: Relative Stärke vs. SPY <= -10% ({rel_staerke}%)")
                return None

        # 52-Wochen-Hoch-Nähe (geladene Daten decken ca. 1 Jahr ab)
        hoch_52w = data['High'].max()
        abstand_52w_hoch = round(((entry / hoch_52w) - 1) * 100, 2)

        if abstand_52w_hoch < -25:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Zu weit vom 52-Wochen-Hoch entfernt ({abstand_52w_hoch}%, Hoch={hoch_52w:.2f})")
            return None

        fib1, fib2 = get_fib_levels(data)
        # Kumo-Grenzen (NEU) als zusätzliche TP-Kandidaten - NaN-sicher, falls
        # die 26-Perioden-Verschiebung noch keinen gültigen Wert liefert
        kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
        potenzial_targets = sorted([data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1], data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte)
        targets_above = [t for t in potenzial_targets if t > entry]

        tp1 = targets_above[0] if targets_above else entry * 1.08
        tp2 = targets_above[1] if len(targets_above) >= 2 else tp1 * 1.05

        # --- Setup-spezifische Stop-/Ziel-Logik (Pullback vs. Breakout) ---
        # Ein Pullback-Setup (Kurs testet EMA20/50, Higher-Low bestätigt, kein
        # Breakout) hat eine andere charttechnische Erwartung als ein Breakout:
        # Das Ziel ist der letzte Swing-High vor dem Pullback (Rückkehr zum
        # vorherigen Hoch), der Stop liegt knapp unter dem jüngsten Swing-Low
        # statt einem starren 10-Tage-Tief, das bei einem Pullback-Entry oft
        # weit über das tatsächliche Setup-Risiko hinausschießt.
        # Breakout-Setups (ema_breakout=True) sind von dieser Anpassung nicht
        # betroffen und nutzen weiterhin die ursprüngliche Stop-/TP1-Logik.
        is_pullback_setup = (not ema_breakout) and in_ema_zone and is_higher_low

        if is_pullback_setup:
            # Engerer, setup-naher Stop: Tief der letzten 5 Kerzen
            swing_low_stop = data['Low'].iloc[-5:].min()
            if swing_low_stop < entry:
                stop = swing_low_stop

            # Letzter Swing-High vor dem aktuellen Pullback (Fenster -40 bis -3,
            # damit die jüngsten Pullback-Kerzen selbst nicht als Ziel zählen)
            vorlauf = data.iloc[-40:-3]
            if not vorlauf.empty:
                swing_high_target = vorlauf['High'].max()
                if swing_high_target > entry:
                    tp1 = swing_high_target
                    hoehere_ziele = [t for t in targets_above if t > tp1]
                    tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- Realitäts-Deckel: TP1 darf nicht über dem höchsten tatsächlich
        # erreichten Kurs der letzten 120 Handelstage liegen. Verhindert, dass
        # eine reine Fibonacci-Extension (mathematische Projektion, kein real
        # getestetes Niveau) als Ziel genutzt wird - das passiert z.B., wenn
        # der Kurs bereits über allen EMAs notiert und nur noch fib1/fib2 als
        # TP-Kandidat übrig bleibt. Greift für Breakout- UND Pullback-Setups.
        realer_deckel_120 = data['High'].iloc[-120:].max()
        if realer_deckel_120 > entry and tp1 > realer_deckel_120:
            tp1 = realer_deckel_120
            hoehere_ziele = [t for t in targets_above if t > tp1]
            tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- TP2-Realitäts-Deckel: großzügigeres 250-Tage-Fenster (statt 120
        # bei TP1), da TP2 bewusst ambitionierter sein darf - aber auch hier
        # keine reine Fib-Extension ohne jemals real erreichtes Kursniveau.
        realer_deckel_250 = data['High'].iloc[-250:].max()
        if realer_deckel_250 > entry and tp2 > realer_deckel_250:
            tp2 = realer_deckel_250
            if tp2 <= tp1:
                tp2 = tp1 * 1.05

        analysten_ziel = get_analyst_target(ticker)
        if analysten_ziel is None: analysten_ziel = 0.0
        
        target_value = analysten_ziel if analysten_ziel > 0 else tp1
        upside_potenzial = round(((target_value - entry) / entry) * 100, 2) if entry > 0 else 0.0

        risiko = entry - stop
        if risiko <= 0:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Risiko <= 0 (Entry={entry:.2f}, Stop={stop:.2f})")
            return None
        
        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
        chance2_perc = round(((tp2 - entry) / entry) * 100, 2)
        if crv1 < 1.0 or crv2 < 1.0:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: CRV zu niedrig (CRV1={crv1}, CRV2={crv2}, TP1={tp1:.2f}, TP2={tp2:.2f}, Entry={entry:.2f}, Risiko={risiko:.2f})")
            return None
        
        risk_perc = round(((entry - stop) / entry) * 100, 2)
        last_row = data.iloc[-1]

        # Plausibilitäts-Check
        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Plausibilitäts-Check fehlgeschlagen (EMA20={last_row['EMA20']:.2f} > 2x Close={last_row['Close']:.2f})")
            return None
        
        # --- Debug-Detektiv ---
        bedingung_erfuellt = (ema_breakout or (in_ema_zone and is_higher_low) or trendlinien_ausbruch or kumo_ausbruch) and stoch_k < 90
        
        # --- Universal-Debugger ---
        # Wir geben die Werte aus, bevor das IF überhaupt startet
        print(f"DEBUG-CHECK: {ticker} | Breakout: {ema_breakout} ({type(ema_breakout)}) | Zone: {in_ema_zone} ({type(in_ema_zone)}) | HL: {is_higher_low} ({type(is_higher_low)}) | TL-Ausbruch: {trendlinien_ausbruch} | Kumo-Ausbruch: {kumo_ausbruch} | Stoch: {stoch_k} ({type(stoch_k)})")

        # Sicherstellen, dass wir echte Booleans haben
        def to_bool(v):
            if isinstance(v, bool): return v
            return str(v).lower() == 'true'

        # Konvertierung
        is_breakout = to_bool(ema_breakout)
        in_zone = to_bool(in_ema_zone)
        is_hl = to_bool(is_higher_low)
        is_tl = to_bool(trendlinien_ausbruch)
        is_kumo = to_bool(kumo_ausbruch)
        stoch = float(stoch_k)

        # Die exakte Prüfung
        if (is_breakout or (in_zone and is_hl) or is_tl or is_kumo) and stoch < 90:
            
            # Setup-Typ: ALLE zutreffenden Pfade auflisten (konsistent zur
            # Hauptprüfung weiter oben)
            pfade = []
            if is_tl:
                pfade.append("Trendlinien-Ausbruch")
            if is_kumo:
                pfade.append("Kumo-Ausbruch")
            if is_breakout:
                pfade.append("EMA-Breakout")
            if in_zone and is_hl:
                pfade.append("Pullback-Zone")
            basis_label = " + ".join(pfade)
            setup_typ = basis_label  # NEU: nur die Basis-Pfade, Pattern (Hammer/Engulfing) bleibt in eigener Spalte, wird NICHT mehr an Setup_Typ angehaengt
            
            res = {
                "Ticker": str(ticker), "Name": str(firma_name), "Sektor": str(sektor),
                "Trend": str(trend_status), "Setup_Typ": str(setup_typ), "Pattern": str(pattern),
                "Golden_Cross_Status": get_golden_cross_status(data),
                "Tech-Kursziel": clean_num(tp1), "Analysten-Kursziel": float(analysten_ziel),
                "Upside-Potenzial%": float(upside_potenzial), "Status2": "VALIDE", 
                "Status_Grund": "Alles ok", "RSI": float(last_row['RSI']),
                "Divergenz": divergenz if divergenz else "Keine",
                "MACD_Trend": str(macd_trend), "CRV1": clean_num(crv1), 
                "CRV2": clean_num(crv2), "Kurs": round(last_row['Close'], 2),
                "Chance1_Perc": clean_num(chance1_perc), "Chance2_Perc": clean_num(chance2_perc),
                "Einstieg": round(last_row['Close'], 2), "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
                "Stop": clean_num(stop), "Risk_Perc": clean_num(risk_perc),
                "TP1": clean_num(tp1), "TP2": clean_num(tp2),
                "Stoch_K": stoch, "Vol_Ratio": clean_num(last_row['Vol_Ratio']), "Ideales_Delta": 0.0,
                "RS_vs_Benchmark%": clean_num(rel_staerke) if rel_staerke is not None else None,
                "Abstand_52W_Hoch%": clean_num(abstand_52w_hoch),
                "Markt": "US", "Waehrung": "USD"
            }
            return res
        
        return None

    except Exception as e:
        print(f"Fehler bei der Analyse von {ticker}: {e}")
        return None

def analyze_a_setup_eu(ticker, sektor, eu_bench_close=None):
    """EU-Variante von analyze_a_setup: identische Analyse-Logik (RSI, EMAs, MACD,
    Stochastik, Breakout/Pullback-Filter, Momentum-Kriterien, CRV, setup-spezifische
    Stop/TP-Logik), aber Kursdaten via yfinance statt Alpaca, da Alpaca DAX-Werte
    nicht abdeckt. Relative Stärke wird gegen den STOXX-Europe-600-ETF statt SPY
    berechnet, sonst laufen die Kriterien 1:1 identisch zur US-Funktion."""
    upside_potenzial = None
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        firma_name = info.get('longName', ticker)
        if not firma_name or firma_name == "":
            firma_name = ticker
    except:
        firma_name = ticker

    setup_typ = "Kein"
    pattern = "Kein"
    tp1 = 0

    try:
        # Kursdaten über yfinance laden (Alpaca deckt DAX-Werte nicht ab)
        data = yf.Ticker(ticker).history(period="1y")

        if data.empty:
            print(f"DEBUG: {ticker} -> Daten von yfinance leer.")
            return None

        # NaN-Platzhalterzeilen entfernen: yfinance legt vor Xetra-Handelsbeginn
        # teils schon eine leere Zeile für den aktuellen Tag an (NaN in Close/High/
        # Low/Volume). Ohne diese Bereinigung würde iloc[-1] auf diese Platzhalter-
        # Zeile zeigen statt auf den letzten echten Schlusskurs, was RSI, Stochastik,
        # EMAs etc. komplett auf NaN kippen lässt (siehe Log vom 2026-07-14).
        data = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])

        if data.empty:
            print(f"DEBUG: {ticker} -> Nach NaN-Bereinigung keine Daten mehr übrig.")
            return None

        # yfinance liefert bereits 'Close','High','Low','Open','Volume' - keine Umbenennung nötig
        if len(data) < 15:
            print(f"Zu wenig Daten für {ticker}: {len(data)} Zeilen")
            return None

        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.000001)
        data['RSI'] = 100 - (100 / (1 + rs))
        data['RSI'] = data['RSI'].fillna(50)
        divergenz = check_rsi_divergence(data)

        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        data['Vol_SMA20'] = data['Volume'].rolling(20).mean()

        # Ichimoku-Basiswerte (siehe US-Funktion für Begründung)
        data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
        data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
        data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)  # wie im Chart: 26 Perioden Vorlauf
        data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

        entry = data['Close'].iloc[-1]
        stop = data['Low'].rolling(10).min().iloc[-1]

        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
        data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()

        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]

        if 'RSI' not in data.columns:
            print(f"RSI-Berechnung fehlgeschlagen für {ticker}")
            return None

        data['Vol_Ratio'] = data['Volume'] / data['Vol_SMA20']
        data['Vol_Ratio'] = data['Vol_Ratio'].fillna(0)

        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        trend_status = "Unter WMA200/EMA200" if (data['Close'].iloc[-1] < data['WMA200'].iloc[-1] or data['Close'].iloc[-1] < data['EMA200'].iloc[-1]) else "OK"

        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        if lower_wick > (2 * body):
            pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        crossover_kuerzlich = any(
            data['EMA8'].iloc[-1 - i] <= data['EMA20'].iloc[-1 - i] for i in range(1, 4)
        )
        volumen_kuerzlich = any(
            data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i] for i in range(0, 3)
        )
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       crossover_kuerzlich and \
                       volumen_kuerzlich

        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        stoch_k = 100 * ((data['Close'].iloc[-1] - low_min.iloc[-1]) / (high_max.iloc[-1] - low_min.iloc[-1] + 1e-9))

        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        buffer = 0.01  # 1.0% Puffer für Zone (vorher 0.3%)

        # Richtungsabhängige Zone-Prüfung (NEU, siehe US-Funktion für Begründung)
        price = data['Close'].iloc[-1]

        def ema_pullback_test(ema_series):
            ema_heute = ema_series.iloc[-1]
            nah_dran = abs(price - ema_heute) < (price * buffer)
            war_ueber_ema_kuerzlich = any(
                data['Close'].iloc[-1 - i] >= ema_series.iloc[-1 - i] for i in range(0, 3)
            )
            return nah_dran and war_ueber_ema_kuerzlich

        in_ema_zone = any(ema_pullback_test(ema_series) for ema_series in [data['EMA20'], data['EMA50'], data['Kijun']])

        # Dritter, eigenständiger Setup-Typ: Ausbruch aus einer fallenden
        # Trendlinie (mind. 3 Berührungspunkte, 1% Toleranz, Pflicht-Volumen)
        trendlinien_ausbruch, tl_level = check_trendline_breakout(data)

        # Vierter, eigenständiger Setup-Typ: echter Kumo-Ausbruch (Ichimoku-
        # Wolke komplett von unten nach oben durchbrochen, Pflicht-Volumen)
        kumo_ausbruch, kumo_level = check_kumo_breakout(data)

        print(f"DEBUG-EU: {ticker} | Breakout: {ema_breakout} | InZone: {in_ema_zone} | "
              f"HL: {is_higher_low} | Stoch: {stoch_k:.1f} | TL-Ausbruch: {trendlinien_ausbruch} | Kumo-Ausbruch: {kumo_ausbruch}")

        if (ema_breakout or (in_ema_zone and is_higher_low) or trendlinien_ausbruch or kumo_ausbruch) and stoch_k < 90:
            pfade = []
            if trendlinien_ausbruch:
                pfade.append("Trendlinien-Ausbruch")
            if kumo_ausbruch:
                pfade.append("Kumo-Ausbruch")
            if ema_breakout:
                pfade.append("EMA-Breakout")
            if in_ema_zone and is_higher_low:
                pfade.append("Pullback-Zone")
            basis_label = " + ".join(pfade)
            setup_typ = basis_label  # NEU: nur die Basis-Pfade, Pattern (Hammer/Engulfing) bleibt in eigener Spalte, wird NICHT mehr an Setup_Typ angehaengt
        else:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Haupt-Filter nicht erfüllt (Breakout={ema_breakout}, InZone={in_ema_zone}, HL={is_higher_low}, TL-Ausbruch={trendlinien_ausbruch}, Kumo-Ausbruch={kumo_ausbruch}, Stoch={stoch_k:.1f})")
            return None

        # Relative Stärke vs. STOXX Europe 600 (statt SPY)
        rel_staerke = None
        if eu_bench_close is not None and len(eu_bench_close) > 60 and len(data) > 60:
            stock_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
            bench_perf_60 = ((eu_bench_close.iloc[-1] / eu_bench_close.iloc[-60]) - 1) * 100
            rel_staerke = round(stock_perf_60 - bench_perf_60, 2)

            if rel_staerke <= -10:
                print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Relative Stärke vs. STOXX600 <= -10% ({rel_staerke}%)")
                return None

        hoch_52w = data['High'].max()
        abstand_52w_hoch = round(((entry / hoch_52w) - 1) * 100, 2)

        if abstand_52w_hoch < -25:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Zu weit vom 52-Wochen-Hoch entfernt ({abstand_52w_hoch}%, Hoch={hoch_52w:.2f})")
            return None

        fib1, fib2 = get_fib_levels(data)
        # Kumo-Grenzen (NEU) als zusätzliche TP-Kandidaten - NaN-sicher, falls
        # die 26-Perioden-Verschiebung noch keinen gültigen Wert liefert
        kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
        potenzial_targets = sorted([data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1], data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte)
        targets_above = [t for t in potenzial_targets if t > entry]

        tp1 = targets_above[0] if targets_above else entry * 1.08
        tp2 = targets_above[1] if len(targets_above) >= 2 else tp1 * 1.05

        is_pullback_setup = (not ema_breakout) and in_ema_zone and is_higher_low

        if is_pullback_setup:
            swing_low_stop = data['Low'].iloc[-5:].min()
            if swing_low_stop < entry:
                stop = swing_low_stop

            vorlauf = data.iloc[-40:-3]
            if not vorlauf.empty:
                swing_high_target = vorlauf['High'].max()
                if swing_high_target > entry:
                    tp1 = swing_high_target
                    hoehere_ziele = [t for t in targets_above if t > tp1]
                    tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- Realitäts-Deckel: TP1 darf nicht über dem höchsten tatsächlich
        # erreichten Kurs der letzten 120 Handelstage liegen (siehe US-Funktion
        # für ausführliche Begründung).
        realer_deckel_120 = data['High'].iloc[-120:].max()
        if realer_deckel_120 > entry and tp1 > realer_deckel_120:
            tp1 = realer_deckel_120
            hoehere_ziele = [t for t in targets_above if t > tp1]
            tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- TP2-Realitäts-Deckel: großzügigeres 250-Tage-Fenster (siehe
        # US-Funktion für ausführliche Begründung).
        realer_deckel_250 = data['High'].iloc[-250:].max()
        if realer_deckel_250 > entry and tp2 > realer_deckel_250:
            tp2 = realer_deckel_250
            if tp2 <= tp1:
                tp2 = tp1 * 1.05

        # Kein Analysten-Kursziel für EU-Werte (get_analyst_target ist auf US-Info-Feld
        # ausgelegt; yf liefert targetMeanPrice aber grundsätzlich auch für DAX-Werte)
        analysten_ziel = get_analyst_target(ticker)
        if analysten_ziel is None: analysten_ziel = 0.0

        target_value = analysten_ziel if analysten_ziel > 0 else tp1
        upside_potenzial = round(((target_value - entry) / entry) * 100, 2) if entry > 0 else 0.0

        risiko = entry - stop
        if risiko <= 0:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Risiko <= 0 (Entry={entry:.2f}, Stop={stop:.2f})")
            return None

        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
        chance2_perc = round(((tp2 - entry) / entry) * 100, 2)
        if crv1 < 1.0 or crv2 < 1.0:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: CRV zu niedrig (CRV1={crv1}, CRV2={crv2}, TP1={tp1:.2f}, TP2={tp2:.2f}, Entry={entry:.2f}, Risiko={risiko:.2f})")
            return None

        risk_perc = round(((entry - stop) / entry) * 100, 2)
        last_row = data.iloc[-1]

        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Plausibilitäts-Check fehlgeschlagen (EMA20={last_row['EMA20']:.2f} > 2x Close={last_row['Close']:.2f})")
            return None

        res = {
            "Ticker": str(ticker), "Name": str(firma_name), "Sektor": str(sektor),
            "Trend": str(trend_status), "Setup_Typ": str(setup_typ), "Pattern": str(pattern),
                "Golden_Cross_Status": get_golden_cross_status(data),
            "Tech-Kursziel": clean_num(tp1), "Analysten-Kursziel": float(analysten_ziel),
            "Upside-Potenzial%": float(upside_potenzial), "Status2": "VALIDE",
            "Status_Grund": "Alles ok", "RSI": float(last_row['RSI']),
            "Divergenz": divergenz if divergenz else "Keine",
            "MACD_Trend": str(macd_trend), "CRV1": clean_num(crv1),
            "CRV2": clean_num(crv2), "Kurs": round(last_row['Close'], 2),
            "Chance1_Perc": clean_num(chance1_perc), "Chance2_Perc": clean_num(chance2_perc),
            "Einstieg": round(last_row['Close'], 2), "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
            "Stop": clean_num(stop), "Risk_Perc": clean_num(risk_perc),
            "TP1": clean_num(tp1), "TP2": clean_num(tp2),
            "Stoch_K": float(stoch_k), "Vol_Ratio": clean_num(last_row['Vol_Ratio']), "Ideales_Delta": 0.0,
            "RS_vs_Benchmark%": clean_num(rel_staerke) if rel_staerke is not None else None,
            "Abstand_52W_Hoch%": clean_num(abstand_52w_hoch),
            "Markt": "EU", "Waehrung": "EUR"
        }
        return res

    except Exception as e:
        print(f"Fehler bei der EU-Analyse von {ticker}: {e}")
        return None

if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # 1. Benchmarks sicher abrufen
    sp500_filter_text = get_sp500_data()
    qqq_text = get_qqq_quote()
    dax_text = get_index_benchmark_yf("^GDAXI", "DAX")
    eurostoxx_text = get_index_benchmark_yf("^STOXX50E", "EuroStoxx50")
    # Globale Risiko-Benchmarks (NEU): keine Setup-Quellen, dienen nur der
    # Marktumfeld-/Risikoeinschätzung im Briefing (u.a. für Gemini).
    # Russell 2000 = US-Small-Cap-Risikobereitschaft, Nikkei = größter
    # Nicht-US/EU-Markt (Frühindikator, öffnet vor Europa), Hang Seng =
    # China-Sentiment über frei handelbare Werte.
    russell_text = get_index_benchmark_yf("^RUT", "Russell 2000")
    nikkei_text = get_index_benchmark_yf("^N225", "Nikkei 225")
    hangseng_text = get_index_benchmark_yf("^HSI", "Hang Seng")
    # Rohstoff-Kontext (NEU): LIT-ETF als automatisierbarer Proxy fuer den
    # Lithium-Zyklus (echter Lithiumcarbonat-Spot aus China ist ueber
    # yfinance/Alpaca nicht verfuegbar). Nur Kontext fuer Lithium-Positionen,
    # keine Setup-Quelle, keine Abwertungsgrundlage.
    lithium_text = get_index_benchmark_yf("LIT", "Lithium-Proxy (LIT-ETF)")
    # Volatilitaets-/Angst-Index (NEU): der etablierteste Risk-On/Risk-Off-
    # Indikator. Hoher VIX (>20) = nervoeser Markt, Setups riskanter. Nur
    # Kontext fuer die Risikoeinschaetzung, keine Setup-/Abwertungsquelle.
    vix_text = get_index_benchmark_yf("^VIX", "VIX (Volatilitaet)")
    # Zins-Warner (NEU): 30J-US-Staatsanleihenrendite als weiterer reiner
    # Kontext-Indikator, analog zu VIX/Lithium-Proxy - keine Setup-Quelle.
    zins_text = get_zins_warner()
    # NEU (24.07.2026): erweiterter Makro-/Rohstoff-Kontext fuer ein
    # eigenstaendiges Morgen-Briefing (unabhaengig von der Sektor-Rotation-
    # Auswahl) - alle rein informativ, keine Setup-Quelle, keine
    # Abwertungsgrundlage. Oel/Gold/Silber/Kupfer als Futures-Kontrakte,
    # DXY als Dollar-Staerke-Indikator (treibt Rohstoffe invers + verzerrt
    # EU-Gewinne/-Kurse bei Waehrungsschwankungen), Bitcoin als zunehmend
    # verbreiteter Liquiditaets-/Risikoappetit-Gauge, 10J-Rendite als
    # Standard-Referenzzins (ergaenzend zur bestehenden 30J-Rendite oben).
    oel_text = get_index_benchmark_yf("CL=F", "Rohöl (WTI)")
    oel_brent_text = get_index_benchmark_yf("BZ=F", "Rohöl (Brent)")
    gold_text = get_index_benchmark_yf("GC=F", "Gold")
    silber_text = get_index_benchmark_yf("SI=F", "Silber")
    kupfer_text = get_index_benchmark_yf("HG=F", "Kupfer")
    dxy_text = get_index_benchmark_yf("DX-Y.NYB", "US-Dollar-Index")
    btc_text = get_index_benchmark_yf("BTC-USD", "Bitcoin")
    rendite10j_text = get_10j_rendite()
    
    # 2. Performance berechnen (US-Sektor-Rotation über Alpaca)
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)

    # 2a. EU-Sektor-Rotation separat berechnen (STOXX-Europe-600-Sektor-ETFs über yfinance,
    # da Alpaca diese nicht abdeckt). Läuft unabhängig von der US-Rotation.
    print("Berechne EU-Sektor-Rotation (STOXX Europe 600)...")
    df_perf_eu = pd.DataFrame([get_perf_yf(t, n) for t, n in eu_sektoren_etf.items()]).sort_values("Rotation-Score", ascending=False)

    # 2b. Benchmarks für die Relative-Stärke-Berechnung laden (einmalig, US + EU getrennt)
    spy_close = get_benchmark_close()
    eu_bench_close = get_eu_benchmark_close()
    
    # 3. Setups verarbeiten (PARALLEL)
    print("Starte Setup-Analyse...")
    blacklist = ["SPLK"] 
    
    # Aufgabenliste erstellen (Top 8 Sektoren, konsistent zum finalen Sektor-Filter unten)
    tasks = []
    for _, row in df_perf.head(8).iterrows():
        aktien_liste = sektoren_aktien.get(row['Ticker'], [])
        for s in aktien_liste:
            if s not in blacklist:
                tasks.append((s, row['Sektor']))

    # EU-Aufgabenliste erstellen (Top 5 von 7 EU-Sektoren, eigene, unabhängige Rotation)
    tasks_eu = []
    for _, row in df_perf_eu.head(5).iterrows():
        aktien_liste_eu = dax_aktien.get(row['Sektor'], [])
        for s in aktien_liste_eu:
            tasks_eu.append((s, row['Sektor']))

    # Rate-Limit-Budget: max. 180 Ticker insgesamt (US + EU) pro Lauf, da sowohl
    # yfinance (Analysten-Ziele + alle EU-Kursdaten) als auch die Alpaca-Anfragen
    # sonst zu viele Requests in kurzer Zeit auslösen könnten. Bei Überschreitung
    # werden zuerst EU-Tasks (kleineres Volumen, geringere Priorität) gekürzt.
    MAX_TICKER_BUDGET = 180
    gesamt_anzahl = len(tasks) + len(tasks_eu)
    if gesamt_anzahl > MAX_TICKER_BUDGET:
        ueberschuss = gesamt_anzahl - MAX_TICKER_BUDGET
        kuerzung_eu = min(ueberschuss, len(tasks_eu))
        if kuerzung_eu > 0:
            print(f"DEBUG: Ticker-Budget überschritten ({gesamt_anzahl} > {MAX_TICKER_BUDGET}) - kürze {kuerzung_eu} EU-Tasks.")
            tasks_eu = tasks_eu[:len(tasks_eu) - kuerzung_eu]
        rest_ueberschuss = (len(tasks) + len(tasks_eu)) - MAX_TICKER_BUDGET
        if rest_ueberschuss > 0:
            print(f"DEBUG: Budget weiterhin überschritten - kürze zusätzlich {rest_ueberschuss} US-Tasks.")
            tasks = tasks[:len(tasks) - rest_ueberschuss]

    print(f"DEBUG: Finale Task-Anzahl -> US: {len(tasks)} | EU: {len(tasks_eu)} | Gesamt: {len(tasks) + len(tasks_eu)}")
    
    # Parallel mit max_workers=10 ausführen (US)
    all_setups = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        # Führt analyze_a_setup für alle Tasks gleichzeitig aus
        results = list(executor.map(lambda p: analyze_a_setup(*p, spy_close=spy_close), tasks))

    # Parallel mit max_workers=10 ausführen (EU)
    results_eu = []
    if tasks_eu:
        with ThreadPoolExecutor(max_workers=10) as executor:
            results_eu = list(executor.map(lambda p: analyze_a_setup_eu(*p, eu_bench_close=eu_bench_close), tasks_eu))
        
    # Ergebnisse filtern (None-Werte entfernen) und US+EU zusammenführen
    all_setups = [r for r in results if r is not None] + [r for r in results_eu if r is not None]
    print(f"Analyse beendet. {len(all_setups)} Setups gefunden.")
    
    # Deine Liste/Spalten und Reihenfolge in setup-Datei (HIER EINGERÜCKT!)
    cols = ['Ticker', 'Name', 'Sektor', 'Markt', 'Waehrung', 'Trend', 'Setup_Typ', 'Pattern', 'Tech-Kursziel', 
            'Analysten-Kursziel', 'Upside-Potenzial%', 'Status2', 'Status_Grund', 
            'RSI', 'MACD_Trend', 'CRV1', 'CRV2', 'Chance1_Perc', 'Chance2_Perc', 'Kurs', 'Einstieg', 'Einstieg2(EMA 20)', 
            'Stop', 'Risk_Perc', 'TP1', 'TP2', 'Stoch_K', 'Vol_Ratio', 'Ideales_Delta',
            'RS_vs_Benchmark%', 'Abstand_52W_Hoch%', 'Golden_Cross_Status']

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
        
        # Hier die Sicherheitsprüfung einfügen:
        if 'Divergenz' not in df_s.columns:
            df_s['Divergenz'] = "Keine"

        # --- VORBEREITUNG FÜR DAS APPLY ---

        # Sicherstellen, dass alle benötigten Spalten existieren, falls der Fetch fehlgeschlagen ist
        for col in ['Divergenz', 'RSI', 'Pattern', 'Vol_Ratio', 'MACD_Trend', 'TP1']:
            if col not in df_s.columns:
                # Falls es eine Zahlenspalte ist: 0 setzen
                if col in ['RSI', 'Vol_Ratio', 'TP1']:
                    df_s[col] = 0
                # Falls es eine Textspalte ist: "Kein" oder "Neutral" setzen
                else:
                    df_s[col] = "Kein"

        # --- SICHERE VORBEREITUNG FÜR APPLY ---

        # Spalten, die sicher numerisch sein müssen
        numeric_cols = ['RSI', 'Vol_Ratio', 'Kurs', 'TP1', 'Stoch_K', 'RS_vs_Benchmark%', 'Abstand_52W_Hoch%']
        
        for col in numeric_cols:
            if col in df_s.columns:
                # Konvertiere in Zahl, Fehler werden zu NaN, diese füllen wir mit 0
                df_s[col] = pd.to_numeric(df_s[col], errors='coerce').fillna(0)
            else:
                # Falls die Spalte komplett fehlt, erstelle sie mit 0
                df_s[col] = 0

        # Spalten, die Text sein müssen
        text_cols = ['Pattern', 'MACD_Trend', 'Divergenz']
        for col in text_cols:
            if col in df_s.columns:
                df_s[col] = df_s[col].fillna("Kein").astype(str)
            else:
                df_s[col] = "Kein"

        # JETZT ist das DataFrame sauber und der Fehler beim Vergleich verschwindet
        df_s[['Status2', 'Status_Grund']] = df_s.apply(update_status_logic, axis=1)
    
    # 5. FILTERN (Erweitert um Trend-Check)
    if not df_s.empty:
        top_8_sektoren = df_perf.nlargest(8, 'Rotation-Score')['Sektor'].tolist()
        top_5_eu_sektoren = df_perf_eu.nlargest(5, 'Rotation-Score')['Sektor'].tolist()

        # DEBUG: Zeigt, an welchem der beiden Kriterien (Sektor oder Trend) die
        # gefundenen Setups vor dem Filter stehen. Marktbewusst, da US- und EU-
        # Sektornamen sich überschneiden können (z.B. "Technologie" in beiden).
        print(f"DEBUG: Top-8-US-Sektoren laut Rotation-Score: {top_8_sektoren}")
        print(f"DEBUG: Top-5-EU-Sektoren laut Rotation-Score: {top_5_eu_sektoren}")
        for tk, r in df_s[['Sektor', 'Markt', 'Trend']].iterrows():
            erlaubte_liste = top_8_sektoren if r['Markt'] == 'US' else top_5_eu_sektoren
            print(f"DEBUG: Setup vor Filter -> {tk} | Markt: {r['Markt']} | Sektor: {r['Sektor']} (in Top: {r['Sektor'] in erlaubte_liste}) | Trend: {r['Trend']}")

        # NEU: Nur Sektoren-Treffer UND nur Aktien, die im Aufwärtstrend (über WMA200) sind.
        # Marktbewusst: US-Setups gegen US-Top-8, DAX-Setups gegen EU-Top-5 (separate Rotationen).
        sektor_ok = (
            ((df_s['Markt'] == 'US') & (df_s['Sektor'].isin(top_8_sektoren))) |
            ((df_s['Markt'] == 'EU') & (df_s['Sektor'].isin(top_5_eu_sektoren)))
        )
        df_s = df_s[sektor_ok & (df_s['Trend'] == 'OK')].copy()
        
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
        'RSI', 'CRV1', 'CRV2', 'Chance1_Perc', 'Chance2_Perc', 'Kurs', 'Einstieg', 'Einstieg2(EMA 20)', 
        'Stop', 'Risk_Perc', 'TP1', 'TP2', 'Stoch_K', 'Vol_Ratio', 'RS_vs_Benchmark%', 'Abstand_52W_Hoch%'
    ]
    df_clean[cols_to_round] = df_clean[cols_to_round].round(2)

    # Fundamental-Ampel (NEU, 21.07.2026): nur für die finale, bereits
    # gefilterte Setup-Liste (klein, API-schonend) - separater Kommentar,
    # kein Modifikator in der Setup-Qualitäts-Matrix.
    if not df_clean.empty:
        ampel_ergebnisse = [
            berechne_fundamental_ampel(t, df_clean.loc[t, 'Sektor'], df_clean.loc[t, 'Markt'])
            for t in df_clean.index
        ]
        df_clean['Fundamental_Ampel'] = [a for a, _ in ampel_ergebnisse]
        df_clean['Fundamental_Hinweis'] = [h for _, h in ampel_ergebnisse]
    else:
        df_clean['Fundamental_Ampel'] = []
        df_clean['Fundamental_Hinweis'] = []
    
    # 4. Leere Spalte entfernen (falls nötig)
    if 'Ideales_Delta' in df_clean.columns:
        # Hier optional noch Delta auf 2 Stellen runden, falls es eine Fließkommazahl ist
        pass
    
    # 8. EXPORT
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_perf_eu.to_csv(f"Performance_EU({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # Hier exportierst du jetzt zwei Versionen (falls das so gewollt ist)
    df_clean.to_csv("setup_liste.csv", index=False)
    df_clean.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')

    relevante_setups = df_clean[df_clean['Status2'] != "GELAUFEN"]
    valide_setups = relevante_setups[relevante_setups['Status2'] == "VALIDE"]
    achtung_setups = relevante_setups[relevante_setups['Status2'] == "ACHTUNG"]
    
    with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n==============================\n\n")

        # Kurzüberblick über den zugrunde liegenden Trading-Ansatz
        f.write("STRATEGIE-ANSATZ\n")
        f.write("-"*50 + "\n")
        f.write("- Sektor-Rotation: Top-8-US-Sektoren (Alpaca) + separat Top-5-EU-Sektoren (STOXX 600, yfinance)\n")
        f.write("- Kandidaten: US-Sektoren (inkl. Themen-ETFs) + EU-Werte (DAX40/MDAX/Eurozonen-Large-Caps, EUR)\n")
        f.write("- Trend-Filter: Kurs muss über WMA200 und EMA200 liegen\n")
        f.write("- Setup: EMA8/20-Breakout ODER Pullback (Zone/Higher-Low) ODER Trendlinien-Ausbruch ODER Kumo-Ausbruch (Setup_Typ listet ALLE zutreffenden Pfade auf, z.B. \"Trendlinien-Ausbruch + Kumo-Ausbruch\")\n")
        f.write("- Pullback-Zone: Kurs nah an EMA20/50 UND in den letzten 3 Tagen mind. einmal auf/über der EMA (kein reiner Bruch nach unten)\n")
        f.write("- Trendlinien-Ausbruch: fallende Linie durch >= 3 Swing-Highs (120 Tage, 1% Toleranz), Pflicht-Volumen\n")
        f.write("- Momentum: Relative Stärke der Aktie > -10% vs. Benchmark (SPY bzw. STOXX600, 60 Tage)\n")
        f.write("- Momentum: Kurs max. 25% unter dem 52-Wochen-Hoch\n")
        f.write("- Risiko: CRV (Chance/Risiko) muss bei TP1 und TP2 jeweils >= 1.0 sein\n")
        f.write("- Stop: Pullback-Setups = Tief der letzten 5 Kerzen, sonst 10-Tage-Tief\n")
        f.write("- Ziel: Pullback-Setups = letzter Swing-High, sonst nächstes EMA/Fib-Level\n")
        f.write("- Realitäts-Deckel: TP1 <= reales 120-Tage-Hoch, TP2 <= reales 250-Tage-Hoch (keine reinen Fib-Extensions ohne Kursdeckung)\n")
        f.write("- Ticker-Budget: max. 180 Werte gesamt pro Lauf (Rate-Limit-Schutz)\n")
        f.write("- Positions-Tracking: manuell in Offene_Positionen.csv (Drive) bestätigte Trades, täglich gegen Stop geprüft\n")
        f.write("- Ichimoku, intern: Kumo-Grenzen (Senkou A/B) als zusätzliche TP-Kandidaten, Kijun-sen als zusätzliches Pullback-Level\n")
        f.write("- Kumo-Ausbruch: Kurs durchbricht komplette Wolke (über Senkou A UND B) innerhalb der letzten 3 Tage, Pflicht-Volumen\n\n")

        f.write(f"BENCHMARKS\n{sp500_filter_text}\n{qqq_text}\n{dax_text}\n{eurostoxx_text}\n{russell_text}\n{nikkei_text}\n{hangseng_text}\n{lithium_text}\n{vix_text}\n{zins_text}\n{rendite10j_text}\n{oel_text}\n{oel_brent_text}\n{gold_text}\n{silber_text}\n{kupfer_text}\n{dxy_text}\n{btc_text}\n\n")

        # 1. TOP-CHANCEN (VALIDE - PRO-CHECK AKTIV, US + EU gemeinsam nach Score sortiert)
        f.write("\n" + "="*50 + "\n")
        f.write("TRADE-ZUSAMMENFASSUNG (Valide Setups, US + EU)\n")
        f.write("="*50 + "\n")

        for ticker_val, row in valide_setups.iterrows():
            # Stochastik sicher auslesen (fallback auf 0.0 falls nicht vorhanden)
            stoch_val = row.get('Stoch_K', 0.0)
            waehrungszeichen = "€" if row.get('Waehrung') == 'EUR' else "$"
            markt_label = row.get('Markt', 'US')

            f.write(f"\n>>> {ticker_val} | {row['Name']} | Markt: {markt_label} <<<\n")
            f.write(f"Sektor: {row['Sektor']} | Status: {row['Status2']} | Grund: {row['Status_Grund']}\n")
            f.write(f"Pattern: {row['Pattern']} ({row['Setup_Typ']})\n")
            f.write("-" * 40 + "\n")
            f.write(f"Kurs: {row['Kurs']}{waehrungszeichen} / RSI: {row['RSI']} / Stoch-K: {stoch_val:.1f} / MACD: {row['MACD_Trend']}\n")
            f.write(f"Einstieg: {row['Einstieg']}{waehrungszeichen} / EMA20: {row['Einstieg2(EMA 20)']}{waehrungszeichen} / Stop: {row['Stop']}{waehrungszeichen} / Risiko: {row['Risk_Perc']}%\n")
            f.write(f"TP1: {row['TP1']}{waehrungszeichen} (Chance: {row['Chance1_Perc']}%) / CRV1: {row['CRV1']} | TP2: {row['TP2']}{waehrungszeichen} (Chance: {row['Chance2_Perc']}%) / CRV2: {row['CRV2']}\n")
            f.write(f"Vol-Ratio: {row['Vol_Ratio']}x | Ideales Delta: {row['Ideales_Delta']}\n")
            f.write(f"RelStärke vs Benchmark: {row.get('RS_vs_Benchmark%', 'n/a')}% | Abstand 52W-Hoch: {row.get('Abstand_52W_Hoch%', 'n/a')}%\n")
            f.write(f"Fundamental-Ampel: {row.get('Fundamental_Ampel', 'N/A')} ({row.get('Fundamental_Hinweis', '')})\n")
            f.write(f"Golden-/Death-Cross (nur Info, keine Bewertung): {row.get('Golden_Cross_Status', 'N/A')}\n")

            # Earnings-Warnung (Gap-Risiko) + jüngste Schlagzeilen (nur Kontext)
            earnings = get_earnings_warnung(ticker_val)
            if earnings:
                f.write(f"{earnings}\n")
            for headline in get_news_headlines(ticker_val):
                f.write(f"News {headline}\n")

            f.write(f"Suche: Hebelprodukt auf {ticker_val} (Fokus: BNP, Goldman, HSBC, UniCredit) | Ziel: {row['TP1']}{waehrungszeichen}\n")
            f.write("\n")

        # 2. WATCHLIST (ACHTUNG)
        f.write("\n" + "="*50 + "\n")
        f.write("WATCHLIST (ACHTUNG - Manuelle Prüfung erforderlich)\n")
        f.write("="*50 + "\n")

        for ticker_val, row in achtung_setups.iterrows():
            upside_val = row.get('Upside_%_vs_Aktuell')
            if upside_val is not None:
                upside_text = f"{upside_val:.2f}%"
            else:
                upside_text = "Kein Ziel"
            waehrungszeichen = "€" if row.get('Waehrung') == 'EUR' else "$"

            f.write(f"Ticker: {ticker_val} | Markt: {row.get('Markt', 'US')} | Grund: {row['Status_Grund']} | Kurs: {row['Kurs']}{waehrungszeichen}\n")
            f.write(f"Upside: Technisch {row['Tech-Kursziel']}{waehrungszeichen} / Potenzial: {upside_text}\n")
            f.write("-" * 30 + "\n")

        # 3. OFFENE POSITIONEN (manuell bestätigte, laufende Trades)
        # Wird von positionen_tracker.py als lokale Datei bereitgestellt (läuft
        # als eigener Workflow-Schritt vor analyse.py). Getrennt von den oben
        # gescannten NEUEN Setups - hier stehen nur Positionen, die der Nutzer
        # aktiv in Offene_Positionen.csv (Google Drive) bestätigt hat.
        f.write("\n" + "="*50 + "\n")
        f.write("OFFENE POSITIONEN (manuell bestätigt)\n")
        f.write("="*50 + "\n")

        positionen_datei = "Offene_Positionen.csv"
        # Eigenes Format für den Vergleich mit Ausstiegsdatum, das
        # positionen_tracker.py jetzt einheitlich als TT.MM.JJJJ schreibt -
        # 'today' (oben, %Y-%m-%d) bleibt für Dateinamen unverändert
        heute_de = datetime.datetime.now().strftime("%d.%m.%Y")
        if os.path.exists(positionen_datei):
            try:
                df_positionen = pd.read_csv(positionen_datei, sep=';', encoding='utf-8-sig')
            except Exception as e:
                df_positionen = pd.DataFrame()
                f.write(f"(Fehler beim Lesen von {positionen_datei}: {e})\n")

            offene = df_positionen[df_positionen['Status'].astype(str).str.strip().str.lower() == 'offen'] if not df_positionen.empty else df_positionen
            gestoppt_heute = df_positionen[
                (df_positionen['Status'].astype(str).str.strip().str.lower() == 'gestoppt') & (df_positionen['Ausstiegsdatum'].astype(str) == heute_de)
            ] if not df_positionen.empty else df_positionen

            if offene.empty and gestoppt_heute.empty:
                f.write("Keine offenen Positionen erfasst.\n")
            else:
                def fmt_de(wert):
                    """Formatiert einen Kurs-/Prozentwert einheitlich mit genau
                    2 Nachkommastellen und deutschem Komma (168.5 -> '168,50').
                    Nicht-numerische Werte (leer, 'n/a', NaN) werden zu 'n/a'."""
                    try:
                        zahl = float(str(wert).replace(',', '.'))
                        if pd.isna(zahl):
                            return "n/a"
                        return f"{zahl:.2f}".replace('.', ',')
                    except (ValueError, TypeError):
                        return wert if str(wert).strip() not in ("", "nan") else "n/a"

                for _, prow in offene.iterrows():
                    waehrungszeichen = {"EUR": "€", "GBP": "£"}.get(str(prow.get("Waehrung", "")).strip(), "$")
                    aktueller_kurs = fmt_de(prow.get('Aktueller_Kurs', "n/a"))
                    performance = fmt_de(prow.get('Performance_Seit_Einstieg%', "n/a"))
                    richtung = str(prow.get('Richtung', '')).strip() or 'Long'
                    f.write(f"\n>>> {prow['Ticker']} | {prow.get('Name', '')} | Markt: {prow.get('Markt', '')} | Richtung: {richtung} <<<\n")
                    f.write(f"Einstieg: {fmt_de(prow['Einstieg'])}{waehrungszeichen} ({prow.get('Einstiegsdatum', '')})\n")
                    f.write(f"Aktuell: {aktueller_kurs}{waehrungszeichen} / Performance: {performance}%\n")
                    f.write(f"Stop: {fmt_de(prow['Stop'])}{waehrungszeichen} / TP1: {fmt_de(prow['TP1'])}{waehrungszeichen} / TP2: {fmt_de(prow['TP2'])}{waehrungszeichen}\n")

                    # TP-Hinweis (NEU): nur ausgeben, wenn tatsächlich gesetzt
                    # (positionen_tracker.py setzt ihn nur einmalig beim ersten
                    # Erreichen von TP1/TP2, Position bleibt trotzdem offen)
                    tp_hinweis = str(prow.get('TP_Hinweis', '')).strip()
                    if tp_hinweis and tp_hinweis.lower() != 'nan':
                        f.write(f"⚠ Kursziel-Hinweis: {tp_hinweis} (Position weiterhin offen, keine automatische Schließung)\n")

                    # Optionsschein-Zusatzzeile: nur anzeigen, wenn Produkt_Typ
                    # tatsächlich als Optionsschein befüllt wurde
                    produkt_typ = str(prow.get('Produkt_Typ', '')).strip().lower()
                    if produkt_typ == 'optionsschein':
                        emittent = prow.get('Emittent', 'n/a')
                        hebel = prow.get('Hebel', 'n/a')
                        os_performance = fmt_de(prow.get('OS_Performance%', 'n/a'))
                        os_quelle = prow.get('OS_Quelle', 'n/a')
                        f.write(f"Optionsschein: {emittent} | Hebel: {hebel}x | OS-Performance: {os_performance}% (Quelle: {os_quelle})\n")

                    # Earnings-Warnung + Schlagzeilen auch für laufende Positionen
                    earnings = get_earnings_warnung(prow['Ticker'])
                    if earnings:
                        f.write(f"{earnings}\n")
                    for headline in get_news_headlines(prow['Ticker']):
                        f.write(f"News {headline}\n")

                if not gestoppt_heute.empty:
                    f.write("\n--- HEUTE GESTOPPT ---\n")
                    for _, prow in gestoppt_heute.iterrows():
                        waehrungszeichen = {"EUR": "€", "GBP": "£"}.get(str(prow.get("Waehrung", "")).strip(), "$")
                        f.write(f"{prow['Ticker']} -- Einstieg: {fmt_de(prow['Einstieg'])}{waehrungszeichen} / Ausstieg: {fmt_de(prow['Ausstiegskurs'])}{waehrungszeichen} (Stop erreicht)\n")
        else:
            f.write("(Positions-Tracker hat heute keine Datei bereitgestellt - Abschnitt übersprungen.)\n")

        # Gesamtes Aktien-Universum: alle Einzelaktien aus den Kandidatenlisten
        # (US-Sektorlisten + DAX-Liste), dedupliziert (manche Ticker stehen in
        # mehreren Sektorlisten), unabhängig davon, welche Sektoren heute in
        # der Rotation waren. ETFs/Benchmarks sind nicht enthalten.
        us_universum = len({t for liste in sektoren_aktien.values() for t in liste})
        eu_universum = len({t for liste in dax_aktien.values() for t in liste})
        f.write(f"\nScan-Statistik: Aktien-Universum {us_universum + eu_universum} Titel (US: {us_universum} / EU: {eu_universum}, ohne ETFs/Benchmarks), heute {len(tasks) + len(tasks_eu)} in den Top-Sektoren analysiert, davon {len(valide_setups)} valide Setups.\n")
