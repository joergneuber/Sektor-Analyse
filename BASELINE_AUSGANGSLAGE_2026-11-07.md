# Ausgangslage aus dem OHLCV-Diagnoselauf vom 07.11.2026

Quelle: GitHub Actions Run `42385848178`, Commit
`f84439bc39777c9fb2e394489929fb57e1760034`, Artifact
`historical-ohlcv-release-diagnostic-42385848178`.

## Universum

- Vollständiges OHLCV-Universum: 852 Symbole.
- US-Kalender XNYS/XNAS: 663 Symbole.
- Davon 658 Aktien/ETFs und 5 Index-Benchmarks mit `^`-Präfix.
- Die fünf Index-Benchmarks bleiben im Yahoo-/SQLite-Vergleich enthalten,
  werden aber nicht an Alpacas Stock-Historical-Data-Endpunkt geschickt.

## Offene historische Lücken

- 153 Ticker mit historischen Handelstagslücken im vollständigen Audit.
- 151 davon historisch-only; 2 mit Lücken im jüngeren 90-Tage-Fenster.
- Kein Ticker mit Kalender XNYS oder XNAS war in diesen 153 Audit-Lücken.
- Die 2 jüngeren Fälle waren `ACT0.DE` und `BZ=F`; damit keine US-Aktien.

## Fehlender letzter US-Handelstag

Die separate aktuelle Yahoo-Verfügbarkeitsprobe aus demselben Artifact fand
661 US-Symbole mit einer offenen SQLite-Lücke am 06.11.2026:

- XNYS: 659 Symbole.
- XNAS: 2 Symbole.
- Yahoo lieferte den fehlenden 06.11.2026-Bar in der aktuellen Probe für alle
  661 Symbole zurück.

Wichtige Zeitkontext-Prüfung: Der Hauptlauf `42258031547` führte das
SQLite-Update am 06.11.2026 um 17:15 UTC aus (12:15 US-Ostküstenzeit), also
vor dem regulären US-Börsenschluss um 21:00 UTC. Der fehlende 06.11.-Tagesbar
war zu diesem Zeitpunkt erwartbar und durfte vom Update nicht vorzeitig als
abgeschlossene Tageskerze übernommen werden. Die 661 Lücken sind deshalb
zunächst ein Zeitfenster-Befund des späteren Samstags-Audits, kein Nachweis
eines Yahoo-Ausfalls oder eines Alpaca-Ausfalls.

Die spätere Yahoo-Probe ist kein Nachweis, dass Yahoo den Bar beim früheren
Update-Lauf bereits geliefert hatte. Sie ist außerdem keine Alpaca-Prüfung.

## Offene Frage für den neuen Coverage-Lauf

Für die 658 US-Aktien/ETFs wird Alpaca mit derselben nicht explizit gesetzten
Feed-Konfiguration wie im Produktionscode abgefragt. Yahoo wird parallel für
die 663 US-Symbole abgefragt. So lässt sich feststellen, ob Alpaca die 06.11.2026
Lücke und die gesamte 365-Tage-Abdeckung liefern kann. Die historischen
US-Lücken werden zusätzlich aus dem Audit abgeleitet und, falls vorhanden,
gezielt gegen beide Provider abgefragt.
