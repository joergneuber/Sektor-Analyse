import ast
import re
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SOURCE = _HERE / "gemini_auswertung.py"
if not SOURCE.exists():
    SOURCE = _HERE.parent / "gemini_auswertung.py"


def load_helper():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in {
            "_gemini_bereichsstatus", "_gemini_report_bereichsstatus"
        }
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["_gemini_bereichsstatus"], namespace["_gemini_report_bereichsstatus"]


class BereichsstatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        status, report_status = load_helper()
        cls.status = staticmethod(status)
        cls.report_status = staticmethod(report_status)

    def test_clean_report_is_final_in_all_areas(self):
        status, findings = self.status()
        self.assertEqual(status["GESAMT"], "FINAL")
        self.assertEqual(status["AKTIEN"], "FINAL")
        self.assertEqual(status["MARKT_MAKRO"], "FINAL")
        self.assertEqual(status["EDELMETALLE"], "FINAL")
        self.assertEqual(findings["GESAMTBERICHT"], [])

    def test_metal_issue_does_not_mark_stock_or_market_nonfinal(self):
        status, findings = self.status(
            edelmetall_nicht_final=True,
            edelmetall_global_warnungen=["ZUORDNUNG | ROHSTOFF=Gold | unklar"],
        )
        self.assertEqual(status["EDELMETALLE"], "NICHT_FINAL")
        self.assertEqual(status["AKTIEN"], "FINAL")
        self.assertEqual(status["MARKT_MAKRO"], "FINAL")
        self.assertEqual(status["GESAMT"], "NICHT_FINAL")
        self.assertEqual(len(findings["EDELMETALLE"]), 1)

    def test_market_numeric_issue_is_assigned_to_market(self):
        status, findings = self.status(zahlen_gate_warnungen=[
            "7.3 Volatilitaet VIX: Ausgabe 17.309999 widerspricht Quellwert 17.31"
        ])
        self.assertEqual(status["MARKT_MAKRO"], "NICHT_FINAL")
        self.assertEqual(status["AKTIEN"], "FINAL")
        self.assertEqual(status["EDELMETALLE"], "FINAL")
        self.assertEqual(status["GESAMT"], "NICHT_FINAL")
        self.assertEqual(len(findings["MARKT_MAKRO"]), 1)

    def test_stock_numeric_issue_is_assigned_to_stock(self):
        status, findings = self.status(zahlen_gate_warnungen=[
            "2.1 Aktie XYZ: Ausgabe 100 widerspricht Quellenwert 101"
        ])
        self.assertEqual(status["AKTIEN"], "NICHT_FINAL")
        self.assertEqual(status["MARKT_MAKRO"], "FINAL")
        self.assertEqual(status["GESAMT"], "NICHT_FINAL")
        self.assertEqual(len(findings["AKTIEN"]), 1)

    def test_unknown_numeric_issue_and_minimum_depth_are_reportwide(self):
        status, findings = self.status(
            zahlen_gate_warnungen=["Abweichung ohne Abschnitts- oder Assetangabe"],
            fachliche_mindesttiefe_offen=True,
        )
        self.assertEqual(status["GESAMTBERICHT"], "NICHT_FINAL")
        self.assertEqual(status["GESAMT"], "NICHT_FINAL")
        self.assertEqual(status["AKTIEN"], "FINAL")
        self.assertEqual(status["MARKT_MAKRO"], "FINAL")
        self.assertEqual(len(findings["GESAMTBERICHT"]), 2)

    def test_mixed_domains_are_reportwide_not_priority_assigned(self):
        status, findings = self.status(zahlen_gate_warnungen=[
            "Warnung: Aktie XYZ im Markt-Makro-Bericht VIX-Zahl weicht ab"
        ])
        self.assertEqual(status["GESAMTBERICHT"], "NICHT_FINAL")
        self.assertEqual(status["AKTIEN"], "FINAL")
        self.assertEqual(status["MARKT_MAKRO"], "FINAL")

    def test_explicit_area_tag_is_deterministic(self):
        status, findings = self.status(zahlen_gate_warnungen=[
            "[MARKT_MAKRO] Abweichung bei VIX: Ausgabe 20, Quelle 21"
        ])
        self.assertEqual(status["MARKT_MAKRO"], "NICHT_FINAL")
        self.assertEqual(findings["MARKT_MAKRO"], [
            "[MARKT_MAKRO] Abweichung bei VIX: Ausgabe 20, Quelle 21"
        ])

    def test_report_consumer_rejects_missing_or_duplicate_status_markers(self):
        good = (
            "EDELMETALL_STATUS=FINAL\nFACHLICH_FINAL=FINAL\nBERICHT_STATUS=FINAL\n"
            "BEREICH_STATUS=AKTIEN|FINAL\n"
            "BEREICH_STATUS=MARKT_MAKRO|FINAL\n"
            "BEREICH_STATUS=EDELMETALLE|FINAL\n"
            "BEREICH_STATUS=GESAMTBERICHT|FINAL\n"
        )
        status, findings = self.report_status(good)
        self.assertEqual(status["GESAMT"], "FINAL")
        missing, _ = self.report_status(good.replace("BEREICH_STATUS=AKTIEN|FINAL\n", ""))
        self.assertEqual(missing["AKTIEN"], "NICHT_FINAL")
        duplicate, _ = self.report_status(good + "BEREICH_STATUS=AKTIEN|FINAL\n")
        self.assertEqual(duplicate["AKTIEN"], "NICHT_FINAL")

    def test_report_consumer_checks_global_marker_consistency(self):
        report = (
            "EDELMETALL_STATUS=FINAL\nFACHLICH_FINAL=FINAL\nBERICHT_STATUS=NICHT_FINAL\n"
            "BEREICH_STATUS=AKTIEN|FINAL\n"
            "BEREICH_STATUS=MARKT_MAKRO|FINAL\n"
            "BEREICH_STATUS=EDELMETALLE|FINAL\n"
            "BEREICH_STATUS=GESAMTBERICHT|FINAL\n"
        )
        status, _ = self.report_status(report)
        self.assertEqual(status["GESAMT"], "NICHT_FINAL")


    def test_section_number_conflicting_with_asset_domain_is_reportwide(self):
        status, findings = self.status(zahlen_gate_warnungen=[
            "2.1 Gold: Ausgabe 100 widerspricht Quellenwert 101"
        ])
        self.assertEqual(status["GESAMTBERICHT"], "NICHT_FINAL")
        self.assertEqual(status["AKTIEN"], "FINAL")
        self.assertEqual(status["EDELMETALLE"], "FINAL")

    def test_report_consumer_rejects_inconsistent_legacy_metal_marker(self):
        report = (
            "EDELMETALL_STATUS=FINAL\\nFACHLICH_FINAL=NICHT_FINAL\\n"
            "BERICHT_STATUS=NICHT_FINAL\\n"
            "BEREICH_STATUS=AKTIEN|FINAL\\n"
            "BEREICH_STATUS=MARKT_MAKRO|FINAL\\n"
            "BEREICH_STATUS=EDELMETALLE|NICHT_FINAL\\n"
            "BEREICH_STATUS=GESAMTBERICHT|FINAL\\n"
        )
        status, _ = self.report_status(report)
        self.assertEqual(status["EDELMETALLE"], "NICHT_FINAL")
        self.assertEqual(status["GESAMT"], "NICHT_FINAL")

if __name__ == "__main__":
    unittest.main()
