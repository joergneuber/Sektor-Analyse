"""Gezielte Regressionstests fuer das 503-Modellwechselbudget.

Die Tests extrahieren die echte Budgetfunktion aus gemini_auswertung.py,
so dass weder Google-API-Zugriff noch Drittanbieterpakete erforderlich sind.
"""
import ast
from collections import defaultdict
from pathlib import Path
import unittest

SOURCE = Path(__file__).with_name("gemini_auswertung(9)_repariert.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NODE = next(
    n for n in TREE.body
    if isinstance(n, ast.FunctionDef)
    and n.name == "_gemini_503_modellwechsel_budget_pruefen"
)
NAMESPACE = {
    "_gemini_503_modellwechsel_je_stage": defaultdict(int),
    "_gemini_last_503_request_info": {},
}
exec(compile(ast.Module(body=[NODE], type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)
check = NAMESPACE["_gemini_503_modellwechsel_budget_pruefen"]


class TestGemini503LogicalRequestBudget(unittest.TestCase):
    def setUp(self):
        NAMESPACE["_gemini_503_modellwechsel_je_stage"].clear()
        NAMESPACE["_gemini_last_503_request_info"] = {}

    def test_same_logical_split_survives_denominator_change(self):
        allowed1, count1, key1 = check(
            "GenerateContent Split 8/39", 2,
            logical_request_id="ctx-abc:Split:8",
        )
        allowed2, count2, key2 = check(
            "GenerateContent Split 8/40", 2,
            logical_request_id="ctx-abc:Split:8",
        )
        self.assertTrue(allowed1 and allowed2)
        self.assertEqual((count1, count2), (1, 2))
        self.assertEqual(key1, key2)

    def test_independent_generic_generatecontent_requests_have_separate_budgets(self):
        a1 = check("GenerateContent", 1, logical_request_id="context-A:GenerateContent")
        b1 = check("GenerateContent", 1, logical_request_id="context-B:GenerateContent")
        a2 = check("GenerateContent", 1, logical_request_id="context-A:GenerateContent")
        self.assertTrue(a1[0])
        self.assertTrue(b1[0])
        self.assertFalse(a2[0])
        self.assertNotEqual(a1[2], b1[2])

    def test_distinct_splits_have_separate_budgets(self):
        a = check("GenerateContent Split 8/39", 1, logical_request_id="ctx:Split:8")
        b = check("GenerateContent Split 9/39", 1, logical_request_id="ctx:Split:9")
        self.assertTrue(a[0] and b[0])
        self.assertNotEqual(a[2], b[2])

    def test_limit_does_not_increment_on_rejection(self):
        self.assertEqual(check("GenerateContent", 2, logical_request_id="ctx" )[:2], (True, 1))
        self.assertEqual(check("GenerateContent", 2, logical_request_id="ctx" )[:2], (True, 2))
        self.assertEqual(check("GenerateContent", 2, logical_request_id="ctx" )[:2], (False, 2))
        self.assertEqual(check("GenerateContent", 2, logical_request_id="ctx" )[:2], (False, 2))

    def test_missing_explicit_id_uses_request_info(self):
        NAMESPACE["_gemini_last_503_request_info"] = {"logical_request_id": "from-request-info"}
        result = check("GenerateContent", 1)
        self.assertIn("from-request-info::GenerateContent", result[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
