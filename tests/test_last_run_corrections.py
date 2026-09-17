import ast
import datetime as dt
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import trade_story_universum as tsu


ROOT = Path(__file__).resolve().parents[1]


def _load_function(source_path, function_names, extra_globals=None):
    source = Path(source_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {n for n in function_names}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert len(nodes) == len(wanted)
    namespace = {
        "__builtins__": __builtins__,
        "re": re,
        "dt": dt,
        "datetime": dt,
    }
    if extra_globals:
        namespace.update(extra_globals)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


def test_hebeltrader_time_shift_a_b_c_rule():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        observation = {
            "SRT3.DE": {
                "status": "KEIN KANDIDAT",
                "letzter_check": "2026-09-16",
                "last_candidate_date": "2026-09-16",
                "quelle": "HEBELTRADER 170/26",
            },
            "GOOGL": {
                "status": "KEIN KANDIDAT",
                "letzter_check": "2026-09-16",
                "last_candidate_date": "2026-09-16",
                "quelle": "HEBELTRADER 170/26",
            },
            "FME.DE": {
                "status": "KAUFKANDIDAT A",
                "letzter_check": "2026-09-16",
                "last_candidate_date": "2026-09-16",
                "quelle": "HEBELTRADER 170/26",
            },
            "EOG": {
                "status": "KAUFKANDIDAT B",
                "letzter_check": "2026-09-16",
                "last_candidate_date": "2026-09-16",
                "quelle": "HEBELTRADER 167/26",
            },
            "PANW": {
                "status": "KAUFKANDIDAT C",
                "letzter_check": "2026-09-16",
                "last_candidate_date": "2026-09-16",
                "quelle": "HEBELTRADER 170/26",
            },
        }
        obs = tmp / "einzel_check_beobachtung.json"
        obs.write_text(json.dumps(observation), encoding="utf-8")
        a = tmp / "Einzel_Check_A_Meldungen(2026-09-16).txt"
        a.write_text(
            "Name: Fresenius Medical Care AG | Ticker: FME.DE | KAUFKANDIDAT A\n",
            encoding="utf-8",
        )

        universe = tsu.build_trade_story_universe(
            {"Einzel_Check_A_Meldungen(...).txt": str(a)},
            observation_path=str(obs),
        )
        by_ticker = {x["ticker"]: x for x in universe["candidates"]}

        assert by_ticker["FME.DE"]["hebeltrader_status"] == "KAUFKANDIDAT A"
        assert by_ticker["FME.DE"]["trade_story_status"] == "VALIDE SETUP"
        assert by_ticker["EOG"]["hebeltrader_status"] == "KAUFKANDIDAT B"
        assert by_ticker["EOG"]["trade_story_status"] == "VORBEREITET"
        assert "SRT3.DE" not in by_ticker
        assert "GOOGL" not in by_ticker
        assert "PANW" not in by_ticker


def test_market_scores_are_removed_only_from_market_environment():
    ns = _load_function(
        ROOT / "gemini_auswertung.py",
        ["_entferne_marktumfeld_scores"],
    )
    fn = ns["_entferne_marktumfeld_scores"]
    text = (
        "MARKTUMFELD & GLOBALE RISIKOLAGE\n"
        "Fazit USA: Bärisch (Score 0.0) – EMA20/50 schwach.\n"
        "KOMPAKTE STICHPOINT-LISTE ZUM MARKTUMFELD\n"
        "Das Modell notiert nach dem Score-Modell auf Stufe Bärisch (Score 0,00).\n"
        "3. SYSTEMPERFORMANCE\n"
        "Setup Score: 0.75\n"
    )
    out = fn(text)
    assert "Score 0.0" not in out
    assert "Score-Modell" not in out
    assert "Setup Score: 0.75" in out


def test_fomc_official_statement_parses_new_target():
    import pandas as pd

    class FakeResponse:
        status_code = 200
        text = (
            "Federal Reserve issues FOMC statement. "
            "The Committee decided to raise the target range for the federal funds rate "
            "by 1/4 percentage point to 3-3/4 to 4 percent."
        )

    fake_requests = Mock()
    fake_requests.get.return_value = FakeResponse()

    ns = _load_function(
        ROOT / "makro_szenario.py",
        ["_rate_token_to_float", "_official_fomc_target_series"],
        {
            "pd": pd,
            "REQUEST_HEADERS": {},
            "requests": fake_requests,
            "dt": dt,
            "_fomc_meeting_dates": lambda year: [dt.date(2026, 9, 16)] if year == 2026 else [],
        },
    )
    df, source = ns["_official_fomc_target_series"]("DFEDTARU")
    assert float(df["DFEDTARU"].iloc[0]) == 4.0
    assert str(df["DATE"].iloc[0].date()) == "2026-09-16"
    assert source.endswith("monetary20260916a.htm")


def test_last_run_macro_values_are_corrected_and_market_score_is_removed():
    ns = _load_function(
        ROOT / "gemini_auswertung.py",
        ["_extrahiere_makro_referenzwerte", "_sichere_makro_kritische_kompaktangaben", "_entferne_marktumfeld_scores"],
        {"datetime": dt},
    )
    macro = (
        "Realzins 10Y TIPS: 2.4600 | Datenstand=2026-09-09\n"
        "US 2Y Treasury: 4.4300 | Datenstand=2026-09-09\n"
        "US 10Y Treasury: 4.8300 | Datenstand=2026-09-09\n"
        "2Y-10Y Spread: 0.4000 | STATUS=CALCULATED\n"
        "Nikkei 225: 63484.10 | Datenstand=2026-09-14\n"
        "VIX: 16.860001 | Datenstand=2026-09-16 | Letzter_Schluss=2026-09-15\n"
    )
    output = (
        "MARKTUMFELD & GLOBALE RISIKOLAGE\n"
        "Fazit USA: Bärisch (Score 0.0)\n"
        "Zinskurve (2J: 4,43% | 10J: 4,83%) ist mit einem Spread von +0,32 Prozentpunkten.\n"
        "Die TIPS-Realrendite 10Y notiert fest bei 4,83%.\n"
        "Nikkei 225: letzter abgeschlossener Handelstag (63.484,10) | Datenstand 15.09.2026\n"
        "VIX (Angstindex) notiert erhöht bei 16,86 / 17,20 Punkten.\n"
    )
    fixed, _ = ns["_sichere_makro_kritische_kompaktangaben"](output, macro)
    fixed = ns["_entferne_marktumfeld_scores"](fixed)
    assert "Score 0.0" not in fixed
    assert "Spread von 0,40 Prozentpunkten" in fixed
    assert "TIPS-Realrendite 10Y notiert fest bei 2,46%" in fixed
    assert "Datenstand 14.09.2026" in fixed
    assert "VIX (aktueller Tageswert) 16,86; letzter Schlusskurs 17,20" in fixed

def test_analyse_fomc_retro_uses_official_statement():
    import pandas as pd  # noqa: F401

    class FakeResponse:
        status_code = 200
        text = (
            "The Committee decided to raise the target range for the federal funds rate "
            "by 1/4 percentage point to 3-3/4 to 4 percent."
        )
        def raise_for_status(self):
            return None

    fake_requests = Mock()
    fake_requests.get.return_value = FakeResponse()
    ns = _load_function(
        ROOT / "analyse.py",
        ["_hole_offiziellen_fomc_korridor"],
        {"requests": fake_requests},
    )
    lower, upper, source = ns["_hole_offiziellen_fomc_korridor"](dt.date(2026, 9, 16))
    assert lower == 3.75
    assert upper == 4.0
    assert source.endswith("monetary20260916a.htm")

def test_source_contains_official_fomc_priority_and_no_market_score_instruction():
    macro = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    gemini = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    assert 'if series_id in {"DFEDTARU", "DFEDTARL"}:' in macro
    assert "_official_fomc_target_series(series_id)" in macro
    assert "MARKTUMFELD-AUSGABEREGEL" in gemini
    assert "_entferne_marktumfeld_scores(text)" in gemini


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("LAST_RUN_CORRECTIONS_TESTS: 6 PASS")
