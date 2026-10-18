"""Regressionstests fuer die quellengebundene Reparatur von Briefing-Abschnitten.

Die Tests verwenden die produktive Funktion aus gemini_auswertung.py und
mocken nur die Quellwert-Extraktion, damit keine externen Datenquellen
benoetigt werden.
"""
import unittest
from unittest.mock import patch

import gemini_auswertung as sut


class TestBriefingSourceRepair(unittest.TestCase):
    def test_leerer_text_bleibt_unveraendert(self):
        result, changed = sut._repariere_7_x_quellengebunden("", "", "")
        self.assertEqual(result, "")
        self.assertFalse(changed)

    def test_abschnitt_ohne_quellenwert_bleibt_unveraendert(self):
        text = "7.1 Aktienmärkte / Indizes\nKeine Daten vorhanden.\n\n7.2 Zinsen\n"
        with patch.object(
            sut,
            "_quellenwert_status_aus_zeile",
            return_value=("MISSING", ""),
        ):
            result, changed = sut._repariere_7_x_quellengebunden(
                text, "", ""
            )
        self.assertEqual(result, text)
        self.assertFalse(changed)

    def test_fehlender_sp500_quellenwert_wird_ergaenzt(self):
        text = (
            "7.1 Aktienmärkte / Indizes\n"
            "Marktkommentar ohne S&P-500-Zahl.\n\n"
            "7.2 Zinsen\nZinskommentar.\n\n"
        )

        def fake_source_status(source_text, label, parenthesized=False):
            if label == "S&P 500":
                return "VALUE", "5.123,45"
            return "MISSING", ""

        with patch.object(
            sut, "_quellenwert_status_aus_zeile", side_effect=fake_source_status
        ):
            result, changed = sut._repariere_7_x_quellengebunden(
                text, "S&P 500: 5.123,45", ""
            )

        self.assertTrue(changed)
        self.assertIn("[AUTORITATIVE QUELLE] S&P 500: 5.123,45", result)
        self.assertIn("7.2 Zinsen", result)


if __name__ == "__main__":
    unittest.main()
