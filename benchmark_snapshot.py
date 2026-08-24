"""
benchmarks_snapshot.py

NEU (27.07.2026, Nutzerwunsch): schlanker Zusatzlauf zu main.yml - aktualisiert
NUR den BENCHMARKS-Block (Marktumfeld-/Risikolage-Kontext: Indizes, Zinskurve,
FOMC-Countdown, Rohstoffe (WTI, Brent, Gold, Silber, Platin, Palladium, Kupfer), EUR/USD, Bitcoin) zu zusaetzlichen Tageszeiten
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

GEAENDERT (30.07.2026, Nutzerwunsch): Dow Jones und STOXX Europe 600
ergaenzt - beide wurden am 28.07.2026 in analyse.py als zusaetzliche
BENCHMARKS-Zeilen eingefuehrt (Dow als reine Info-Zeile, STOXX Europe 600
als Marktbreite-Input fuer den EU-Marktumfeld-Score), dieses Skript war
seither nicht mehr synchron - der 11:43-Uhr-Zwischenstand vom 30.07.
zeigte deshalb beide Zeilen nicht an, obwohl der volle main.yml-Lauf sie
laengst hatte. Reihenfolge und Label bewusst identisch zu analyse.py
gehalten, damit ein Vergleich zwischen Zwischenstand und Tageslauf nicht
durch unterschiedliche Anordnung erschwert wird.
"""
import datetime

from analyse import get_index_benchmark_yf, get_zinskurve_fred, get_fomc_countdown, get_eurusd_wechselkurs, _hole_kursdaten_gecached
from bitcoin_50w_sma import calculate_bitcoin_50w_sma


def erzeuge_snapshot():
    jetzt = datetime.datetime.now()

    sp500_text = get_index_benchmark_yf("^GSPC", "S&P 500")
    nasdaq_text = get_index_benchmark_yf("^IXIC", "Nasdaq")
    dow_text = get_index_benchmark_yf("^DJI", "Dow Jones")
    dax_text = get_index_benchmark_yf("^GDAXI", "DAX")
    eurostoxx_text = get_index_benchmark_yf("^STOXX50E", "EuroStoxx50")
    stoxx600_text = get_index_benchmark_yf("^STOXX", "STOXX Europe 600")
    russell_text = get_index_benchmark_yf("^RUT", "Russell 2000")
    nikkei_text = get_index_benchmark_yf("^N225", "Nikkei 225")
    hangseng_text = get_index_benchmark_yf("^HSI", "Hang Seng")
    lithium_text = get_index_benchmark_yf("LIT", "Lithium-Proxy (LIT-ETF)")
    vix_text = get_index_benchmark_yf("^VIX", "VIX (Volatilitaet)")
    zins_text = get_zinskurve_fred()
    fomc_text = get_fomc_countdown()
    oel_text = get_index_benchmark_yf("CL=F", "Rohöl (WTI)")
    oel_brent_text = get_index_benchmark_yf("BZ=F", "Rohöl (Brent)")
    gold_text = get_index_benchmark_yf("XAUUSD=X", "Gold")
    silber_text = get_index_benchmark_yf("XAGUSD=X", "Silber")
    platin_text = get_index_benchmark_yf("XPTUSD=X", "Platin")
    palladium_text = get_index_benchmark_yf("XPDUSD=X", "Palladium")
    kupfer_text = get_index_benchmark_yf("HG=F", "Kupfer")
    # US-Dollar-Index (ENTFERNT 29.07.2026, Nutzerentscheidung - analog zu
    # analyse.py): wird bewusst nicht mehr abgerufen/ausgewertet. EUR/USD
    # bleibt als einzige Waehrungs-Referenz.
    eurusd_text = get_eurusd_wechselkurs()
    btc_text = get_index_benchmark_yf("BTC-USD", "Bitcoin")
    btc_50w_sma_result = calculate_bitcoin_50w_sma(
        _hole_kursdaten_gecached("BTC-USD"), consume_cross=False
    )
    btc_50w_sma_text = btc_50w_sma_result.get(
        "message", "Bitcoin 50W-SMA: nicht verfuegbar"
    )

    zeitstempel_dateiname = jetzt.strftime("%Y-%m-%d_%H-%M")
    dateiname = f"Benchmarks_Briefing({zeitstempel_dateiname}).txt"

    with open(dateiname, "w", encoding="utf-8-sig") as f:
        f.write(f"BENCHMARKS-SNAPSHOT {jetzt.strftime('%d.%m.%Y %H:%M')} Uhr\n")
        f.write("(Zusatzlauf zwischen den regulären main.yml-Läufen - nur Marktumfeld-\n")
        f.write("Kontext, kein Setup-Scan, keine Gemini-Auswertung)\n")
        f.write("=" * 66 + "\n\n")
        f.write(
            f"{sp500_text}\n{nasdaq_text}\n{dow_text}\n{dax_text}\n{eurostoxx_text}\n"
            f"{stoxx600_text}\n{russell_text}\n{nikkei_text}\n{hangseng_text}\n"
            f"{lithium_text}\n{vix_text}\n{zins_text}\n{fomc_text}\n{oel_text}\n"
            f"{oel_brent_text}\n{gold_text}\n{silber_text}\n{platin_text}\n{palladium_text}\n{kupfer_text}\n"
            f"{eurusd_text}\n{btc_text}\n{btc_50w_sma_text}\n"
        )

    print(f"Gespeichert: {dateiname}")
    return dateiname


if __name__ == "__main__":
    print("Benchmarks-Snapshot gestartet...")
    ausgabe_pfad = erzeuge_snapshot()
    print(f"SNAPSHOT_DATEI={ausgabe_pfad}")
