
"""Automatisierte Regressionstests fuer die 503-Budget-/Fallback-Reparatur.

Die Produktionsfunktionen werden per AST aus gemini_auswertung.py
extrahiert, damit Tests auch ohne Google-API, Secrets oder Projekt-Importe laufen.
"""
from __future__ import annotations

import ast
import hashlib
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
SOURCE_TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE_TEXT, filename=str(SOURCE))
FUNCTIONS = {
    node.name: node
    for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def load_functions(names, namespace):
    nodes = [FUNCTIONS[name] for name in names]
    module = ast.Module(body=nodes, type_ignores=[])
    exec(
        compile(ast.fix_missing_locations(module), str(SOURCE), "exec"),
        namespace,
    )


class Gemini503RegressionTests(unittest.TestCase):

    def test_503_budget_is_isolated_per_logical_split_and_exact_limit(self):
        ns = {"defaultdict": defaultdict}
        load_functions(
            ["_gemini_503_modellwechsel_budget_pruefen"],
            ns,
        )

        counts = defaultdict(int)

        # Split 8 darf sein eigenes Budget verbrauchen.
        # Die Request-ID muss den Budget-Schluessel eindeutig praegen.
        for expected in range(1, 4):
            allowed, count, key = (
                ns["_gemini_503_modellwechsel_budget_pruefen"](
                    "GenerateContent Split 8/39",
                    3,
                    counts,
                    logical_request_id="test_request_1",
                )
            )

            self.assertTrue(allowed)
            self.assertEqual(count, expected)
            self.assertEqual(
                key,
                "test_request_1::GenerateContent Split 8",
            )

        # Nach Erreichen des Limits darf keine weitere Reservierung erfolgen.
        allowed, count, key = (
            ns["_gemini_503_modellwechsel_budget_pruefen"](
                "GenerateContent Split 8/39",
                3,
                counts,
                logical_request_id="test_request_1",
            )
        )

        self.assertFalse(allowed)
        self.assertEqual(count, 3)
        self.assertEqual(
            key,
            "test_request_1::GenerateContent Split 8",
        )

        # Re-Chunking darf den Nenner aendern, ohne fuer denselben
        # logischen Split ein neues Budget zu oeffnen.
        allowed, count, key = (
            ns["_gemini_503_modellwechsel_budget_pruefen"](
                "GenerateContent Split 8/40",
                3,
                counts,
                logical_request_id="test_request_1",
            )
        )

        self.assertFalse(allowed)
        self.assertEqual(count, 3)
        self.assertEqual(
            key,
            "test_request_1::GenerateContent Split 8",
        )

        # Split 9 beginnt unabhaengig bei 1.
        allowed, count, key = (
            ns["_gemini_503_modellwechsel_budget_pruefen"](
                "GenerateContent Split 9/39",
                3,
                counts,
                logical_request_id="test_request_1",
            )
        )

        self.assertTrue(allowed)
        self.assertEqual(count, 1)
        self.assertEqual(
            key,
            "test_request_1::GenerateContent Split 9",
        )

        # Ein anderer logischer Request mit identischem Stage-Namen
        # bekommt ebenfalls ein eigenes Budget.
        allowed, count, key = (
            ns["_gemini_503_modellwechsel_budget_pruefen"](
                "GenerateContent Split 8/39",
                3,
                counts,
                logical_request_id="test_request_2",
            )
        )

        self.assertTrue(allowed)
        self.assertEqual(count, 1)
        self.assertEqual(
            key,
            "test_request_2::GenerateContent Split 8",
        )

    def test_429_then_503_uses_next_fallback_not_same_model_again(self):
        # Der echte Produktions-Quota-Safe-Pfad wird mit deterministischen
        # API-/Quota-Stubs ausgefuehrt: A=429 PerDay, B=503, C=Erfolg.
        ns = {
            "defaultdict": defaultdict,
            "set": set,
            "types": SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs
            ),
            "GEMINI_INPUT_EXCEPTION_BUDGET": 121200,
            "GEMINI_INPUT_SAFE_BUDGET": 120000,
            "GEMINI_FREE_TIER_INPUT_WINDOW_SECONDS": 60.0,
            "GEMINI_FREE_TIER_INPUT_WINDOW_RESERVE_SECONDS": 2.0,
            "_gemini_503_failed_models_by_request": defaultdict(set),
            "_gemini_503_failures_this_run": defaultdict(int),
            "_gemini_503_pool_passes_this_run": defaultdict(int),
            "_gemini_last_503_request_info": {},
            "_gemini_last_request_model": None,
            "_gemini_active_modell": None,
            "_gemini_failed_models": set(),
            "_gemini_rpd_exhausted": set(),
            "_gemini_input_quota_cooldown_until": {},
            "_gemini_input_quota_usage": defaultdict(list),
            "_gemini_project_input_quota_usage": [],
            "_gemini_rpd_requests": defaultdict(int),
            "_gemini_token_count_cache": {},
            "time": SimpleNamespace(sleep=lambda seconds: None),
            "print": lambda *args, **kwargs: None,
        }

        load_functions(
            [
                "_gemini_503_request_fingerprint",
                "_gemini_fallback_kandidaten_ohne_versuchte",
                "_gemini_generate_content_quota_safe",
            ],
            ns,
        )

        ns["hashlib"] = hashlib
        ns["json"] = __import__("json")
        ns["re"] = __import__("re")

        ns["_gemini_quota_kandidaten"] = (
            lambda modell, fehlgeschlagene_einschliessen=False:
            ["A", "B", "C"]
        )

        def choose(
            preferred,
            tokens,
            ausgeschlossene_modelle=None,
            fehlgeschlagene_einschliessen=False,
        ):
            excluded = set(ausgeschlossene_modelle or ())
            return (preferred, 0) if preferred not in excluded else (None, 0)

        ns["_gemini_quota_waehlen"] = choose
        ns["_gemini_tokenzahl"] = lambda *args, **kwargs: 100
        ns["_gemini_quota_registriere_sendung"] = (
            lambda modell, tokens: {"model": modell}
        )
        ns["_gemini_quota_rollback_sendung"] = (
            lambda reservation: True
        )

        calls = []

        def api_attempt(
            client,
            model,
            contents,
            config,
            label,
            system_instruction=None,
            logical_request_id=None,
        ):
            calls.append(model)

            if model == "A":
                raise RuntimeError(
                    "429 RESOURCE_EXHAUSTED "
                    "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                )

            if model == "B":
                raise RuntimeError("503 UNAVAILABLE high demand")

            return SimpleNamespace(text="OK from C", candidates=[])

        ns["_gemini_generate_content_einzelversuch"] = api_attempt

        # Ein Request-spezifischer failed-model-Satz wie im Produktionspfad.
        contents = [SimpleNamespace(text="same split payload")]
        fp = ns["_gemini_503_request_fingerprint"](contents, "system")

        ns["_gemini_503_failed_models_by_request"][fp] = set()
        ns["_gemini_failed_models"] = (
            ns["_gemini_503_failed_models_by_request"][fp]
        )

        response = ns["_gemini_generate_content_quota_safe"](
            client=object(),
            preferred_modell="A",
            contents=contents,
            config_kwargs={"system_instruction": "system"},
            measured_tokens=100,
            label="GenerateContent Split 8/39",
        )

        self.assertEqual(response.text, "OK from C")
        self.assertEqual(calls, ["A", "B", "C"])
        self.assertEqual(
            len(calls),
            len(set(calls)),
            "Ein Modell wurde im Fallback-Zyklus doppelt versucht",
        )

    def test_source_compiles_and_legacy_global_counter_is_gone(self):
        compile(SOURCE_TEXT, str(SOURCE), "exec")

        self.assertNotIn(
            "gemini_503_modellwechsel_versuche = 0",
            SOURCE_TEXT,
        )
        self.assertIn(
            "_gemini_503_modellwechsel_je_stage.clear()",
            SOURCE_TEXT,
        )
        self.assertIn(
            "_gemini_503_modellwechsel_budget_pruefen",
            SOURCE_TEXT,
        )
        self.assertIn(
            "_gemini_fallback_kandidaten_ohne_versuchte",
            SOURCE_TEXT,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
