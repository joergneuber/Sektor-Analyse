# Forensik – Trading Radar FINAL-Gate, Paket 1

Stand: 05.11.2026  
Repository: `joergneuber/Sektor-Analyse`  
Branch: `main`  
Ausgangs-Commit: `78c32bb55a56a17dbc144323cbd4a260a4f8149a`  
Arbeitsgrundlage: `Sektor-Analyse-main(20261105-192637).zip`  
Fachliche Referenz: Trading Radar Zielarchitektur v2.1 vom 27.10.2026

## 1. Umfang

Geändert wurden ausschließlich:
- `trading_radar_report_gate.py`
- `tests/test_trading_radar_report_gate.py`

Zusätzlich im ZIP enthalten:
- diese Forensik-Dokumentation
- vollständiger Unified Diff (`.diff`)

Nicht geändert wurden Produktionsworkflow, `Trading_Radar_Alleinauswertung.py`,
`trading_radar_neuauswertung.py`, `trading_radar_market_first.py`, Positionsdateien,
Scannerregeln, CRV-Berechnung oder Portfolio-Daten.

## 2. Byte-/Dateiebene

Die Ausgangsdateien aus der ZIP stimmen mit den von GitHub gelesenen Blob-SHAs
des angegebenen Commits überein. Der aktuelle Default-Branch `main` lieferte
für `trading_radar_report_gate.py` denselben Blob-SHA.

| Datei | Bytes vorher | Bytes nachher | Zeilen vorher | Zeilen nachher | Hinzugefügt | Entfernt |
|---|---:|---:|---:|---:|---:|---:|
| `trading_radar_report_gate.py` | 111354 | 118913 | 2176 | 2297 | 121 | 0 |
| `tests/test_trading_radar_report_gate.py` | 43352 | 48487 | 775 | 848 | 72 | 0 |

### SHA-256 und Git-Blob-SHA

**trading_radar_report_gate.py**
- ZIP-Original SHA-256: `bff5db6a6ec39a6d28d08b55b3f370e0b1f011063deda0d4653b5b0cd8b37658`
- Neue Datei SHA-256: `a8fac981b98ba3af96953c9dbbf6679fa66ab32e61b96fbfde99f8a7c989974c`
- GitHub-/ZIP-Ausgangs-Blob: `4bf25913c67adf7a7598c95abd2d85d99d680657`
- Neue Git-Blob-SHA (lokal berechnet): `2acd49c285fd7a07c3ffb426ce4015c0271f68cd`
- UTF-8 ohne BOM; LF-Zeilenenden; keine CRLF-Zeilen.

**tests/test_trading_radar_report_gate.py**
- ZIP-Original SHA-256: `63e86781e9057d12ad369bea6e805f6dd37e2a585365e660d7a93d6af2e90238`
- Neue Datei SHA-256: `304c6c738231398c29b0d2928b7d43610a7cced2d7dcf1271cafed865e6501a7`
- GitHub-/ZIP-Ausgangs-Blob: `f1af0623352b57fa96adab35dfc29a5a6271b849`
- Neue Git-Blob-SHA (lokal berechnet): `3cc4f5f28078fc0285865c6639da485cdf30658e`
- UTF-8 ohne BOM; LF-Zeilenenden; keine CRLF-Zeilen.

## 3. Zeilenweise und strukturelle Änderungen

Der vollständige zeilenweise Unified Diff liegt in
`FORENSIK/Trading-Radar-FINAL-Gate-Paket1-20261105.diff`.

Die Top-Level-AST-Deklarationen (Funktionen/Klassen) sind unverändert vorhanden:
keine Funktionen, Klassen oder Imports hinzugefügt oder entfernt. Die Änderungen
liegen innerhalb bestehender Validierungsfunktionen und der Regressionstests.

## 4. Semantische Änderungen

### M-01 – Portfolio-/Benchmarkaussagen
Wenn die kanonische Portfolio-Gesamtperformance `NICHT VERFÜGBAR` ist, erkennt
das Gate nun auch numerische Aussagen wie „Underperformance des Portfolios …
vs. MSCI …“ außerhalb des kanonischen Benchmarkabschnitts. Ein Trade-/
Signalvergleich kann so nicht mehr unbemerkt als Portfolio-Gesamtperformance
bezeichnet werden.

### M-06 – bestätigte Setups
Eine Scanner-Quellenangabe mit Status `valide` reicht nicht mehr als Beleg für
`BESTÄTIGTE SETUPS`. Jeder Eintrag muss Setup-ID, Quelle, Richtung, These,
technische Bestätigung, Datenstichtag, Einstieg, Stop, CRV, Gegenargumente,
Datenlücken und Regelstatus tragen. Richtung muss LONG/SHORT sein, der
Datenstichtag ISO-datiert, Einstieg und Stop numerisch/positiv und CRV numerisch
mit CRV1 >= 1. Mehrzeilige Datensätze werden unterstützt. Die Erkennung von
Kandidatenidentitäten umfasst `Name:`-Zeilen.

### M-09 – Szenarien
Eine begründete Datenlücke bleibt in einem `NICHT_FINAL`-Bericht sichtbar und
zulässig. Wenn ein Bericht `FINAL` beansprucht und eines der drei Pflichtszenarien
als `NICHT ABLEITBAR`, `NICHT VERFÜGBAR` oder vergleichbar ausweist, blockiert
das Gate die Freigabe.

### M-03 – OHLCV und Kandidaten-Mapping
Wenn die Statussummen nicht-VALID Symbole enthalten, kann der Anhang nicht
gleichzeitig pauschal Datenlückenfreiheit behaupten. Ebenso wird die Kombination
aus `Kandidaten-Mapping: NICHT VERIFIZIERBAR` und `Kandidaten nicht eingeschränkt`
als widersprüchlich zurückgewiesen.

## 5. Prüfung am Originalbericht vom 05.11.2026

Der unveränderte Bericht wurde erneut gegen das geänderte Gate geprüft.

Ergebnis: `REPORT_CHECK=FAIL`, Exit-Code `1`, **16
konkrete Gate-Befunde**:
- 1 M-01: numerische Portfolio-/Benchmarkaussage bei nicht verfügbarer Portfolioaggregation.
- 10 M-06: als bestätigt bezeichnete Kandidaten ohne erforderlichen technischen Nachweis.
- 3 M-09: Basis-, positives und negatives Szenario nicht ableitbar, Bericht beansprucht FINAL.
- 2 M-03: OHLCV-Freitext widerspricht 118 nicht-VALID Symbolen; Mapping nicht verifizierbar bei gleichzeitiger Aussage, Kandidaten seien nicht eingeschränkt.

Damit wird der problematische November-Bericht nicht mehr als FINAL akzeptiert.
Der Bericht selbst wurde nicht verändert.

## 6. Tests

Gezielte Testgruppe: `[33m[32m182 passed[0m, [33m[1m1 warning[0m, [32m79 subtests passed[0m[33m in 33.24s[0m[0m`  
Return-Code: `0`

Testmodule:
- `tests/test_trading_radar_report_gate.py`
- `tests/test_trading_radar_report_gate_regressions.py`
- `tests/test_trading_radar_market_first.py`
- `tests/test_trading_radar_market_first_markdown_normalization_regression.py`
- `tests/test_trading_radar_gate.py`
- `tests/test_trading_radar_production_market_first_integration.py`
- `tests/test_trading_radar_failure_chain_integration.py`
- `tests/test_trading_radar_report_formatting.py`
- `tests/test_trading_radar_run_evidence.py`

Zusätzlich:
- `python -m py_compile trading_radar_report_gate.py tests/test_trading_radar_report_gate.py` erfolgreich.
- `python -m compileall -q ...` erfolgreich.
- Originalbericht: Gate liefert 16 Befunde und Exit-Code 1.

### Vollständiger pytest-Sammellauf

`pytest -q` konnte nicht vollständig sammeln. Der bereits dokumentierte
Projektkonflikt tritt auf, weil `test_trading_radar_production_market_first_integration.py`
sowohl im Repository-Root als auch unter `tests/` liegt. Pytest meldet
`import file mismatch`. Keine dieser Dateien wurde im Paket geändert.

Ein nicht blockierender `.pytest_cache`-Berechtigungshinweis wurde ausgegeben.
Ein optionaler Spreadsheet-Runtime-Warmup-Fehler beim Python-Start beeinflusste
die erfolgreichen gezielten pytest-Läufe nicht. Ruff/Pyflakes waren in der
Umgebung nicht installiert.

## 7. Regressionen und Vollständigkeit

- Scanner- und CRV-Regeln außerhalb der Prüfung bestätigter Zusammenfassungen unverändert.
- Keine Portfolio-/Positionsdaten verändert.
- Keine Workflow- oder Produktionsverkabelung verändert.
- Keine Kurs-/Marktdatenberechnung verändert.
- Bestehende Tests für vollständige Szenarien, leere Kandidatengruppen,
  Benchmarkberechnung, OHLCV-Normalisierung und Run-Evidence liefen in der
  gezielten Testgruppe erfolgreich.

## 8. Abnahmegrenze

Dieses Paket repariert den nachgewiesenen Durchlass des FINAL-Gates für die
konkreten Fehlerklassen des November-Berichts. Es ist keine vollständige
End-to-End-Abnahme von Zielarchitektur v2.1. Die differenzierte
Handelstagslückenanalyse in `historical_ohlcv.py` und die vollständige
Quellenverifikation der Kandidaten bleiben separate Arbeitspakete.

GitHub wurde nicht verändert. Die Dateien sind lokal vorbereitet; der Nutzer
lädt sie selbst hoch.
