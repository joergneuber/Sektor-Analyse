import yfinance as yf
import pandas as pd
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
def get_perf(ticker, name):
    try:
        hist = yf.download(ticker, period="120d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist = hist['Close']
        if hist.empty: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}
        close = hist.iloc[:, 0] if isinstance(hist, pd.DataFrame) else hist
        last = close.iloc[-1]
        def p(d): return round(((last / close.iloc[-d]) - 1) * 100, 2)
        res = {"Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60)}
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res
    except: return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "Rotation-Score": 0}

def analyze_a_setup(ticker, sektor):
    time.sleep(0.1)
    try:
        hist = yf.download(ticker, period="250d", progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.get_level_values(0)
        if hist.empty or len(hist) < 200: return None
        atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
        entry = hist['High'].rolling(20).max().iloc[-1]
        stop = hist['Low'].rolling(20).min().iloc[-1]
        risiko = entry - stop
        return {
            "Ticker": ticker, "Name": yf.Ticker(ticker).info.get('longName', ticker), "Sektor": sektor,
            "Kurs": round(hist['Close'].iloc[-1], 2), "Einstieg": round(entry, 2), "Stop": round(stop, 2),
            "TP1": round(entry + atr, 2), "TP2": round(entry + (atr * 3), 2),
            "CRV1": round(((entry + atr) - entry) / risiko, 2) if risiko > 0 else 0,
            "CRV2": round(((entry + (atr * 3)) - entry) / risiko, 2) if risiko > 0 else 0
        }
    except: return None

# --- HAUPTTEIL ---
if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    
    all_setups = []
    for _, row in df_perf.head(2).iterrows():
        sector_results = [analyze_a_setup(s, row['Sektor']) for s in sektoren_aktien.get(row['Ticker'], [])]
        all_setups.extend(sorted([r for r in sector_results if r], key=lambda x: x['CRV1'], reverse=True)[:5])
    
    df_s = pd.DataFrame(all_setups)
    
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_s.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    with open(f"Briefing_{today}.txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n" + "="*30 + "\n\n")
        f.write("GESAMTMARKTFILTER\n")
        f.write("Trend SPY: Nicht bewertet (Daten nicht verfügbar)\n")
        f.write("Trend QQQ: Nicht bewertet (Daten nicht verfügbar)\n")
        f.write("Markteinschätzung: Aufgrund fehlender Kursdaten für EMA50/EMA200 erfolgt keine algorithmische Trendbewertung. Die Setup-Qualität
