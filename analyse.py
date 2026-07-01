import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
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
def analyze_stock(ticker, sektor):
    time.sleep(0.1)
    try:
        hist = yf.download(ticker, period="300d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        if hist.empty or len(hist) < 200: return None
        
        c = hist['Close']
        akt = c.iloc[-1]
        
        # Hilfsfunktion für Ja/Nein
        def check(span): return "Ja" if akt > c.ewm(span=span, adjust=False).mean().iloc[-1] else "Nein"
        
        # WMA200 Berechnung
        weights = np.arange(1, 201)
        w200_val = c.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]
        
        return {
            "Ticker": ticker, "Sektor": sektor, "Kurs": round(akt, 2),
            "Über EMA20": check(20), "Über EMA50": check(50), 
            "Über EMA100": check(100), "Über EMA200": check(200),
            "Über WMA200": "Ja" if akt > w200_val else "Nein"
        }
    except: return None

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    perf_data = []
    
    # Durchlaufe alle Aktien für die Performance-Datei
    for ticker, name in sektoren_map.items():
        for s in sektoren_aktien.get(ticker, []):
            res = analyze_stock(s, name)
            if res: perf_data.append(res)
    
    df = pd.DataFrame(perf_data)
    
    # CSV Export
    df.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # Briefing
    with open(f"Briefing_{today}.txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n\n")
        f.write("PERFORMANCE-ÜBERSICHT (Alle Aktien):\n")
        f.write(df.to_string(index=False))

    print("Analyse erfolgreich abgeschlossen.")
