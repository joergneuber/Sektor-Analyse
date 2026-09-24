# Trendwende Research-Backtest

Dieser Research-Layer testet die bestehende Trendwende-Erkennung gegen strukturelle
Alternativen auf derselben historischen OHLCV-Datenbasis.

## Varianten

- `CURRENT`: signal-level Abbildung des aktuellen Trendwende-Kerns:
  WMA200-Vorlauf + 52W-Naehe + (RSI-Divergenz/Kumo oder Multi-Wochen-Hoch/Volumen)
- `CURRENT+...`: vollstaendige Kombinationen der Zusatzbedingungen
  Higher Low, Higher High, Break of Structure und Retest.
- `CURRENT+TL` und drei Kombinationen mit der produktiven Trendlinien-Methodik.

Insgesamt werden 20 Varianten gleichzeitig auf exakt denselben Stichtagen getestet.

## Kausalitaet / Look-ahead-Schutz

- Signalentscheidungen sehen nur Daten bis zum jeweiligen Stichtag.
- Swing-Pivots werden erst verwendet, wenn sie durch `pivot_order` Folgebars
  bestaetigt sind.
- Ein Retest darf nicht gleichzeitig mit dem heutigen BOS aus OHLC-Daten
  behauptet werden; Retests werden nur auf bereits abgeschlossenen Vortagen
  gesucht.
- Zukunftsdaten werden ausschliesslich zur Messung der Forward-Performance
  verwendet.

## Messung

Fuer jede Signalvariante werden pro Ereignis gespeichert:

- Signalzeitpunkt und Instrument
- aktueller Triggerpfad
- HL/HH/BOS/Retest/Trendlinie
- Abstand zum 52W-Tief
- Forward Return nach 1/3/5/10/20/40 Handelstagen
- Maximum Favorable Excursion (MFE)
- Maximum Adverse Excursion (MAE)

## Bewusst nicht enthalten

Die historische Fundamental-Ampel und der produktive CRV/TP-Filter werden im
primären Forschungs-Backtest nicht simuliert. Beide benoetigen Informationen,
die in einem historischen Preis-Signaltest sonst leicht zu einer nicht-kausalen
Rueckschau fuehren. Der Test beantwortet daher zuerst die Kernfrage:

> Liefert eine zusaetzliche Marktstruktur-/Trendlinienbestaetigung gegenueber der
> heutigen Trendwende-Erkennung messbar bessere historische Signale?

## Ausfuehrung

```bash
python -m pytest -q tests/test_trendwende_backtest.py
python trendwende_backtest.py --db historical_ohlcv.sqlite
```

Der manuelle GitHub-Workflow `.github/workflows/trendwende_backtest.yml` laedt
den persistenten historischen OHLCV-Research-Bestand und fuehrt den grossen
Variantenlauf aus. Die produktiven Trendwende-Dateien werden dabei nicht
veraendert.
