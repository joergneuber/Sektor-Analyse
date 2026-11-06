# Forensische Codeprüfung – OHLCV Symbolbereinigung
Stand: 06.11.2026

## Ergebnis
Die drei gelieferten Dateien wurden gegen den aktuellen GitHub-Stand `main` geprüft. Die hochgeladene `analyse.py` stimmt bytegenau mit dem aktuellen GitHub-Blob überein (Git-Blob-SHA1 `691baf3ec7002fba727f326ade505291f0769652`). Auch die beiden Basisdateien `historical_ohlcv.py` und `tests/test_historical_ohlcv.py` stimmen bytegenau mit den aktuell ausgelesenen GitHub-Blobs überein (siehe Manifest).

Es wurden ausschließlich diese drei Dateien geändert:
- `analyse.py`
- `historical_ohlcv.py`
- `tests/test_historical_ohlcv.py`

Keine Workflow-Datei, keine Datenbank, keine Secrets, keine Automatisierung und keine GitHub-Einstellung wurde verändert.

## 1. Byte-/Dateiebene
Alle drei Dateien sind UTF-8 mit LF-Zeilenenden. Exakte Größen, Zeilenzahlen, SHA-256- und Git-Blob-SHA1-Werte stehen in `MANIFEST.json`.

## 2. Zeilenweise Änderungen
### `analyse.py`
Acht alte US-Ticker aus den Sektorlisten entfernt: `EA`, `AVB`, `EQR`, `WBS`, `QRVO`, `WBD`, `PSKY`, `KVUE`.
Zwei gültige Nachfolger/aktuelle Symbole ergänzt: `VMRK`, `SKYD`.
EU: `ACT.DE` durch `ACT0.DE` ersetzt.
Die Änderung umfasst 8 geänderte Diff-Zeilen plus 8 Gegenzeilen (keine Zeilenanzahländerung).

### `historical_ohlcv.py`
`EXCLUDED_TICKERS` von 26 auf 35 Einträge erweitert. Neu ausgeschlossen: `EA`, `AVB`, `EQR`, `WBS`, `QRVO`, `WBD`, `PSKY`, `KVUE`, `ACT.DE`.
Keine Funktion, kein Import, kein Kontrollfluss wurde geändert.

### `tests/test_historical_ohlcv.py`
- Universe-Mindestgröße an die netto kleinere, bereinigte Menge angepasst (vorher 802, nachher 796).
- Neue Symbole und das Fehlen der neun alten Kennungen werden im Universumstest geprüft.
- Ausschlusstest prüft nun alle 35 Einträge und die Filterwirkung auch bei dynamischen Registrierungen.
- Neuer AST-basierter Regressionstest prüft die Sektorlisten in `analyse.py`.

## 3. Strukturell / AST
`analyse.py`: Imports sowie Top-Level-Funktionen/-Klassen unverändert. Geändert wurden ausschließlich String-Mitgliedschaften in den beiden bestehenden Universumszuweisungen.
`historical_ohlcv.py`: Imports sowie Top-Level-Funktionen/-Klassen unverändert. Nur die Konstante `EXCLUDED_TICKERS` und ihr erläuternder Kommentar wurden angepasst.
`tests/test_historical_ohlcv.py`: Bestehende Tests aktualisiert und ein zusätzlicher Regressionstest ergänzt.

## 4. Semantik / Logik
Die tatsächliche Universe-Auswertung wurde vor/nach der Änderung ausgeführt:
- Vorher: 802 Ticker
- Nachher: 796 Ticker
- Entfernt: `ACT.DE`, `AVB`, `EA`, `EQR`, `KVUE`, `PSKY`, `QRVO`, `WBD`, `WBS`
- Hinzugefügt: `ACT0.DE`, `SKYD`, `VMRK`

Die neue Ausschlussliste greift sowohl bei `discover_universe()` als auch bei `register_ticker()`. Bereits in einer dynamischen JSON-Datei gespeicherte Altkennungen werden nicht physisch gelöscht; sie werden aber bei der Universumsbildung herausgefiltert. Die SQLite-Daten werden nicht gelöscht oder verändert.

## 5. Vollständigkeit
Die 9 Altkennungen sind in den aktiven Sektorlisten nicht mehr enthalten und in der OHLCV-Ausschlussliste enthalten. Die drei aktuellen Symbole sind aktiv und nicht ausgeschlossen. `PPFB.DE` und `DSV.CO` bleiben unverändert aktiv.

## 6. Gap-Auswirkung / fachliche Einschränkung
Die Diagnose enthielt 356 Gap-Einträge über 22 Symbole. 301 Einträge entfallen auf die neun alten Kennungen. Davon sind 290 eindeutig mit abgelaufenen/ersetzten Symbolen verbunden. Bei `EQR` liegen 11 Einträge vor dem Zusammenschluss; das sind historische Datenlücken der damaligen Aktie und keine nachträglich „reparierten“ Fehler. Durch den Ausschluss von `EQR` werden sie aus dem aktiven Coverage-Bericht ausgeblendet. Sie bleiben in der Datenbank unangetastet.

Nach der Bereinigung bleiben 55 Gap-Einträge sichtbar: 43 Futures, 6 EuroStoxx-Indizes, 2 koreanische Aktie, 2 FX, sowie je 1 für `DSV.CO` und `PPFB.DE`. Diese wurden bewusst nicht verändert.

## 7. Tests / Regression
- `py_compile`: alle drei Dateien erfolgreich.
- `pytest tests/test_historical_ohlcv.py -k "not market_cache"`: 22 bestanden.
- Ein vorhandener, unabhängiger Test wurde nicht ausgeführt, weil `market_cache.py` in der bereitgestellten lokalen Testbasis nicht enthalten war. Daher kein vollständiger Testlauf über alle 23 Tests.
- Kein Live-Yahoo-Download, kein produktiver Datenbanklauf und kein GitHub-Workflow-Lauf durchgeführt.

## 8. Regressionen / Rest-Risiken
Keine Änderungen an Berechnungen, API-Aufrufen, Datenbankoperationen, Stop-/Scanner-Logik oder Workflows. Das Hauptrisiko ist die bewusste Ausblendung der 11 historischen EQR-Gaps aus der aktiven Coverage-Auswertung. Es wurde keine historische Datenlöschung vorgenommen.

## 9. GitHub-Abgleich
Die Originalstände aller drei Dateien wurden read-only aus `joergneuber/Sektor-Analyse` (Default-Branch `main`) ausgelesen und die gelieferten Originaldateien über Git-Blob-SHA1 exakt abgeglichen. Es gab keine Schreiboperation auf GitHub.

## 10. Exakte Metadaten\n\n```json\n{
  "analyse.py": {
    "github_current": {
      "bytes": 261936,
      "lines": 4879,
      "sha256": "f9dec474d2d4579171bba005bb2e144ed4c590b9a84aaed16a778bb3b61fd82a",
      "git_blob_sha1": "691baf3ec7002fba727f326ade505291f0769652",
      "encoding": "UTF-8",
      "line_endings": "LF"
    },
    "patch": {
      "bytes": 261895,
      "lines": 4879,
      "sha256": "ec086f225af601caba4b2b385ef0a175f15ca199c12ca31d7fbc7c18c6163aa8",
      "git_blob_sha1": "62937997dff84e327990a3de25782c10db14fafc",
      "encoding": "UTF-8",
      "line_endings": "LF"
    }
  },
  "historical_ohlcv.py": {
    "github_current": {
      "bytes": 53064,
      "lines": 1174,
      "sha256": "4807e5120eaa2204b204d24002c1b600d984627b018392cabce13efc2b43e9c0",
      "git_blob_sha1": "5e2ea578711d8a6757d8780a43c817a21cf78434",
      "encoding": "UTF-8",
      "line_endings": "LF"
    },
    "patch": {
      "bytes": 53137,
      "lines": 1175,
      "sha256": "0fbf64b2d51d1e10b570dccd85034df33059f5a6ee3f54e71757de23b33a3b2e",
      "git_blob_sha1": "fed843ca932587ef9daeb012388024da7561e307",
      "encoding": "UTF-8",
      "line_endings": "LF"
    }
  },
  "tests/test_historical_ohlcv.py": {
    "github_current": {
      "bytes": 21277,
      "lines": 520,
      "sha256": "dc180616e004fcc6b95ead3b39d04aad1bfa4042e625b1328bd88c1eb9ab15a7",
      "git_blob_sha1": "061e857ba74c353df758cdfc031ec89e78bd209a",
      "encoding": "UTF-8",
      "line_endings": "LF"
    },
    "patch": {
      "bytes": 22963,
      "lines": 564,
      "sha256": "09882f6bf1bbb396234cecb191561f71e2daaffc56af190d2e30664b40602994",
      "git_blob_sha1": "e5a20832b74fd9dc5f47a6f6ba676487a248b708",
      "encoding": "UTF-8",
      "line_endings": "LF"
    }
  }
}\n```\n