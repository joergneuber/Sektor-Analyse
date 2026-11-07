# OHLCV-Qualitätspolitik: historischer INVALID-Bestand und aktuelles Trading

**Gültig ab:** Paket `HISTORICAL-INVALID-POLICY-1`  
**Ausgangsbefund:** Diagnose-Run `42378369876`, Stand 07.11.2026  
**Bekannter Bestand:** 112 Ticker / 5.953 fehlerhafte Tageskerzen

## Zweck

Das Quality Gate soll aktuelle Handelsentscheidungen und neue Positionen schützen. Es soll nicht dazu führen, dass ein Instrument wegen einzelner, weit zurückliegender fehlerhafter Tageskerzen dauerhaft gesperrt bleibt oder dass derselbe historische Befund in jedem Trading-Bericht erneut als akuter Fehler erscheint.

## Verbindliche Behandlung

1. Historische INVALID-Kerzen bleiben unverändert in der SQLite-Datenbank und in den Diagnoseexporten erhalten.
2. Historische INVALID-Befunde werden im Audit separat mit Ticker- und Kerzenanzahl ausgewiesen.
3. Ein ausschließlich historischer INVALID-Befund außerhalb des jüngsten 90-Kalendertage-Fensters setzt den aktuellen Symbolstatus nicht auf `INVALID` und blockiert den Ticker nicht pauschal.
4. INVALID-Kerzen innerhalb des jüngsten 90-Tage-Fensters bleiben aktuelle Qualitätsfehler und werden im Symbolstatus `INVALID` berücksichtigt.
5. Unabhängig vom Bestandsaudit prüft `ohlcv_quality_gate.py` das jeweilige aktuelle Berechnungsfenster anhand der erwarteten Handelssitzungen. Ungültige Beobachtungen werden maskiert; fehlende aktuelle Sessions, ungültige letzte Kerzen und erforderliche Strukturfenster bleiben nach den bestehenden Regeln gesperrt.
6. Fehlende, veraltete, nicht zuordenbare oder nicht ausreichend verifizierte Daten bleiben eigenständige Sperr-/Prüfgründe. Diese Politik hebt keine bestehenden Fail-Closed-Regeln auf.
7. Die 112 historischen Ticker werden nicht als dauerhaft gesperrte Instrumente geführt. Entscheidend ist die Qualität der konkreten Daten, die für die jeweilige Handelsentscheidung benötigt werden.
8. Keine automatische Reparatur, Löschung, Überschreibung oder Neuvalidierung des historischen Bestands ist Bestandteil dieser Politik.

## Audit-Felder

- `invalid_ohlcv_tickers`: alle Ticker mit mindestens einer strukturell ungültigen gespeicherten Kerze, unabhängig vom Alter.
- `recent_invalid_ohlcv_tickers`: Ticker mit mindestens einer INVALID-Kerze innerhalb der jüngsten 90 Kalendertage.
- `historical_only_invalid_ohlcv_tickers`: Ticker mit INVALID-Kerzen, aber ohne INVALID-Kerze im jüngsten 90-Tage-Fenster.
- `quality_status_by_ticker` und `quality_status_counts`: aktuelle Trading-Relevanz; historische-only INVALID-Befunde werden separat dokumentiert und zählen hier nicht als aktueller `INVALID`-Status.
- `invalid_ohlcv_candle_count`, `recent_invalid_ohlcv_candle_count`, `historical_only_invalid_ohlcv_candle_count`: Kerzenanzahlen, nicht Reason-String-Zählungen. Mehrfachverletzungen einer Kerze werden nur einmal gezählt.

## Berichtsausgabe

Der Trading-Radar-Bericht nennt den historischen Bestand kompakt als Anzahl betroffener Ticker und Kerzen. Die vollständigen Symbollisten und Einzeldaten bleiben in den Audit-/Diagnoseartefakten verfügbar. Dadurch wird der historische Befund nicht verschwiegen, aber nicht bei jedem Lauf erneut als akuter Handlungsbedarf dargestellt.

## Grenzen

Das 90-Tage-Fenster ist die Bestandsaudit-Klassifikation für die aktuelle Symbolübersicht. Es ersetzt nicht die kalender- und berechnungsfensterbezogene Prüfung des `ohlcv_quality_gate.py`. Ein aktueller Kandidat darf nur freigegeben werden, wenn seine tatsächlich verwendeten Daten und die übrigen Qualitätsbedingungen die bestehenden Gates bestehen.
