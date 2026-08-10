# Neuber Macro & Markets

Automatisierte tägliche Markt-, Sektor- und Setup-Analyse.

## Architektur

- Hauptscanner: Top-8-US- und Top-5-EU-Sektoren der aktuellen Rotation
- Trendwende: vollständiges US-/EU-Universum
- Short: Bottom-Sektoren
- Edelmetalle: Gold, Silber, Platin, Palladium
- zentrale Sektorzuordnung
- zentrale Trendlinien-Logik für Long und Short
- Daten-/Benchmark-Cache zur Vermeidung redundanter Abrufe

## Repository-Regeln

Generierte Tagesdateien, Python-Cache und lokale Laufzeitdateien gehören nicht
ins Git-Repository. Sie werden über `.gitignore` ausgeschlossen.

Die produktive Hauptdatei heißt `analyse.py`. Alte Kopien wie
`analyse(7).py`, `analyse(8).py`, `analyse(9).py` usw. gehören nicht ins
Produktiv-Repository.

## GitHub

Das Repository kann später von `Sektor-Analyse` auf
`Neuber-Macro-Markets` umbenannt werden. Die Workflow-Dateien verwenden
relative Pfade und müssen dafür nicht inhaltlich geändert werden.
