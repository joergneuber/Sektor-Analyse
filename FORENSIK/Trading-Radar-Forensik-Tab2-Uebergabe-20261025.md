# Forensischer Reparaturbericht – Trading-Radar Tab-2-Übergabe

Datum: 25.10.2026

## 1. Reproduzierter Befund

Der Lauf vom 25.10.2026 protokolliert `quellzeilen=51`, `datumswerte_parsebar=51`, `übernommen=51` und listet 51 Ticker aus Tab 2. Trotzdem enthält der gespeicherte Bericht `TAB-2-QUELLE NICHT VERIFIZIERBAR: geschlossene Positionsquelle wurde im Lauf nicht geladen` und `TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL`.

## 2. Root Cause

In `_gemini_trading_radar_eigenstaendig()` wurde `lade_offenen_positionen_check_tab2()` aufgerufen und der Rückgabewert nur lokal in `geschlossene_positionen_fakten` gehalten. `speichere_ergebnis()` liest dagegen `_GEMINI_GESCHLOSSENE_10_5_FINAL`. Der eigenständige Pfad setzte diese globale Übergabevariable nicht. Der Speicherpfad fiel deshalb auf seinen „Quelle nicht geladen“-Platzhalter zurück, obwohl Tab 2 erfolgreich eingelesen worden war.

## 3. Korrektur

- Die globale Übergabevariable wird im eigenständigen Radar-Pfad explizit deklariert.
- Direkt nach dem Laden von Tab 2 wird der geladene Faktenblock in `_GEMINI_GESCHLOSSENE_10_5_FINAL` übernommen.
- Bei einem echten Ladefehler bleibt die konkrete Fehlermeldung im Faktenblock erhalten; es werden keine Quellenmarker erfunden.
- Ein Regressionstest stellt sicher, dass der eigenständige Pfad den Faktenblock vor dem Aufbau des Prompts an den Speicherpfad übergibt.

## 4. Prüfergebnisse

- `py_compile` für `Trading_Radar_Alleinauswertung.py`: bestanden.
- `python -m unittest discover -s tests -p 'test_trading_radar_data_contract.py' -v`: 19 Tests bestanden.
- Vollständiger lokaler Testsuchlauf: 193 Tests liefen, aber zwei Testmodule schlugen beim Import fehl, weil andere Tests das Arbeitsverzeichnis veränderten (`FileNotFoundError: discover` und `IsADirectoryError: tests`). Diese beiden Fehler sind keine nachgewiesenen Fehler der Tab-2-Korrektur; der vollständige Suite-Lauf ist damit dennoch nicht vollständig grün.
- Kein Live-Google-Sheets-Zugriff und kein neuer GitHub-Actions-Lauf in dieser Sitzung. Ein tatsächlich erzeugter FINAL-Bericht kann daher nicht vorab garantiert werden. Der nächste Actions-Lauf muss die Integration bestätigen.

## 5. Änderungsumfang

Nur diese Dateien wurden geändert:
1. `Trading_Radar_Alleinauswertung.py`
2. `tests/test_trading_radar_data_contract.py`

## 6. SHA-256

- `Trading_Radar_Alleinauswertung.py` — 899899 Bytes — SHA-256 `fb5409d326f016b2a2b5efa2f2ab3c2ce82d94152bfd8d7686f800dcddf35a0b`
- `tests/test_trading_radar_data_contract.py` — 26958 Bytes — SHA-256 `cff7e3b2c6aee0b9805bbbaedca5fdec0673fdb67619d88bcbba3f015f846fb1`

## 7. Sicherheitsgrenze

Die Korrektur behebt den nachgewiesenen Übergabefehler. Sie hebt keine fachlichen Report-Gates auf. Falls die echten 51 Datensätze ungültige Identitäten, doppelte Schlüssel oder andere Vertragsverletzungen enthalten, muss der Bericht weiterhin `NICHT_FINAL` bleiben.
