"""
benchmarks_snapshot.py

NEU (27.07.2026, Nutzerwunsch): schlanker Zusatzlauf zu main.yml - aktualisiert
NUR den BENCHMARKS-Block (Marktumfeld-/Risikolage-Kontext: Indizes, Zinskurve,
FOMC-Countdown, Rohstoffe, Dollar-Index, Bitcoin) zu zusaetzlichen Tageszeiten
(12/16/20 Uhr MESZ, siehe benchmarks_check.yml), OHNE den vollen Setup-Scan
(~180 Ticker ueber Alpaca/yfinance) oder einen Gemini-Aufruf - bleibt dadurch
guenstig (kein Gemini-Kontingent-Verbrauch, kein Alpaca-Sektor-Scan).

Sinnvoll v. a. um 20 Uhr MESZ: das ist exakt der Veroeffentlichungszeitpunkt
von FOMC-Zinsentscheidungen (14:00 Uhr US-Ostkuestenzeit) - faengt die
unmittelbare Marktreaktion (Zinskurve, VIX, Indizes) noch am selben Tag ein,
statt sie erst am naechsten Morgen im regulaeren main.yml-Lauf zu sehen.

Importiert die bestehenden Benchmark-Funktionen aus analyse.py (kein
duplizierter Code, identische Werte/Formatierung wie im Hauptlauf). Der
Import fuehrt zwar den kompletten Modul-Code von analyse.py aus (inkl.
Client-Initialisierung fuer Alpaca/Groq) - das ist aber reine Objekt-
erstellung ohne API-Call und deshalb unproblematisch, gleiches Vorgehen
wie bei trendwende_scanner.py/short_scanner.py/edelmetalle_scanner.py.

Ergebnis wird als eigene, zeitgestempelte Datei gespeichert (NICHT
Auswertung(<Datum>).txt ueberschrieben - das bleibt ausschliesslich der
volle main.yml-Lauf) und von upload_to_drive.py automatisch mit
hochgeladen (Dateiname enthaelt "Briefing", passt damit ins bestehende
.txt-Upload-Muster).
"""
import datetime

from analyse import get_index_benchmark_yf, get_zinskurve_fred, get_fomc_countdown, get_eurusd_wechselkurs


def erzeuge_snapshot():
    jetzt = datetime.datetime.now()

    sp500_text = get_index_benchmark_yf("^GSPC", "S&P 500")
    nasdaq_text = get_index_benchmark_yf("^IXIC", "Nasdaq")
    dax_text = get_index_benchmark_yf("^GDAXI", "DAX")
    eurostoxx_text = get_index_benchmark_yf("^STOXX50E", "EuroStoxx50")
    russell_text = get_index_benchmark_yf("^RUT", "Russell 2000")
    nikkei_text = get_index_benchmark_yf("^N225", "Nikkei 225")
    hangseng_text = get_index_benchmark_yf("^HSI", "Hang Seng")
    lithium_text = get_index_benchmark_yf("LIT", "Lithium-Proxy (LIT-ETF)")
    vix_text = get_index_benchmark_yf("^VIX", "VIX (Volatilitaet)")
    zins_text = get_zinskurve_fred()
    fomc_text = get_fomc_countdown()
    oel_text = get_index_benchmark_yf("CL=F", "Rohöl (WTI)")
    oel_brent_text = get_index_benchmark_yf("BZ=F", "Rohöl (Brent)")
    gold_text = get_index_benchmark_yf("GC=F", "Gold")
    silber_text = get_index_benchmark_yf("SI=F", "Silber")
    kupfer_text = get_index_benchmark_yf("HG=F", "Kupfer")
    dxy_text = get_index_benchmark_yf("DX-Y.NYB", "US-Dollar-Index")
    eurusd_text = get_eurusd_wechselkurs()
    btc_text = get_index_benchmark_yf("BTC-USD", "Bitcoin")

    zeitstempel_dateiname = jetzt.strftime("%Y-%m-%d_%H-%M")
    dateiname = f"Benchmarks_Briefing({zeitstempel_dateiname}).txt"

    with open(dateiname, "w", encoding="utf-8-sig") as f:
        f.write(f"BENCHMARKS-SNAPSHOT {jetzt.strftime('%d.%m.%Y %H:%M')} Uhr\n")
        f.write("(Zusatzlauf zwischen den regulären main.yml-Läufen - nur Marktumfeld-\n")
        f.write("Kontext, kein Setup-Scan, keine Gemini-Auswertung)\n")
        f.write("=" * 66 + "\n\n")
        f.write(
            f"{sp500_text}\n{nasdaq_text}\n{dax_text}\n{eurostoxx_text}\n{russell_text}\n"
            f"{nikkei_text}\n{hangseng_text}\n{lithium_text}\n{vix_text}\n{zins_text}\n"
            f"{fomc_text}\n{oel_text}\n{oel_brent_text}\n{gold_text}\n{silber_text}\n"
            f"{kupfer_text}\n{dxy_text}\n{eurusd_text}\n{btc_text}\n"
        )

    print(f"Gespeichert: {dateiname}")
    return dateiname


if __name__ == "__main__":
    print("Benchmarks-Snapshot gestartet...")
    ausgabe_pfad = erzeuge_snapshot()
    print(f"SNAPSHOT_DATEI={ausgabe_pfad}")
