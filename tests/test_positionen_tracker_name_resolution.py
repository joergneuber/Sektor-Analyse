from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def _load_stammdaten_functions(yfinance_stub):
    source = (ROOT / "positionen_tracker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_bekannter_ticker_name", "vervollstaendige_stammdaten"}
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    assert {node.name for node in nodes} == wanted
    namespace = {
        "pd": pd,
        "yf": yfinance_stub,
        "TICKER_NAME_ALIASES": {"1ANT.F": "Anthropic PBC"},
        "ANLEITUNG_TICKER": "ANLEITUNG",
        "GOLD_FUTURES_TICKER": "GC=F",
        "GOLD_SPOT_TICKER": "XAU/USD",
        "_ist_gold_spot_ticker": lambda ticker: str(ticker).strip().upper() in {"XAU/USD", "XAUUSD"},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "positionen_tracker.py"), "exec"), namespace)
    return namespace["vervollstaendige_stammdaten"]


def test_1ant_f_ticker_placeholder_is_resolved_to_anthropic_without_yahoo():
    class ForbiddenYahoo:
        def Ticker(self, ticker):
            raise AssertionError("Der gepflegte 1ANT.F-Alias darf keine Yahoo-Abfrage benötigen.")

    resolve = _load_stammdaten_functions(ForbiddenYahoo())
    frame = pd.DataFrame([{
        "Ticker": "1ANT.F",
        "Name": "1ANT.F",
        "Sektor": "Technology",
        "Markt": "EU",
        "Waehrung": "EUR",
    }])

    result = resolve(frame)

    assert result.loc[0, "Name"] == "Anthropic PBC"
    assert result.loc[0, "Ticker"] == "1ANT.F"


def test_manually_entered_company_name_is_not_overwritten():
    class ForbiddenYahoo:
        def Ticker(self, ticker):
            raise AssertionError("Ein gepflegter Name darf nicht neu abgefragt werden.")

    resolve = _load_stammdaten_functions(ForbiddenYahoo())
    frame = pd.DataFrame([{
        "Ticker": "1ANT.F",
        "Name": "Manuell geprüfter Name",
        "Sektor": "Technology",
        "Markt": "EU",
        "Waehrung": "EUR",
    }])

    result = resolve(frame)

    assert result.loc[0, "Name"] == "Manuell geprüfter Name"


def test_generic_ticker_placeholder_triggers_yahoo_name_resolution():
    class YahooInfo:
        @property
        def info(self):
            return {"longName": "Example Holdings"}

    class Yahoo:
        def Ticker(self, ticker):
            return YahooInfo()

    resolve = _load_stammdaten_functions(Yahoo())
    frame = pd.DataFrame([{
        "Ticker": "EXAMPLE.DE",
        "Name": "EXAMPLE.DE",
        "Sektor": "Technology",
        "Markt": "EU",
        "Waehrung": "EUR",
    }])

    result = resolve(frame)

    assert result.loc[0, "Name"] == "Example Holdings"


def test_sold_position_performance_uses_manual_exit_without_current_quote():
    import datetime
    import math

    source = (ROOT / "positionen_tracker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "aktualisiere_positionen"
    )

    quote_calls = []

    def forbidden_quote(*args, **kwargs):
        quote_calls.append((args, kwargs))
        raise AssertionError("Geschlossene manuelle Verkäufe dürfen keinen Live-Kurs benötigen.")

    def safe_float(value, ticker=None, field=None):
        try:
            number = float(str(value).replace(",", "."))
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    namespace = {
        "datetime": datetime,
        "pd": pd,
        "sicheres_float": safe_float,
        "ermittle_richtung": lambda row, stop, entry: str(row.get("Richtung", "")).strip().lower() == "short",
        "hole_aktuellen_kurs": forbidden_quote,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / "positionen_tracker.py"), "exec"), namespace)

    frame = pd.DataFrame([{
        "Ticker": "1ANT.F",
        "Status": "Verkauft",
        "Richtung": "Long",
        "Einstieg": 75.75,
        "Stop": 70.00,
        "Aktueller_Kurs": "",
        "Ausstiegsdatum": "19.11.2026",
        "Ausstiegskurs": 88.38,
        "Performance_Seit_Einstieg%": "",
    }])
    # Reproduziert Pandas-3-StringDtype aus all-text Google-Sheets-Spalten.
    frame["Performance_Seit_Einstieg%"] = frame["Performance_Seit_Einstieg%"].astype("string")

    result, alerts = namespace["aktualisiere_positionen"](frame)

    assert result.loc[0, "Performance_Seit_Einstieg%"] == 16.67
    assert result.loc[0, "Aktueller_Kurs"] == ""
    assert alerts == []
    assert quote_calls == []


def test_new_1ant_f_position_uses_alias_without_yahoo_lookup():
    import ast
    import datetime
    import math

    source = (ROOT / "positionen_tracker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "ergaenze_neue_zeilen"
    )

    class ForbiddenYahoo:
        def Ticker(self, ticker):
            raise AssertionError("Der bekannte 1ANT.F-Alias darf keinen Yahoo-Aufruf auslösen.")

    def safe_float(value, ticker=None, field=None):
        try:
            number = float(str(value).replace(",", "."))
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    namespace = {
        "datetime": datetime,
        "pd": pd,
        "yf": ForbiddenYahoo(),
        "ANLEITUNG_TICKER": "ANLEITUNG",
        "GOLD_FUTURES_TICKER": "GC=F",
        "GOLD_SPOT_TICKER": "XAU/USD",
        "TICKER_NAME_ALIASES": {"1ANT.F": "Anthropic PBC"},
        "_bekannter_ticker_name": lambda ticker: {"1ANT.F": "Anthropic PBC"}.get(str(ticker).upper()),
        "_ist_gold_spot_ticker": lambda ticker: str(ticker).strip().upper() in {"XAU/USD", "XAUUSD"},
        "_ist_trendfolge_position": lambda row: False,
        "sicheres_float": safe_float,
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / "positionen_tracker.py"), "exec"),
        namespace,
    )

    frame = pd.DataFrame([{
        "Ticker": "1ANT.F",
        "Name": "",
        "Sektor": "",
        "Markt": "",
        "Waehrung": "",
        "Richtung": "Long",
        "Einstieg": "75.75",
        "Stop": "70.00",
        "TP1": "",
        "TP2": "",
        "Status": "",
        "Einstiegsdatum": "",
        "Produkt_Typ": "",
    }])
    # TP-Zellen aus Google Sheets können in Pandas 3 als StringDtype vorliegen.
    frame["TP1"] = frame["TP1"].astype("string")
    frame["TP2"] = frame["TP2"].astype("string")

    result = namespace["ergaenze_neue_zeilen"](frame)

    assert result.loc[0, "Name"] == "Anthropic PBC"
    assert result.loc[0, "Markt"] == "EU"
    assert result.loc[0, "Waehrung"] == "EUR"
    assert result.loc[0, "Status"] == "Offen"
