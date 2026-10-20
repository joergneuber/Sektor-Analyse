# Investment Radar (separater Offline-Baustein)

Der Radar wertet bereits erzeugte Projekt-Ergebnisdateien aus. Er ruft **keine** Scanner, APIs, Gemini oder den Hauptworkflow auf und verändert keine vorhandenen Analyse-Dateien.

## Ausführen

```bash
python investment_radar.py --input-dir /pfad/zu/vorhandenen_ergebnissen
```

Optionaler Berichtspfad: `--output /pfad/Investment_Radar_Bericht.txt`.

## Stufe 2: Makro-/Sektor-Überschneidungen

Zusätzlich zur Ergebnisübersicht werden nachvollziehbare, regelbasierte Recherchehypothesen ausgegeben. Die Regeln suchen in bereits vorhandenen Makrobriefing-Zeilen nach festgelegten Begriffen und verbinden diese nur mit Sektoren, die in Performance-, Setup- oder Trade-Story-Dateien tatsächlich vorkommen. Angezeigt werden die gefundenen Makro-Belegzeilen sowie vorhandene Performance- und Setup-Kandidaten.

Die Zuordnung ist eine **heuristische Recherchehilfe**, kein Kausalitätsnachweis, kein Kaufsignal und keine Prognose. Die Keyword-Regeln sind im Skript in `MACRO_RULES` offengelegt und können dort kontrolliert erweitert werden. Fehlende Daten werden nicht ergänzt.

## Abgrenzung

- Eingaben: vorhandene CSV-, JSON- und TXT-Ergebnisse.
- Keine neuen Datenabrufe, Scannerläufe oder Gemini-Aufrufe.
- Kein automatischer Eingriff in den Hauptworkflow; dessen Workflow-YAML bleibt unverändert.
