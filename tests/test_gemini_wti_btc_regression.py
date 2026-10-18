"""Focused regression tests for Edelmetall source binding and price repair.

Run with: python test_gemini_edelmetall_regression.py
The production module's optional Google/Drive dependencies are stubbed because
these tests exercise only deterministic source-repair functions.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("gemini_auswertung_wti_btc_korrigiert.py")


def _install_import_stubs():
    trade = types.ModuleType("trade_story_universum")
    trade.write_trade_story_universe = lambda *a, **k: None
    sys.modules["trade_story_universum"] = trade
    for name in (
        "google", "google.genai", "google.oauth2", "google.oauth2.credentials",
        "google.auth", "google.auth.transport", "google.auth.transport.requests",
        "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
    ):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    sys.modules["google"].genai = sys.modules["google.genai"]
    sys.modules["google.genai"].types = types.SimpleNamespace()
    sys.modules["google.oauth2.credentials"].Credentials = type("Credentials", (), {})
    sys.modules["google.auth.transport.requests"].Request = type("Request", (), {})
    sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
    sys.modules["googleapiclient.http"].MediaIoBaseDownload = type("MediaIoBaseDownload", (), {})
    contract = types.ModuleType("quellenwert_vertrag")
    contract.values_match_display = lambda *a, **k: False
    contract.find_exact_labeled_line = lambda *a, **k: None
    contract.extract_four_week_percent = lambda *a, **k: None
    contract.extract_metal_four_week_value = lambda *a, **k: None
    sys.modules["quellenwert_vertrag"] = contract


_install_import_stubs()
spec = importlib.util.spec_from_file_location("gemini_auswertung_under_test", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# Stand-in for the project's external contract parser, kept deliberately
# strict and scoped to the single matching LAGE JE METALL row.
def _fixture_4w(source, metal):
    rows = [line for line in (source or "").splitlines()
            if line.startswith(f"{metal}: Kurs ")]
    if len(rows) != 1:
        return None
    import re
    match = re.search(r"\|\s*([-+]?\d+(?:[.,]\d+)?)%\s+in den letzten 4 Wochen", rows[0])
    if not match:
        return None
    return match.group(1), module._quellenwert_float(match.group(1))
module.extract_metal_four_week_value = _fixture_4w
module._extract_metal_4w_value = lambda source, metal: _fixture_4w(source, metal)

METALS = (("Gold", "GC=F", "4181.10", "-3.2"),
          ("Silber", "SI=F", "57.31", "-11.0"),
          ("Platin", "PL=F", "1633.10", "-10.4"),
          ("Palladium", "PA=F", "1118.60", "-11.1"))


def source_fixture(duplicate_gold=False, wrong_gold_ticker=False):
    rows = ["LAGE JE METALL"]
    for metal, ticker, price, perf in METALS:
        actual_ticker = "SI=F" if wrong_gold_ticker and metal == "Gold" else ticker
        rows.append(f"{metal}: Kurs {price} | {perf}% in den letzten 4 Wochen")
        if actual_ticker != ticker:
            rows[-1] += f" | Ticker {actual_ticker}"
        if metal == "Gold" and duplicate_gold:
            rows.append(rows[-1])
    rows += ["TRENDFOLGE-DIAGNOSE JE METALL"]
    for metal, ticker, price, perf in METALS:
        rows += [f"- {metal} ({ticker})", f"200er-Trend: Kurs {price} | EMA200=4500 (x) | WMA200=4400"]
    for strategy in ("TRENDFOLGE", "TRENDWENDE", "SHORT"):
        rows += [f"STRATEGIE: {strategy}"]
        rows += ["Keine Trendwende-Kandidaten" if strategy == "TRENDWENDE" else "=> SHORT-KANDIDAT: 0"]
    return "\n".join(rows)


def report_fixture():
    return "\n".join([f"8.{i} {metal}\n- Gemini-Interpretation: Test" for i, metal in enumerate(("Gold", "Silber", "Platin", "Palladium"), 1)])


class EdelmetallRegression(unittest.TestCase):
    def test_source_prices_and_metal_ticker_bindings(self):
        fixed = module._repariere_8_x_quellengebunden(report_fixture(), source_fixture())
        for metal, ticker, price, _ in METALS:
            self.assertIn(f"Kurs (Futures {ticker}): {price}$", fixed)
            self.assertNotIn(f"Kurs (Futures {ticker}): {price}{price}", fixed)

    def test_duplicate_source_mapping_is_not_guessed(self):
        with self.assertRaisesRegex(RuntimeError, "Kurs-Zuordnung nicht eindeutig"):
            module._repariere_8_x_quellengebunden(report_fixture(), source_fixture(duplicate_gold=True))

    def test_explicit_wrong_ticker_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "Ticker-Konflikt"):
            module._repariere_8_x_quellengebunden(report_fixture(), source_fixture(wrong_gold_ticker=True))

    def test_four_week_conflict_is_reported_and_dedicated_source_wins(self):
        from contextlib import redirect_stdout
        from io import StringIO
        market = "\n".join(f"{metal}: Kurs {price} | -5.5% in den letzten 4 Wochen"
                           for metal, ticker, price, perf in METALS)
        output = StringIO()
        with redirect_stdout(output):
            fixed = module._repariere_8_x_quellengebunden(report_fixture(), source_fixture(), market)
        self.assertIn("EDELMETALL_4W_QUELLENKONFLIKT", output.getvalue())
        self.assertIn("Kurs (Futures GC=F): 4181.10$", fixed)
        self.assertIn("4W: -3.2%", fixed)

    def test_corrupted_main_report_gold_price_is_replaced(self):
        edel = source_fixture()
        report = ("1.1 Was hat sich seit dem letzten Lauf verändert?\n"
                  "Gold 4.1814181,10USD; Silber 57,3157,30USD auf Futures-Basis.\n"
                  "1.2 Sofort handelbare Chancen\n")
        repaired = module._repariere_1_1_rohstoffperioden(report, "", None, edel)
        self.assertIn("Gold 4.181,10USD", repaired)
        self.assertIn("Silber 57,31USD", repaired)
        self.assertNotIn("4.1814181,10USD", repaired)
        self.assertNotIn("57,3157,30USD", repaired)

    def test_wti_and_btc_corrupt_intro_prices_repaired_from_macro_source(self):
        macro = (
            "Bitcoin: 81632.226562 | Datenstand=2026-10-17 | SOURCE=BTC-USD\n"
            "WTI: 99.019997 | Datenstand=2026-10-16 | SOURCE=CL=F\n"
        )
        report = (
            "1.1 Was hat sich seit dem letzten Lauf verändert?\n"
            "WTI-Öl markiert einen Aufwärtsimpuls (99,0299,02USD). "
            "Bitcoin (81.63281632,23USD) notiert stabil.\n"
            "1.2 Sofort handelbare Chancen\n"
        )
        repaired, changes = module._repariere_1_1_wti_btc_kurse(report, macro)
        self.assertIn("WTI-Öl markiert einen Aufwärtsimpuls (99,02 USD)", repaired)
        self.assertIn("Bitcoin (81.632,23 USD)", repaired)
        self.assertNotIn("99,0299,02USD", repaired)
        self.assertNotIn("81.63281632,23USD", repaired)
        self.assertEqual(len(changes), 2)

    def test_ambiguous_macro_source_does_not_guess_or_hide_issue(self):
        macro = (
            "WTI: 99.019997 | SOURCE=CL=F\n"
            "WTI: 98.019997 | SOURCE=CL=F\n"
        )
        report = (
            "1.1 Was hat sich seit dem letzten Lauf verändert?\n"
            "WTI-Öl markiert einen Aufwärtsimpuls (99,0299,02USD).\n"
            "1.2 Sofort handelbare Chancen\n"
        )
        repaired, changes = module._repariere_1_1_wti_btc_kurse(report, macro)
        self.assertIn("99,0299,02USD", repaired)
        self.assertTrue(any("WTI" in item and "mehrdeutig" in item for item in changes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
