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


class TradingRadarFailureChainIntegrationTests(unittest.TestCase):
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
                self.assertIn("FALLBACK_MANIFEST.txt", names)
                self.assertIn("QUELLE=GEMINI-INPUT-MANIFEST",
                              archive.read("FALLBACK_MANIFEST.txt").decode("utf-8"))

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
