"""Regression tests for the persistent Einzel-Check OHLCV universe workflow."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]


def test_dynamic_universe_is_not_restored_from_or_saved_to_actions_cache():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    restore = _section(
        text,
        "- name: Historische OHLCV-Datenbank wiederherstellen",
        "- name: Historische OHLCV-Datenbank und dynamisches Universum aus Dauerarchiv wiederherstellen",
    )
    save = _section(
        text,
        "- name: Historische OHLCV-Datenbank speichern",
        "# Zusaetzlich wird der aktuelle Research-Bestand",
    )
    assert "historical_ohlcv_universe.json" not in restore
    assert "historical_ohlcv_universe.json" not in save


def test_dynamic_universe_is_always_refreshed_from_release():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    restore = _section(
        text,
        "- name: Historische OHLCV-Datenbank und dynamisches Universum aus Dauerarchiv wiederherstellen",
        "- name: Historische OHLCV-Datenbank aktualisieren",
    )
    assert "rm -f historical_ohlcv_universe.json" in restore
    assert "gh release download historical-ohlcv --pattern historical_ohlcv_universe.json" in restore
    assert "if [ ! -f historical_ohlcv_universe.json ]" not in restore
