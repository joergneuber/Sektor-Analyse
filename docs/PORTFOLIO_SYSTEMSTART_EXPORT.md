# Portfolio-Systemstart: historische Kursdaten exportieren

## Zweck

Der manuelle Workflow `Portfolio Systemstart – historische Kursdaten exportieren` stellt das verifizierte `historical-ohlcv`-Release-Bundle wieder her und exportiert die täglichen OHLCV-Datensätze der Positionen, die zwischen Systemstart und Bewertungsstichtag aktiv waren.

Standardzeitraum:

- Systemstart: `2026-08-07`
- Bewertungsstichtag: `2026-11-26`

Die Positionsdaten werden ausschließlich aus dem produktiven Google-Drive-Master `Offene Positionen+Check` geladen: Tab 1 `Offene Positionen+Check` und Tab 2 `Geschlossene Positionen`. Es gibt keinen CSV-Fallback.

## Ausführung

1. Die vier Dateien aus diesem Änderungspaket in die angegebenen Repository-Pfade übernehmen.
2. Die Änderungen selbst auf GitHub hochladen (der Assistent nimmt keine GitHub-Schreibaktionen vor).
3. In GitHub unter **Actions** den Workflow **Portfolio Systemstart – historische Kursdaten exportieren** auswählen.
4. `Run workflow` mit den Standarddaten `2026-08-07` und `2026-11-26` starten.
5. Nach Abschluss das Artefakt `portfolio-system-start-prices-<run_id>` herunterladen und hier bereitstellen.

Der Workflow benötigt das bereits im Projekt verwendete Repository-Secret `GDRIVE_TOKEN`. Es werden ausschließlich `contents: read`-Berechtigungen benötigt. Das Release wird nur gelesen; es erfolgt kein Release-Upload und keine Änderung an der Datenbank.

## Artefaktdateien

- `portfolio_positions.csv`: relevante offene und geschlossene Positionsdatensätze einschließlich OS-Feldern. EUNL bleibt enthalten, wenn es tatsächlich gehalten wird.
- `portfolio_historical_prices.csv`: alle in SQLite vorhandenen täglichen OHLCV-Bars für relevante Aktien/ETFs und EUNL, einschließlich `close`, `adj_close` und `bars.source`.
- `portfolio_instrument_coverage.csv`: Anzahl und Zeitraum der exportierten Bars pro Ticker.
- `portfolio_instruments_metadata.csv`: SQLite-Instrument-Metadaten.
- `portfolio_export_issues.json`: fehlerhafte oder widersprüchliche Positionszeilen.
- `portfolio_export_summary.json`: Umfang, Status und ausdrückliche Grenzen des Exports.

## Fachliche Schutzregeln

- Geschlossene Positionen vor dem 07.08.2026 werden ausgeschlossen.
- Positionen, die am 07.08. bereits offen waren, bleiben enthalten.
- Später eröffnete Positionen bleiben ab ihrem tatsächlichen Einstiegsdatum enthalten.
- Geschlossene Positionen nach dem Systemstart bleiben bis zum tatsächlichen Ausstieg enthalten.
- EUNL wird als echte Position genau einmal exportiert, wenn es tatsächlich gehalten wird. Ist es nicht gehalten, wird nur eine Benchmark-Marktpreisreihe angefordert; daraus entsteht keine synthetische Portfolio-Position.
- Optionsscheine werden anhand `Produkt_Typ` oder `OS_WKN` erkannt. Für sie werden keine Basiswert-OHLCV-Daten als Optionsscheinrendite verwendet. Ihre eigenen OS-Kurse bleiben in `portfolio_positions.csv`.
- Der Exporter berechnet ausdrücklich **keine Rendite** und führt ausdrücklich **keinen Quality Gate** aus. Er markiert daher, dass die Vollständigkeit erwarteter Handelstage noch nicht bewertet wurde. Fehlende Daten werden nicht ergänzt oder geschätzt.
- `Close` und `Adj Close` werden getrennt ausgegeben. Es werden keine Dividenden-, Währungs- oder sonstigen Anpassungsannahmen getroffen.

## Nachgelagerte Auswertung

Nach Bereitstellung des Artefakts werden die exakten Start-/Endkurse je Position gegen dieselben EUNL-Zeitfenster abgeglichen. Die vier manuell bestätigten OS-Startkurse und die OS-Depot-Screenshots werden separat mit den eigenen Optionsscheinpreisen verarbeitet. Fehlende oder unplausible Werte bleiben fail-closed und werden als Datenlücke ausgewiesen.
