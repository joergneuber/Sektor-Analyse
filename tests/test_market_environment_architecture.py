from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_analyse_keeps_benchmark_data_but_has_no_market_environment_classifier():
    src = (ROOT / "analyse.py").read_text(encoding="utf-8")
    assert "BENCHMARK_LEVELS = {}" in src
    assert "def klassifiziere_marktumfeld(" not in src
    assert "def klassifiziere_index(" not in src
    assert "us_score" not in src
    assert "eu_score" not in src
    assert "MARKTUMFELD (Score-Modell" not in src
    assert "Marktumfeld USA:" not in src
    assert "Marktumfeld Europa:" not in src


def test_short_keeps_benchmark_performance_but_has_no_market_environment_gate():
    src = (ROOT / "short_scanner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_pruefe_short_setup")
    args = [a.arg for a in fn.args.args]
    assert "bench_close" in args
    assert "marktumfeld_baerisch" not in args
    assert "klassifiziere_marktumfeld" not in src
    assert "marktumfeld_baerisch" not in src
    assert "Score-Modell" not in src
    assert "Heutiges Marktumfeld" not in src
    assert "RS_vs_Benchmark" in src
    assert "Rotation-Score" in src
    assert "sektor_momentum" not in src


def test_gemini_master_has_no_python_market_environment_score_or_short_modifikator():
    src = (ROOT / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md").read_text(encoding="utf-8")
    assert "Übernimm Einstufung UND Score WÖRTLICH" not in src
    assert "MARKTUMFELD (Score-Modell)" not in src
    assert "Marktumfeld-Abwertung" not in src
    assert "bärisches Marktumfeld wertet HIER auf" not in src
    assert "Python liefert hierfür ausschließlich die bereitgestellten Benchmark-, Markt-" in src
    assert "Erzeuge keinen eigenen numerischen Marktumfeld-Score" in src
    assert "Sektor-Modifikator" in src


def test_short_call_sites_no_longer_pass_market_environment_flag():
    src = (ROOT / "short_scanner.py").read_text(encoding="utf-8")
    assert "marktumfeld_baerisch_us" not in src
    assert "marktumfeld_baerisch_eu" not in src
    assert '_pruefe_short_setup, t, s, "US", us_daten[t], spy_close)' in src
    assert '_pruefe_short_setup, t, s, "EU", eu_daten[t], eu_bench_close)' in src


def test_all_short_call_sites_match_new_signature():
    for filename in ("einzel_check.py", "edelmetalle_scanner.py"):
        src = (ROOT / filename).read_text(encoding="utf-8")
        assert "_pruefe_short_setup(" in src
        assert "marktumfeld_baerisch" not in src


def test_edelmetall_short_briefing_has_no_market_environment_modifier():
    src = (ROOT / "edelmetalle_scanner.py").read_text(encoding="utf-8")
    assert "Marktumfeld-Modifikator" not in src
    assert "marktumfeld_baerisch" not in src


def test_all_runtime_short_call_boundaries_have_no_legacy_market_environment_argument():
    for filename in ("short_scanner.py", "einzel_check.py", "edelmetalle_scanner.py"):
        src = (ROOT / filename).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=filename)
        found = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct = isinstance(node.func, ast.Name) and node.func.id == "_pruefe_short_setup"
            submitted = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "_pruefe_short_setup"
            )
            if direct or submitted:
                found += 1
                assert all(kw.arg != "marktumfeld_baerisch" for kw in node.keywords)
        assert found >= 1, filename


def test_runtime_short_path_contains_no_legacy_market_environment_interface():
    for filename in ("analyse.py", "short_scanner.py", "einzel_check.py", "edelmetalle_scanner.py"):
        src = (ROOT / filename).read_text(encoding="utf-8")
        assert "marktumfeld_baerisch" not in src
        assert "Marktumfeld-Modifikator" not in src
        assert "MARKTUMFELD (Score-Modell" not in src


def test_short_and_edelmetall_scanners_do_not_emit_letter_grades():
    short = (ROOT / "short_scanner.py").read_text(encoding="utf-8")
    metals = (ROOT / "edelmetalle_scanner.py").read_text(encoding="utf-8")
    einzel = (ROOT / "einzel_check.py").read_text(encoding="utf-8")

    assert "Setup_Qualitaet" not in short
    assert "Setup_Qualitaet" not in metals
    assert "Setup_Qualitaet" not in einzel
    assert "-> +1 Stufe" not in short
    assert "Sektor-Modifikator" not in short
    tree = ast.parse(short)
    short_fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_pruefe_short_setup"
    )
    direction_fields = [
        (key, value)
        for node in ast.walk(short_fn) if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "Richtung"
    ]
    assert len(direction_fields) == 1
    assert isinstance(direction_fields[0][1], ast.Constant)
    assert direction_fields[0][1].value == "SHORT"

    main_fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    columns_node = next(
        node.value for node in ast.walk(main_fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SPALTEN" for target in node.targets)
    )
    columns = ast.literal_eval(columns_node)
    assert "Richtung" in columns
    assert "Setup_Qualitaet" not in columns
    assert "Risikohinweis" in columns

    # The independent Trendwende bonus is not a letter grade and remains.
    assert "Qualitaets_Bonus" in metals


def test_gemini_master_keeps_letter_grade_matrix_scoped_to_long_only():
    src = (ROOT / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md").read_text(encoding="utf-8")
    short_section = src.split("7. Short-Setups", 1)[1].split("8. Edelmetalle-Setups", 1)[0]
    metals_section = src.split("8. Edelmetalle-Setups", 1)[1].split("9.", 1)[0]
    metals_flat = " ".join(metals_section.split())

    assert "KEINE Buchstabennote" in short_section
    assert "Setup_Qualitaet" not in short_section
    assert "Setup-Qualität: [" not in short_section
    assert "Sektor-Momentum darf nur als separater Kontext" in short_section
    assert "KEINE Buchstabennote" in metals_section
    assert "Setup-Qualität: [" not in metals_section
    assert "Feinstufe aus der 6-stufigen Skala" not in metals_section
    assert "Long-Trendfolge-Matrix gilt nur für normale Aktien-Setups" in metals_flat
    assert "Trendwende-Qualitäts-Bonus bleibt als eigenständiges Feld" in metals_flat
    assert "Geltungsbereich: Die nachfolgende Setup-Qualitäts-Matrix" in src
    assert "ausschließlich für normale Long-Trendfolge-Aktien-" in src
