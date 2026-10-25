# Forensischer Reparaturbericht – Einstiegsdatum-Validierung

**Basis:** `Trading-Radar-Positionsschluessel-Ticker-Kaufdatum-20261025(3).zip`

## Ergebnis

Die Validierung lehnt nun sowohl fehlende als auch nicht leere, aber unparsebare Einstiegsdaten ab. Beide Pfade verwenden dieselben textuellen Datumsformate: `TT.MM.JJJJ`, `JJJJ-MM-TT`, `TT/MM/JJJJ`. Kalenderungültige Werte wie `32.13.2026` und `2026-02-30` werden nicht als gültige Positionsidentität akzeptiert. Die Generatorseite behält die Quellzeile im Diagnoseblock, unterdrückt aber Anzahl-/Quellen-/SHA-256-Marker und setzt den Fachstatus auf `NICHT_FINAL`.

## Geänderte Dateien

1. `trading_radar_report_gate.py` – neue Funktion `_normalize_tab2_entry_date`; nur tatsächlich parsebare Daten werden in den zusammengesetzten Schlüssel aufgenommen.
2. `Trading_Radar_Alleinauswertung.py` – gemeinsame textuelle Parserfunktion `_parse_tab2_entry_date_text`; Loader und Speichergate verwenden dieselbe Datumslogik für Textdaten. Google-Sheets-Seriennummern werden weiterhin separat im Loader unterstützt und anschließend normalisiert.
3. `tests/test_trading_radar_report_gate.py` – ungültige nicht leere Daten und alle drei gültigen Formate geprüft.
4. `tests/test_trading_radar_data_contract.py` – Parser- und Writer-Regression für ungültiges Datum ergänzt; 51-Zeilen-Synthesetest korrigiert, sodass alle Testdaten echte Kalenderdaten verwenden.

## Tests

- `py_compile`: bestanden.
- `test_trading_radar_report_gate.py`: 34 Tests bestanden.
- `test_trading_radar_data_contract.py`: 18 Tests bestanden, 3 übersprungen.
- Gesamt: **52 bestanden, 3 übersprungen**.
- Die drei übersprungenen Tests betreffen Workflow-/Benchmark-Dateien, die im beigefügten Patch-Archiv fehlen.

## Nicht ausführbare externe Prüfungen

### Tatsächliche 51 Google-Sheets-Datensätze
Nicht durchgeführt: Das angehängte Archiv enthält keine Datenexportdatei mit den 51 tatsächlichen Datensätzen. Der Google-Drive-Connector ist in dieser Sitzung nicht verfügbar. Deshalb werden keine Aussagen über den Live-Inhalt oder die tatsächliche Anzahl in `Offene Positionen+Check` → `Geschlossene Positionen` getroffen. Für den Abschluss ist ein Export des vollständigen Tabs mit Kopfzeile erforderlich oder eine autorisierte Verbindung, die in dieser Sitzung tatsächlich verfügbar ist.

### Vollständiger GitHub-Actions-Workflow
Nicht durchgeführt: Das Basisarchiv ist ein Patch-Archiv und enthält keine `.github/workflows/main.yml`. Eine Datei gleichen Namens außerhalb des gelieferten Archivs wurde nicht als Projektbasis angenommen. Außerdem ist keine aktive GitHub-Verbindung bestätigt. Lokale Unit- und Integrationsprüfungen sind kein Ersatz für einen echten GitHub-Actions-Lauf. Der GitHub-Connector wurde zur Verbindung vorgeschlagen; solange er nicht verbunden ist, kann kein Workflow-Lauf ausgelöst oder verifiziert werden.

## Hashes und Dateimetadaten

| Datei | Vorher Bytes/Zeilen | Vorher SHA-256 | Nachher Bytes/Zeilen | Nachher SHA-256 |
|---|---:|---|---:|---|
| `Trading_Radar_Alleinauswertung.py` | 899545 / 17408 | `86e4f2e9d4be68b785bc1775104b25b93f97a92204bab665b411854349789a76` | 899472 / 17409 | `79e9038ff9df44da943ba75fc82164fc97bd8c99b9f9169cb15e19b8ababb92f` |
| `trading_radar_report_gate.py` | 24633 / 503 | `baca9c8af662be7c83324fb1e34dd3d132c0439fecafdb8db062a3be0817fdf9` | 24726 / 508 | `e50d6ade7b0ab927f8312d6452e2e19255e3539c61fbef2490d95f5e7ae8dad4` |
| `tests/test_trading_radar_report_gate.py` | 16635 / 284 | `cb9d1f80c90211acd6b0d3bfb7e187e694424db97b4c198c6537be63d0011dbd` | 17779 / 306 | `f3ab7afa1d18b987357684775781605afe3a5c87635a495d2ff632b0162e4c72` |
| `tests/test_trading_radar_data_contract.py` | 22613 / 381 | `4fde478f8568c5fe12d09528066adf907ad6b7b0e3a70901172edaca83dabeab` | 26104 / 439 | `d18cba8815c75b8bc39334f0dd0953958d29d92e1c588c3dc095f63d697d303e` |

## Vollständige Unified Diffs

```diff
--- BASE/Trading_Radar_Alleinauswertung.py
+++ PATCH/Trading_Radar_Alleinauswertung.py
@@ -184,10 +184,21 @@
 
 # Gleicher Drive-Ordner wie in upload_to_drive.py - dort landen alle
 # Scanner-Ausgaben, von dort werden ggf. die Short-Dateien nachgeladen.
 DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
 BEOBACHTUNGSLISTE_DATEI = "einzel_check_beobachtung.json"
+
+
+def _parse_tab2_entry_date_text(value):
+    """Parse only the textual date formats accepted by the Tab-2 contract."""
+    raw = str(value or "").strip()
+    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
+        try:
+            return datetime.datetime.strptime(raw, fmt).date()
+        except ValueError:
+            continue
+    return None
 
 
 def _tab2_nonempty_rows_with_source_numbers(values, start_sheet_row=3):
     """Return nonempty row dictionaries and their original physical Sheet row numbers.
 
@@ -507,15 +518,13 @@
                 return value.date()
             if isinstance(value, datetime.date):
                 return value
 
             raw = str(value or "").strip()
-            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
-                try:
-                    return datetime.datetime.strptime(raw, fmt).date()
-                except ValueError:
-                    pass
+            parsed_text_date = _parse_tab2_entry_date_text(raw)
+            if parsed_text_date is not None:
+                return parsed_text_date
 
             # Google-Sheets-Datumssystem: Seriennummer 25569 = 1970-01-01.
             # Nur ein plausibler Kalenderbereich wird als Datum interpretiert,
             # damit normale numerische Werte wie 270.58 niemals als Datum gelten.
             if re.fullmatch(r"\d+(?:\.\d+)?", raw):
@@ -16571,23 +16580,15 @@
             entry_match = re.search(r"(?:^|\s)Einstiegsdatum:\s*([^|\r\n]+)", line)
             source_row_match = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
             ticker = ticker_match.group(1).strip().casefold() if ticker_match else ""
             entry_date = entry_match.group(1).strip() if entry_match else ""
             source_row = source_row_match.group(1) if source_row_match else "?"
-            normalized_date = entry_date.casefold()
-            for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
-                try:
-                    normalized_date = datetime.datetime.strptime(entry_date, date_format).date().isoformat()
-                    break
-                except ValueError:
-                    continue
-            if not ticker or not entry_date or entry_date.casefold() in {
-                "nicht verfügbar", "nicht verfuegbar", "n/a", "tbd"
-            }:
+            parsed_entry_date = _parse_tab2_entry_date_text(entry_date)
+            if not ticker or parsed_entry_date is None:
                 invalid_identity_rows.append(source_row)
             else:
-                position_keys.append((ticker, normalized_date))
+                position_keys.append((ticker, parsed_entry_date.isoformat()))
         if invalid_identity_rows:
             validation_errors.append(
                 "Ticker+Einstiegsdatum fehlen/ungueltig bei Quellzeilen="
                 + ", ".join(invalid_identity_rows)
             )
--- BASE/trading_radar_report_gate.py
+++ PATCH/trading_radar_report_gate.py
@@ -37,10 +37,23 @@
                 and int(m.group("number")) > number), len(text))
     return text[start.end():end]
 
 def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
     return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns)
+
+def _normalize_tab2_entry_date(value: str) -> str | None:
+    """Return an ISO date only for dates accepted by the Tab-2 data contract."""
+    raw = str(value or "").strip()
+    if not raw or _MISSING_VALUE_RE.fullmatch(raw):
+        return None
+    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
+        try:
+            return datetime.datetime.strptime(raw, date_format).date().isoformat()
+        except ValueError:
+            continue
+    return None
+
 
 _MISSING_VALUE_RE = re.compile(
     r"^(?:|[-–—]|n/?a|na|tbd|todo|xxx|placeholder|platzhalter|unbekannt|"
     r"nicht\s+(?:vorhanden|verfügbar|verifizierbar|bekannt)|fehlt|unklar)$",
     re.IGNORECASE,
@@ -420,20 +433,12 @@
             entry_match = re.search(r"(?:^|\s)Einstiegsdatum:\s*([^|\r\n]+)", line)
             source_row_match = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
             ticker = ticker_match.group(1).strip().casefold() if ticker_match else ""
             entry_date = entry_match.group(1).strip() if entry_match else ""
             source_row = source_row_match.group(1) if source_row_match else "?"
-            normalized_date = entry_date.casefold()
-            for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
-                try:
-                    normalized_date = datetime.datetime.strptime(entry_date, date_format).date().isoformat()
-                    break
-                except ValueError:
-                    continue
-            if not ticker or not entry_date or entry_date.casefold() in {
-                "nicht verfügbar", "nicht verfuegbar", "n/a", "tbd"
-            }:
+            normalized_date = _normalize_tab2_entry_date(entry_date)
+            if not ticker or normalized_date is None:
                 invalid_identity_rows.append(source_row)
             else:
                 position_keys.append((ticker, normalized_date))
         if invalid_identity_rows:
             errors.append(
--- BASE/tests/test_trading_radar_report_gate.py
+++ PATCH/tests/test_trading_radar_report_gate.py
@@ -226,10 +226,32 @@
         )
         text = self._recompute_tab2_digest(text)
         errors = gate.validate_report(text)
         self.assertTrue(any("Positionsschluessel Ticker+Einstiegsdatum doppelt" in e for e in errors), errors)
 
+    def test_m05_rejects_nonempty_but_invalid_entry_dates(self):
+        for invalid_date in ("32.13.2026", "2026-02-30", "Datum unbekannt", "TBD"):
+            with self.subTest(invalid_date=invalid_date):
+                text = report_fixture().replace(
+                    "Einstiegsdatum: 02.08.2026", f"Einstiegsdatum: {invalid_date}"
+                )
+                text = self._recompute_tab2_digest(text)
+                errors = gate.validate_report(text)
+                self.assertTrue(
+                    any("Positionsidentitaet fehlt" in e for e in errors), errors
+                )
+
+    def test_m05_accepts_all_contract_entry_date_formats(self):
+        for valid_date in ("02.08.2026", "2026-08-02", "02/08/2026"):
+            with self.subTest(valid_date=valid_date):
+                text = report_fixture().replace(
+                    "Einstiegsdatum: 02.08.2026", f"Einstiegsdatum: {valid_date}"
+                )
+                text = self._recompute_tab2_digest(text)
+                errors = gate.validate_report(text)
+                self.assertFalse(any("Positionsidentitaet fehlt" in e for e in errors), errors)
+
     def test_m05_rejects_missing_entry_date(self):
         text = report_fixture(tab2_count=2).replace(
             "Einstiegsdatum: 02.08.2026 | ", ""
         )
         text = self._recompute_tab2_digest(text)
--- BASE/tests/test_trading_radar_data_contract.py
+++ PATCH/tests/test_trading_radar_data_contract.py
@@ -185,10 +185,68 @@
 
     def test_radar_filename_contract_exists_at_both_save_paths(self):
         self.assertGreaterEqual(self.radar_source.count("TRADING_RADAR_DATEINAME_UNGUELTIG"), 2)
         self.assertIn(r"Trading_Radar\(\d{4}-\d{2}-\d{2}\)\.txt", self.radar_source)
         self.assertIn("TRADING_RADAR_ABLAGEPFAD_UNGUELTIG", self.radar_source)
+
+    def test_tab2_entry_date_text_parser_rejects_nonempty_invalid_dates(self):
+        radar = load_radar_module()
+        for invalid_date in ("32.13.2026", "2026-02-30", "Datum unbekannt", "TBD", ""):
+            with self.subTest(invalid_date=invalid_date):
+                self.assertIsNone(radar._parse_tab2_entry_date_text(invalid_date))
+        for valid_date in ("02.08.2026", "2026-08-02", "02/08/2026"):
+            with self.subTest(valid_date=valid_date):
+                self.assertEqual(
+                    radar._parse_tab2_entry_date_text(valid_date),
+                    datetime.date(2026, 8, 2),
+                )
+
+    def test_generator_marks_nonempty_invalid_entry_date_nonverifiable(self):
+        radar = load_radar_module()
+        with tempfile.TemporaryDirectory() as temp_dir:
+            previous_cwd = os.getcwd()
+            os.chdir(temp_dir)
+            try:
+                csv_path = Path(temp_dir) / "Offene Positionen+Check.csv"
+                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
+                    writer = csv.DictWriter(
+                        handle, delimiter=";",
+                        fieldnames=["Name", "Ticker", "Status", "Einstieg", "Einstiegsdatum", "Stop"],
+                    )
+                    writer.writeheader()
+                    writer.writerow({
+                        "Name": "Alpha", "Ticker": "AAA", "Status": "Offen",
+                        "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9",
+                    })
+                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
+                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
+                radar._GEMINI_GESCHlossene_10_5_FINAL = ""
+                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
+                    "TAB-2-ANZAHL=1\nTAB-2-QUELLE=Geschlossene Positionen\n"
+                    "Quellzeile: 3 | Ticker: AAA | Einstiegsdatum: 32.13.2026 | Status: Verkauft"
+                )
+                report = "\n".join(
+                    f"{i}. {title}\n" + ("Ausreichend dokumentierter Abschnittsinhalt und Prüfkontext." * 2)
+                    for i, title in enumerate([
+                        "Trading-Radar", "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
+                        "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang"
+                    ], start=1)
+                ) + "\nTRADING_RADAR_FACHLICH_FINAL=FINAL\n"
+                with redirect_stdout(StringIO()):
+                    output = radar.speichere_ergebnis(
+                        report, {"Offene Positionen+Check.csv": str(csv_path)}
+                    )
+                saved = Path(output).read_text(encoding="utf-8-sig")
+                closed_section = saved[saved.index("10.5 Geschlossene Positionen"):saved.rfind("TRADING_RADAR_FACHLICH_FINAL=")]
+                self.assertIn("TAB-2-QUELLE NICHT VERIFIZIERBAR", closed_section)
+                self.assertIn("Einstiegsdatum: 32.13.2026", closed_section)
+                self.assertNotIn("TAB-2-ANZAHL=", closed_section)
+                self.assertNotIn("TAB-2-QUELLE=Geschlossene Positionen", closed_section)
+                self.assertNotIn("TAB-2-EXPORT-SHA256=", closed_section)
+                self.assertTrue(saved.rstrip().endswith("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"))
+            finally:
+                os.chdir(previous_cwd)
 
     def test_closed_positions_loader_has_no_date_filter_and_keeps_unparseable_dates(self):
         start = self.radar_source.find("def lade_offenen_positionen_check_tab2")
         end = self.radar_source.find("\ndef ", start + 5)
         section = self.radar_source[start:end if end >= 0 else None]
@@ -313,11 +371,11 @@
                     "Quellzeile: 4 | Ticker: BADDATE | Einstiegsdatum: 02.01.2019 | Ausstiegsdatum: unbekannt\n"
                     + "\n".join(
                         (
                             f"Quellzeile: {i + 4} | Ticker: OLD | Einstiegsdatum: {i + 2:02d}.01.2019 | Ausstiegsdatum: 2019-01-01"
                             if i == 1 else
-                            f"Quellzeile: {i + 4} | Ticker: CLOSED{i:02d} | Einstiegsdatum: {i + 2:02d}.02.2026 | Ausstiegsdatum: 2026-10-24"
+                            f"Quellzeile: {i + 4} | Ticker: CLOSED{i:02d} | Einstiegsdatum: {(datetime.date(2026, 2, 1) + datetime.timedelta(days=i)).strftime('%d.%m.%Y')} | Ausstiegsdatum: 2026-10-24"
                         )
                         for i in range(1, 50)
                     )
                 )
                 report = "\n".join(f"{i}. {title}" for i, title in enumerate([
```
