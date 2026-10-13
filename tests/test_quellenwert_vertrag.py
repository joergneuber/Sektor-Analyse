import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quellenwert_vertrag import (
    decimal_places,
    display_tolerance,
    four_week_return,
    same_measurement_basis,
    values_match_display,
    find_exact_labeled_line,
    extract_four_week_percent,
)


def test_display_precision_accepts_normal_rounding():
    assert values_match_display(3.13, 3.129, "3,13")
    assert values_match_display(6.77, 6.7735, "6,77")
    assert values_match_display(1.1206, 1.120574, "1,1206")


def test_display_precision_rejects_real_conflict():
    assert not values_match_display(2803.38, 2806.98, "2.803,38")
    assert not values_match_display(334.131, 337.765, "334,1310")


def test_precision_uses_displayed_decimal_count():
    assert decimal_places("334,1310") == 4
    assert decimal_places("2.803,38") == 2
    assert display_tolerance("3,13") == 0.005


def test_live_quote_and_close_are_not_same_basis():
    assert not same_measurement_basis("price", "intraday", "price", "last_close")
    assert same_measurement_basis("price", "last_close", "price", "last_close")


def test_four_week_return_uses_20_intervals_not_20_rows():
    closes = list(range(100, 121))  # 21 closes, 20 completed intervals
    result = four_week_return(closes, sessions=20)
    assert result is not None
    assert abs(result - ((120 / 100 - 1) * 100)) < 1e-9
    assert four_week_return(list(range(100, 120)), sessions=20) is None


def test_four_week_return_rejects_zero_base():
    assert four_week_return([0, 1], sessions=1) is None


def test_exact_label_does_not_confuse_core_cpi_with_cpi():
    source = "CPI: 334.1310 | YOY=+3.35%\nCore CPI: 337.7650 | YOY=+2.45%"
    assert find_exact_labeled_line(source, "CPI").startswith("CPI:")
    assert find_exact_labeled_line(source, "Core CPI").startswith("Core CPI:")

def test_four_week_extractor_ignores_52_week_distance():
    line = "Gold: Kurs 4196.20 | -3.6% 4W | nahe 52W-Tief (3901.30, 7.6% darüber)"
    assert extract_four_week_percent(line) == "-3.6"
    assert extract_four_week_percent("Gold: 7.6% darüber zum 52W-Tief") is None
    assert extract_four_week_percent("Silber: Kurs 60.81 | -7.1% in den letzten 4 Wochen") == "-7.1"


def test_metal_four_week_source_rows_detect_source_conflict():
    from quellenwert_vertrag import extract_metal_four_week_value
    market = "- Gold: Kurs 4196.20 | +0.9% 5T | -3.6% 4W | nahe 52W-Tief (3901.30, 7.6% darüber)"
    detail = "  Gold: Kurs 4196.20 | -4.6% in den letzten 4 Wochen - nahe seinem 52-Wochen-Tief (3901.30, 7.6% darüber)"
    assert extract_metal_four_week_value(market, "Gold") == ("-3.6", -3.6)
    assert extract_metal_four_week_value(detail, "Gold") == ("-4.6", -4.6)
    assert extract_metal_four_week_value("Gold: Kurs 4196.20 | 7.6% darüber zum 52W-Tief", "Gold") is None


def test_speichere_ergebnis_loads_market_briefing_before_8x_repair():
    """Prevent UnboundLocalError and keep the 4W source-conflict check active."""
    import ast
    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    target = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "speichere_ergebnis"
    )
    calls_8x = [
        node for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_repariere_8_x_quellengebunden"
    ]
    briefing_assignments = [
        node for node in ast.walk(target)
        if isinstance(node, ast.Assign)
        and any(isinstance(name, ast.Name) and name.id == "briefing_text" for name in node.targets)
    ]
    assert calls_8x and briefing_assignments
    assert min(node.lineno for node in briefing_assignments) < min(node.lineno for node in calls_8x)
    assert 'BRIEFING_QUELLE_FEHLT' in source_path.read_text(encoding="utf-8")


def test_punkt7_index_gate_uses_same_briefing_value_format_as_repair():
    """The final gate must parse the same index-value location as the repair pass."""
    import ast

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    def index_mapping(function_name):
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        assignment = next(
            node for node in function.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "index_labels"
                    for target in node.targets)
        )
        return dict(ast.literal_eval(assignment.value))

    repair = index_mapping("_repariere_7_x_quellengebunden")
    gate = index_mapping("_pruefe_punkt7_quellenabdeckung")
    assert gate == repair
    assert gate == {
        "S&P 500": True,
        "Nasdaq": True,
        "DAX": True,
        "EuroStoxx50": True,
        "Russell 2000": False,
        "Nikkei 225": True,
        "Hang Seng": True,
        "Shanghai Composite": True,
    }



def test_punkt8_200er_trend_parser_accepts_gate_and_repair_formats():
    """Repair and gate must parse the same valid 200er-Trend line variants."""
    import ast
    import re

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_extrahiere_200er_trend"
    )
    namespace = {"re": re}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source_path), "exec"), namespace)
    parse_trend = namespace["_extrahiere_200er_trend"]

    assert parse_trend(
        "200er-Trend: Kurs 4195.40 | EMA200 4324.15 (NEIN) | WMA200 4413.91 (NEIN) | Gesamt NEIN"
    ) == ("4195.40", "4324.15", "4413.91")
    assert parse_trend(
        "200er-Trend: Kurs=4.196,20 | EMA200=4.021,10 | WMA200=3.988,00"
    ) == ("4.196,20", "4.021,10", "3.988,00")
    assert parse_trend(
        "200er-Trend: Kurs 4196.20 | EMA200 4021.10 | WMA200 3988.00"
    ) == ("4196.20", "4021.10", "3988.00")
    assert parse_trend("200er-Trend: keine Daten") is None


def test_punkt8_repair_and_gate_share_200er_trend_parser():
    """Prevent a source-format mismatch between section-8 repair and its gate."""
    import ast

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    def function(name):
        return next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    for name in ("_repariere_8_x_quellengebunden", "_pruefe_punkt8_quellenabdeckung"):
        calls = [
            node for node in ast.walk(function(name))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_extrahiere_200er_trend"
        ]
        assert calls, f"{name} must use the shared 200er-Trend parser"


def test_edelmetall_4w_priority_uses_specialized_source_on_conflict():
    """The specialized metals briefing wins even when market values disagree."""
    import ast
    import re

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_waehle_edelmetall_4w"
    )
    namespace = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source_path), "exec"), namespace)
    choose = namespace["_waehle_edelmetall_4w"]

    assert choose(("-8.9", -8.9), ("-4.9", -4.9)) == ("-8.9", -8.9, "Edelmetalle-Briefing")


def test_edelmetall_4w_priority_falls_back_only_when_detail_value_missing():
    """Missing/unusable detail value falls back to market; no source means None."""
    import ast

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_waehle_edelmetall_4w"
    )
    namespace = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source_path), "exec"), namespace)
    choose = namespace["_waehle_edelmetall_4w"]

    assert choose(None, ("-4.9", -4.9)) == ("-4.9", -4.9, "Markt-Briefing-Fallback")
    assert choose(("not-a-number", None), ("-4.9", -4.9)) == ("-4.9", -4.9, "Markt-Briefing-Fallback")
    assert choose(None, None) is None


def test_edelmetall_repair_and_final_gate_use_shared_priority_rule():
    """Prevent later repair/gate paths from restoring market-first behavior."""
    import ast

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for name in (
        "_repariere_8_x_quellengebunden",
        "_pruefe_punkt8_quellenabdeckung",
        "_repariere_auswertung_quellenkonflikte",
        "_pruefe_auswertung_quellenkonflikte",
    ):
        calls = [
            node for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_waehle_edelmetall_4w"
        ]
        assert calls, f"{name} must use the shared Edelmetalle-first source rule"


def test_edelmetall_4w_extractor_uses_lage_je_metall_not_duplicate_rows():
    """A duplicate value outside the designated source block must not win."""
    import ast
    import re
    from quellenwert_vertrag import extract_metal_four_week_value

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    wanted = {"_edelmetall_lage_block", "_edelmetall_4w_value"}
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "re": re,
        "_extract_metal_4w_value": lambda source, metal: extract_metal_four_week_value(source or "", metal),
    }
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(source_path), "exec"), namespace)
    source = (
        "Gold: Kurs 100 | -3.4% 4W\n"
        "LAGE JE METALL\n"
        "Gold: Kurs 101 | -4.9% in den letzten 4 Wochen\n"
        "TRENDFOLGE-DIAGNOSE JE METALL\n"
    )
    assert namespace["_edelmetall_4w_value"](source, "Gold") == ("-4.9", -4.9)



def test_final_futures_basis_sanitizer_removes_inline_duplicates_only_from_8x():
    """The final sanitizer removes all gate-triggering variants from 8.1–8.4."""
    import ast
    import re

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_entferne_futures_basis_duplikate_aus_abschnitt8"
    )
    namespace = {"re": re}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source_path), "exec"), namespace)
    clean = namespace[helper.name]
    report = (
        "8.1 Gold\n"
        "- Autoritative Fakten: Kurs 100 | 4W: -4.8%\n"
        "- Futures-Basis-Duplikate: alter Kurs 101\n"
        "Qualitative Interpretation bleibt erhalten.\n"
        "8.2 Silber\n"
        "Futures-Basis (abweichender Wert): 99\n"
        "8.3 Platin\n"
        "- Notiz: Futures-Basis: widersprüchlicher Wert\n"
        "8.4 Palladium\n"
        "Kurs (Futures PA=F): 100$\n"
        "9. 📅 NÄCHSTE KATALYSATOREN\n"
        "Futures-Basis in einem späteren Abschnitt bleibt unangetastet.\n"
    )
    result = clean(report)
    section8 = result[result.index("8.1 Gold"):result.index("9. 📅")]
    assert "Futures-Basis" not in section8
    assert "Qualitative Interpretation bleibt erhalten." in section8
    assert "Futures-Basis in einem späteren Abschnitt bleibt unangetastet." in result


def test_final_futures_basis_sanitizer_runs_before_final_repair_gate():
    """The sanitizer must run after final mutations and before the hard gate."""
    import ast

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    save = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "speichere_ergebnis"
    )
    sanitizer_calls = [
        node for node in ast.walk(save)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_entferne_futures_basis_duplikate_aus_abschnitt8"
    ]
    source_gate_calls = [
        node for node in ast.walk(save)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_pruefe_auswertung_quellenkonflikte"
    ]
    documented_gate_calls = [
        node for node in ast.walk(save)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_pruefe_dokumentierte_reparaturen"
    ]
    assert sanitizer_calls and source_gate_calls and documented_gate_calls
    sanitizer_lines = sorted(node.lineno for node in sanitizer_calls)
    source_gate_line = max(node.lineno for node in source_gate_calls)
    documented_gate_lines = sorted(node.lineno for node in documented_gate_calls)
    assert sanitizer_lines[0] < documented_gate_lines[0]
    assert source_gate_line < sanitizer_lines[-1] < documented_gate_lines[-1]
    source = source_path.read_text(encoding="utf-8")
    assert 'AUSGABE_ERSTELLT=PASS' in source
    assert 'FACHLICH_FINAL=NICHT_FINAL' in source



def test_edelmetall_conflict_is_written_into_report_without_blocking():
    """A conflicting 4W source is visible in the report and marked non-final."""
    import ast
    import re

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    helpers = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_dokumentiere_edelmetall_4w_quellenkonflikte",
        }
    ]
    namespace = {
        "re": re,
        "_edelmetall_4w_quellenkonflikte": lambda edel, markt: [
            "Gold: Markt-Briefing -2.8% vs. Edelmetalle-Briefing -4.8%"
        ],
    }
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(source_path), "exec"), namespace)
    document = namespace["_dokumentiere_edelmetall_4w_quellenkonflikte"]
    report = "8.1 Gold\nKurs: 100\n11. METHODIK / DATENQUALITÄT\nMethodiktext\n"
    result = document(report, "edel", "markt")
    assert "EDELMETALL-4W-QUELLENKONFLIKT | FACHLICH_FINAL=NICHT_FINAL" in result
    assert "Edelmetalle-Briefing -4.8%" in result
    assert result.index("EDELMETALL-4W-QUELLENKONFLIKT") < result.index("11. METHODIK")
    assert document(result, "edel", "markt").count("EDELMETALL-4W-QUELLENKONFLIKT") == 1

def test_technical_fallback_logs_nonfinal_before_early_return():
    """The fallback status must be emitted before the no-save early return."""
    import ast

    source_path = Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    save = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "speichere_ergebnis"
    )
    fallback_branch = next(
        node for node in ast.walk(save)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and isinstance(node.test.func, ast.Attribute)
        and node.test.func.attr == "startswith"
        and any(
            isinstance(arg, ast.Constant)
            and arg.value == "[GEMINI_TECHNISCHER_FALLBACK]"
            for arg in node.test.args
        )
        and any(
            isinstance(child, ast.Return)
            and isinstance(child.value, ast.Constant)
            and child.value.value is None
            for child in node.body
        )
    )
    return_index = next(
        i for i, node in enumerate(fallback_branch.body)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is None
    )
    prior_prints = [
        node for node in fallback_branch.body[:return_index]
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "print"
        and any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and "FACHLICH_FINAL=NICHT_FINAL" in arg.value
            for arg in node.value.args
        )
    ]
    assert prior_prints, "Fallback must log FACHLICH_FINAL=NICHT_FINAL before return None"

