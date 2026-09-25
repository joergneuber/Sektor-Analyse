# Neuber Macro & Markets

Automatisierte Markt-, Sektor-, Makro-, Setup- und Positionsanalyse auf Basis von Python und GitHub Actions.

Das Projekt hat sich aus einer klassischen Sektor-/Aktienanalyse zu einer modularen Research- und Monitoring-Pipeline entwickelt. Es verbindet Marktdaten, technische Setups, Makrodaten, Wirtschaftstermine, Nachrichten-/Kontextquellen, strukturelle Trendanalyse, Positionsüberwachung, historische Daten und eine nachgelagerte Gemini-Auswertung.

> **Wichtig:** Das Projekt liefert Research- und Analyseinformationen. Es ist keine Anlageberatung und keine Garantie für zukünftige Kursentwicklungen.

---

## 1. Was das Projekt macht

Die zentrale Idee ist die Verbindung mehrerer Analyseebenen:

```text
Marktdaten / Makrodaten / Nachrichten / Termine
                    │
                    ▼
             Daten + Caches
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
    Trendfolge   Trendwende    Short
        │           │           │
        └───────────┼───────────┘
                    ▼
             Struktur / Trend
                    │
                    ▼
             Makro-Kontext
                    │
                    ▼
          Kandidaten / Positionen
                    │
                    ▼
             Gemini-Auswertung
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      Reporting            Fallback
   Drive / E-Mail          ChatGPT-Paket
```

Dabei werden **Berechnung und Interpretation bewusst getrennt**: Python erzeugt möglichst reproduzierbare Fakten, Kennzahlen und Quellinformationen; Gemini verarbeitet diese anschließend zu einem strukturierten Briefing. Für technische Ausfälle existiert ein separater Fallback-Mechanismus.

---

## 2. Analysebereiche

### 2.1 Hauptscanner – `analyse.py`

Der Hauptscanner analysiert die aktuelle Sektorrotation und sucht technische Setups in den jeweils relevanten US- und EU-Sektoren.

Zum technischen Prüfbereich gehören unter anderem:

- Trend- und gleitende Durchschnitte
- RSI
- Ichimoku/Kumo und Kijun
- Fibonacci-Level
- Trendlinien und Breakouts
- Pullback-/Retest-Logik
- Volumenbestätigung
- Relative Stärke gegenüber Benchmarks
- CRV bzw. technische Ziel-/Stop-Zonen
- Earnings-/News-Kontext
- fundamentale Zusatzinformationen, soweit verfügbar

Der Scanner arbeitet als mehrstufiger Funnel: Kandidaten werden nicht allein aufgrund eines einzelnen Indikators übernommen.

### 2.2 Trendwende – `trendwende_scanner.py`

Die Trendwende ist als **eigenständige Strategie** vom normalen Trendfolge-Scanner getrennt.

Der Scanner betrachtet das breitere US-/EU-Universum und sucht unter anderem nach:

- Nähe zu längerfristigen Tiefs
- RSI-Divergenzen
- Kumo-/Kijun-Ausbrüchen
- Mehrwochen-Ausbrüchen
- Volumenbestätigung
- strukturellen Veränderungen
- CRV- und Zielzonen
- nahezu erfüllten Kandidaten

Damit wird nicht einfach ein Long-Signal „umgedreht“, sondern eine andere Marktsituation untersucht: die mögliche Beendigung eines Abwärtstrends und der Beginn einer neuen Aufwärtsstruktur.

### 2.3 Short – `short_scanner.py`

Der Short-Scanner bildet eine eigenständige Gegenlogik für schwache Sektoren und Titel.

Dazu gehören beispielsweise:

- schwache/bottom Sektoren
- inverse Relative Stärke
- EMA-Breakdowns
- Short-Pullbacks
- Trendlinien-Breakdowns
- Kumo-Breakdowns
- Stop- und Zielberechnung für Short-Setups

Gemeinsame Trendlinienfunktionen werden über `trendline_utils.py` zentral genutzt, anstatt Long- und Short-Code unnötig zu duplizieren.

### 2.4 Edelmetalle – `edelmetalle_scanner.py`

Gold, Silber, Platin und Palladium werden als eigener Asset-Bereich behandelt.

Der Edelmetall-Scanner bündelt drei Strategien:

- Trendfolge
- Trendwende
- Short

Damit wird die Logik der Aktienanalyse wiederverwendet, ohne Edelmetalle künstlich als normale Aktien zu behandeln.

### 2.5 Langfrist-Bewertung – `langfrist_scanner.py`

Neben den kurzfristigen Setups existiert eine separate längerfristige Analyseebene. Sie wird über einen eigenen wöchentlichen Workflow ausgeführt und ist damit vom täglichen Setup-Scanning getrennt.

---

## 3. Makro-Engine – `makro_szenario.py`

Die Makro-Engine ist inzwischen ein eigenständiger Bestandteil des Systems.

Sie verarbeitet unter anderem Daten aus:

- FRED
- BLS
- ADP
- Fed/FOMC
- ECB
- Treasury-/Zinsdaten
- ISM
- BEA
- LME bzw. Rohstoffquellen
- weiteren öffentlichen Daten-/Fallback-Quellen

Je nach Datenreihe kommen unterschiedliche Abruf- und Fallback-Mechanismen zum Einsatz. Ein mehrstufiger Cache reduziert unnötige Abrufe und hilft dabei, externe API-Ausfälle zu überbrücken.

### ADP

Die US-ADP-Beschäftigungsänderung ist als eigener Bestandteil der Makro-Engine integriert. Sie besitzt eine eigene Cache-/Abruflogik und wird nicht lediglich als allgemeiner FRED-Wert ohne separate Behandlung eingebunden.

### Makro-Fokus

Die Makro-Architektur ist darauf ausgelegt, einen relevanten Tagesfokus mit den dazugehörigen Daten und Nachrichten zu verbinden:

```text
Wirtschaftstermin / Ereignis
          ↓
       MACRO_FOCUS
          ↓
   relevante Makrodaten
          ↓
       Nachrichten
          ↓
   Fed-/EZB-Kontext
          ↓
     Marktreaktion
          ↓
  technische Bestätigung
```

Das Ziel ist nicht die isolierte Betrachtung einer einzelnen Zahl, sondern die Einordnung eines relevanten Makro-Ereignisses in den Gesamtmarkt.

---

## 4. Nachrichten und externe Kontextquellen

Das Projekt kann mehrere Nachrichten- und Kontextquellen kombinieren, darunter insbesondere:

- GDELT/GKG
- Deutsche-Welle-RSS
- ForexFactory-Kontext und Event-Mapping
- YouTube-Marktquellen
- weitere projektbezogene öffentliche Quellen

Für GDELT existieren eigene Runner- und Hardening-Tests. HTTP-Fehler wie Rate Limits werden nicht als normaler Erfolgsfall behandelt.

Das System versucht damit, **harte Makrodaten** von **Nachrichten-/Kontextinformationen** zu unterscheiden.

---

## 5. Struktur- und Trendanalyse – `struktur_trends.py`

`struktur_trends.py` liefert einen eigenständigen strukturellen Blick auf den Markt.

Der Baustein untersucht unter anderem:

- Unterstützungen und Widerstände
- Pivot-/Strukturinformationen
- Fibonacci-Erweiterungen
- Trendkanäle
- Formationen
- Überdehnung
- Relative Stärke auf Sektorebene
- technische Zustände und Zielzonen

Der Struktur-Trend-Baustein ist bewusst vom Hauptscanner getrennt. Dadurch kann Gemini später technische Setup-Daten und strukturellen Markt-Kontext getrennt betrachten.

Wenn der strukturelle Lauf im Hauptworkflow fehlschlägt, wird ein möglicherweise unvollständiges Struktur-Briefing nicht einfach weitergereicht.

---

## 6. Offene Positionen und Positionsmanagement

### `positionen_tracker.py`

Der Positions-Tracker aktualisiert laufende Positionen, Kurse und Statusinformationen. Er übernimmt außerdem Aufgaben rund um geschlossene Positionen und die Synchronisierung mit den vorgesehenen Ablagen.

### `offene_positionen_check.py`

Die offene Positionsanalyse betrachtet unter anderem:

- aktuelle Kursentwicklung
- technische Situation
- Stop-/Take-Profit-Zonen
- Unterstützungen und Widerstände
- Swing-Strukturen
- laufenden Positionsstatus

### Automatische Alerts

Der Workflow `stop_check.yml` führt an Börsentagen regelmäßig Positionsprüfungen aus und kann bei relevanten Ereignissen Alerts auslösen.

---

## 7. Einzel-Checks und Beobachtungsliste

Mit `einzel_check.py` können einzelne Ticker außerhalb des normalen Universums untersucht werden.

Eine Beobachtungsliste kann anschließend weitergeführt und regelmäßig erneut analysiert werden.

Damit existiert neben dem automatischen Scanner auch ein manueller Research-Pfad:

```text
Ticker auswählen
      ↓
Einzel-Check
      ↓
interessant?
      ↓
Beobachtungsliste
      ↓
regelmäßige Nachanalyse
```

---

## 8. Historische Daten und Backtesting

### Historische OHLCV-Daten – `historical_ohlcv.py`

Das Projekt besitzt inzwischen eine persistente historische OHLCV-Datenbasis.

Der Hauptworkflow kann die historische Datenbank aktualisieren und anschließend deren Coverage prüfen. Dadurch entsteht eine stabilere Grundlage für Forschung und Backtests als ausschließlich durch den jeweiligen Live-Datenabruf.

### Trendwende-Backtest – `trendwende_backtest.py`

Die Trendwende-Logik kann auf historische Daten angewendet werden.

Der Backtest berücksichtigt unter anderem:

- historische Signale
- bestätigte Pivot-Punkte
- verschiedene Varianten der Trendwende-Regeln
- Forward-Horizonte
- Auswertung der nachfolgenden Kursentwicklung

Das Projekt ist damit bereits auf dem Weg von einer reinen Live-Analyse zu einer reproduzierbaren Research-Komponente.

> Der vorhandene Backtest ist noch kein vollständiger institutioneller Backtesting-Stack. Insbesondere realistische Transaktionskosten, Slippage, Survivorship Bias, Delistings und weitere Ausführungsannahmen müssen bei einer weitergehenden Strategievalidierung separat berücksichtigt werden.

---

## 9. Gemini-Auswertung

`gemini_auswertung.py` bildet die Interpretationsschicht des Systems.

Python stellt dafür strukturierte Eingabedaten bereit, darunter je nach Lauf:

- Setup-Ergebnisse
- offene Positionen
- Langfristdaten
- Short-Daten
- Beobachtungsliste
- Makro-Briefing
- Struktur-/Trend-Briefing
- weitere technische und marktbezogene Quellen

Die Auswertung besitzt zusätzliche Prüf- und Normalisierungsschritte. Dazu gehören beispielsweise:

- Validierung von Abschnitten
- Sicherung kritischer Makrozahlen
- Normalisierung technischer Werte
- Konsistenzprüfung zwischen Quelle und Ausgabe
- Prüfung von Setup-/Trade-Story-Zuordnungen
- Entfernung unerwünschter eigener Scores, wenn diese nicht Teil der autoritativen Faktenbasis sind

### Grundprinzip

```text
Python
  │
  ├── berechnet Fakten
  ├── sammelt Quellen
  ├── normalisiert Daten
  └── prüft Konsistenz
          │
          ▼
       Gemini
          │
          ▼
   Interpretation / Briefing
```

Gemini soll damit primär interpretieren und strukturieren, nicht die Python-Berechnungen heimlich ersetzen.

---

## 10. Gemini-Fallback

Für den Fall eines technischen Gemini-Problems existiert `chatgpt_fallback_paket.py`.

Der Hauptworkflow kann bei einem fehlgeschlagenen Gemini-Lauf ein separates Fallback-Paket erzeugen und als Artefakt bereitstellen. Zusätzlich wird der technische Fehlerstatus entsprechend behandelt.

Damit bleibt die Research-Kette auch bei einem externen LLM-Ausfall nachvollziehbar, statt einen möglicherweise veralteten oder unvollständigen Gemini-Output als aktuellen Stand auszugeben.

---

## 11. Caching und Datenhaltung

Caching ist ein zentraler Bestandteil der Architektur.

Wichtige Komponenten sind unter anderem:

- `market_cache.py`
- Makro-Cache in `makro_szenario.py`
- historische OHLCV-Datenbank
- `struktur_trends_cache.json`
- GitHub Actions Cache
- persistierte Projekt-/Historienbestände

Das Ziel ist dreifach:

1. unnötige externe Abrufe vermeiden,
2. Rate Limits und temporäre Ausfälle abfedern,
3. bereits bekannte Daten für spätere Läufe verfügbar halten.

Temporäre Laufzeitdateien und generierte Tagesdateien sollen dabei nicht als normale Quelldateien im Git-Repository behandelt werden.

---

## 12. Automatisierung mit GitHub Actions

Das Repository nutzt mehrere GitHub-Workflows mit unterschiedlichen Aufgaben und Zeitebenen.

### Tägliche bzw. werktägliche Prozesse

- `main.yml` – tägliche Hauptanalyse
- `short_check.yml` – Short-Analyse
- `stop_check.yml` – Positions-/Alert-Prüfung
- `benchmarks_check.yml` – Benchmark-Zwischenstände
- `hebeltrader_einzel_check.yml` – HEBELTRADER-bezogene Checks

### Wöchentliche Prozesse

- `langfrist_check.yml` – langfristige Bewertung

### Manuell bzw. Research/Test

- `einzel_check.yml`
- `trendwende_backtest.yml`
- verschiedene Datenquellen- und Normalisierungstests
- Alert-/Benchmark-Tests

Der zentrale Hauptworkflow orchestriert unter anderem:

```text
historische Daten aktualisieren
        ↓
Daten-/Regressionstests
        ↓
Positions-Tracker
        ↓
offene Positionen
        ↓
Beobachtungsliste
        ↓
Hauptscanner
        ↓
Trendwende / Edelmetalle / Short / Langfrist
        ↓
Makro
        ↓
Struktur/Trend
        ↓
YouTube-/Marktkontext
        ↓
Gemini
        ↓
Fallback bei technischem Gemini-Fehler
        ↓
Google Drive / E-Mail / Artefakte
```

---

## 13. Tests und Qualitätssicherung

Das Repository enthält eine umfangreiche Testsuite. Der aktuelle Stand umfasst **26 Python-Testdateien**.

Die Tests decken unter anderem ab:

- historische OHLCV-Daten
- Makro-Architektur
- Marktumgebung
- GDELT/GKG
- GDELT-Hardening
- Deutsche-Welle-RSS
- ForexFactory-Event-Mapping und Normalisierung
- LME-Datenquellen
- numerische Normalisierung
- Positions-/Lifecycle-Logik
- HEBELTRADER-Status und Synchronisierung
- Trendwende
- Bitcoin-/Pi-Cycle-Komponenten
- weitere Architektur- und Regressionsthemen

Ein wichtiger Schwerpunkt ist nicht nur die Prüfung einzelner Funktionen, sondern die Prüfung von **Architekturregeln und Schnittstellen**.

---

## 14. Projektstruktur – wichtige Dateien

| Datei / Bereich | Aufgabe |
|---|---|
| `analyse.py` | zentraler Long-/Sektor-Scanner |
| `trendwende_scanner.py` | Trendwende-Scanner |
| `short_scanner.py` | Short-Scanner |
| `edelmetalle_scanner.py` | Gold, Silber, Platin, Palladium |
| `langfrist_scanner.py` | langfristige Bewertung |
| `makro_szenario.py` | Makrodaten, Makro-Szenario und Cache |
| `struktur_trends.py` | strukturelle Markt-/Trenddaten |
| `offene_positionen_check.py` | laufende Positionsanalyse |
| `positionen_tracker.py` | Positionsverwaltung und Synchronisierung |
| `einzel_check.py` | Einzel-Ticker und Beobachtungsliste |
| `historical_ohlcv.py` | historische Kursdatenbank |
| `trendwende_backtest.py` | historische Trendwende-Analyse |
| `gemini_auswertung.py` | LLM-Auswertung und Output-Prüfungen |
| `chatgpt_fallback_paket.py` | technischer Gemini-Fallback |
| `market_cache.py` | Markt-/Benchmark-Cache |
| `market_data.py` | zentrale Marktdaten-Hilfsfunktionen |
| `.github/workflows/` | Automatisierung, Zeitpläne und Tests |
| `tests/` | Regression-, Architektur- und Datenquellentests |

---

## 15. Stärken der aktuellen Architektur

### Klare Trennung der Strategien

Trendfolge, Trendwende, Short, Langfrist-Bewertung, Edelmetalle und Positionsmanagement sind als eigene Komponenten organisiert.

### Wiederverwendbare Kernlogik

Gemeinsame Funktionen werden zentralisiert, beispielsweise bei Trendlinien, Marktdaten und Caches. Dadurch wird unnötige Code-Duplizierung vermieden.

### Fehlerbehandlung als Bestandteil des Designs

Externe Datenquellen werden nicht als permanent verfügbar vorausgesetzt. Caches, Fallbacks, Hardening-Tests und kontrollierte Fehlerpfade sind ein wesentlicher Bestandteil des Systems.

### Historische Datenbasis

Die persistente OHLCV-Datenbank schafft eine Grundlage für reproduzierbare Research- und Backtesting-Läufe.

### Trennung von Fakten und Interpretation

Die Python-Schicht liefert berechnete Daten und Quellen; die LLM-Schicht interpretiert diese. Zusätzliche Prüfungen versuchen, kritische Werte und Zuordnungen konsistent zu halten.

### Operativer Regelkreis

Das Projekt deckt nicht nur die Suche nach neuen Setups ab, sondern auch Beobachtung, Positionsüberwachung, Alerts und Reporting.

---

## 16. Aktuelle Lücken / nächste Entwicklungsschritte

Die größten verbleibenden Aufgaben liegen weniger bei zusätzlichen technischen Indikatoren, sondern bei Datenqualität, Reproduzierbarkeit und Komplexitätskontrolle.

### 16.1 Data Provenance / Datenherkunft

Für wichtige Werte sollte langfristig eindeutig gespeichert werden:

```text
Wert
Quelle
Beobachtungsdatum
Abrufzeitpunkt
Revision / Veröffentlichungsstatus
Cache-Status
```

Damit lässt sich später nachvollziehen, warum ein bestimmter Wert in einem Briefing stand.

### 16.2 Einheitliches Data Dictionary

Viele Datenobjekte werden heute über CSV, TXT, JSON und Python-Strukturen transportiert. Ein verbindliches Schema für Setup-, Positions-, Makro- und Marktobjekte würde die weitere Entwicklung vereinfachen.

### 16.3 Vollständigeres Backtesting

Für eine belastbare quantitative Strategievalidierung fehlen noch beziehungsweise benötigen weitere Modellierung:

- Slippage
- Transaktionskosten
- Spread
- Liquiditätsannahmen
- Survivorship Bias
- Delistings
- Corporate Actions
- Out-of-Sample-Tests
- Walk-Forward-Tests
- realistische Ausführungsregeln

### 16.4 Derivatebene

Bei Optionsscheinen und Knock-outs reicht die Entwicklung des Underlyings allein nicht aus. Eine weitergehende quantitative Modellierung müsste insbesondere Laufzeit, Delta, implizite Volatilität, Theta, Spread und Knock-out-Abstand berücksichtigen.

### 16.5 Dokumentation

Der tatsächliche Funktionsumfang ist inzwischen deutlich größer als eine klassische kurze Projekt-README. Deshalb sollte diese README als Einstieg dienen und langfristig durch eine detaillierte Architektur-/Datenflussdokumentation ergänzt werden.

### 16.6 Komplexitätskontrolle

Mit zunehmender Zahl von Datenquellen und Workflows wird eine eindeutige Dokumentation der Abhängigkeiten immer wichtiger:

```text
Quelle
  ↓
Abruf
  ↓
Cache
  ↓
Python-Modul
  ↓
Output
  ↓
Gemini / Reporting
```

Jede zusätzliche Funktion sollte möglichst in dieses Modell eingeordnet werden.

---

## 17. Grundsätze für Änderungen am Projekt

Bei Änderungen am produktiven System sollte die bestehende Architektur möglichst gezielt erweitert und nicht unnötig umgebaut werden.

Besonders wichtig sind:

1. **keine unnötige Vereinfachung bestehender Logik**
2. **keine ungeplante Änderung anderer Module**
3. **bestehende Fallbacks und Caches erhalten**
4. **Regressionstests vor produktiver Verwendung**
5. **Datenquelle und Datenherkunft nachvollziehbar halten**
6. **bei neuen Strategien gemeinsame Komponenten wiederverwenden**
7. **Output-Schnittstellen nicht stillschweigend verändern**

---

## 18. Sicherheits- und Konfigurationshinweise

API-Schlüssel, Google-Credentials und andere Secrets gehören ausschließlich in die dafür vorgesehenen GitHub-Secrets bzw. sichere Laufzeitkonfigurationen.

Keine Zugangsdaten, Tokens oder privaten Schlüssel in Python-Dateien, CSV-Dateien, JSON-Dateien oder die README eintragen.

---

## 19. Status des Projekts

Der aktuelle Stand ist nicht mehr nur ein einfacher Aktien-Screener. Das Repository bildet eine modulare Research- und Monitoring-Plattform mit folgenden Ebenen:

```text
                 NEUBER MACRO & MARKETS

                       DATEN
                         │
        ┌────────────────┼────────────────┐
        │                │                │
      Markt             Makro            News
        │                │                │
        └────────────────┼────────────────┘
                         ▼
                 Daten / Cache / DB
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
          Trendfolge  Trendwende  Short
              │          │          │
              └──────────┼──────────┘
                         ▼
                  Struktur / Trend
                         │
                         ▼
                    Makro-Fokus
                         │
                         ▼
                Kandidaten / Positionen
                         │
                         ▼
                  Gemini-Auswertung
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
          Reporting                Fallback
```

Die weitere Entwicklung sollte sich deshalb vor allem auf **Datenqualität, Nachvollziehbarkeit, reproduzierbares Backtesting, klare Schnittstellen und kontrollierte Komplexität** konzentrieren – nicht ausschließlich auf die Erweiterung um weitere Signale.

---

## 20. Repository-Regeln

Generierte Tagesdateien, lokale Laufzeitdateien und Cache-Dateien sollen nicht als normale Quelldateien in das Produktiv-Repository aufgenommen werden.

Die produktive Hauptdatei ist `analyse.py`. Alte lokale Kopien wie `analyse(7).py`, `analyse(8).py` oder ähnliche Versionskopien gehören nicht in das Produktiv-Repository.

---

## 21. GitHub

Das Repository kann perspektivisch von `Sektor-Analyse` auf `Neuber-Macro-Markets` umbenannt werden. Die vorhandenen Workflow-Dateien verwenden relative Pfade; ein Repository-Namewechsel erfordert daher grundsätzlich keine inhaltliche Änderung der Workflows.
