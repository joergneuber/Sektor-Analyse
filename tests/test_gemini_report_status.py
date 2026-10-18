import ast
import os
import re
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"


def load_status_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted = {"_gemini_report_metall_status", "_gemini_schreibe_github_outputs"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"os": os, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class ReportStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_status_helpers()

    def test_multiple_metal_statuses_are_serialized_as_strings(self):
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
        self.assertIsInstance(actual, str)

    def test_no_metal_status_returns_empty_string(self):
        self.assertEqual(
            self.helpers["_gemini_report_metall_status"]("Report ohne Statuszeilen"),
            "",
        )

    def test_nicht_final_is_written_as_technical_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github_output.txt"
            previous = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output)
            try:
                self.helpers["_gemini_schreibe_github_outputs"](
                    "SUCCESS", "NICHT_FINAL", "report.txt", "NICHT_FINAL", "", "Gold|NICHT_FINAL"
                )
            finally:
                if previous is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = previous
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["status"], "SUCCESS")
            self.assertEqual(values["fachlich_final"], "NICHT_FINAL")
            self.assertEqual(values["edelmetall_metall_status"], "Gold|NICHT_FINAL")


if __name__ == "__main__":
    unittest.main()
