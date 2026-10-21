# Investment Radar

Das Radar interpretiert bereits erzeugte Projektergebnisse. Es startet keine Scanner, APIs oder Gemini.

## Ablauf
- `investment_radar_drive.py` lädt die neuesten vorhandenen Radar-Eingaben aus dem bestehenden Google-Drive-Ordner.
- `investment_radar.py` erzeugt einen kompakten, interpretationsorientierten Tagesbericht. Es gibt keine feste Kandidatenobergrenze; plausible Kandidaten werden aus dem Trade-Story-Universum und Setup-Dateien zusammengeführt. Unternehmen erscheinen als `Name (Ticker)`, soweit Name und Ticker in den Quelldaten vorhanden sind.
- `investment_radar_upload.py` speichert den Bericht als `Investment_Radar_YYYY-MM-DD.txt` im Radar-Google-Drive-Ordner. Datierte Dateien werden nicht überschrieben oder gelöscht und ermöglichen Tagesvergleiche.
- Der Workflow lädt den Bericht nicht mehr als GitHub-Artefakt hoch. Google Drive ist die dauerhafte Ablage.

## Ausführen
```bash
python investment_radar.py --input-dir investment_radar_input --output Investment_Radar_YYYY-MM-DD.txt
python investment_radar_upload.py --report Investment_Radar_YYYY-MM-DD.txt
```
