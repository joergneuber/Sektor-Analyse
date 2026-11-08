# OHLCV Quality Gate – Abgrenzung der Datenpfade

Stand: 08.11.2026
Quality-Gate-Regelversion: 1.3
Repository-Basis der geprüften Fassung: `joergneuber/Sektor-Analyse`, `main`,
Commit `a27fc59c8e54701cfda8d6674cf9de7e6f648581`.

Dieses Dokument grenzt Datenpfade ab, die im konsolidierten Kandidaten nicht
durch den zentralen vollständigen OHLCV-Vertrag geschützt werden. Eine
ähnliche Datenquelle oder ein vorhandener Close-Wert ist kein Nachweis für
eine zentrale OHLCV-Gate-Prüfung.

| Datei / Pfad | Datenvertrag | Abgrenzung |
|---|---|---|
| `trading_radar_market_first.py` | Close-only Live-Markt-Snapshot und Relative-Stärke-Matrix mit gemeinsamem Stichtag | `append_live_market_snapshot` wird aus `Trading_Radar_Alleinauswertung.py` in den Berichtskontext eingebunden, nicht in die technische Einzelaktien-Entry-Berechnung. `classify_ohlcv_row` ist ein eigenständiger Per-Row-Helper und wird dort nicht aufgerufen. Der Snapshot lädt nur `Close`, entfernt fehlende Werte per `dropna()` und prüft keine erwarteten Börsensitzungen. Interne Kurslücken können daher die 21-/63-Beobachtungsfenster zeitlich komprimieren. Kein zentraler OHLCV-Vertrag und kein vollständiger Gap-Nachweis. |
| `makro_szenario.py::market_snapshots_parallel` | Shared-Market-Cache mit vorgelagerter zentraler OHLCV-Prüfung; nachgelagert Close-only-Makro-Snapshot | Der Snapshot entfernt maskierte Close-Werte anschließend per `dropna()` und sucht für Kalenderzeitfenster den letzten verfügbaren Schlusskurs vor dem Zielstichtag. Die Lücke bleibt im Snapshot nicht sichtbar; das tatsächliche Basisdatum kann vor dem Zielstichtag liegen. Kontextpfad, keine technische Einzelaktien-Entry-Berechnung. |
| `langfrist_scanner.py` – monatliche KGV-Näherung | Yahoo-Monatshistorie, ausschließlich `Close` geteilt durch EPS | Close-only-Kennzahl, keine vollständige OHLCV-Strukturprüfung. Die separaten täglichen Kurs-/Technikpfade sind davon zu unterscheiden und laufen über den gemeinsamen Markt-Cache. |
| `ohlcv_invalid_crosscheck.py` | Read-only Diagnose und unabhängiger Quellenvergleich einzelner OHLCV-Fälle | Diagnosepfad, kein Trading-Signalpfad und kein Ersatz für das zentrale Quality Gate. Ergebnisse dürfen nicht als Gate-Freigabe interpretiert werden. |

## Nachgewiesene Signalpfad-Regeln (Paket 2)

- `analyse.py` verwendet im US- und EU-Hauptscanner ein 200-Tage-WMA als
  Trendfilter. Die finale Kandidatenfilterung verlangt `Trend == "OK"`.
  Fehlt innerhalb des 200-Sitzungsfensters eine erwartete Schlusskursbeobachtung,
  bleibt WMA200 unvollständig; der Titel wird deshalb nicht als Trend-OK
  freigegeben. Das Instrument bleibt im Universum und wird nicht delistet.
- Der initiale 10-Sitzungs-Stop und der alternative 5-Sitzungs-Pullback-Stop
  müssen vollständig berechenbar sein. Eine Lücke in diesen Fenstern darf nicht
  durch `NaN`-Vergleiche oder ein `min()` über nur verbliebene Bars zu einem
  scheinbar gültigen Risiko/CRV führen. Die US-/EU-Hauptpfade sperren solche
  Setups jetzt fail-closed.

## Freigaberegel

- Die genannten Close-only-/Diagnosepfade werden nicht als vollständig durch
  das zentrale OHLCV-Gate abgesichert ausgewiesen. Insbesondere die Market-First-
  Relative-Stärke-Matrix ist nur nach gemeinsamem Endstichtag vergleichbar; ihre
  internen 21-/63-Beobachtungsfenster sind ohne Session-Achse nicht gap-sicher.
  Der Makro-Snapshot nutzt zwar das zentrale Gate vorgelagert, verliert aber
  beim nachgelagerten `dropna()` die sichtbare Lückenposition; sein Basisdatum
  ist deshalb als letzter verfügbarer Schlusskurs vor dem Kalenderziel zu lesen.
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
