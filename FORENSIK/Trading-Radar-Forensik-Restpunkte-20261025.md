# Forensische Reparatur: Quellzeilennummern und Ereignisfelder

**Basisarchiv:** `Trading_Radar_Alleinauswertung.py`  
**Umfang:** Vier geänderte Dateien; keine anderen Projektdateien im Patch geändert.

## Befund 1 – Tab-2-Quellzeilennummern

Die bisherige Meldung berechnete Sheet-Zeilennummern mit `enumerate(rows)` nach dem Filtern leerer Zeilen. Dadurch wurden physische Zeilen nach einer Leerzeile zu klein nummeriert. Die neue Funktion `_tab2_nonempty_rows_with_source_numbers` filtert Datensätze und bewahrt parallel die ursprünglichen Sheet-Zeilennummern. Fehler zu fehlenden Tickern verwenden nun diese unveränderten Quellkoordinaten. Der Regressionstest belegt beispielsweise: Sheet-Zeilen 3, 5, 6 bleiben 3, 5, 6; die leere Zeile 4 verschiebt nichts.

## Befund 2 – Ereignisfelder je Ereignis

Die bisherige M-08-Prüfung wertete Pflichtfelder im gesamten Ereignisradar aus. So konnte ein vollständiger erster Termin einen unvollständigen zweiten Termin maskieren. Das Gate teilt nun nummerierte und Bullet-Einträge sowie `Ereignis:`-/`Termin:`-/`Event:`-Einträge in einzelne Datensätze. Für jeden Datensatz werden Uhrzeit, Zeitzone, Kennzahl, Berichtszeitraum und mindestens ein Wert-/Konsensfeld unabhängig geprüft. Explizite Nichtverfügbarkeit bleibt zulässig; Platzhalter wie `TBD` werden abgelehnt. Ein ausdrücklich dokumentierter Zustand ohne relevante Ereignisse bleibt zulässig.

## Vollständiger Dateifingerabdruck-Vergleich

| Datei | Alt Bytes / Zeilen | Neu Bytes / Zeilen | SHA-256 alt | SHA-256 neu |
|---|---:|---:|---|---|
| `Trading_Radar_Alleinauswertung.py` | 888135 / 17179 | 889154 / 17204 | `75af8bdfd6d70bf2f9273bd92c5875133150f9a5b1e80833748133a8afa37036` | `49937a2298b197a52b8e61553100bfb5da0f66aa40c8de74082fd3d692dcf05f` |
| `trading_radar_report_gate.py` | 19326 / 391 | 20802 / 429 | `44123f6fd49c9317a9bdcb88f110c7ad1cf610830a1c21fffcc89fad577f81e7` | `24f8f8335c1bd05b365c8c16dde08b082b93f2eefe2fb426c13e8e5ec2ebc6e5` |
| `tests/test_trading_radar_report_gate.py` | 11737 / 202 | 13027 / 220 | `19c3586a609907b366f61baf23c794175937abec0e18aa296938bb4ccdcea2f7` | `90262075531da8a6c6e04b1686719596754925ca7f7dd47ebed18af03f3cbf63` |
| `tests/test_trading_radar_data_contract.py` | 15286 / 262 | 15924 / 277 | `6dc0beb5eee5c8828806d9949be0e13cd2c9ab09aee97994bd42d747b6231d1b` | `6cb41c2fbe7942aa6f87e6d77d7217fd2f4e0f9c089d47932806658a39036809` |

## Regressionen und technische Checks

- `tests.test_trading_radar_report_gate`: **26 Tests bestanden**; neu: zwei Tests für getrennte Ereignisdatensätze.
- `tests.test_trading_radar_data_contract`: **13 Tests bestanden**; neu: Test, der die physischen Tab-2-Zeilennummern nach Leerzeilen absichert.
- `py_compile` für alle vier geänderten Dateien: **bestanden**.
- Die vollständige Testentdeckung wurde gestartet, überschritt jedoch das Ausführungsfenster. Sie wird deshalb nicht als bestanden ausgewiesen.
- Beim Python-Start erscheint ein unabhängiger Spreadsheet-Runtime-Warmup-Traceback; die oben genannten gezielten Test- und Compile-Befehle endeten dennoch jeweils mit Exitcode 0.
- Kein Live-Zugriff auf Google Sheets, Gemini, SMTP oder GitHub Actions wurde ausgeführt.

## Vollständige Unified-Diffs

### `Trading_Radar_Alleinauswertung.py`

```diff
--- Vorgänger/Trading_Radar_Alleinauswertung.py
+++ Neu/Trading_Radar_Alleinauswertung.py
@@ -186,6 +186,22 @@
 # Scanner-Ausgaben, von dort werden ggf. die Short-Dateien nachgeladen.
 DRIVE_FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
 BEOBACHTUNGSLISTE_DATEI = "einzel_check_beobachtung.json"
+
+
+def _tab2_nonempty_rows_with_source_numbers(values, start_sheet_row=3):
+    """Return nonempty row dictionaries and their original physical Sheet row numbers.
+
+    `values` contains rows beginning at `start_sheet_row`. Blank rows are
+    omitted from the export but never renumber the original source coordinates.
+    """
+    rows = []
+    source_row_numbers = []
+    for source_row_number, source_values in enumerate(values, start=start_sheet_row):
+        if not any(str(cell).strip() for cell in source_values):
+            continue
+        rows.append(source_values)
+        source_row_numbers.append(source_row_number)
+    return rows, source_row_numbers
 GEMINI_HISTORIE_DATEI = "Gemini_Auswertung_Historie.txt"
 GEMINI_A3_HISTORIE_DATEI = ".gemini_einzel_check_historie_a3.json"
 _GEMINI_GESCHLOSSENE_10_5_FINAL = ""  # Autoritative Tab-2-Fakten fuer den finalen Ausgabe-Gate.
@@ -467,13 +483,22 @@
         # tatsächlich befüllte Quellzeile muss dagegen einen Ticker haben;
         # sonst darf der Export nicht mit einer scheinbar passenden Anzahl
         # und einem Fingerabdruck freigegeben werden.
+        # Preserve the physical Google Sheets row number before filtering.
+        # Enumerating the filtered `rows` list would renumber rows after blank
+        # gaps and report the wrong source coordinates in diagnostics.
+        nonempty_source_rows, source_row_numbers = _tab2_nonempty_rows_with_source_numbers(
+            values[2:], start_sheet_row=3
+        )
         rows = [
             dict(zip(headers, row + [""] * max(0, len(headers) - len(row))))
-            for row in values[2:]
-            if any(str(cell).strip() for cell in row)
+            for row in nonempty_source_rows
         ]
-        missing_ticker_rows = [index + 3 for index, row in enumerate(rows)
-                               if not str(row.get("Ticker", "")).strip()]
+
+        missing_ticker_rows = [
+            source_row_numbers[index]
+            for index, row in enumerate(rows)
+            if not str(row.get("Ticker", "")).strip()
+        ]
         if missing_ticker_rows:
             raise RuntimeError(
                 "10.5: Befuellte Tab-2-Quellzeilen ohne Ticker; "
```

### `trading_radar_report_gate.py`

```diff
--- Vorgänger/trading_radar_report_gate.py
+++ Neu/trading_radar_report_gate.py
@@ -175,8 +175,8 @@
     ):
         errors.append("M-07: Thesen-Update benötigt Thesen-ID, positive Version und zulässigen Status oder den expliziten Nachweis, dass keine These aktualisiert wurde.")
 
-    # M-08: every field needs a non-placeholder value. Explicit FEHLT/UNKLAR
-    # remains visible as missing data, rather than being mistaken for a value.
+    # M-08: validate each event record independently. A complete event must
+    # not mask a second event that lacks its own time/zone/metric/period/value.
     events = _section_body(text, 3, SECTION_NAMES[2])
     event_labels = (
         (r"Uhrzeit", r"Zeit"),
@@ -184,17 +184,54 @@
         (r"Kennzahl", r"Indikator"),
         (r"Berichtszeitraum", r"Zeitraum"),
     )
-    def event_field_ok(labels, timezone=False):
+    observation_labels = (r"letzter Wert", r"Vorwert", r"Konsens", r"Istwert")
+    no_relevant_events = _has_any(events, (
+        r"keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?Ereignisse",
+        r"Ereignisradar\s*:\s*keine",
+        r"keine relevanten Ereignisse im geprüften Zeitraum",
+        r"keine relevanten Ereignisse im geprueften Zeitraum",
+    ))
+
+    def event_records(section_body):
+        """Split bullet/numbered event entries; field-only bullets continue a record."""
+        records = []
+        current = []
+        field_only = re.compile(
+            r"(?i)^\s*(?:[-*•]\s+|\d+[.)]\s+)?"
+            r"(?:Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|Berichtszeitraum|Zeitraum|"
+            r"letzter Wert|Vorwert|Konsens|Istwert)\s*[:=]"
+        )
+        bullet = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.*)$")
+        named_event = re.compile(r"(?i)^\s*(?:Ereignis|Termin|Event)\s*[:=]")
+        for line in section_body.splitlines():
+            match = bullet.match(line)
+            starts_record = (
+                (bool(match) and not field_only.match(line))
+                or (bool(named_event.match(line)) and not field_only.match(line))
+            )
+            if starts_record and current:
+                records.append("\n".join(current).strip())
+                current = []
+            current.append(line)
+        if current:
+            records.append("\n".join(current).strip())
+        records = [record for record in records if record]
+        return records or ([section_body.strip()] if section_body.strip() else [])
+
+    def field_ok(record, labels, timezone=False):
         label = r"(?:" + "|".join(labels) + r")"
         matches = list(re.finditer(
             rf"(?im)(?:^|[|;]\s*|\n)\s*{label}\s*[:=]\s*([^|;\r\n]+)",
-            events,
+            record,
         ))
         if not matches:
-            return _is_explicit_unavailable(events, labels)
+            return _is_explicit_unavailable(record, labels)
         for match in matches:
             value = match.group(1).strip().strip(". ")
-            if re.fullmatch(r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?", value):
+            if re.fullmatch(
+                r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?",
+                value,
+            ):
                 continue
             if _MISSING_VALUE_RE.fullmatch(value):
                 return False
@@ -206,23 +243,24 @@
                 return False
         return True
 
-    values_ok = all(
-        event_field_ok(event_labels[i], timezone=(i == 1)) for i in range(4)
-    )
-    # Observation may be on the same line or a subsequent line. Require a
-    # non-placeholder value after the observation label.
-    observation_labels = (r"letzter Wert", r"Vorwert", r"Konsens", r"Istwert")
-    event_observation = any(
-        _label_value(events, (label,)) or _is_explicit_unavailable(events, (label,))
-        for label in observation_labels
-    )
-    no_relevant_events = _has_any(events, (
-        r"keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?Ereignisse",
-        r"Ereignisradar\s*:\s*keine",
-    ))
-    if not no_relevant_events and not (values_ok and event_observation):
-        errors.append("M-08: Ereignisdetails benötigen konkrete Uhrzeit, Zeitzone, Kennzahl, Zeitraum und einen ausgewiesenen Wert/Konsens oder expliziten Fehlstatus; alternativ muss ausdrücklich festgehalten sein, dass keine relevanten Ereignisse vorliegen.")
-
+    if not no_relevant_events:
+        records = event_records(events)
+        for event_index, record in enumerate(records, start=1):
+            values_ok = all(
+                field_ok(record, event_labels[i], timezone=(i == 1))
+                for i in range(4)
+            )
+            observation_ok = any(
+                _label_value(record, (label,)) is not None
+                or _is_explicit_unavailable(record, (label,))
+                for label in observation_labels
+            )
+            if not values_ok or not observation_ok:
+                errors.append(
+                    f"M-08: Ereignisdatensatz {event_index} benötigt eigene Uhrzeit, "
+                    "Zeitzone, Kennzahl, Zeitraum und einen ausgewiesenen Wert/Konsens "
+                    "oder expliziten Fehlstatus."
+                )
     # M-09: candidate labels need usable values. Numeric fields must contain
     # parseable numbers; explicit FEHLT is accepted only as a declared absence,
     # and does not silently become a numeric trade level.
```

### `tests/test_trading_radar_report_gate.py`

```diff
--- Vorgänger/tests/test_trading_radar_report_gate.py
+++ Neu/tests/test_trading_radar_report_gate.py
@@ -63,6 +63,24 @@
         errors = gate.validate_report(text)
         self.assertTrue(any("M-08" in e for e in errors), errors)
 
+
+    def test_m08_validates_each_event_record_independently(self):
+        text = report_fixture().replace(
+            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
+            "- CPI Veröffentlichung | Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.\n"
+            "- Arbeitsmarktdaten | Uhrzeit: TBD | Zeitzone: ET | Kennzahl: NFP | Berichtszeitraum: Oktober 2026 | Konsens: 150000.",
+        )
+        errors = gate.validate_report(text)
+        self.assertTrue(any("M-08: Ereignisdatensatz 2" in e for e in errors), errors)
+
+    def test_m08_accepts_separate_complete_event_records(self):
+        text = report_fixture().replace(
+            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
+            "- CPI Veröffentlichung | Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.\n"
+            "- Arbeitsmarktdaten | Uhrzeit: 08:30 | Zeitzone: ET | Kennzahl: NFP | Berichtszeitraum: Oktober 2026 | letzter Wert: 140000 | Konsens: 150000.",
+        )
+        self.assertFalse(any("M-08" in e for e in gate.validate_report(text)))
+
     def test_m09_requires_actionable_candidate_fields(self):
         text = report_fixture().replace("JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; Regelstatus: valide.", "Ein technischer Kandidat ist vorhanden, aber ohne konkrete Handlungsfelder.")
         errors = gate.validate_report(text)
```

### `tests/test_trading_radar_data_contract.py`

```diff
--- Vorgänger/tests/test_trading_radar_data_contract.py
+++ Neu/tests/test_trading_radar_data_contract.py
@@ -119,6 +119,21 @@
         self.assertIn("Quellen-/Exportzählung abweichend", section)
         self.assertIn("nicht zuordenbar", section)
         self.assertIn("Keine offenen Positionen exportiert", section)
+
+
+    def test_tab2_source_row_numbers_survive_blank_rows(self):
+        radar = load_radar_module()
+        source_rows = [
+            ["AAA", "Closed"],  # sheet row 3
+            ["", ""],           # sheet row 4 is blank and omitted
+            ["", "Closed"],     # sheet row 5 is populated but missing ticker
+            ["BBB", "Closed"],  # sheet row 6
+        ]
+        nonempty, row_numbers = radar._tab2_nonempty_rows_with_source_numbers(
+            source_rows, start_sheet_row=3
+        )
+        self.assertEqual(nonempty, [source_rows[0], source_rows[2], source_rows[3]])
+        self.assertEqual(row_numbers, [3, 5, 6])
 
     def test_report_section_boundaries_are_robust_and_source_has_no_bom(self):
         source_bytes = RADAR.read_bytes()
```

