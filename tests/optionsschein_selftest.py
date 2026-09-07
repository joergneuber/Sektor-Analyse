"""Deterministischer Regressionstest fuer die Optionsschein-Integration.

Der Test ist absichtlich ohne Live-Netzwerkabhaengigkeit. Der echte HTTP-Abruf
wird durch repräsentatives HTML der Börse-Stuttgart-Produktseite simuliert.
Damit werden Parser, Priorität, Stale-Data-Verhalten, Zeitstempel und deutsches
CSV-Format zuverlässig regressiongetestet. Der produktive Workflow führt diesen
Test vor positionen_tracker.py aus.
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from os_kurse import (  # noqa: E402
    berechne_os_performance,
    parse_deutsche_zahl,
    parse_stuttgart_html,
)


STUTTGART_HTML = """
<html><body>
WKN GW5GT1
Geld 2,12 Brief 2,13
Letzter Preis 2,10 G
Kurszeit 21.08.2026 / 19:59:47 Uhr Tagesvolumen 0
</body></html>
"""

STUTTGART_NO_BID_HTML = """
<html><body>
WKN PM0216
Geld - Brief -
Letzter Preis 1,27 G
Kurszeit 07.08.2026 / 10:59:07 Uhr Tagesvolumen 0
</body></html>
"""

STUTTGART_NO_PRICE_HTML = """
<html><body>
WKN UN4N9T
Geld - Brief -
Letzter Preis -
Kurszeit 07.08.2026 / 11:00:00 Uhr Tagesvolumen 0
</body></html>
"""


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> None:
    # 1) Deutsches Zahlenformat
    cases = {
        "0,91": 0.91,
        "1,234": 1.234,
        "1.234,56": 1234.56,
        "2.20": 2.20,
        "1234.56": 1234.56,
    }
    for raw, expected in cases.items():
        got = parse_deutsche_zahl(raw)
        if got is None or not math.isclose(got, expected, rel_tol=0, abs_tol=1e-12):
            fail(f"Zahlenparser: {raw!r} -> {got!r}, erwartet {expected!r}")
    print("PASS 1/10: deutsches Zahlenformat")

    # 2) Primärfall: Geldkurs wird bevorzugt
    q = parse_stuttgart_html(STUTTGART_HTML, "GW5GT1")
    assert q.geld == 2.12, q
    assert q.brief == 2.13, q
    assert q.letzter_kurs == 2.10, q
    assert q.aktueller_kurs == 2.12, q
    assert q.quelle == "boerse_stuttgart_geld", q
    assert q.kurszeit == "21.08.2026 / 19:59:47 Uhr", q
    print("PASS 2/10: Geldkurs-Priorisierung + Kurszeit")

    # 3) Wenn kein Geldkurs vorhanden ist, wird der letzte Preis verwendet.
    q2 = parse_stuttgart_html(STUTTGART_NO_BID_HTML, "PM0216")
    assert q2.geld is None and q2.brief is None, q2
    assert q2.letzter_kurs == 1.27, q2
    assert q2.aktueller_kurs == 1.27, q2
    assert q2.quelle == "boerse_stuttgart_letzter", q2
    print("PASS 3/10: Last-Price-Fallback")

    # 4) Kein echter Kurs: Parser verweigert bewusst einen Ersatzwert.
    try:
        parse_stuttgart_html(STUTTGART_NO_PRICE_HTML, "UN4N9T")
    except Exception as exc:
        assert type(exc).__name__ == "OptionsscheinAbrufFehler", exc
    else:
        fail("Kein echter Kurs darf keinen Ersatzwert liefern.")
    print("PASS 4/10: kein echter Kurs -> kein Ersatzkurs")

    # 5) Es gibt keinen manuellen OS-Kurs mehr: automatische Quelle ist allein maßgeblich.
    result = berechne_os_performance(5.01, 5.50)
    assert result == (9.78, "automatisch"), result
    print("PASS 5/10: automatischer Kurs ist alleinige Quelle")

    # 6) Automatischer Kurs liefert echte Performance.
    result = berechne_os_performance(5.01, 5.50)
    assert result == (9.78, "automatisch"), result
    print("PASS 6/10: automatischer Kurs liefert echte Performance")

    # 7) Kein echter Kurs -> keine Schätzung, keine Performance.
    result = berechne_os_performance(5.01, None)
    assert result is None, result
    print("PASS 7/10: kein echter Kurs -> keine Schätzung")

    # 8) Stale-Data-Sicherheitsregel: fehlender automatischer Kurs wird
    #    nicht durch einen geschätzten Wert ersetzt.
    result = berechne_os_performance(2.20, None)
    assert result is None, result
    print("PASS 8/10: kein Stale-Data-/Schätzungs-Fallback")

    # 9) Deutsche CSV-Roundtrip mit den für den Check relevanten OS-Feldern.
    fixture = pd.DataFrame([{
        "Ticker": "UNH",
        "Produkt_Typ": "Optionsschein",
        "OS_Einstiegskurs": 5.01,
        "OS_Aktueller_Kurs": 5.50,
        "OS_Geld": 5.50,
        "OS_Brief": 5.60,
        "OS_Spread": 0.10,
        "OS_Performance%": 9.78,
        "OS_Quelle": "automatisch",
        "OS_WKN": "HM0ZHD",
    }])
    buf = io.StringIO()
    fixture.to_csv(buf, sep=";", decimal=",", index=False, encoding="utf-8-sig")
    raw = buf.getvalue()
    if "5,01" not in raw or "5,5" not in raw or "9,78" not in raw:
        fail(f"Deutsches CSV-Format fehlerhaft: {raw!r}")
    back = pd.read_csv(io.StringIO(raw), sep=";", decimal=",")
    assert back.loc[0, "OS_WKN"] == "HM0ZHD", back
    assert back.loc[0, "OS_Quelle"] == "automatisch", back
    assert math.isclose(float(back.loc[0, "OS_Aktueller_Kurs"]), 5.50), back
    assert math.isclose(float(back.loc[0, "OS_Performance%"]), 9.78), back
    print("PASS 9/10: deutscher CSV-Roundtrip / Übergabefelder")

    # 10) Echter lokaler Übergabetest gegen offene_positionen_check.py.
    # yfinance wird nur für den Import des bestehenden Check-Moduls stubbed;
    # der eigentliche Positions-/CSV-Pfad wird unverändert ausgeführt.
    import types
    fake_yf = types.ModuleType("yfinance")
    class _Ticker:
        def __init__(self, *args, **kwargs):
            pass
    fake_yf.Ticker = _Ticker
    sys.modules.setdefault("yfinance", fake_yf)

    import offene_positionen_check as opc  # noqa: E402
    from tempfile import TemporaryDirectory

    base_rows = [
        ("SIX2.DE", "Uni Credit", 15.80, 0.51, "UN68EW", 0.44),
        ("BABA", "BNP Paribas", 11.97, 2.20, "PM0216", 0.95),
        ("ALB", "BNP Paribas", 9.45, 3.99, "PM026C", 1.10),
        ("FCX", "BNP Paribas", 3.64, 1.19, "PK9UHB", 2.00),
        ("NEM", "Goldman Sachs", 5.20, 2.18, "GW5GT1", 2.30),
        ("ENR.DE", "", 1.75, 0.30, "UN4N9T", 0.60),
        ("UNH", "", 7.13, 5.01, "HM0ZHD", 5.50),
    ]
    fixture_rows = []
    for ticker, emittent, hebel, os_entry, wkn, auto_quote in base_rows:
        source = "automatisch"
        effective = float(auto_quote)
        os_perf = round(((effective - float(os_entry)) / float(os_entry)) * 100, 2)
        fixture_rows.append({
            "Ticker": ticker, "Name": ticker, "Sektor": "Test", "Markt": "US" if "." not in ticker else "EU",
            "Waehrung": "USD" if "." not in ticker else "EUR", "Richtung": "Long",
            "Ideen_Quelle": "Trendfolge", "Einstiegsdatum": "03.09.2026", "Einstieg": 100.0,
            "Aktueller_Kurs": 99.0, "Stop": 90.0, "TP1": 110.0, "TP2": 120.0, "Status": "Offen",
            "Produkt_Typ": "Optionsschein", "Emittent": emittent, "Hebel": hebel,
            "OS_Einstiegskurs": os_entry, "OS_Aktueller_Kurs": auto_quote,
            "OS_Geld": auto_quote, "OS_Brief": round(auto_quote + 0.01, 2), "OS_Spread": 0.01,
            "OS_Performance%": os_perf, "OS_Quelle": source, "OS_WKN": wkn,
            "OS_Kurszeit": "07.09.2026 / 08:00:00 Uhr", "OS_Kursquelle": "boerse_stuttgart_geld",
        })
    with TemporaryDirectory() as td:
        td = Path(td)
        input_path = td / "Offene_Positionen.csv"
        output_path = td / "Offene Positionen+Check.csv"
        history_path = td / "Geschlossene Positionen.csv"
        pd.DataFrame(fixture_rows).to_csv(input_path, sep=";", decimal=",", index=False, encoding="utf-8-sig")

        # Nur technische Marktdaten simulieren; die OS-Spalten kommen aus der
        # echten Eingabedatei und werden durch make_row() übernommen.
        opc.fetch_history = lambda ticker: pd.DataFrame()
        opc.analyze_technical = lambda hist, row: opc.TechnicalResult(
            close=99.0, data_quality="Testdaten", note="Regressionstest"
        )

        out, _ = opc.run_local(str(input_path), str(output_path), str(history_path))
        for col in [
            "Produkt_Typ", "Emittent", "OS_Einstiegskurs", "OS_Aktueller_Kurs",
            "OS_Geld", "OS_Brief", "OS_Spread", "OS_Performance%", "OS_Quelle",
            "OS_WKN", "OS_Kursquelle"
        ]:
            if col not in out.columns:
                fail(f"Check-Übergabe: Spalte {col!r} fehlt")
        expected = {
            "UN68EW": ("automatisch", 0.44),
            "PM0216": ("automatisch", 0.95),
            "PM026C": ("automatisch", 1.10),
            "PK9UHB": ("automatisch", 2.00),
            "GW5GT1": ("automatisch", 2.30),
            "UN4N9T": ("automatisch", 0.60),
            "HM0ZHD": ("automatisch", 5.50),
        }
        if len(out) != 7:
            fail(f"Check-Übergabe: erwartet 7 Optionsscheinpositionen, erhalten {len(out)}")
        for _, r in out.iterrows():
            wkn = r["OS_WKN"]
            source, expected_quote = expected[wkn]
            if r["OS_Quelle"] != source:
                fail(f"Priorität für {wkn}: {r['OS_Quelle']!r} statt {source!r}")
            if not math.isclose(float(r["OS_Aktueller_Kurs"]), expected_quote):
                fail(f"Kursübergabe für {wkn}: {r['OS_Aktueller_Kurs']!r} statt {expected_quote!r}")
        raw_check = output_path.read_text(encoding="utf-8-sig")
        if "0,6" not in raw_check or "5,5" not in raw_check or "2,2" not in raw_check:
            fail(f"Check-CSV verwendet kein deutsches Dezimalformat: {raw_check!r}")
    print("PASS 10/10: reale lokale Offene_Positionen -> Offene Positionen+Check Übergabe")

    print("OPTIONS-SCHEIN SELFTEST: PASS")


if __name__ == "__main__":
    main()
