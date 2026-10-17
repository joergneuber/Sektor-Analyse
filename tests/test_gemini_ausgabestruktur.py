"""Regression suite for the whole Auswertung structure repair.\n
Run:
  python test_gemini_ausgabestruktur_generalisiert.py gemini_auswertung_struktur_generalisiert.py

The harness compiles the production structure functions from the supplied source
file, avoiding imports of unrelated API/network dependencies in the full program.
"""
import ast
import contextlib
import io
import re
import sys
import unittest
from pathlib import Path


SOURCE = Path(sys.argv[1] if len(sys.argv) > 1 else "gemini_auswertung_struktur_generalisiert.py").resolve()
if len(sys.argv) > 1:
    sys.argv = [sys.argv[0]]  # keep unittest from treating the source path as a test selector

FUNCTIONS = {
    "_ausgabe_pflichtstruktur",
    "_ausgabe_heading_key",
    "_bereinige_doppelte_ausgabestruktur",
    "_inhaltlicher_abgrenzungstext",
    "_repariere_gesamte_ausgabestruktur",
    "_ergaenze_fehlende_ausgabestruktur",
    "_pruefe_neue_ausgabestruktur",
    "_echte_unterabschnittsueberschrift",
    "_kanonische_unterabschnittstitel",
}


def load_production_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in FUNCTIONS
    ]
    missing = FUNCTIONS - {node.name for node in selected}
    if missing:
        raise AssertionError(f"Production source lacks functions: {sorted(missing)}")
    namespace = {"re": re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


NS = load_production_functions(SOURCE)
REPAIR = NS["_repariere_gesamte_ausgabestruktur"]
CHECK = NS["_pruefe_neue_ausgabestruktur"]
REQUIRED = NS["_ausgabe_pflichtstruktur"]()


def valid_report():
    return "\n\n".join(f"{heading}\nFachinhalt-Block-{index:02d}" for index, heading in enumerate(REQUIRED)) + "\n"


def quiet_repair(text, required=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return REPAIR(text, required)


class WholeStructureRegression(unittest.TestCase):
    def test_valid_complete_report_passes_gate_and_is_stable(self):
        source = valid_report()
        repaired = quiet_repair(source)
        self.assertEqual(quiet_repair(repaired), repaired)
        CHECK(repaired)
        self.assertEqual(repaired.count("Fachinhalt-Block-"), len(REQUIRED))

    def test_every_required_section_can_be_missing_and_is_restored(self):
        source = valid_report()
        for heading in REQUIRED:
            with self.subTest(missing=heading):
                # Remove exactly the heading and its unique body, leaving all
                # remaining sections and content untouched.
                block = re.escape(heading) + r"\nFachinhalt-Block-\d{2}"
                broken, count = re.subn(block, "", source, count=1)
                self.assertEqual(count, 1)
                repaired = quiet_repair(broken)
                CHECK(repaired)
                self.assertEqual(repaired.count(heading), 1)
                self.assertEqual(repaired.count("Fachinhalt-Block-"), len(REQUIRED) - 1)

    def test_every_required_section_duplicate_is_merged_without_content_loss(self):
        source = valid_report()
        for index, heading in enumerate(REQUIRED):
            with self.subTest(duplicate=heading):
                broken = source.rstrip() + f"\n\n{heading}\nZusatzinhalt-Duplikat-{index:02d}\n"
                repaired = quiet_repair(broken)
                CHECK(repaired)
                self.assertEqual(repaired.count(heading), 1)
                self.assertIn(f"Zusatzinhalt-Duplikat-{index:02d}", repaired)
                self.assertIn(f"Fachinhalt-Block-{index:02d}", repaired)

    def test_global_order_and_targeted_swaps_are_repaired(self):
        source = valid_report()
        cases = [list(reversed(REQUIRED))]
        for left, right in (("3.3 Politik → Branche → Aktie", "3.4 Technologie → Branche → Aktie"),
                            ("8.1 Gold", "8.2 Silber"),
                            ("11.6 Hinweise zur Interpretation", "11.7 Abgrenzung:")):
            order = list(REQUIRED)
            i, j = order.index(left), order.index(right)
            order[i], order[j] = order[j], order[i]
            cases.append(order)
        for order in cases:
            with self.subTest(first=order[:3]):
                blocks = {}
                for index, heading in enumerate(REQUIRED):
                    match = re.search(r"(?m)^" + re.escape(heading) + r"\nFachinhalt-Block-" + f"{index:02d}$", source)
                    self.assertIsNotNone(match)
                    blocks[heading] = match.group(0)
                broken = "\n\n".join(blocks[heading] for heading in order) + "\n"
                repaired = quiet_repair(broken)
                CHECK(repaired)
                actual_order = [
                    NS["_ausgabe_heading_key"](line, REQUIRED)[1]
                    for line in repaired.splitlines()
                    if NS["_ausgabe_heading_key"](line, REQUIRED)
                ]
                self.assertEqual(actual_order, REQUIRED)

    def test_production_33_duplicate_and_known_heading_variant_preserve_both_analyses(self):
        source = valid_report()
        heading = "3.3 Politik → Branche → Aktie"
        source = source.replace(
            heading + "\nFachinhalt-Block-",
            heading + "\nPOLITIK-ANALYSE-A\n\n3.3 Politik → Geopolitik → Branche → Aktie\nPOLITIK-ANALYSE-B\n\nFachinhalt-Block-",
            1,
        )
        repaired = quiet_repair(source)
        CHECK(repaired)
        self.assertEqual(repaired.count(heading), 1)
        self.assertIn("POLITIK-ANALYSE-A", repaired)
        self.assertIn("POLITIK-ANALYSE-B", repaired)
        self.assertEqual(quiet_repair(repaired), repaired)

    def test_final_gate_remains_fail_closed_for_missing_duplicate_and_order(self):
        source = valid_report()
        with self.assertRaises(RuntimeError):
            CHECK(source.replace(REQUIRED[20] + "\nFachinhalt-Block-20", "", 1))
        with self.assertRaises(RuntimeError):
            CHECK(source.rstrip() + "\n\n" + REQUIRED[20] + "\nZusatz")
        # The direct validator must reject out-of-order required sections; it
        # is not allowed to silently repair or accept the malformed report.
        order = list(REQUIRED)
        order[20], order[21] = order[21], order[20]
        blocks = {
            heading: f"{heading}\nFachinhalt-Block-{index:02d}"
            for index, heading in enumerate(REQUIRED)
        }
        with self.assertRaises(RuntimeError):
            CHECK("\n\n".join(blocks[heading] for heading in order))

    def test_schema_rejects_duplicate_required_keys(self):
        # Repeating a canonical section in the schema must fail closed rather
        # than collapse two schema entries into one output heading.
        with self.assertRaises(RuntimeError):
            quiet_repair(valid_report(), REQUIRED + [REQUIRED[20]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
