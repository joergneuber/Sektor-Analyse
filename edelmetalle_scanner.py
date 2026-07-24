"""
edelmetalle_scanner.py

Separater Scanner fuer die vier klassischen Edelmetalle (Gold, Silber, Platin,
Palladium) - wendet die IDENTISCHEN Kriterien des Hauptscanners (analyse.py,
Trendfolge/Fortsetzung) auf diese vier Instrumente an, damit sie genauso wie
Aktien-Setups gehandelt werden koennen. Architektur-Entscheidung vom
24.07.2026 (siehe Chat-Verlauf):

  - Eigener, kleiner Scanner statt Integration in analyse.py: feste
    4er-Liste statt Sektor-Rotation, kein Risiko fuer das 180-Ticker-Budget
    des Hauptscanners, unabhaengig testbar/abschaltbar.
  - Kursdaten via yfinance-Futures (GC=F/SI=F/PL=F/PA=F), NICHT Alpaca (deckt
    keine Rohstoff-Futures ab) und NICHT ETFs (GLD/SLV/PPLT/PALL haben
    Tracking-Differenz zum echten Metallpreis und kuerzere/luecken-behaftete
    Historie fuer EMA200/WMA200) - Futures liefern die reinste, laengste und
    zeitnaheste Kursreihe (nahezu 23/5-Handel statt nur US-Boersenzeiten).
  - Alle Haupt-Kriterien 1:1 uebernommen (EMA-Breakout, Pullback-Zone,
    Trendlinien-Ausbruch, Kumo-Ausbruch, CRV>=1.0, Abstand-52W-Hoch<=-25%,
    Plausibilitaets-Check) - "alles was wichtig ist muss rein".
  - Relative Staerke (NEU): gegen einen Rohstoff-Index-ETF (DBC, Invesco DB
    Commodity Index Tracking Fund) statt SPY/STOXX600 - ein Aktien-Index als
    Vergleichsmassstab fuer Edelmetalle waere konzeptionell nicht sinnvoll,
    ein breiter Rohstoff-Index dagegen schon (bleibt derselbe -10%-Schwellwert
    wie beim Hauptscanner).
  - Fundamental-Ampel (KGV) entfaellt bewusst - Rohstoffe haben keine
    Unternehmensgewinne, eine KGV-Kennzahl ergibt hier keinen Sinn.
  - Analysten-Kursziel entfaellt bewusst (kein Analysten-Konsens fuer
    Rohstoff-Futures via yfinance verfuegbar) - Tech-Kursziel bleibt einzige
    Zielgroesse, wie bei den meisten EU-Setups ohne Kursziel auch.
  - Eigene Datei (Edelmetalle_Setups.csv) + eigener Briefing-Abschnitt
    (Edelmetalle_Briefing.txt) - wird von upload_to_drive.py automatisch mit
    hochgeladen (Dateiname-Muster "Setups"/"Briefing" bereits vorhanden,
    keine Anpassung an upload_to_drive.py noetig).

Voraussetzungen: dieselben Umgebungsvariablen wie analyse.py (ALPACA_KEY,
ALPACA_SECRET, GROQ_API_KEY - auch wenn dieses Skript selbst weder Alpaca
noch Groq direkt nutzt: der Import von analyse.py fuehrt dessen kompletten
Modul-Code aus, siehe main.yml-Kommentar bei trendwende_scanner.py fuer die
identische Begruendung). Muss im selben Verzeichnis wie analyse.py liegen.
"""

import datetime
import numpy as np
import pandas as pd
import yfinance as yf

# --- Bewaehrte Bausteine aus dem Hauptscanner wiederverwenden ---
from analyse import (
    check_rsi_divergence,
    check_trendline_breakout,
    check_kumo_breakout,
    get_fib_levels,
    get_golden_cross_status,
    clean_num,
)

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

# Feste 4er-Liste statt Sektor-Rotation - Ticker -> Anzeige-Name.
EDELMETALLE = {
    "GC=F": "Gold",
    "SI=F": "Silber",
    "PL=F": "Platin",
    "PA=F": "Palladium",
}

# Rohstoff-Index-ETF als Vergleichsmassstab fuer die Relative-Staerke-
# Berechnung (statt SPY/STOXX600, siehe Modul-Docstring).
COMMODITY_BENCHMARK_TICKER = "DBC"

RS_MIN = -10.0  # gleicher Schwellwert wie beim Hauptscanner
ABSTAND_52W_HOCH_MAX = -25.0  # gleicher Schwellwert wie beim Hauptscanner


def get_commodity_benchmark_close():
    """Laedt die rohen DBC-Schlusskurse (ca. 1 Jahr) fuer die Relative-
    Staerke-Berechnung der Edelmetalle gegenueber einem breiten Rohstoff-
    Index - analog zu get_benchmark_close()/get_eu_benchmark_close() in
    analyse.py, nur mit einem Rohstoff- statt einem Aktien-Index."""
    try:
        hist = yf.Ticker(COMMODITY_BENCHMARK_TICKER).history(period="1y")
        if hist.empty:
            print("DEBUG: Rohstoff-Benchmark (DBC) leer, Relative Stärke wird übersprungen.")
            return None
        hist = hist.dropna(subset=['Close'])
        if hist.empty:
            print("DEBUG: Rohstoff-Benchmark (DBC) nach NaN-Bereinigung leer, Relative Stärke wird übersprungen.")
            return None
        return hist['Close']
    except Exception as e:
        print(f"FEHLER beim Laden der Rohstoff-Benchmark (DBC): {e}")
        return None


def analyze_edelmetall(ticker, name, bench_close=None):
    """Analysiert ein einzelnes Edelmetall - identische Kriterien wie
    analyze_a_setup_eu() in analyse.py (yfinance-basiert, da Alpaca keine
    Rohstoff-Futures abdeckt), aber ohne Fundamental-Ampel (kein KGV bei
    Rohstoffen) und ohne Analysten-Kursziel (nicht verfuegbar fuer Futures).
    """
    try:
        data = yf.Ticker(ticker).history(period="2y")  # 2 Jahre statt 1 -
        # Futures haben teils luecken-behaftete Historie, mehr Puffer fuer
        # eine zuverlaessige WMA200/EMA200-Berechnung (siehe Modul-Docstring:
        # "viel Vergangenheit" war ausdruecklicher Wunsch).

        if data.empty:
            print(f"DEBUG-EDELMETALL: {ticker} -> Daten von yfinance leer.")
            return None

        data = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])
        if data.empty:
            print(f"DEBUG-EDELMETALL: {ticker} -> Nach NaN-Bereinigung keine Daten mehr übrig.")
            return None

        if len(data) < 210:  # WMA200 braucht mind. 200 Zeilen, etwas Puffer
            print(f"DEBUG-EDELMETALL: {ticker} -> Zu wenig Daten ({len(data)} Zeilen) für WMA200.")
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

        data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
        data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
        data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)
        data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

        entry = data['Close'].iloc[-1]
        stop = data['Low'].rolling(10).min().iloc[-1]

        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
        data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()

        data['Vol_Ratio'] = data['Volume'] / data['Vol_SMA20']
        data['Vol_Ratio'] = data['Vol_Ratio'].fillna(0)

        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        # Trend-Filter (Pflicht, wie beim Hauptscanner): Kurs muss ueber
        # WMA200 UND EMA200 liegen - hier direkt geprueft statt per
        # nachgelagertem DataFrame-Filter (keine Sektor-Stufe bei nur 4
        # festen Tickern).
        trend_ok = data['Close'].iloc[-1] >= data['WMA200'].iloc[-1] and data['Close'].iloc[-1] >= data['EMA200'].iloc[-1]
        if not trend_ok:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Trend nicht OK (unter WMA200/EMA200)")
            return None

        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        pattern = "Kein"
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
                       crossover_kuerzlich and volumen_kuerzlich

        stoch_k = data['Stoch_K'].iloc[-1]
        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        buffer = 0.01
        price = data['Close'].iloc[-1]

        def ema_pullback_test(ema_series):
            ema_heute = ema_series.iloc[-1]
            nah_dran = abs(price - ema_heute) < (price * buffer)
            war_ueber_ema_kuerzlich = any(
                data['Close'].iloc[-1 - i] >= ema_series.iloc[-1 - i] for i in range(0, 3)
            )
            return nah_dran and war_ueber_ema_kuerzlich

        in_ema_zone = any(ema_pullback_test(s) for s in [data['EMA20'], data['EMA50'], data['Kijun']])

        trendlinien_ausbruch, tl_level = check_trendline_breakout(data)
        kumo_ausbruch, kumo_level = check_kumo_breakout(data)

        print(f"DEBUG-EDELMETALL: {ticker} ({name}) | Breakout: {ema_breakout} | InZone: {in_ema_zone} | "
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
            setup_typ = " + ".join(pfade)
        else:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Haupt-Filter nicht erfüllt "
                  f"(Breakout={ema_breakout}, InZone={in_ema_zone}, HL={is_higher_low}, "
                  f"TL-Ausbruch={trendlinien_ausbruch}, Kumo-Ausbruch={kumo_ausbruch}, Stoch={stoch_k:.1f})")
            return None

        # Relative Staerke vs. Rohstoff-Index (DBC) statt SPY/STOXX600
        rel_staerke = None
        if bench_close is not None and len(bench_close) > 60 and len(data) > 60:
            metall_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
            bench_perf_60 = ((bench_close.iloc[-1] / bench_close.iloc[-60]) - 1) * 100
            rel_staerke = round(metall_perf_60 - bench_perf_60, 2)
            if rel_staerke <= RS_MIN:
                print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Relative Stärke vs. DBC <= {RS_MIN}% ({rel_staerke}%)")
                return None

        hoch_52w = data['High'].max()
        abstand_52w_hoch = round(((entry / hoch_52w) - 1) * 100, 2)
        if abstand_52w_hoch < ABSTAND_52W_HOCH_MAX:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Zu weit vom 52-Wochen-Hoch entfernt ({abstand_52w_hoch}%, Hoch={hoch_52w:.2f})")
            return None

        fib1, fib2 = get_fib_levels(data)
        kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
        potenzial_targets = sorted([data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1],
                                     data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte)
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

        # Realitaets-Deckel (wie beim Hauptscanner)
        realer_deckel_120 = data['High'].iloc[-120:].max()
        if realer_deckel_120 > entry and tp1 > realer_deckel_120:
            tp1 = realer_deckel_120
            hoehere_ziele = [t for t in targets_above if t > tp1]
            tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        realer_deckel_250 = data['High'].iloc[-250:].max()
        if realer_deckel_250 > entry and tp2 > realer_deckel_250:
            tp2 = realer_deckel_250
            if tp2 <= tp1:
                tp2 = tp1 * 1.05

        risiko = entry - stop
        if risiko <= 0:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Risiko <= 0 (Entry={entry:.2f}, Stop={stop:.2f})")
            return None

        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
        chance2_perc = round(((tp2 - entry) / entry) * 100, 2)
        if crv1 < 1.0 or crv2 < 1.0:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: CRV zu niedrig (CRV1={crv1}, CRV2={crv2}, TP1={tp1:.2f}, TP2={tp2:.2f}, Entry={entry:.2f}, Risiko={risiko:.2f})")
            return None

        risk_perc = round(((entry - stop) / entry) * 100, 2)
        last_row = data.iloc[-1]

        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Plausibilitätscheck fehlgeschlagen")
            return None

        return {
            "Ticker": str(ticker), "Name": str(name), "Sektor": "Edelmetalle",
            "Markt": "Global", "Waehrung": "USD", "Trend": "OK",
            "Setup_Typ": str(setup_typ), "Pattern": str(pattern),
            "Golden_Cross_Status": get_golden_cross_status(data),
            "Tech-Kursziel": clean_num(tp1), "Analysten-Kursziel": 0.0,
            "Upside_%_vs_Aktuell": clean_num(chance1_perc),
            "Status2": "VALIDE", "Status_Grund": "Alles ok",
            "RSI": float(last_row['RSI']), "Divergenz": divergenz if divergenz else "Keine",
            "MACD_Trend": str(macd_trend), "CRV1": clean_num(crv1), "CRV2": clean_num(crv2),
            "Kurs": round(last_row['Close'], 2),
            "Chance1_Perc": clean_num(chance1_perc), "Chance2_Perc": clean_num(chance2_perc),
            "Einstieg": round(last_row['Close'], 2), "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
            "Stop": clean_num(stop), "Risk_Perc": clean_num(risk_perc),
            "TP1": clean_num(tp1), "TP2": clean_num(tp2),
            "Stoch_K": float(stoch_k), "Vol_Ratio": clean_num(last_row['Vol_Ratio']),
            "Ideales_Delta": 0.0,
            "RS_vs_Benchmark%": clean_num(rel_staerke) if rel_staerke is not None else None,
            "Abstand_52W_Hoch%": clean_num(abstand_52w_hoch),
        }
    except Exception as e:
        print(f"FEHLER bei {ticker} ({name}): {e}")
        return None


def edelmetalle_scan_starten():
    print("Edelmetalle-Scanner gestartet...")
    bench_close = get_commodity_benchmark_close()

    ergebnisse = []
    for ticker, name in EDELMETALLE.items():
        res = analyze_edelmetall(ticker, name, bench_close)
        if res is not None:
            ergebnisse.append(res)

    print(f"DEBUG: {len(ergebnisse)} von {len(EDELMETALLE)} Edelmetallen mit validem Setup.")
    return ergebnisse


SPALTEN = [
    "Ticker", "Name", "Sektor", "Markt", "Waehrung", "Trend", "Setup_Typ", "Pattern",
    "Tech-Kursziel", "Analysten-Kursziel", "Upside_%_vs_Aktuell", "Status2", "Status_Grund",
    "RSI", "MACD_Trend", "CRV1", "CRV2", "Chance1_Perc", "Chance2_Perc", "Kurs", "Einstieg",
    "Einstieg2(EMA 20)", "Stop", "Risk_Perc", "TP1", "TP2", "Stoch_K", "Vol_Ratio",
    "Ideales_Delta", "RS_vs_Benchmark%", "Abstand_52W_Hoch%", "Divergenz", "Golden_Cross_Status",
]


def speichere_ergebnisse(ergebnisse):
    heute = datetime.date.today().isoformat()
    df = pd.DataFrame(ergebnisse, columns=SPALTEN)  # feste Spaltenliste, auch bei 0 Zeilen
    # bewusst sortiert: bestes CRV1 zuerst (analog zur "Sortierung nach
    # Rotation-Score"-Logik anderswo im Projekt - hier gibt es keine
    # Sektor-Rotation, daher CRV1 als naheliegendster Sortierschluessel)
    if not df.empty:
        df = df.sort_values("CRV1", ascending=False)

    dateiname_csv = f"Edelmetalle_Setups({heute}).csv"
    df.to_csv(dateiname_csv, index=False, sep=';', encoding='utf-8-sig')
    print(f"Gespeichert: {dateiname_csv}")

    dateiname_txt = f"Edelmetalle_Briefing({heute}).txt"
    with open(dateiname_txt, "w", encoding="utf-8-sig") as f:
        f.write(f"EDELMETALLE-SCAN {heute}\n")
        f.write("=" * 50 + "\n\n")
        f.write("STRATEGIE-ANSATZ (Edelmetalle, separat vom Hauptscanner)\n")
        f.write("-" * 50 + "\n")
        f.write(
            "- Grundidee: identische Kriterien wie der Hauptscanner (Trendfolge/\n"
            "  Fortsetzung), angewendet auf Gold/Silber/Platin/Palladium statt auf\n"
            "  Aktien - damit handelbar wie ein normales Setup.\n"
            "- Universum: feste 4er-Liste (keine Sektor-Rotation, immer alle 4 geprüft).\n"
            "- Kursbasis: Futures (GC=F/SI=F/PL=F/PA=F) - reinste, längste, zeitnaheste\n"
            "  Kursreihe (kein ETF-Tracking-Fehler, kein Alpaca, das Rohstoffe nicht abdeckt).\n"
            "- Trend-Filter: Kurs muss über WMA200 UND EMA200 liegen (wie Hauptscanner).\n"
            "- Setup: EMA8/20-Breakout ODER Pullback (Zone/Higher-Low) ODER Trendlinien-\n"
            "  Ausbruch ODER Kumo-Ausbruch (Setup_Typ listet ALLE zutreffenden Pfade auf).\n"
            "- Relative Stärke: gegen DBC (Rohstoff-Index-ETF) statt SPY/STOXX600 -\n"
            "  ein Aktienindex wäre als Vergleichsmaßstab für Edelmetalle nicht sinnvoll.\n"
            "- Risiko: CRV (Chance/Risiko) muss bei TP1 und TP2 jeweils >= 1.0 sein.\n"
            "- Fundamental-Ampel (KGV) entfällt bewusst - Rohstoffe haben keine\n"
            "  Unternehmensgewinne. Analysten-Kursziel entfällt ebenfalls (nicht\n"
            "  verfügbar für Futures) - Tech-Kursziel bleibt einzige Zielgröße.\n\n"
        )

        if not ergebnisse:
            f.write("Keine validen Edelmetall-Setups gefunden.\n")
        else:
            for r in sorted(ergebnisse, key=lambda x: x["CRV1"], reverse=True):
                f.write(f"{r['Ticker']} ({r['Name']}) | Sektor: {r['Sektor']} | Status: {r['Status2']} ({r['Status_Grund']})\n")
                f.write(f"Kurs: {r['Kurs']}$\n")
                f.write(f"Technisches Kursziel: {r['Tech-Kursziel']}$\n")
                f.write(f"Stop: {r['Stop']}$ | Risiko: {r['Risk_Perc']}%\n")
                f.write(f"TP1: {r['TP1']}$ (Chance: {r['Chance1_Perc']}%) | CRV1: {r['CRV1']}\n")
                f.write(f"TP2: {r['TP2']}$ (Chance: {r['Chance2_Perc']}%) | CRV2: {r['CRV2']}\n")
                f.write(f"RSI: {r['RSI']:.2f} | MACD-Trend: {r['MACD_Trend']} | Vol-Ratio: {r['Vol_Ratio']:.2f}x | Divergenz: {r['Divergenz']}\n")
                rs_txt = f"{r['RS_vs_Benchmark%']}%" if r['RS_vs_Benchmark%'] is not None else "n/a"
                f.write(f"RS vs. DBC (Rohstoff-Index): {rs_txt} | Abstand 52W-Hoch: {r['Abstand_52W_Hoch%']}%\n")
                f.write(f"Golden-/Death-Cross (nur Info, keine Bewertung): {r['Golden_Cross_Status']}\n")
                f.write(f"Setup-Typ: {r['Setup_Typ']} | Muster: {r['Pattern']}\n\n")

    print(f"Gespeichert: {dateiname_txt}")
    return dateiname_csv, dateiname_txt


if __name__ == "__main__":
    ergebnisse = edelmetalle_scan_starten()
    speichere_ergebnisse(ergebnisse)
    print("Edelmetalle-Scanner abgeschlossen.")
