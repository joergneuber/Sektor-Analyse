"""End-to-end contract tests for the Gemini failure -> fallback -> publication guards.

All network services are replaced or avoided. These tests exercise the real
fallback packager and the real publication guard functions against a temporary
filesystem; no Gemini, SMTP, or Google Drive API call is made.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def position_csv(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Builds the same five identity/status fields used by normal position handling."""
    header = "Ticker;Name;Einstieg;Einstiegsdatum;Status\n"
    return header + "".join(
        f"{ticker};{name};{entry};{date};{status}\n"
        for ticker, name, entry, date, status in rows
    )


def add_position_sources(work: Path, manifest: dict) -> None:
    """Adds a small matching fixture; production holdings are always dynamic."""
    rows = [
        ("TEST01", "Test Company 01", "10,00", "01.11.2026", "Offen"),
        ("TEST02", "Test Company 02", "20,00", "02.11.2026", "Offen"),
        ("TEST03", "Test Company 03", "30,00", "03.11.2026", "Offen"),
    ]
    for logical, filename in (("Offene Positionen+Check.csv", "Offene Positionen+Check.csv"),
                              ("Offene_Positionen.csv", "Offene_Positionen.csv")):
        path = work / filename
        path.write_text(position_csv(rows), encoding="utf-8-sig")
        manifest.setdefault("files", []).append({
            "logical_name": logical, "path": str(path.resolve()), "filename": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })



class TradingRadarFailureChainIntegrationTests(unittest.TestCase):
    def test_missing_section_is_completed_and_merged_without_rewriting_valid_sections(self):
        """A targeted retry fills only the missing section and keeps the original seven."""
        import types

        trade_story = types.ModuleType("trade_story_universum")
        trade_story.write_trade_story_universe = lambda *a, **k: None
        sources = types.ModuleType("quellenwert_vertrag")
        sources.values_match_display = lambda *a, **k: False
        sources.find_exact_labeled_line = lambda *a, **k: None
        sources.extract_four_week_percent = lambda *a, **k: None
        sources.extract_metal_four_week_value = lambda *a, **k: None
        sys.modules.setdefault("trade_story_universum", trade_story)
        sys.modules.setdefault("quellenwert_vertrag", sources)
        radar = load_module(
            "trading_radar_partial_section_retry_under_test",
            ROOT / "Trading_Radar_Alleinauswertung.py",
        )

        required = (
            "1. Trading-Radar",
            "2. Thesen-Update",
            "3. Ereignisradar",
            "4. Assetklassenvergleich",
            "5. Technische Kandidaten",
            "6. Portfolio & Risiko",
            "7. MSCI-Vergleich",
            "8. Daten-/Prüfanhang",
        )
        first_response = "\n\n".join(
            f"{heading}\nOriginalinhalt für Abschnitt {required.index(heading) + 1}"
            for heading in required if heading != "2. Thesen-Update"
        )
        second_response = "2. Thesen-Update\nGezielte Ergänzung der fehlenden Thesen."
        responses = iter([
            types.SimpleNamespace(text=first_response),
            types.SimpleNamespace(text=second_response),
        ])
        calls = []

        with tempfile.TemporaryDirectory() as td:
            previous = Path.cwd()
            try:
                os.chdir(td)
                radar._offene_positionen_quellblock = lambda path: "OFFENE POSITIONEN TEST"
                radar.lade_offenen_positionen_check_tab2 = lambda: "TAB-2 TESTDATEN"
                radar._gemini_tokenzahl = lambda *a, **k: 100
                radar.GEMINI_MODELLREIHENFOLGE = ("model-a",)
                radar.GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN = 1
                radar._gemini_503_failed_models_by_request.clear()
                radar._gemini_503_failures_this_run.clear()

                def fake_send(*args, **kwargs):
                    calls.append((args[5] if len(args) > 5 else kwargs.get("label"), args[2]))
                    return next(responses)

                radar._gemini_generate_content_quota_safe = fake_send
                radar._speichere_gemini_input_manifest = lambda *_: None

                from contextlib import redirect_stdout
                from io import StringIO
                with redirect_stdout(StringIO()) as output:
                    result = radar._gemini_trading_radar_eigenstaendig(
                        client=object(), preferred_modell="model-a",
                        eingabedateien={}, bestehende_auswertung="",
                    )

                self.assertIsInstance(result, str)
                for heading in required:
                    self.assertIn(heading, result)
                self.assertIn("Originalinhalt für Abschnitt 1", result)
                self.assertIn("Originalinhalt für Abschnitt 4", result)
                self.assertIn("Gezielte Ergänzung der fehlenden Thesen.", result)
                self.assertEqual(result.count("1. Trading-Radar"), 1)
                self.assertEqual(result.count("4. Assetklassenvergleich"), 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "EIGENSTAENDIG_ERZEUGT")
                self.assertIn("ABSCHNITTS-RETRY", calls[1][1][0])
                self.assertIn("GEMINI_RESPONSE_META", output.getvalue())
            finally:
                os.chdir(previous)

    def test_fallback_package_is_built_from_exact_manifest_after_failure(self):
        """The real packager creates an auditable ZIP from the same manifest inputs."""
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
            master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
            master.write_text("# Master test instruction\n", encoding="utf-8")
            source = work / "Makro_Briefing(2026-10-26).txt"
            source.write_text("TESTDATEN: keine echten Marktdaten\n", encoding="utf-8")
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            manifest = {
                "master_instruction": master.name,
                "master_sha256": sha(master),
                "files": [{"path": str(source), "sha256": sha(source)}],
            }
            add_position_sources(work, manifest)
            (work / ".gemini_input_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            run = subprocess.run(
                [sys.executable, "chatgpt_fallback_paket.py"],
                cwd=work, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            zips = list(work.glob("CHATGPT_FALLBACK_*.zip"))
            self.assertEqual(len(zips), 1)
            with zipfile.ZipFile(zips[0]) as archive:
                names = set(archive.namelist())
                self.assertIn(master.name, names)
                self.assertIn(source.name, names)
                self.assertIn("GEMINI_INPUT_MANIFEST.json", names)
                self.assertIn("CHATGPT_FALLBACK_ANWEISUNG.txt", names)
                self.assertIn("CHATGPT_FALLBACK_BERICHTSSTRUKTUR.txt", names)
                report_spec = archive.read("CHATGPT_FALLBACK_BERICHTSSTRUKTUR.txt").decode("utf-8")
                required_headings = (
                    "DOKUMENTENKOPF",
                    "BLICK AUF WICHTIGE INDIZES",
                    "LIVE-PERFORMANCE VS. MSCI WORLD",
                    "KURZ-ZUSAMMENFASSUNG",
                    "RISIKO-WATCH",
                    "WOCHENAUSBLICK",
                    "1. MARKTUMFELD & GLOBALE RISIKOLAGE",
                    "2. MAKRO-ZUKUNFTSSZENARIO",
                    "3. TRENDFOLGE-SETUPS",
                    "4. TRENDWENDE-SETUPS",
                    "5. HEBELTRADER-SETUPS",
                    "6. SHORT-SETUPS",
                    "7. EDELMETALLE-SETUPS",
                    "OFFENE POSITIONEN",
                    "GESCHLOSSENE POSITIONEN",
                    "METHODIK & LESEHILFE",
                )
                for heading in required_headings:
                    self.assertIn(heading, report_spec)
                self.assertLess(report_spec.index("DOKUMENTENKOPF"),
                                report_spec.index("BLICK AUF WICHTIGE INDIZES"))
                self.assertLess(report_spec.index("WOCHENAUSBLICK"),
                                report_spec.index("1. MARKTUMFELD & GLOBALE RISIKOLAGE"))
                self.assertIn("Bei einem Widerspruch gilt immer die Master-Spezifikation.", report_spec)
                self.assertIn("Keine Markdown-Syntax", report_spec)
                self.assertIn("FALLBACK_MANIFEST.txt", report_spec)
                self.assertIn("FALLBACK_MANIFEST.txt", names)
                self.assertNotIn("POSITIONSABGLEICH.txt", names)
                fallback_manifest = archive.read("FALLBACK_MANIFEST.txt").decode("utf-8")
                self.assertIn("QUELLE=GEMINI-INPUT-MANIFEST", fallback_manifest)
                self.assertIn("POSITIONSABGLEICH – Fallback-Sicherheitsprüfung", fallback_manifest)
                self.assertIn("Offene Positionen+Check.csv", names)
                self.assertIn("Offene_Positionen.csv", names)
                self.assertEqual(archive.read("Offene Positionen+Check.csv"),
                                 (work / "Offene Positionen+Check.csv").read_bytes())

    def test_fallback_reports_position_differences_without_selecting_backend_as_current(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
            master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
            master.write_text("# Master\n", encoding="utf-8")
            base = work / "Makro.txt"
            base.write_text("fixture\n", encoding="utf-8")
            manifest = {"master_instruction": master.name, "created_at": "2026-11-04T20:00:00+00:00", "files": [{"path": str(base), "sha256": hashlib.sha256(base.read_bytes()).hexdigest()}]}
            add_position_sources(work, manifest)
            check = work / "Offene Positionen+Check.csv"
            backend = work / "Offene_Positionen.csv"
            check_rows = [(f"T{i:02d}", f"Company {i:02d}", str(10+i), f"2026-11-{i+1:02d}", "Offen") for i in range(6)]
            backend_rows = [(f"T{i:02d}", f"Company {i:02d}", str(10+i), f"2026-11-{i+1:02d}", "Offen") for i in range(5)]
            backend_rows.append(("BACKEND_ONLY", "Backend Only Ltd", "99,00", "2026-11-20", "Offen"))
            check.write_text(position_csv(check_rows), encoding="utf-8-sig")
            backend.write_text(position_csv(backend_rows), encoding="utf-8-sig")
            for entry in manifest["files"]:
                path = Path(entry["path"])
                entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            (work / ".gemini_input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            run = subprocess.run([sys.executable, "chatgpt_fallback_paket.py"], cwd=work, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            package = next(work.glob("CHATGPT_FALLBACK_*.zip"))
            with zipfile.ZipFile(package) as archive:
                self.assertNotIn("POSITIONSABGLEICH.txt", archive.namelist())
                report = archive.read("FALLBACK_MANIFEST.txt").decode("utf-8")
                self.assertIn("ERGEBNIS: ABWEICHUNG", report)
                self.assertIn("Nur/zusätzlich in Drive-Checkquelle: T05 | company 05 | Einstieg=15 | Einstiegsdatum=2026-11-06", report)
                self.assertIn("Nur/zusätzlich in Backend-CSV: BACKEND_ONLY | backend only ltd | Einstieg=99 | Einstiegsdatum=2026-11-20", report)
                self.assertIn("KEIN BESTAND WIRD AUTOMATISCH ALS AKTUELL AUSGEWÄHLT", report)
                self.assertIn("Offene Positionen Drive-Checkquelle: 6", report)
                self.assertIn("Offene Positionen+Check.csv", archive.namelist())

    def test_position_reader_accepts_variable_and_empty_daily_holdings(self):
        """The number of open positions is dynamic; zero is a valid daily state."""
        import importlib.util

        module_path = ROOT / "chatgpt_fallback_paket.py"
        spec = importlib.util.spec_from_file_location("fallback_positions_dynamic_test", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            for count in (0, 1, 6, 18):
                csv_path = work / f"positions_{count}.csv"
                rows = [(f"T{i:02d}", f"Company {i:02d}", str(i+1), f"2026-10-{(i%28)+1:02d}", "Offen") for i in range(count)]
                csv_path.write_text(position_csv(rows), encoding="utf-8-sig")
                positions = module._read_open_positions(csv_path, "Testbestand")
                self.assertEqual(len(positions), count)

    def test_fallback_is_created_when_optional_backend_csv_is_missing(self):
        """The mandatory Drive check CSV remains usable; missing backend is explicit, not fatal."""
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
            master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
            master.write_text("# Master\n", encoding="utf-8")
            check = work / "Offene Positionen+Check.csv"
            check.write_text(position_csv([
                ("AAA", "Alpha AG", "10,00", "01.11.2026", "Offen"),
                ("BBB", "Beta AG", "20,00", "02.11.2026", "Offen"),
            ]), encoding="utf-8-sig")
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            manifest = {
                "master_instruction": master.name,
                "master_sha256": sha(master),
                "created_at": "2026-11-05T04:00:00+00:00",
                "files": [{
                    "logical_name": "Offene Positionen+Check.csv",
                    "filename": check.name,
                    "path": str(check.resolve()),
                    "sha256": sha(check),
                }],
            }
            (work / ".gemini_input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            run = subprocess.run(
                [sys.executable, "chatgpt_fallback_paket.py"],
                cwd=work, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            package = next(work.glob("CHATGPT_FALLBACK_*.zip"))
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
                self.assertIn(check.name, names)
                self.assertNotIn("Offene_Positionen.csv", names)
                report = archive.read("FALLBACK_MANIFEST.txt").decode("utf-8")
                self.assertIn("ERGEBNIS: NICHT VERGLEICHBAR", report)
                self.assertIn("Backend-CSV fehlt", report)
                self.assertIn("Drive-Abrufzeitpunkt: im Quellmanifest nicht separat erfasst", report)
                self.assertIn("Gemini-Input-Snapshot erstellt (UTC): 2026-11-05T04:00:00+00:00", report)
                self.assertIn("kein belegter Drive-Abrufzeitpunkt", report)

    def test_fallback_rejects_empty_or_unknown_position_status(self):
        """Only exact Offen is active; malformed statuses and missing identity fields fail closed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fallback_status_validation_test", ROOT / "chatgpt_fallback_paket.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            for status in ("", "unbekannt", "Aktiv"):
                path.write_text(position_csv([("AAA", "Alpha AG", "10,00", "01.11.2026", status)]), encoding="utf-8-sig")
                with self.subTest(status=status):
                    with self.assertRaises(SystemExit):
                        module._read_open_positions(path, "Testbestand")
            path.write_text("Ticker;Name;Einstieg;Status\nAAA;Alpha AG;10,00;Offen\n", encoding="utf-8-sig")
            with self.assertRaisesRegex(SystemExit, "Einstiegsdatum"):
                module._read_open_positions(path, "Testbestand")
            path.write_text(position_csv([("ANLEITUNG", "Vorlage", "", "", "")]), encoding="utf-8-sig")
            self.assertEqual(module._read_open_positions(path, "Testbestand"), set())

    def test_same_ticker_with_different_entry_dates_is_compared_as_two_positions(self):
        """Same ticker is not the identity: each complete position key is compared separately."""
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
            master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
            master.write_text("# Master\n", encoding="utf-8")
            check = work / "Offene Positionen+Check.csv"
            backend = work / "Offene_Positionen.csv"
            check.write_text(position_csv([
                ("AAA", "Alpha AG", "10,00", "01.11.2026", "Offen"),
                ("AAA", "Alpha AG", "12,00", "03.11.2026", "Offen"),
                ("BBB", "Beta AG", "20,00", "02.11.2026", "Offen"),
            ]), encoding="utf-8-sig")
            backend.write_text(position_csv([
                ("AAA", "Alpha AG", "10.00", "01.11.2026", "Offen"),
                ("BBB", "Beta AG", "20,00", "02.11.2026", "Offen"),
            ]), encoding="utf-8-sig")
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            manifest = {
                "master_instruction": master.name,
                "master_sha256": sha(master),
                "files": [
                    {"logical_name": "Offene Positionen+Check.csv", "path": str(check.resolve()), "filename": check.name, "sha256": sha(check)},
                    {"logical_name": "Offene_Positionen.csv", "path": str(backend.resolve()), "filename": backend.name, "sha256": sha(backend)},
                ],
            }
            (work / ".gemini_input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            run = subprocess.run(
                [sys.executable, "chatgpt_fallback_paket.py"], cwd=work,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            package = next(work.glob("CHATGPT_FALLBACK_*.zip"))
            with zipfile.ZipFile(package) as archive:
                report = archive.read("FALLBACK_MANIFEST.txt").decode("utf-8")
            self.assertIn("ERGEBNIS: ABWEICHUNG", report)
            self.assertIn("Offene Positionen Drive-Checkquelle: 3", report)
            self.assertIn("Offene Positionen Backend-CSV: 2", report)
            self.assertIn("AAA | alpha ag | Einstieg=12 | Einstiegsdatum=03.11.2026", report)
            self.assertIn("Ticker", report)  # Ticker is explicitly part of the position key.

    def test_position_key_normalization_matches_normal_position_manager(self):
        """Name normalization and German/US numeric formatting follow parse_number/_position_key."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fallback_position_key_normalization_test", ROOT / "chatgpt_fallback_paket.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            path.write_text(position_csv([
                (" aaa ", "  Example   Company  ", "1.234,50", "01.11.2026", "Offen"),
            ]), encoding="utf-8-sig")
            first = module._read_open_positions(path, "Testbestand")
            path.write_text(position_csv([
                ("AAA", "example company", "1234.5", "01.11.2026", "Offen"),
            ]), encoding="utf-8-sig")
            second = module._read_open_positions(path, "Testbestand")
            self.assertEqual(first, second)

    def test_position_number_normalization_matches_parse_number_numeric_finiteness(self):
        """Numeric objects follow parse_number's finite check; text keeps its text-path behavior."""
        import importlib.util
        import math
        spec = importlib.util.spec_from_file_location(
            "fallback_position_number_finiteness_test", ROOT / "chatgpt_fallback_paket.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(module._parse_position_number(value))

        self.assertEqual(module._parse_position_number(1234.5), 1234.5)
        self.assertEqual(module._parse_position_number(True), 1.0)

        # The production parse_number() checks finiteness in its numeric-object
        # branch, but returns float(text) directly in its string branch.
        self.assertTrue(math.isinf(module._parse_position_number("inf")))
        self.assertTrue(math.isinf(module._parse_position_number("1e309")))
        self.assertIsNone(module._parse_position_number("nan"))

        try:
            import numpy as np
        except ImportError:
            np = None
        if np is not None:
            self.assertIsNone(module._parse_position_number(np.float64("inf")))
            self.assertIsNone(module._parse_position_number(np.float32("nan")))
            self.assertEqual(module._parse_position_number(np.int64(7)), 7.0)
            self.assertIsNone(module._parse_position_number(np.timedelta64(1, "D")))
            self.assertEqual(module._parse_position_number(np.array(7)), 7.0)

    def test_identical_duplicate_position_key_is_rejected(self):
        """The normal position manager rejects duplicate complete position keys too."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fallback_duplicate_position_key_test", ROOT / "chatgpt_fallback_paket.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            row = ("AAA", "Alpha AG", "10,00", "01.11.2026", "Offen")
            path.write_text(position_csv([row, row]), encoding="utf-8-sig")
            with self.assertRaisesRegex(SystemExit, "doppelter identischer Positionsschlüssel"):
                module._read_open_positions(path, "Testbestand")

    def test_fallback_packager_rejects_input_changed_after_manifest(self):
        """A stale/changed source must stop packaging, not create a misleading fallback."""
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
            master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
            master.write_text("# Master\n", encoding="utf-8")
            source = work / "Briefing.txt"
            source.write_text("original\n", encoding="utf-8")
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            manifest = {
                "master_instruction": master.name,
                "master_sha256": sha(master),
                "files": [{"path": str(source), "sha256": sha(source)}],
            }
            add_position_sources(work, manifest)
            (work / ".gemini_input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            source.write_text("changed after Gemini input\n", encoding="utf-8")
            run = subprocess.run(
                [sys.executable, "chatgpt_fallback_paket.py"],
                cwd=work, capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("seit Manifest-Erstellung veraendert", run.stdout + run.stderr)
            self.assertEqual(list(work.glob("CHATGPT_FALLBACK_*.zip")), [])

    def test_drive_guard_blocks_failed_or_skipped_radar_even_if_report_exists(self):
        """The real Drive gate never authorizes a technical fallback as normal report."""
        drive = load_module("upload_to_drive_failure_chain_test", ROOT / "upload_to_drive.py")
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "Trading_Radar(2026-10-26).txt"
            report.write_text("TRADING_RADAR_FACHLICH_FINAL=FINAL\n", encoding="utf-8")
            cases = [
                ("TECHNISCHER_FALLBACK", "FINAL", str(report), "success", False),
                ("SUCCESS", "FINAL", str(report), "failure", False),
                ("SUCCESS", "FINAL", str(report), "success", True),
                ("SUCCESS", "NICHT_FINAL", str(report), "success", False),
            ]
            for status, finality, path, gate, skip in cases:
                with self.subTest(status=status, finality=finality, gate=gate, skip=skip):
                    self.assertIsNone(drive.validierter_trading_radar_report(
                        status, finality, path, gate, skip_upload=skip
                    ))
            self.assertEqual(
                drive.validierter_trading_radar_report(
                    "SUCCESS", "FINAL", str(report), "success", skip_upload=False
                ),
                str(report),
            )

    def test_mail_guard_blocks_technical_fallback_before_smtp_send(self):
        """The real mail entry refuses a failed Gemini status before any SMTP send."""
        mail = load_module("send_mail_failure_chain_test", ROOT / "send_mail.py")
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "Trading_Radar(2026-10-26).txt"
            report.write_text(
                "1. Trading-Radar\nTRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n",
                encoding="utf-8",
            )
            env = {
                "SMTP_HOST": "localhost", "SMTP_PORT": "587", "SMTP_USER": "test",
                "SMTP_PASSWORD": "test", "MAIL_EMPFAENGER": "test@example.invalid",
                "TRADING_RADAR_DATEI": str(report),
                "TRADING_RADAR_STATUS": "TECHNISCHER_FALLBACK",
                "TRADING_RADAR_FACHLICH_FINAL": "NICHT_FINAL",
                "TRADING_RADAR_REPORT_CHECK_OUTCOME": "failure",
            }
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(mail.smtplib, "SMTP") as smtp:
                with self.assertRaisesRegex(ValueError, "Reportversand blockiert"):
                    mail.main()
                smtp.assert_not_called()


    def test_503_failure_writes_real_status_then_builds_fallback_package(self):
        """Chain: production Gemini failure -> status file -> manifest-based fallback ZIP."""
        # Load the production module with the same lightweight optional-module stubs
        # used by the dedicated failure-contract tests.
        import types
        trade_story = types.ModuleType("trade_story_universum")
        trade_story.write_trade_story_universe = lambda *a, **k: None
        sources = types.ModuleType("quellenwert_vertrag")
        sources.values_match_display = lambda *a, **k: False
        sources.find_exact_labeled_line = lambda *a, **k: None
        sources.extract_four_week_percent = lambda *a, **k: None
        sources.extract_metal_four_week_value = lambda *a, **k: None
        sys.modules.setdefault("trade_story_universum", trade_story)
        sys.modules.setdefault("quellenwert_vertrag", sources)
        spec = importlib.util.spec_from_file_location(
            "trading_radar_failure_chain_real_production", ROOT / "Trading_Radar_Alleinauswertung.py"
        )
        radar = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(radar)

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            previous = Path.cwd()
            try:
                os.chdir(work)
                radar._offene_positionen_quellblock = lambda path: "OFFENE POSITIONEN TEST"
                radar.lade_offenen_positionen_check_tab2 = lambda: "TAB-2 TESTDATEN"
                radar._gemini_tokenzahl = lambda *a, **k: 100
                radar.GEMINI_MODELLREIHENFOLGE = ("model-a",)
                radar.GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN = 1
                radar._gemini_503_failed_models_by_request.clear()
                radar._gemini_503_failures_this_run.clear()
                radar._gemini_generate_content_quota_safe = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("503 UNAVAILABLE: simulated overload")
                )
                with patch.object(radar.time, "sleep", lambda *_: None):
                    from contextlib import redirect_stdout
                    from io import StringIO
                    with redirect_stdout(StringIO()):
                        result = radar._gemini_trading_radar_eigenstaendig(
                            client=object(), preferred_modell="model-a",
                            eingabedateien={}, bestehende_auswertung="",
                        )
                self.assertIsNone(result)
                self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "REQUEST_FEHLER")
                status_files = list(work.glob("Gemini_Fallback_Status_*.txt"))
                self.assertEqual(len(status_files), 1)
                status_text = status_files[0].read_text(encoding="utf-8")
                self.assertIn("GEMINI_STATUS=TECHNISCHER_FALLBACK", status_text)
                self.assertIn("GEMINI_AUSWERTUNG_DATEI=KEINE", status_text)
                self.assertIn("503 UNAVAILABLE", status_text)

                # Build the same kind of manifest-backed fallback artifact used by
                # the workflow. Only temporary fixture data is packaged.
                shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
                master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
                master.write_text("# Master instruction fixture\n", encoding="utf-8")
                source = work / "Makro_Briefing(2026-10-26).txt"
                source.write_text("Fixture input only\n", encoding="utf-8")
                sha = lambda file_path: hashlib.sha256(file_path.read_bytes()).hexdigest()
                manifest = {
                    "master_instruction": master.name,
                    "master_sha256": sha(master),
                    "files": [{"path": str(source), "sha256": sha(source)}],
                }
                add_position_sources(work, manifest)
                (work / ".gemini_input_manifest.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                packaged = subprocess.run(
                    [sys.executable, "chatgpt_fallback_paket.py"],
                    cwd=work, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(packaged.returncode, 0, packaged.stdout + packaged.stderr)
                zips = list(work.glob("CHATGPT_FALLBACK_*.zip"))
                self.assertEqual(len(zips), 1)
                with zipfile.ZipFile(zips[0]) as archive:
                    names = set(archive.namelist())
                    self.assertIn(source.name, names)
                    self.assertIn("GEMINI_INPUT_MANIFEST.json", names)
            finally:
                os.chdir(previous)

    def test_workflow_wires_failure_to_fallback_and_blocks_normal_publication(self):
        """Static integration contract: fallback package/mail and publication gates are wired."""
        workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8-sig")
        self.assertIn("id: trading-radar", workflow)
        self.assertIn("python Trading_Radar_Alleinauswertung.py", workflow)
        self.assertIn("python chatgpt_fallback_paket.py", workflow)
        self.assertIn("CHATGPT_FALLBACK_ERZEUGT == 'true'", workflow)
        self.assertIn("Technischen Gemini-Fallback per E-Mail melden", workflow)
        self.assertIn("msg.add_attachment", workflow)
        self.assertIn("CHATGPT_FALLBACK_*.zip", workflow)
        self.assertIn("Fallback-ZIP fehlt", workflow)
        self.assertIn("SKIP_TRADING_RADAR_UPLOAD=true", workflow)
        self.assertIn("SKIP_AUSWERTUNG_UPLOAD=true", workflow)
        # Normal email requires a successful radar step/status; technical fallback
        # uses a separate mail step and cannot fall through to normal publication.
        self.assertIn("steps.trading-radar.outcome == 'success'", workflow)
        self.assertIn("steps.trading-radar.outputs.status == 'SUCCESS'", workflow)
        self.assertIn("steps.trading-radar-report-check.outcome == 'success'", workflow)
        self.assertIn("Trading-Radar technische Fehlerkette – Pflicht-Integrationstest", workflow)
        self.assertIn("python -m unittest tests.test_trading_radar_failure_chain_integration", workflow)


    def test_orchestrated_single_decision_503_manifest_package_and_publication_lock(self):
        """Ein durchgehender lokaler Lauf: 503 -> Status/Manifest -> genau ein ZIP -> Fallback statt Normalpublikation."""
        import types
        from contextlib import redirect_stdout
        from io import StringIO

        # Die Produktionsmodule werden echt geladen; nur Gemini/SMTP/Drive-Netzwerk
        # wird durch deterministische Fakes bzw. die reinen Freigabegates ersetzt.
        trade_story = types.ModuleType("trade_story_universum")
        trade_story.write_trade_story_universe = lambda *a, **k: None
        sources = types.ModuleType("quellenwert_vertrag")
        sources.values_match_display = lambda *a, **k: False
        sources.find_exact_labeled_line = lambda *a, **k: None
        sources.extract_four_week_percent = lambda *a, **k: None
        sources.extract_metal_four_week_value = lambda *a, **k: None
        sys.modules.setdefault("trade_story_universum", trade_story)
        sys.modules.setdefault("quellenwert_vertrag", sources)
        spec = importlib.util.spec_from_file_location(
            "trading_radar_orchestrated_failure_chain", ROOT / "Trading_Radar_Alleinauswertung.py"
        )
        radar = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(radar)

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            previous = Path.cwd()
            try:
                os.chdir(work)
                master = work / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"
                master.write_text("# Master instruction fixture\n", encoding="utf-8")
                source = work / "current_input_fixture.txt"
                source.write_text("Current run fixture input; no market data.\n", encoding="utf-8")
                stale = work / "Trading_Radar(2026-10-25).txt"
                stale.write_text(
                    "STALE_REPORT_MUST_NOT_BE_CURRENT\n"
                    "TRADING_RADAR_FACHLICH_FINAL=FINAL\n",
                    encoding="utf-8",
                )

                radar.ANWEISUNG_DATEI = master.name
                radar._offene_positionen_quellblock = lambda path: "OFFENE POSITIONEN TEST"
                radar.lade_offenen_positionen_check_tab2 = lambda: "TAB-2 TESTDATEN"
                radar._gemini_tokenzahl = lambda *a, **k: 100
                radar.GEMINI_MODELLREIHENFOLGE = ("model-a", "model-b")
                radar.GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN = 2
                radar.GEMINI_503_POOL_BACKOFF_SECONDS = 0
                radar.time.sleep = lambda *_: None
                radar._gemini_503_failed_models_by_request.clear()
                radar._gemini_503_failures_this_run.clear()
                calls = []

                def fake_send(client, model, contents, config, token_count, label, **kwargs):
                    calls.append(model)
                    if model == "model-a":
                        raise RuntimeError("503 UNAVAILABLE: simulated overload")
                    raise RuntimeError("503 UNAVAILABLE: second model overloaded")

                radar._gemini_generate_content_quota_safe = fake_send
                radar._speichere_gemini_input_manifest({"fixture": str(source)})
                with redirect_stdout(StringIO()):
                    result = radar._gemini_trading_radar_eigenstaendig(
                        client=object(), preferred_modell="model-a",
                        eingabedateien={"fixture": str(source)}, bestehende_auswertung="",
                    )

                self.assertIsNone(result)
                self.assertEqual(calls, ["model-a", "model-b"], "Modellwechsel muss im selben Lauf belegt sein")
                self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "REQUEST_FEHLER")
                status_files = list(work.glob("Gemini_Fallback_Status_*.txt"))
                self.assertEqual(len(status_files), 1, "genau ein aktueller Statusmarker")
                status_text = status_files[0].read_text(encoding="utf-8")
                self.assertIn("GEMINI_STATUS=TECHNISCHER_FALLBACK", status_text)
                self.assertIn("GEMINI_AUSWERTUNG_DATEI=KEINE", status_text)
                self.assertIn("503 UNAVAILABLE", status_text)

                manifest_path = work / ".gemini_input_manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(len(manifest["files"]), 1)
                self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(manifest["files"][0]["path"], str(source.resolve()))

                add_position_sources(work, manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                shutil.copy2(ROOT / "chatgpt_fallback_paket.py", work / "chatgpt_fallback_paket.py")
                packaged = subprocess.run(
                    [sys.executable, "chatgpt_fallback_paket.py"],
                    cwd=work, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(packaged.returncode, 0, packaged.stdout + packaged.stderr)
                packages = list(work.glob("CHATGPT_FALLBACK_*.zip"))
                self.assertEqual(len(packages), 1, "es muss genau ein Fallback-ZIP entstehen")
                with zipfile.ZipFile(packages[0]) as archive:
                    names = set(archive.namelist())
                    self.assertIn(source.name, names)
                    self.assertIn("GEMINI_INPUT_MANIFEST.json", names)
                    self.assertNotIn(stale.name, names)
                    self.assertNotIn("Trading_Radar(2026-10-26).txt", names)

                # Eine einzige maßgebliche Entscheidung: technischer Fallback.
                # Der Normal-Mailpfad und die Drive-Freigabe müssen unabhängig
                # voneinander fail-closed bleiben, obwohl ein alter FINAL-Bericht liegt.
                mail = load_module("send_mail_orchestrated_failure_chain", ROOT / "send_mail.py")
                env = {
                    "SMTP_HOST": "localhost", "SMTP_PORT": "587", "SMTP_USER": "test",
                    "SMTP_PASSWORD": "test", "MAIL_EMPFAENGER": "test@example.invalid",
                    "TRADING_RADAR_DATEI": str(stale),
                    "TRADING_RADAR_STATUS": "TECHNISCHER_FALLBACK",
                    "TRADING_RADAR_FACHLICH_FINAL": "FINAL",
                    "TRADING_RADAR_REPORT_CHECK_OUTCOME": "success",
                }
                with patch.dict(os.environ, env, clear=False), patch.object(mail.smtplib, "SMTP") as smtp:
                    with self.assertRaisesRegex(ValueError, "Reportversand blockiert"):
                        mail.main()
                    smtp.assert_not_called()

                drive = load_module("upload_to_drive_orchestrated_failure_chain", ROOT / "upload_to_drive.py")
                self.assertIsNone(drive.validierter_trading_radar_report(
                    "TECHNISCHER_FALLBACK", "FINAL", str(stale), "success", skip_upload=False
                ))
                self.assertEqual(len(packages), 1)
                self.assertEqual(len(status_files), 1)

                workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8-sig")
                self.assertIn("env.CHATGPT_FALLBACK_ERZEUGT != 'true'", workflow)
                self.assertIn("steps.trading-radar.outputs.status == 'SUCCESS'", workflow)
                self.assertIn("SKIP_TRADING_RADAR_UPLOAD=true", workflow)
                self.assertIn("SKIP_AUSWERTUNG_UPLOAD=true", workflow)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
