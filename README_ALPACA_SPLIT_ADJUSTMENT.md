# Alpaca-Aktienkurse: Split-adjustierte OHLCV-Daten

## Verbindliche Datenkonvention

Alle produktiven historischen Alpaca-Aktien-Barabrufe verwenden explizit
`adjustment=Adjustment.SPLIT`. Dadurch werden historische Preise und Volumina
auf die aktuelle Aktien-Stueckelung normalisiert. Die Einstellung ist bewusst
`SPLIT` und nicht `ALL`: Bardividenden und andere Kapitalmassnahmen sollen
nicht zusaetzlich in technische Kursreihen eingerechnet werden.

Die produktiven Abrufstellen liegen in `analyse.py`, `market_data.py`,
`short_scanner.py` und `positionen_tracker.py`. `market_data.py` ist der
geteilte US-Sammelabruf fuer weitere Scanner. Der SPY-Cache verwendet eine neue
Generation (`ohlcv-quality-v1.4-split`), damit alte RAW-Cachewerte nicht als
split-adjustierte Daten wiederverwendet werden.

## Bewusst getrennte Diagnosepfade

- `provider_coverage_us.py` bleibt explizit auf `Adjustment.RAW`, weil die
  bestehende Provider-Coverage-Diagnose die bisherige Rohdatenbasis und deren
  Abweichungen dokumentiert. Diese Diagnose ist kein produktiver Scannerpfad.
- `diagnose_us_split_adjustment.py` bleibt explizit auf `Adjustment.SPLIT`,
  weil sie die split-adjustierte SIP-Datenbasis gegen Yahoo vergleicht.
- Crypto-Bars, Yahoo-Kurse und die lokale `historical_ohlcv.sqlite` werden durch
  diese Aenderung nicht umgeschrieben.

## Wichtiger Hinweis fuer offene Positionen

Der Positions-Tracker verwendet split-adjustierte Alpaca-Historie fuer seine
Kurs-/OHLCV-Berechnungen. Die manuell gespeicherten Werte `Einstieg`, `Stop`,
`TP1` und `TP2` in `Offene_Positionen.csv` werden durch diese Aenderung jedoch
nicht automatisch angepasst. Wenn eine offene Position einen Vorwaerts- oder
Rueckwaertssplit ueberspannt, muessen diese Werte vor dem naechsten Trackerlauf
mechanisch auf die neue Aktien-Stueckelung umgerechnet werden. Das ist eine
Corporate-Action-Anpassung, keine Aenderung der Handelsregeln. Die Datei wird
von diesem Patch nicht veraendert.

Vor dem ersten produktiven Lauf ist daher zu pruefen, ob eine aktuell offene
US-Position seit ihrem Einstiegsdatum einen Split hatte. Ohne einen solchen
Split bleibt ihre gespeicherte Preisbasis unveraendert passend.

## SDK und Regression

Alle GitHub-Workflows, die `alpaca-py` installieren, pinnen dieselbe Version
`0.45.0`. `tests/test_alpaca_split_adjustment_contract.py` prueft die SDK-Enum-
und Request-Kompatibilitaet, alle produktiven `StockBarsRequest`-Stellen, die
absichtlichen RAW-/SPLIT-Diagnosekonventionen und die Cache-Generation.
