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
    assert '_pruefe_short_setup, t, s, "US", us_daten[t], spy_close, momentum_us.get(s)' in src
    assert '_pruefe_short_setup, t, s, "EU", eu_daten[t], eu_bench_close, momentum_eu.get(s)' in src
