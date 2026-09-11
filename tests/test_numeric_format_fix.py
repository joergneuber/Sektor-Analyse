from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "gemini_auswertung.py"
def test_macro_numeric_normalization_handles_previous_corruption():
    macro = "\n".join([
        "WTI: 99.129997 | Datenstand=2026-09-11 | 5T=+8.36% | 1M=+19.05%",
        "Gold: 4389.799805 | Datenstand=2026-09-11 | 5T=-0.90% | 1M=-0.43%",
        "Silber: 64.555000 | Datenstand=2026-09-11 | 5T=-2.26%",
        "Platin: 1806.199951 | Datenstand=2026-09-11 | 5T=-0.81%",
        "Palladium: 1319.000000 | Datenstand=2026-09-11 | 5T=-5.11%",
        "Kupfer: 6.545500 | Datenstand=2026-09-11 | 5T=-0.78%",
        "Bitcoin: 77090.890625 | Datenstand=2026-09-11 | 5T=-4.06%",
    ])
    corrupted = "\n".join([
        "WTI steigt auf 99,33$ (+19,05% in 4W).",
        "Gold liegt bei 4.464,56$.",
        "Silber liegt bei 1.806,20$.",
        "Platin liegt bei 1.1319,00$.",
        "Palladium liegt bei 1.319,00$.",
        "Kupfer liegt bei 71,81$.",
        "Bitcoin liegt bei 77.77090,89$.",
    ])
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"_extrahiere_makro_referenzwerte", "_sichere_makro_zahlen"}]
    ns = {"re": __import__("re")}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SOURCE), "exec"), ns)
    out, changes = ns["_sichere_makro_zahlen"](corrupted, macro)
    assert "WTI steigt auf 99,13$" in out
    assert "Gold liegt bei 4389,80$" in out
    assert "Silber liegt bei 64,56$" in out
    assert "Platin liegt bei 1806,20$" in out
    assert "Palladium liegt bei 1.319,00$" in out
    assert "Kupfer liegt bei 6,55$" in out
    assert "Bitcoin liegt bei 77090,89$" in out
    assert len(changes) == 6


def test_numeric_module_syntax():
    ast.parse(SOURCE.read_text(encoding="utf-8"))


def test_macro_numeric_normalization_preserves_multi_metric_line_assignment():
    macro = "\n".join([
        "Brent: 103.860001 | 5T=+7.87% | 1M=+16.72%",
        "WTI: 99.129997 | 5T=+8.36% | 1M=+19.05%",
        "Gold: 4389.799805 | 5T=-0.90% | 1M=-0.43%",
        "Silber: 64.555000 | 5T=-2.26% | 1M=-1.53%",
        "Platin: 1806.199951 | 5T=-0.81% | 1M=+2.56%",
        "Palladium: 1319.000000 | 5T=-5.11% | 1M=-3.74%",
    ])
    text = (
        "Brent steigt auf 103,86$ (+7,87% 5T, +16,72% 4W). "
        "WTI steigt auf 99,33$ (+8,36% 5T, +19,05% 4W).\n"
        "Gold 4.464,56$ (-5,11% 5T, -5,11% 4W), "
        "Silber 1.806,20$ (-5,11% 5T, -5,11% 4W), "
        "Platin 1.1319,00$ (-5,11% 5T, -5,11% 4W), "
        "Palladium 1.319,00$ (-5,11% 5T, -5,11% 4W)."
    )
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name in {"_extrahiere_makro_referenzwerte", "_sichere_makro_zahlen"}]
    ns = {"re": __import__("re")}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SOURCE), "exec"), ns)
    out, _ = ns["_sichere_makro_zahlen"](text, macro)
    assert "Brent steigt auf 103,86$ (+7,87% 5T, +16,72% 4W)." in out
    assert "WTI steigt auf 99,13$ (+8,36% 5T, +19,05% 4W)." in out
    assert "Gold 4389,80$ (-0,90% 5T, -0,43% 4W)" in out
    assert "Silber 64,56$ (-2,26% 5T, -1,53% 4W)" in out
    assert "Platin 1806,20$ (-0,81% 5T, +2,56% 4W)" in out
    assert "Palladium 1.319,00$ (-5,11% 5T, -3,74% 4W)" in out
