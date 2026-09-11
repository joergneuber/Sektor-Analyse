from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_setup_export_has_explicit_ticker_column():
    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    assert "df_clean.insert(0, 'Ticker', df_clean.index.astype(str).str.strip())" in source
    ast.parse(source)
