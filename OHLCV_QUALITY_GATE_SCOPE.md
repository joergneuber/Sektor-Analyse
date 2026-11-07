# OHLCV Quality Gate – Abgrenzung der Datenpfade

Stand: 07.11.2026
Quality-Gate-Regelversion: 1.3
Repository-Basis: `joergneuber/Sektor-Analyse`, `main`, Commit
`e0f182f39b875993acad5d36ddeb8fc6f3963644`.

Dieses Dokument grenzt Datenpfade ab, die im konsolidierten Kandidaten nicht
durch den zentralen vollständigen OHLCV-Vertrag geschützt werden. Eine
ähnliche Datenquelle oder ein vorhandener Close-Wert ist kein Nachweis für
eine zentrale OHLCV-Gate-Prüfung.

| Datei / Pfad | Datenvertrag | Abgrenzung |
|---|---|---|
| `trading_radar_market_first.py` | Eigener Markt-Snapshot und Close-basierte Relative-Stärke-Auswertung; zusätzliche lokale OHLCV-Validierung | Kein Aufruf des zentralen `ohlcv_quality_gate.py`-Vertrags. Der Close-basierte Snapshot ist nicht als vollständig zentral OHLCV-geprüft zu deklarieren. |
| `langfrist_scanner.py` – monatliche KGV-Näherung | Yahoo-Monatshistorie, ausschließlich `Close` geteilt durch EPS | Close-only-Kennzahl, keine vollständige OHLCV-Strukturprüfung. Die separaten täglichen Kurs-/Technikpfade sind davon zu unterscheiden und laufen über den gemeinsamen Markt-Cache. |
| `ohlcv_invalid_crosscheck.py` | Read-only Diagnose und unabhängiger Quellenvergleich einzelner OHLCV-Fälle | Diagnosepfad, kein Trading-Signalpfad und kein Ersatz für das zentrale Quality Gate. Ergebnisse dürfen nicht als Gate-Freigabe interpretiert werden. |

## Freigaberegel

- Die genannten Close-only-/Diagnosepfade werden nicht als vollständig durch
  das zentrale OHLCV-Gate abgesichert ausgewiesen.
- Falls diese Pfade künftig in Signal-, Struktur- oder Stop-Berechnungen
  einfließen, benötigen sie einen eigenen dokumentierten Qualitätsvertrag
  oder eine explizite Anbindung an das zentrale Gate mit passenden
  Datenanforderungen.
- Die Diagnose `ohlcv_invalid_crosscheck.py` darf Registry-Klassifikationen
  unterstützen, aber ungeklärte Fälle nicht automatisch freigeben.
- Keine dieser Abgrenzungen verändert die produktive SQLite oder startet
  einen Workflow.

## Ergänzende Festlegungen

- Für ICE Brent (`BZ=F`, Kalender `IFEU`) wird der volle Handelsschluss 23:00
  London-Zeit als konservativer Cutoff verwendet. Der Settlement-Zeitpunkt
  19:28 ist nicht mit dem Ende der täglichen Handelssitzung gleichzusetzen.
  Maßgeblich: https://www.ice.com/products/219/WTI-Light-Sweet-Crude-Oil-Futures
- Die WTI-Negativpreisregel ist ausschließlich auf NYMEX WTI May-2020 (`CLK20`),
  Yahoo-Frontmonat `CL=F` und die Sitzungen 20./21.04.2020 begrenzt.
  Die beiden historischen Registry-Einträge sind als `SPECIAL_CASE` mit
  `ALLOW_WITH_RULE` klassifiziert; erlaubt sind nur `LOW/CLOSE` am 20.04.
  sowie `OPEN/LOW` am 21.04. Zusätzlich müssen die negativen Werte mit den
  fest im Code hinterlegten historischen Feldwerten (Toleranz 0,005 USD/bbl)
  übereinstimmen. Nullwerte und jede Erweiterung auf andere Werte, Felder,
  Daten oder Instrumente bleiben gesperrt. Die historische Persistenzvalidierung
  verwendet dieselbe Instrument-/Datum-/Feld-/Wertprüfung; ohne Registry oder
  bei Gate-Fehlern werden negative Werte fail-closed verworfen.
- Die Freigabe betrifft nur die Datenqualitätsbehandlung der zwei historischen
  Kerzen. Sie ist keine pauschale Aussage, dass einfache Prozent-Renditen bei
  negativen Futurespreisen sinnvoll sind. Betroffene WTI-Performancefenster
  liefern deshalb `nicht verfügbar` statt einer irreführenden Prozentzahl.
