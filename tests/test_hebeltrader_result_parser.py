from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


def _module(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=None))
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=None))
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))
    sys.modules.pop("hebeltrader_einzel_check", None)
    return importlib.import_module("hebeltrader_einzel_check")


def test_missing_samsung_result_cannot_consume_following_royal_gold_result(monkeypatch):
    module = _module(monkeypatch)
    stdout = """SSNLF - Samsung Electronics Co., Ltd.   (Sektor: N/A)
==============================================================
SSNLF -> Daten von Alpaca leer.
TRENDFOLGE: kein Setup (Grund siehe DEBUG-Zeilen oben)
==============================================================
RGLD - Royal Gold, Inc.   (Sektor: Gold-Miner | Sektor-ETF: GDX)
==============================================================
DEBUG-TRENDWENDE-VERWORFEN: RGLD | Spannen-Position 72%
  KAUFKANDIDATEN-BEWERTUNG
  Ergebnis: KEIN KANDIDAT (Momentum 1/4)
    ✓ Sektor-RS positiv (+8.5 %-Pkt.)
    ⚠ Momentum zu schwach (1/4)
==============================================================
"""

    parsed = {
        row["ticker"]: row
        for row in module._parse_einzel_ergebnisse(stdout, ["SSNLF", "RGLD"])
    }

    assert parsed["SSNLF"]["status"] == "NICHT AUSGELESEN"
    assert parsed["SSNLF"]["momentum"] is None
    assert "Alpaca leer" in " ".join(parsed["SSNLF"]["technischer_zustand"])
    assert "Kein Ergebnis im eigenen Ticker-Abschnitt" in parsed["SSNLF"]["auslesegrund"]

    assert parsed["RGLD"]["status"] == "KEIN KANDIDAT"
    assert parsed["RGLD"]["momentum"] == "1/4"
    assert any("Sektor-RS positiv (+8.5" in line
               for line in parsed["RGLD"]["technischer_zustand"])
    assert not any("Samsung" in line or "SSNLF" in line
                   for line in parsed["RGLD"]["technischer_zustand"])


def test_valid_a_b_c_results_are_parsed_from_their_own_blocks(monkeypatch):
    module = _module(monkeypatch)
    stdout = """AAA - Alpha Inc. (Sektor: Technologie)
  Ergebnis: KAUFKANDIDAT A (Momentum 4/4)
    ✓ bestätigtes Setup
==============================================================
BBB - Beta Inc. (Sektor: Technologie)
  Ergebnis: KAUFKANDIDAT B (Momentum 3/4)
    ✓ Vorbereitung
==============================================================
CCC - Gamma Inc. (Sektor: Technologie)
  Ergebnis: KAUFKANDIDAT C (Momentum 2/4)
    ⚠ noch kein Einstieg
==============================================================
"""

    parsed = module._parse_einzel_ergebnisse(stdout, ["AAA", "BBB", "CCC"])

    assert [(x["ticker"], x["status"], x["momentum"]) for x in parsed] == [
        ("AAA", "KAUFKANDIDAT A", "4/4"),
        ("BBB", "KAUFKANDIDAT B", "3/4"),
        ("CCC", "KAUFKANDIDAT C", "2/4"),
    ]


def test_missing_or_duplicate_ticker_blocks_fail_closed(monkeypatch):
    module = _module(monkeypatch)
    missing = "AAA - Alpha Inc. (Sektor: Technologie)\nErgebnis: KEIN KANDIDAT (Momentum 0/4)\n"
    parsed_missing = module._parse_einzel_ergebnisse(missing, ["ZZZ"])[0]
    assert parsed_missing["status"] == "NICHT AUSGELESEN"
    assert parsed_missing["momentum"] is None

    duplicate = """AAA - Alpha Inc. (Sektor: Technologie)
Ergebnis: KEIN KANDIDAT (Momentum 0/4)
BBB - Beta Inc. (Sektor: Technologie)
Ergebnis: KEIN KANDIDAT (Momentum 0/4)
AAA - Alpha Inc. (Sektor: Technologie)
Ergebnis: KAUFKANDIDAT A (Momentum 4/4)
"""
    parsed_duplicate = module._parse_einzel_ergebnisse(duplicate, ["AAA"])[0]
    assert parsed_duplicate["status"] == "NICHT AUSGELESEN"
    assert "2 eigene Ticker-Abschnitte" in parsed_duplicate["auslesegrund"]


def test_unreadable_hebeltrader_status_is_not_promoted_into_trade_story_universe(tmp_path):
    import json
    from pathlib import Path

    from trade_story_universum import build_trade_story_universe

    state = Path(tmp_path) / "hebeltrader_einzel_check.json"
    state.write_text(json.dumps({
        "candidates": [{
            "ticker": "SSNLF",
            "name": "Samsung Electronics Co., Ltd.",
            "einzel_check": {
                "ticker": "SSNLF",
                "status": "NICHT AUSGELESEN",
                "momentum": None,
            },
        }],
    }), encoding="utf-8")

    universe = build_trade_story_universe({"HEBELTRADER-Einzelcheck": str(state)})

    assert all(item.get("ticker") != "SSNLF" for item in universe["candidates"])


def test_stored_state_reparse_corrects_cross_contamination_and_is_idempotent(monkeypatch):
    module = _module(monkeypatch)
    state = {
        "schema_version": 3,
        "issue_number": 211,
        "issue_label": "HEBELTRADER 211/26",
        "processed_at": "2026-11-12T19:00:00+01:00",
        "custom_metadata": {"must_survive": True},
        "einzel_check_stdout": """SSNLF - Samsung Electronics Co., Ltd. (Sektor: N/A)
SSNLF -> Daten von Alpaca leer.
TRENDFOLGE: kein Setup (Grund siehe DEBUG-Zeilen oben)
RGLD - Royal Gold, Inc. (Sektor: Gold-Miner)
DEBUG-TRENDWENDE-VERWORFEN: RGLD | Spannen-Position 72%
Ergebnis: KEIN KANDIDAT (Momentum 1/4)
  ✓ Sektor-RS positiv (+8.5 %-Pkt.)
  ⚠ Momentum zu schwach (1/4)
""",
        "candidates": [
            {
                "ticker": "SSNLF",
                "name": "Samsung Electronics Co., Ltd.",
                "role": "peer",
                "confidence": 0.8,
                "yahoo_valid": True,
                "einzel_check": {
                    "ticker": "SSNLF",
                    "status": "KEIN KANDIDAT",
                    "momentum": "1/4",
                    "technischer_zustand": ["fälschlich RGLD zugeordnet"],
                    "legacy_extra": "muss erhalten bleiben",
                },
            },
            {
                "ticker": "RGLD",
                "name": "Royal Gold, Inc.",
                "role": "peer",
                "confidence": 0.7,
                "yahoo_valid": True,
                "einzel_check": {"ticker": "RGLD", "status": "KEIN KANDIDAT"},
            },
        ],
    }

    repaired = module.wiederaufbereite_gespeicherten_state(state)
    by_ticker = {row["ticker"]: row for row in repaired["candidates"]}
    assert by_ticker["SSNLF"]["einzel_check"]["status"] == "NICHT AUSGELESEN"
    assert by_ticker["SSNLF"]["einzel_check"]["momentum"] is None
    assert "auslesegrund" in by_ticker["SSNLF"]["einzel_check"]
    assert by_ticker["RGLD"]["einzel_check"]["status"] == "KEIN KANDIDAT"
    assert by_ticker["RGLD"]["einzel_check"]["momentum"] == "1/4"
    assert by_ticker["SSNLF"]["einzel_check"]["legacy_extra"] == "muss erhalten bleiben"
    assert by_ticker["SSNLF"]["role"] == "peer"
    assert repaired["issue_number"] == 211
    assert repaired["custom_metadata"] == {"must_survive": True}

    reparsed_again = module.wiederaufbereite_gespeicherten_state(repaired)
    assert reparsed_again == repaired


def test_persisted_raw_to_candidate_json_to_trade_story_universe_and_next_run_recovery(
    monkeypatch, tmp_path
):
    import json
    from pathlib import Path

    module = _module(monkeypatch)
    from trade_story_universum import build_trade_story_universe

    raw = """SSNLF - Samsung Electronics Co., Ltd. (Sektor: N/A)
SSNLF -> Daten von Alpaca leer.
TRENDFOLGE: kein Setup (Grund siehe DEBUG-Zeilen oben)
RGLD - Royal Gold, Inc. (Sektor: Gold-Miner)
Ergebnis: KEIN KANDIDAT (Momentum 1/4)
  ✓ Sektor-RS positiv (+8.5 %-Pkt.)
"""
    state = {
        "schema_version": 3,
        "issue_number": 211,
        "issue_label": "HEBELTRADER 211/26",
        "einzel_check_stdout": raw,
        "candidates": [
            {
                "ticker": "SSNLF",
                "name": "Samsung Electronics Co., Ltd.",
                "einzel_check": {"ticker": "SSNLF", "status": "KEIN KANDIDAT", "momentum": "1/4"},
            },
            {
                "ticker": "RGLD",
                "name": "Royal Gold, Inc.",
                "einzel_check": {"ticker": "RGLD", "status": "KEIN KANDIDAT"},
            },
        ],
    }
    state_path = Path(tmp_path) / "hebeltrader_einzel_check.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # Tatsächlicher Recovery-Pfad: persistierten (kontaminierten) Stand laden,
    # ausschließlich den gespeicherten Roh-Output erneut parsen und JSON sichern.
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    repaired = module.wiederaufbereite_gespeicherten_state(stored)
    state_path.write_text(json.dumps(repaired, ensure_ascii=False), encoding="utf-8")
    reloaded = json.loads(state_path.read_text(encoding="utf-8"))
    universe = build_trade_story_universe({"HEBELTRADER-Einzelcheck": str(state_path)})
    by_ticker = {row["ticker"]: row for row in universe["candidates"]}

    assert "SSNLF" not in by_ticker
    assert by_ticker["RGLD"]["hebeltrader_status"] == "KEIN KANDIDAT"
    assert by_ticker["RGLD"]["trade_story_status"] == "KEIN KANDIDAT"
    assert reloaded["candidates"][0]["einzel_check"]["status"] == "NICHT AUSGELESEN"

    # NICHT AUSGELESEN ist nur der Status dieses Laufs: Ein frischer Folgelauf
    # mit einem regulären A-Ergebnis darf denselben Ticker wieder aufnehmen.
    next_raw = "SSNLF - Samsung Electronics Co., Ltd. (Sektor: Technologie)\nErgebnis: KAUFKANDIDAT A (Momentum 4/4)\n  ✓ Setup bestätigt\n"
    next_parsed = module._parse_einzel_ergebnisse(next_raw, ["SSNLF"])[0]
    next_state = {
        "schema_version": 3,
        "issue_number": 212,
        "einzel_check_stdout": next_raw,
        "candidates": [{
            "ticker": "SSNLF",
            "name": "Samsung Electronics Co., Ltd.",
            "einzel_check": next_parsed,
        }],
    }
    state_path.write_text(json.dumps(next_state, ensure_ascii=False), encoding="utf-8")
    next_universe = build_trade_story_universe({"HEBELTRADER-Einzelcheck": str(state_path)})
    next_by_ticker = {row["ticker"]: row for row in next_universe["candidates"]}
    assert next_by_ticker["SSNLF"]["hebeltrader_status"] == "KAUFKANDIDAT A"


def test_manual_reparse_command_uses_backup_and_never_calls_external_providers(
    monkeypatch, tmp_path
):
    import json
    from pathlib import Path

    module = _module(monkeypatch)
    state_path = Path(tmp_path) / "hebeltrader_einzel_check.json"
    original = {
        "issue_number": 211,
        "issue_label": "HEBELTRADER 211/26",
        "einzel_check_stdout": """AAA - Alpha Inc. (Sektor: Technologie)
Ergebnis: KAUFKANDIDAT A (Momentum 4/4)
BBB - Beta Inc. (Sektor: Technologie)
keine Kursdaten
""",
        "candidates": [
            {"ticker": "AAA", "name": "Alpha Inc.", "einzel_check": {"status": "KEIN KANDIDAT"}},
            {"ticker": "BBB", "name": "Beta Inc.", "einzel_check": {"status": "KEIN KANDIDAT"}},
        ],
    }
    original_bytes = json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8")
    state_path.write_bytes(original_bytes)
    monkeypatch.setattr(module, "STATE_FILE", str(state_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("Externe Abfrage oder erneuter Einzel-Check wurde unerwartet aufgerufen.")

    monkeypatch.setattr(module, "extrahiere_kandidaten", forbidden)
    monkeypatch.setattr(module, "yahoo_validiere", forbidden)
    monkeypatch.setattr(module, "fuehre_einzel_check_aus", forbidden)

    assert module.main(["--reparse-stored"]) == 0
    backup_path = Path(str(state_path) + ".pre-reparse.bak")
    assert backup_path.read_bytes() == original_bytes
    repaired = json.loads(state_path.read_text(encoding="utf-8"))
    statuses = {row["ticker"]: row["einzel_check"]["status"] for row in repaired["candidates"]}
    assert statuses == {"AAA": "KAUFKANDIDAT A", "BBB": "NICHT AUSGELESEN"}

    # Ein zweiter Reparaturversuch darf das vorhandene forensische Backup nicht
    # überschreiben oder den persistierten Stand erneut verändern.
    repaired_bytes = state_path.read_bytes()
    try:
        module.main(["--reparse-stored"])
    except FileExistsError:
        pass
    else:
        raise AssertionError("Vorhandenes Backup wurde nicht als Schutzsperre erkannt.")
    assert state_path.read_bytes() == repaired_bytes
    assert backup_path.read_bytes() == original_bytes


def test_workflow_exposes_manual_reparse_and_requires_backup_before_drive_upload():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "hebeltrader_einzel_check.yml"
    ).read_text(encoding="utf-8")
    assert "reparse_stored:" in workflow
    assert "if: ${{ inputs.reparse_stored }}" in workflow
    assert "python hebeltrader_einzel_check.py --reparse-stored" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert workflow.index("actions/upload-artifact@v4") < workflow.index(
        "HEBELTRADER-Ergebnis und Watchlist in Drive sichern"
    )
