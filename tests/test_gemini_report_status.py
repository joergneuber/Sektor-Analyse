"""Regressionstests fuer Reportstatus und GitHub-Output ohne Gemini/API-Aufruf."""
import ast
import os
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"


def load_helpers(source_path):
    """Lädt nur die beiden reinen Statusfunktionen aus dem großen Skript."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    wanted = {"_gemini_report_metall_status", "_gemini_schreibe_github_outputs"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"os": os, "re": __import__("re")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class TestGeminiReportStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_helpers(SOURCE)

    def test_multiple_metal_status_lines_are_serialized(self):
        report = "\n".join([
            "EDELMETALL_ROHSTOFF_STATUS=Gold|FINAL",
            "EDELMETALL_ROHSTOFF_STATUS=Silber|NICHT_FINAL",
            "EDELMETALL_ROHSTOFF_STATUS=Platin|FINAL",
            "EDELMETALL_ROHSTOFF_STATUS=Palladium|NICHT_FINAL",
        ])
        actual = self.helpers["_gemini_report_metall_status"](report)
        self.assertEqual(
            actual,
            "Gold|FINAL|Silber|NICHT_FINAL|Platin|FINAL|Palladium|NICHT_FINAL",
        )

    def test_empty_or_unrecognized_report_returns_empty_string(self):
        helper = self.helpers["_gemini_report_metall_status"]
        self.assertEqual(helper("" ), "")
        self.assertEqual(helper("EDELMETALL_ROHSTOFF_STATUS=Kupfer|FINAL"), "")

    def test_nicht_final_is_valid_successful_technical_output(self):
        writer = self.helpers["_gemini_schreibe_github_outputs"]
        with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8", delete=False) as output:
            output_path = output.name
        old = os.environ.get("GITHUB_OUTPUT")
        try:
            os.environ["GITHUB_OUTPUT"] = output_path
            writer("SUCCESS", "NICHT_FINAL", "/tmp/Auswertung.txt", "NICHT_FINAL", "Befund", "Gold|NICHT_FINAL")
            contents = Path(output_path).read_text(encoding="utf-8")
            self.assertIn("status=SUCCESS\n", contents)
            self.assertIn("fachlich_final=NICHT_FINAL\n", contents)
            self.assertIn("edelmetall_status=NICHT_FINAL\n", contents)
            self.assertIn("edelmetall_metall_status=Gold|NICHT_FINAL\n", contents)
        finally:
            if old is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = old
            os.unlink(output_path)


if __name__ == "__main__":
    unittest.main()
