from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    ROOT / "Trading_Radar_Alleinauswertung.py",
    ROOT / "gemini_auswertung.py",
)


def load_central_reader(source: Path):
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    wanted = {
        "_normalisiere_positionsname",
        "_normalisiere_ticker",
        "_trade_story_setup_universum",
        "_trade_story_zentrales_universum",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {"re": __import__("re"), "os": __import__("os"), "json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_only_final_valide_is_authoritative_in_both_radar_scripts(tmp_path):
    central = tmp_path / "Trade_Story_Universum.json"
    central.write_text(json.dumps({
        "schema_version": 2,
        "candidates": [
            {"ticker": "FIN", "name": "Final Ltd", "trade_story_status": "FINAL_VALIDE"},
            {"ticker": "PRE", "name": "Prefilter Valid Ltd", "trade_story_status": "VORFILTER_VALIDE"},
            {"ticker": "ATT", "name": "Prefilter Attention Ltd", "trade_story_status": "VORFILTER_ACHTUNG"},
            {"ticker": "NFI", "name": "Non Final Ltd", "trade_story_status": "NICHT_FINAL_QUALIFIZIERT"},
            {"ticker": "OLD", "name": "Legacy Ltd", "trade_story_status": "VALIDE SETUP"},
            {"ticker": "PRP", "name": "Prepared Ltd", "trade_story_status": "VORBEREITET"},
        ],
    }), encoding="utf-8")

    for source in SOURCES:
        ns = load_central_reader(source)
        files = {"Trade_Story_Universum(...).json": str(central)}
        valid, read, missing = ns["_trade_story_setup_universum"](files)
        assert ns["_normalisiere_ticker"]("FIN") in valid
        for ticker in ("PRE", "ATT", "NFI", "OLD", "PRP"):
            assert ns["_normalisiere_ticker"](ticker) not in valid
        assert read == {"Trade_Story_Universum(...).json"}
        assert not missing

        valid, prepared, available = ns["_trade_story_zentrales_universum"](files)
        assert available
        assert ns["_normalisiere_ticker"]("FIN") in valid
        assert ns["_normalisiere_ticker"]("NFI") in prepared
        assert ns["_normalisiere_ticker"]("PRP") in prepared
        for ticker in ("PRE", "ATT", "OLD"):
            assert ns["_normalisiere_ticker"](ticker) not in valid
            assert ns["_normalisiere_ticker"](ticker) not in prepared


def test_analyse_maps_raw_scanner_status_to_prefilter_labels():
    analyse = ROOT / "analyse.py"
    tree = ast.parse(analyse.read_text(encoding="utf-8"), filename=str(analyse))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_trade_story_status_label"
    )
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(analyse), "exec"), namespace)
    label = namespace["_trade_story_status_label"]
    assert label("VALIDE") == "VORFILTER_VALIDE"
    assert label("ACHTUNG") == "VORFILTER_ACHTUNG"
    assert label("GELAUFEN") == "NICHT_FINAL_QUALIFIZIERT"
    assert label("") == "NICHT_FINAL_QUALIFIZIERT"


def test_universe_json_contract_version_and_status_constants():
    source = (ROOT / "trade_story_universum.py").read_text(encoding="utf-8")
    for status in (
        "VORFILTER_VALIDE",
        "VORFILTER_ACHTUNG",
        "FINAL_VALIDE",
        "NICHT_FINAL_QUALIFIZIERT",
    ):
        assert status in source
    assert '"schema_version": 2' in source
