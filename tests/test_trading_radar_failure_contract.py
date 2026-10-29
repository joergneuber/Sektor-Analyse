"""Regressionen fuer Gemini-Ausfallstatus und die alleinige produktive Pipeline.

Alle API-Fehler werden deterministisch simuliert; kein echter Gemini-Aufruf.
"""
from contextlib import redirect_stdout
from io import StringIO
import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
RADAR = ROOT / "Trading_Radar_Alleinauswertung.py"
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"


def load_radar_module():
    trade_story = types.ModuleType("trade_story_universum")
    trade_story.write_trade_story_universe = lambda *a, **k: None
    sources = types.ModuleType("quellenwert_vertrag")
    sources.values_match_display = lambda *a, **k: False
    sources.find_exact_labeled_line = lambda *a, **k: None
    sources.extract_four_week_percent = lambda *a, **k: None
    sources.extract_metal_four_week_value = lambda *a, **k: None
    import sys
    sys.modules.setdefault("trade_story_universum", trade_story)
    sys.modules.setdefault("quellenwert_vertrag", sources)
    spec = importlib.util.spec_from_file_location("trading_radar_failure_contract_under_test", RADAR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TradingRadarFailureContractTests(unittest.TestCase):
    def test_gemini_503_and_429_never_become_successful_radar_reports(self):
        """API-Fehler muessen NICHT_FINAL/REQUEST_FEHLER setzen und Fallback markieren."""
        for api_error in (
            "503 UNAVAILABLE: high demand",
            "429 RESOURCE_EXHAUSTED: requests per day quota exceeded",
        ):
            with self.subTest(api_error=api_error), tempfile.TemporaryDirectory() as temp_dir:
                radar = load_radar_module()
                old_cwd = os.getcwd()
                os.chdir(temp_dir)
                try:
                    calls = []
                    status_writes = []
                    radar._offene_positionen_quellblock = lambda path: "OFFENE POSITIONEN TEST"
                    radar.lade_offenen_positionen_check_tab2 = lambda: "TAB-2 TESTDATEN"
                    radar._gemini_tokenzahl = lambda *a, **k: 100
                    radar.GEMINI_MODELLREIHENFOLGE = ("gemini-test-model",)
                    radar.GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN = 1

                    def fail_send(*args, **kwargs):
                        calls.append((args, kwargs))
                        raise RuntimeError(api_error)

                    radar._gemini_generate_content_quota_safe = fail_send
                    radar._gemini_fallback_status_datei = lambda action, message=None: status_writes.append((action, message))
                    with redirect_stdout(StringIO()):
                        result = radar._gemini_trading_radar_eigenstaendig(
                            client=object(),
                            preferred_modell="gemini-test-model",
                            eingabedateien={},
                            bestehende_auswertung="",
                        )
                    self.assertIsNone(result)
                    self.assertIsNone(radar._GEMINI_TRADING_RADAR_TEXT)
                    self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "REQUEST_FEHLER")
                    self.assertEqual(len(calls), 1, "ein Fehler darf keine unprotokollierte Doppel-Auswertung erzeugen")
                    self.assertTrue(any(action == "write" for action, _ in status_writes))
                    self.assertTrue(Path("Trading_Radar_Fehler.log").is_file())
                finally:
                    os.chdir(old_cwd)


    def test_standalone_radar_retries_503_on_next_model_and_returns_one_report(self):
        """Integration: echter Radar-Entry der Funktion wechselt nach 503 einmal das Modell."""
        with tempfile.TemporaryDirectory() as temp_dir:
            radar = load_radar_module()
            old_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
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
                report = "\n".join((
                    "1. Trading-Radar", "2. Thesen-Update", "3. Ereignisradar",
                    "4. Assetklassenvergleich", "5. Technische Kandidaten",
                    "6. Portfolio & Risiko", "7. MSCI-Vergleich", "8. Daten-/Prüfanhang",
                ))
                class FakeModels:
                    def generate_content(self, model, contents, config):
                        calls.append(model)
                        if model == "model-a":
                            raise RuntimeError("503 UNAVAILABLE: high demand")
                        return types.SimpleNamespace(text=report)
                client = types.SimpleNamespace(models=FakeModels())
                radar.types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
                radar._gemini_quota_waehlen = lambda preferred, tokens, **kwargs: (preferred, 0.0)
                radar._gemini_quota_registriere_sendung = lambda model, tokens: (model, tokens)
                radar._gemini_quota_rollback_sendung = lambda reservation: None

                with redirect_stdout(StringIO()) as output:
                    result = radar._gemini_trading_radar_eigenstaendig(
                        client=client, preferred_modell="model-a",
                        eingabedateien={}, bestehende_auswertung="",
                    )
                self.assertEqual(calls, ["model-a", "model-b"])
                self.assertEqual(result, report)
                self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "EIGENSTAENDIG_ERZEUGT")
                self.assertEqual(output.getvalue().count("TRADING_RADAR_STATUS=EIGENSTAENDIG_ERZEUGT"), 1)
                self.assertIn("Modellwechsel=model-a->model-b", output.getvalue())
            finally:
                os.chdir(old_cwd)

    def test_standalone_radar_switches_model_after_transient_429_and_timeout(self):
        """429/Timeout nach innerem Retry: Modellwechsel; Tagesquota bleibt terminal."""
        transient_cases = (
            ("429 RESOURCE_EXHAUSTED: requests per minute quota exceeded", "quota_temporär"),
            ("Read timed out while waiting for Gemini", "netzwerk"),
        )
        for error_text, expected_category in transient_cases:
            with self.subTest(error_text=error_text), tempfile.TemporaryDirectory() as temp_dir:
                radar = load_radar_module()
                self.assertEqual(radar.analysiere_api_fehler(error_text)[2], expected_category)
                self.assertEqual(
                    radar._gemini_503_aeussere_entscheidung(
                        error_text, "model-a", ("model-a", "model-b"), set()
                    ),
                    ("switch", "model-b"),
                )
                old_cwd = os.getcwd()
                os.chdir(temp_dir)
                try:
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
                    report = "\n".join((
                        "1. Trading-Radar", "2. Thesen-Update", "3. Ereignisradar",
                        "4. Assetklassenvergleich", "5. Technische Kandidaten",
                        "6. Portfolio & Risiko", "7. MSCI-Vergleich", "8. Daten-/Prüfanhang",
                    ))
                    class FakeModels:
                        def generate_content(self, model, contents, config):
                            calls.append(model)
                            if model == "model-a":
                                raise RuntimeError(error_text)
                            return types.SimpleNamespace(text=report)
                    client = types.SimpleNamespace(models=FakeModels())
                    radar.types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
                    radar._gemini_quota_waehlen = lambda preferred, tokens, **kwargs: (preferred, 0.0)
                    radar._gemini_quota_registriere_sendung = lambda model, tokens: (model, tokens)
                    radar._gemini_quota_rollback_sendung = lambda reservation: None
                    with redirect_stdout(StringIO()):
                        result = radar._gemini_trading_radar_eigenstaendig(
                            client=client, preferred_modell="model-a",
                            eingabedateien={}, bestehende_auswertung="",
                        )
                    self.assertEqual(calls, ["model-a", "model-b"])
                    self.assertEqual(result, report)
                    self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "EIGENSTAENDIG_ERZEUGT")
                finally:
                    os.chdir(old_cwd)


    def test_unknown_quota_and_bare_429_are_terminal_not_model_switches(self):
        """Unbekannte Quota-IDs und nackte 429-Antworten loesen keinen Modellwechsel aus."""
        radar = load_radar_module()
        cases = (
            "429 RESOURCE_EXHAUSTED: quotaId='NewUnknownQuotaMetric-FreeTier'",
            "429 RESOURCE_EXHAUSTED: quotaId='FutureQuotaPerMinuteMetric' requests per minute quota exceeded",
            "429 RESOURCE_EXHAUSTED: request rejected; no quota details supplied",
            '429 RESOURCE_EXHAUSTED: {"quotaId": "FutureQuotaMetric-FreeTier"}',
        )
        for error_text in cases:
            with self.subTest(error_text=error_text):
                self.assertEqual(radar.analysiere_api_fehler(error_text)[2], "quota_unbekannt")
                self.assertEqual(
                    radar._gemini_503_aeussere_entscheidung(
                        error_text, "model-a", ("model-a", "model-b"), set()
                    ),
                    ("other", None),
                )

    def test_billing_and_permission_errors_override_conflicting_429(self):
        """Billing-/Berechtigungsfehler haben Vorrang vor einem widersprüchlichen 429."""
        radar = load_radar_module()
        cases = (
            "429 RESOURCE_EXHAUSTED: billing account is not enabled",
            "429 RESOURCE_EXHAUSTED: API key not valid",
            "403 PERMISSION_DENIED: request quota exceeded",
        )
        for error_text in cases:
            with self.subTest(error_text=error_text):
                self.assertEqual(radar.analysiere_api_fehler(error_text)[2], "konfiguration")
                self.assertEqual(
                    radar._gemini_503_aeussere_entscheidung(
                        error_text, "model-a", ("model-a", "model-b"), set()
                    ),
                    ("other", None),
                )

    def test_known_rpm_quota_is_retryable_but_retry_delay_is_parsed_and_capped(self):
        """Bekannte RPM-Metriken bleiben retrybar; JSON retryDelay wird robust begrenzt."""
        radar = load_radar_module()
        error_text = (
            '429 RESOURCE_EXHAUSTED: quotaId="GenerateRequestsPerMinutePerProjectPerModel-FreeTier"; '
            '"retryDelay": "42.5s"'
        )
        self.assertEqual(
            radar.analysiere_api_fehler(error_text),
            (False, 42.5, "quota_temporär"),
        )
        long_delay = (
            '429 RESOURCE_EXHAUSTED: requests per minute quota exceeded; '
            '"retryDelay": "999999s"'
        )
        self.assertEqual(
            radar.analysiere_api_fehler(long_delay),
            (False, 300.0, "quota_temporär"),
        )
        malformed_delay = (
            '429 RESOURCE_EXHAUSTED: requests per minute quota exceeded; '
            '"retryDelay": "not-a-duration"'
        )
        self.assertEqual(
            radar.analysiere_api_fehler(malformed_delay),
            (False, None, "quota_temporär"),
        )

    def test_input_token_limit_uses_json_retry_delay_with_minimum_window(self):
        """TPM/Input-Token-Limit respektiert Mindestfenster und JSON retryDelay."""
        radar = load_radar_module()
        short = (
            '429 RESOURCE_EXHAUSTED: GenerateContentInputTokensPerModelPerMinute-FreeTier; '
            '"retryDelay": "5s"'
        )
        self.assertEqual(
            radar.analysiere_api_fehler(short),
            (False, 65.0, "input_token_limit"),
        )
        long = (
            '429 RESOURCE_EXHAUSTED: GenerateContentInputTokensPerModelPerMinute-FreeTier; '
            '"retryDelay": "999999s"'
        )
        self.assertEqual(
            radar.analysiere_api_fehler(long),
            (False, 300.0, "input_token_limit"),
        )

    def test_daily_quota_is_terminal_and_does_not_switch_models(self):
        """Ein erschöpftes Tageskontingent darf nicht als temporärer 429-Retry gelten."""
        radar = load_radar_module()
        error_text = "429 RESOURCE_EXHAUSTED: requests per day quota exceeded; GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        self.assertEqual(radar.analysiere_api_fehler(error_text)[2], "tageskontingent")
        self.assertEqual(
            radar._gemini_503_aeussere_entscheidung(
                error_text, "model-a", ("model-a", "model-b"), set()
            ),
            ("other", None),
        )

    def test_standalone_radar_all_models_503_ends_in_terminal_error_state(self):
        """Integration: erschoepfter Modellpool bleibt Fehler, publiziert keinen Bericht."""
        with tempfile.TemporaryDirectory() as temp_dir:
            radar = load_radar_module()
            old_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
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
                class FakeModels:
                    def generate_content(self, model, contents, config):
                        calls.append(model)
                        raise RuntimeError("503 UNAVAILABLE: high demand")
                client = types.SimpleNamespace(models=FakeModels())
                radar.types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
                radar._gemini_quota_waehlen = lambda preferred, tokens, **kwargs: (preferred, 0.0)
                radar._gemini_quota_registriere_sendung = lambda model, tokens: (model, tokens)
                radar._gemini_quota_rollback_sendung = lambda reservation: None
                with redirect_stdout(StringIO()):
                    result = radar._gemini_trading_radar_eigenstaendig(
                        client=client, preferred_modell="model-a",
                        eingabedateien={}, bestehende_auswertung="",
                    )
                self.assertEqual(calls, ["model-a", "model-b"])
                self.assertIsNone(result)
                self.assertIsNone(radar._GEMINI_TRADING_RADAR_TEXT)
                self.assertEqual(radar._GEMINI_TRADING_RADAR_STATUS, "REQUEST_FEHLER")
            finally:
                os.chdir(old_cwd)

    def test_production_entrypoint_uses_only_the_new_radar_entrypoint(self):
        """Der produktive Einstieg muss die neue Pipeline aufrufen und deren Resultat zurückgeben."""
        import ast
        source = RADAR.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "gemini_auswertung_starten"
        )
        # Der produktive Pfad endet mit return radar_text. Nur Statements bis
        # zu diesem ersten Top-Level-Return sind erreichbar; darunter liegt
        # historischer, nicht mehr produktiver Code.
        reachable_statements = []
        for statement in function.body:
            reachable_statements.append(statement)
            if isinstance(statement, ast.Return):
                break
        reachable_calls = [
            node for statement in reachable_statements for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_gemini_trading_radar_eigenstaendig"
        ]
        self.assertEqual(len(reachable_calls), 1, "produktiver Entry-Point muss genau einen Radar-Lauf starten")
        self.assertIn("return radar_text", ast.get_source_segment(source, function))
        self.assertIn("EXKLUSIVER TRADING-RADAR-LAUF", ast.get_source_segment(source, function))

    def test_workflow_has_one_active_radar_python_run_and_no_legacy_run(self):
        """Workflow darf keine zweite alte Auswertung als separaten Run starten."""
        self.assertTrue(WORKFLOW.is_file(), "Workflow fehlt; produktiver Einzelbetrieb nicht nachweisbar")
        workflow = WORKFLOW.read_text(encoding="utf-8-sig")
        active_run_lines = [
            line.strip() for line in workflow.splitlines()
            if line.strip().startswith("run:") and "python " in line
        ]
        radar_runs = [line for line in active_run_lines if "python Trading_Radar_Alleinauswertung.py" in line]
        legacy_runs = [line for line in active_run_lines if "python gemini_auswertung.py" in line]
        self.assertEqual(len(radar_runs), 1, f"erwartet genau einen Radar-Run, gefunden: {radar_runs}")
        self.assertEqual(legacy_runs, [], f"alte Auswertung wird produktiv separat gestartet: {legacy_runs}")


    def test_generic_http_403_is_terminal_configuration_without_model_switch(self):
        radar = load_radar_module()
        error_text = "HTTP 403: generic access problem"
        self.assertEqual(radar.analysiere_api_fehler(error_text), (True, None, "konfiguration"))
        self.assertEqual(
            radar._gemini_503_aeussere_entscheidung(
                error_text, "model-a", ("model-a", "model-b"), set()
            ),
            ("other", None),
        )


if __name__ == "__main__":
    unittest.main()
