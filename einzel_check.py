"""
einzel_check.py
Version 10.08.2026

Einzelprüfung beliebiger Ticker gegen die bestehenden Strategien.

WICHTIG:
- Trendfolge, Trendwende und Short werden NICHT verändert.
- Der Kaufkandidaten-Algorithmus ist bewusst strenger als die frühere
  Momentum-Punktesumme.
- Momentum allein ist KEIN Kauf.
- KAUFKANDIDAT A = bestätigtes technisches Setup.
- KAUFKANDIDAT B = starke Vorbereitung / Trigger-Nähe, aber noch KEIN Kauf.
- Alles andere = KEIN KAUF.

Aufruf:
    python einzel_check.py GM F CMI BWA PCAR
    python einzel_check.py AVGO,ANET,VRT,DELL
"""

import datetime
import glob
import sys

import pandas as pd
import yfinance as yf

from analyse import (
    analyze_a_setup,
    analyze_a_setup_eu,
    get_benchmark_close,
    get_eu_benchmark_close,
)
from trendwende_scanner import _pruefe_trendwende, _indikatoren_berechnen
from short_scanner import _pruefe_short_setup
from edelmetalle_scanner import SPANNEN_POSITION_MAX


# ============================================================
# KONFIGURATION
# ============================================================

MOMENTUM_VOL_SCHWELLE = 1.5
MOMENTUM_EMA50_MIN = 5.0
MOMENTUM_STOCH_MIN = 80.0

# B-Kandidat: starke Vorbereitung, aber noch kein bestätigter Kauf
KAUF_B_MOMENTUM_MIN = 3

# A-Kandidat:
# Mindestens ein bestätigtes Setup ist zwingend.
KAUF_A_MIN_CRV = 1.0

# Bei Momentum 5/5 darf der Titel als starke Vorbereitung gelten.
# Das ersetzt aber weiterhin KEIN bestätigtes Setup.
KAUF_B_STARKES_MOMENTUM = 4


# Standardliste
TICKER_DEFAULT = [
    "GM", "F", "CMI", "BWA", "PCAR",
    "BABA", "NEM", "ALB", "SIX2.DE", "DRH.F", "ENR.DE",
]


SEKTOR_HINWEIS = {
    "GM": "Zyklischer Konsum",
    "F": "Zyklischer Konsum",
    "BWA": "Zyklischer Konsum",
    "BABA": "Zyklischer Konsum",
    "CMI": "Infrastruktur",
    "PCAR": "Industrie",
    "NEM": "Gold-Miner",
    "ALB": "Rohstoffe",
    "SIX2.DE": "Industrie",
    "DRH.F": "Rüstung/Aerospace",
    "ENR.DE": "Industrie",

    # Hightech / digitale Infrastruktur
    "AVGO": "Technologie",
    "ANET": "Technologie",
    "VRT": "Industrie",
    "DELL": "Technologie",
    "MRVL": "Technologie",
    "MU": "Technologie",
    "AMD": "Technologie",
    "CSCO": "Technologie",
}


NAME_HINWEIS = {
    "GM": "General Motors",
    "F": "Ford Motor Company",
    "CMI": "Cummins",
    "BWA": "BorgWarner",
    "PCAR": "Paccar",
    "BABA": "Alibaba",
    "NEM": "Newmont",
    "ALB": "Albemarle",
    "SIX2.DE": "Sixt SE",
    "DRH.F": "DroneShield (EUR, Frankfurt)",
    "ENR.DE": "Siemens Energy",
    "AVGO": "Broadcom",
    "ANET": "Arista Networks",
    "VRT": "Vertiv",
    "DELL": "Dell Technologies",
    "MRVL": "Marvell Technology",
    "MU": "Micron Technology",
    "AMD": "AMD",
    "CSCO": "Cisco Systems",
}


# ============================================================
# DATEN / ROTATION
# ============================================================

def lade_rotation_scores():
    """Liest vorhandene Performance-Dateien.

    Rückgabe:
        rotation_scores: Sektor -> Rotation-Score
        sektor_5t:       Sektor -> 5-Tage-Performance
    """
    scores = {}
    sektor_5t = {}

    muster_liste = (
        "Performance(*).csv",
        "Performance_EU(*).csv",
    )

    for muster in muster_liste:
        for pfad in sorted(glob.glob(muster)):
            try:
                df = pd.read_csv(
                    pfad,
                    sep=";",
                    encoding="utf-8-sig",
                )

                if "Sektor" not in df.columns:
                    continue

                for _, z in df.iterrows():
                    sektor = str(z["Sektor"])

                    if "Rotation-Score" in df.columns:
                        try:
                            scores[sektor] = float(z["Rotation-Score"])
                        except (TypeError, ValueError):
                            pass

                    if "5T" in df.columns:
                        try:
                            sektor_5t[sektor] = float(z["5T"])
                        except (TypeError, ValueError):
                            pass

            except Exception:
                pass

    return scores, sektor_5t


def hole_kursdaten(ticker):
    """Lädt zwei Jahre Daten und verwendet anschließend ca. 52 Wochen."""
    try:
        data = yf.Ticker(ticker).history(period="2y")
    except Exception:
        return None

    if data.empty:
        return None

    required = ["Close", "High", "Low", "Volume"]
    data = data.dropna(subset=required)

    if data.empty:
        return None

    stichtag = pd.Timestamp(
        datetime.date.today() - datetime.timedelta(days=365)
    )

    if getattr(data.index, "tz", None) is not None:
        stichtag = stichtag.tz_localize(data.index.tz)

    fenster = data[data.index >= stichtag]

    if len(fenster) >= 60:
        return fenster

    return data.tail(252)


# ============================================================
# MOMENTUM
# ============================================================

def momentum_ausbruch_score(ticker, data, sektor, sektor_5t):
    """Berechnet den Momentum-Ausbruch-Score.

    Die vier Kernkriterien bleiben:
      1. Stochastik > 80
      2. Kurs nahe am 3-Monats-Hoch (1 % Toleranz)
      3. Volumen > 1.5x SMA20
      4. Kurs mindestens 5 % über EMA50

    Relative Stärke zum Sektor wird separat als Zusatzinformation ausgegeben.
    Sie reduziert nicht den Kernscore, wenn keine Sektorperformance vorliegt.
    """
    try:
        df = _indikatoren_berechnen(data.copy())

        if len(df) < 60:
            return {
                "score": 0,
                "max_score": 4,
                "core_score": 0,
                "details": [],
                "stoch": None,
                "vol_ratio": None,
                "ema50_distance": None,
                "near_high": False,
                "sector_rs": None,
                "text": "  MOMENTUM-AUSBRUCH-SCORE: zu wenig Kurshistorie",
            }

        kurs = float(df["Close"].iloc[-1])
        stoch = float(df["Stoch_K"].iloc[-1])
        vol_ratio = float(df["Vol_Ratio"].iloc[-1])
        ema50 = float(df["EMA50"].iloc[-1])

        ema_distance = (
            (kurs - ema50) / ema50 * 100
            if ema50 > 0
            else float("nan")
        )

        stichtag = pd.Timestamp(
            datetime.date.today() - datetime.timedelta(days=90)
        )

        idx = df.index

        if getattr(idx, "tz", None) is not None:
            stichtag = stichtag.tz_localize(idx.tz)

        fenster_3m = df[idx >= stichtag]

        if len(fenster_3m) < 40:
            fenster_3m = df.tail(63)

        hoch_3m = float(fenster_3m["High"].max())

        # 1 % Toleranz: Kurs gilt als nahe am 3-Monats-Hoch.
        near_high = kurs >= hoch_3m * 0.99

        p1 = stoch > MOMENTUM_STOCH_MIN
        p2 = near_high
        p3 = vol_ratio > MOMENTUM_VOL_SCHWELLE
        p4 = ema_distance >= MOMENTUM_EMA50_MIN

        punkte = [
            (
                "Stochastik > 80",
                p1,
                f"{stoch:.1f}",
            ),
            (
                "Neues 3-Monats-Hoch (Toleranz 1%)",
                p2,
                f"Kurs {kurs:.2f} vs. Hoch {hoch_3m:.2f} "
                f"({kurs / hoch_3m * 100:.1f}%)",
            ),
            (
                f"Volumenanstieg (>{MOMENTUM_VOL_SCHWELLE:.1f}x SMA20)",
                p3,
                f"{vol_ratio:.2f}x",
            ),
            (
                f"Abstand EMA50 (>={MOMENTUM_EMA50_MIN:.0f}%)",
                p4,
                f"{ema_distance:+.1f}%",
            ),
        ]

        core_score = sum(1 for _, ok, _ in punkte if ok)

        sector_rs = None
        sector_rs_text = (
            "Sektor-Relative-Stärke nicht verfügbar"
        )

        if sektor in sektor_5t and len(df) >= 6:
            eigene_5t = (
                kurs / float(df["Close"].iloc[-6]) - 1
            ) * 100

            sektor_wert = float(sektor_5t[sektor])

            sector_rs = eigene_5t > sektor_wert

            sector_rs_text = (
                f"Aktie {eigene_5t:+.1f}% "
                f"vs. Sektor {sektor_wert:+.1f}% (5 Tage)"
            )

        zeilen = [
            f"  MOMENTUM-AUSBRUCH-SCORE: {core_score}/4"
        ]

        for name, ok, detail in punkte:
            zeilen.append(
                f"    {'✓' if ok else '–'} {name}: {detail}"
            )

        zeilen.append(
            f"    • Sektor-RS: {sector_rs_text}"
        )

        return {
            "score": core_score,
            "max_score": 4,
            "core_score": core_score,
            "details": punkte,
            "stoch": stoch,
            "vol_ratio": vol_ratio,
            "ema50_distance": ema_distance,
            "near_high": near_high,
            "sector_rs": sector_rs,
            "text": "\n".join(zeilen),
        }

    except Exception as e:
        return {
            "score": 0,
            "max_score": 4,
            "core_score": 0,
            "details": [],
            "stoch": None,
            "vol_ratio": None,
            "ema50_distance": None,
            "near_high": False,
            "sector_rs": None,
            "text": (
                "  MOMENTUM-AUSBRUCH-SCORE: Fehler "
                f"({type(e).__name__}: {e})"
            ),
        }


# ============================================================
# KAUFKANDIDATEN-ALGORITHMUS
# ============================================================

def _crv_ok(res):
    """Prüft, ob mindestens ein vorhandenes Kursziel CRV >= 1.0 besitzt."""
    if not res:
        return False

    for key in ("CRV1", "CRV2"):
        value = res.get(key)

        try:
            if value is not None and float(value) >= KAUF_A_MIN_CRV:
                return True
        except (TypeError, ValueError):
            pass

    return False


def bewerte_kaufkandidat(
    ticker,
    momentum_ergebnis,
    trendfolge_res,
    trendwende_res,
    rotation_score,
):
    """Entscheidet, ob ein Titel wirklich kaufbar ist.

    NEUE GRUNDREGEL:
        Momentum allein -> niemals Kauf.

    A:
        Trendfolge ODER Trendwende bestätigt
        UND mindestens ein CRV >= 1.0.

    B:
        Noch kein bestätigtes Setup,
        aber starkes Momentum >= 3/4
        und mindestens ein zusätzlicher Trigger-Indikator.

    B bedeutet ausdrücklich: "Trigger abwarten", nicht kaufen.
    """

    momentum = momentum_ergebnis.get("score", 0)
    momentum_max = momentum_ergebnis.get("max_score", 4)

    tf_ok = trendfolge_res is not None and _crv_ok(trendfolge_res)
    tw_ok = trendwende_res is not None and _crv_ok(trendwende_res)

    near_high = bool(momentum_ergebnis.get("near_high"))
    vol_ratio = momentum_ergebnis.get("vol_ratio")
    ema_distance = momentum_ergebnis.get("ema50_distance")
    sector_rs = momentum_ergebnis.get("sector_rs")

    gruende = []
    risiken = []

    # --------------------------------------------------------
    # A: bestätigter Kauf
    # --------------------------------------------------------

    if tf_ok or tw_ok:
        if tf_ok:
            gruende.append("Trendfolge-Setup bestätigt, CRV >= 1.0")

        if tw_ok:
            gruende.append("Trendwende-Setup bestätigt, CRV >= 1.0")

        if momentum >= 3:
            gruende.append(
                f"Momentum unterstützt das Setup ({momentum}/{momentum_max})"
            )

        if sector_rs is True:
            gruende.append("Relative Stärke zum Sektor positiv")
        elif sector_rs is False:
            risiken.append("Relative Stärke zum Sektor nicht besser")

        if rotation_score is not None and rotation_score <= 0:
            risiken.append("Sektor-Rotation ohne positiven Rückenwind")

        return {
            "Ticker": ticker,
            "Status": "KAUFKANDIDAT A",
            "Score": momentum,
            "Momentum": f"{momentum}/{momentum_max}",
            "Gruende": gruende,
            "Risiken": risiken,
        }

    # --------------------------------------------------------
    # B: Vorbereitung / Trigger abwarten
    # --------------------------------------------------------

    b_trigger = False

    if near_high:
        b_trigger = True

    if vol_ratio is not None:
        try:
            if float(vol_ratio) > MOMENTUM_VOL_SCHWELLE:
                b_trigger = True
        except (TypeError, ValueError):
            pass

    if momentum >= KAUF_B_MOMENTUM_MIN and b_trigger:
        gruende.append(
            f"starkes Momentum ({momentum}/{momentum_max})"
        )

        if near_high:
            gruende.append("Kurs nahe am 3-Monats-Hoch")

        if vol_ratio is not None:
            try:
                if float(vol_ratio) > MOMENTUM_VOL_SCHWELLE:
                    gruende.append(
                        f"Volumen bestätigt ({float(vol_ratio):.2f}x SMA20)"
                    )
            except (TypeError, ValueError):
                pass

        if ema_distance is not None:
            try:
                if float(ema_distance) >= MOMENTUM_EMA50_MIN:
                    gruende.append(
                        f"über EMA50 (+{float(ema_distance):.1f}%)"
                    )
            except (TypeError, ValueError):
                pass

        risiken.append(
            "Noch kein bestätigtes Trendfolge-/Trendwende-Setup"
        )
        risiken.append(
            "KEIN Sofortkauf – Trigger/CRV abwarten"
        )

        if sector_rs is False:
            risiken.append("Relative Stärke zum Sektor negativ")

        return {
            "Ticker": ticker,
            "Status": "KAUFKANDIDAT B",
            "Score": momentum,
            "Momentum": f"{momentum}/{momentum_max}",
            "Gruende": gruende,
            "Risiken": risiken,
        }

    # --------------------------------------------------------
    # Kein Kauf
    # --------------------------------------------------------

    if momentum < KAUF_B_MOMENTUM_MIN:
        risiken.append(
            f"Momentum zu schwach ({momentum}/{momentum_max})"
        )

    if not tf_ok and not tw_ok:
        risiken.append("kein bestätigtes Einstiegssignal")

    if not near_high:
        risiken.append("kein Ausbruch in Nähe des 3-Monats-Hochs")

    if vol_ratio is not None:
        try:
            if float(vol_ratio) <= MOMENTUM_VOL_SCHWELLE:
                risiken.append(
                    f"Volumen nicht bestätigt ({float(vol_ratio):.2f}x SMA20)"
                )
        except (TypeError, ValueError):
            pass

    return {
        "Ticker": ticker,
        "Status": "KEIN KAUF",
        "Score": momentum,
        "Momentum": f"{momentum}/{momentum_max}",
        "Gruende": gruende,
        "Risiken": risiken,
    }


# ============================================================
# EINZELPRÜFUNG
# ============================================================

KAUFKANDIDATEN_ERGEBNISSE = []


def pruefe(ticker, spy_close, eu_close, scores, sektor_5t):
    trendfolge_res = None
    trendwende_res = None

    ist_eu = "." in ticker

    sektor = SEKTOR_HINWEIS.get(ticker, "N/A")
    rotation_score = scores.get(sektor)

    klarname = NAME_HINWEIS.get(ticker)

    kopf = ticker
    if klarname:
        kopf += f" - {klarname}"

    print("=" * 62)
    print(
        f"{kopf}   (Sektor laut Zuordnung: {sektor}"
        + (
            f", Rotation-Score {rotation_score:+.3f}"
            if rotation_score is not None
            else ""
        )
        + ")"
    )
    print("=" * 62)

    # --------------------------------------------------------
    # 1) Trendfolge
    # --------------------------------------------------------

    try:
        trendfolge_res = (
            analyze_a_setup_eu(
                ticker,
                sektor,
                eu_close,
            )
            if ist_eu
            else analyze_a_setup(
                ticker,
                sektor,
                spy_close,
            )
        )

        if trendfolge_res:
            print(
                f"  TRENDFOLGE: TREFFER - "
                f"Status {trendfolge_res.get('Status2')} "
                f"({trendfolge_res.get('Status_Grund')})"
            )

            print(
                f"    Setup: {trendfolge_res.get('Setup_Typ')} | "
                f"Kurs {trendfolge_res.get('Kurs')} | "
                f"Stop {trendfolge_res.get('Stop')} "
                f"(Risiko {trendfolge_res.get('Risk_Perc')}%)"
            )

            print(
                f"    TP1 {trendfolge_res.get('TP1')} "
                f"(CRV {trendfolge_res.get('CRV1')}) | "
                f"TP2 {trendfolge_res.get('TP2')} "
                f"(CRV {trendfolge_res.get('CRV2')})"
            )

            print(
                f"    RSI {trendfolge_res.get('RSI')} | "
                f"MACD {trendfolge_res.get('MACD_Trend')} | "
                f"Ampel {trendfolge_res.get('Fundamental_Ampel')}"
            )

        else:
            print(
                "  TRENDFOLGE: kein Setup "
                "(Grund siehe DEBUG-Zeilen oben)"
            )

    except Exception as e:
        print(
            f"  TRENDFOLGE: Fehler "
            f"({type(e).__name__}: {e})"
        )

    # --------------------------------------------------------
    # Kursdaten
    # --------------------------------------------------------

    data = hole_kursdaten(ticker)

    if data is None or data.empty:
        print("  TRENDWENDE/SHORT: keine Kursdaten")
        return

    # --------------------------------------------------------
    # 2) Momentum
    # --------------------------------------------------------

    momentum_ergebnis = momentum_ausbruch_score(
        ticker,
        data,
        sektor,
        sektor_5t,
    )

    print(momentum_ergebnis["text"])
    print()

    # --------------------------------------------------------
    # 3) Trendwende
    # --------------------------------------------------------

    def _trendwende(spannen_max=None):
        return _pruefe_trendwende(
            ticker,
            sektor,
            "EU" if ist_eu else "US",
            data.copy(),
            eu_close if ist_eu else spy_close,
            spannen_position_max=spannen_max,
        )

    ergebnisse_tw = {}

    for label, spannen_max in (
        (
            "TRENDWENDE "
            "(Aktien-Regel: max. 20% ueber 52W-Tief)",
            None,
        ),
        (
            f"TRENDWENDE "
            f"(Metall-Regel: Spannen-Position <= "
            f"{SPANNEN_POSITION_MAX:.0%})",
            SPANNEN_POSITION_MAX,
        ),
    ):
        try:
            res, grund = _trendwende(spannen_max)

            if res:
                print(
                    f"  {label}: TREFFER - "
                    f"{res.get('Setup_Typ')} | "
                    f"Kurs {res.get('Kurs')} | "
                    f"Stop {res.get('Stop')} | "
                    f"TP1 {res.get('TP1')} "
                    f"(CRV {res.get('CRV1')}) | "
                    f"Bonus: {res.get('Qualitaets_Bonus')}"
                )

                # Für die Kaufentscheidung verwenden wir den ersten
                # tatsächlich bestätigten Trendwende-Treffer.
                if trendwende_res is None:
                    trendwende_res = res

            else:
                print(
                    f"  {label}: kein Kandidat "
                    f"(Stufe: {grund})"
                )

            ergebnisse_tw[spannen_max] = (
                res is not None,
                grund,
            )

        except Exception as e:
            print(
                f"  {label}: Fehler "
                f"({type(e).__name__}: {e})"
            )

            ergebnisse_tw[spannen_max] = (
                None,
                "fehler",
            )

    # --------------------------------------------------------
    # Abweichung Aktien-/Metallregel
    # --------------------------------------------------------

    aktien_ok = ergebnisse_tw.get(
        None,
        (None, None),
    )[0]

    metall_ok = ergebnisse_tw.get(
        SPANNEN_POSITION_MAX,
        (None, None),
    )[0]

    if aktien_ok is False and metall_ok is True:
        try:
            kurs = float(data["Close"].iloc[-1])
            tief = float(data["Low"].min())
            hoch = float(data["High"].max())

            print(
                "  >>> ABWEICHUNG: nur die Metall-Regel "
                "laesst diesen Titel zu "
                f"({(kurs / tief - 1) * 100:.1f}% "
                "ueber 52W-Tief, "
                f"Spannen-Position "
                f"{(kurs - tief) / (hoch - tief):.0%})"
            )

        except Exception:
            print(
                "  >>> ABWEICHUNG: nur die Metall-Regel "
                "laesst diesen Titel zu."
            )

    elif aktien_ok is True and metall_ok is False:
        print(
            "  >>> ABWEICHUNG umgekehrt: nur die Aktien-Regel "
            "laesst diesen Titel zu."
        )

    # --------------------------------------------------------
    # 4) KAUFKANDIDATEN-BEWERTUNG
    # --------------------------------------------------------

    kauf = bewerte_kaufkandidat(
        ticker=ticker,
        momentum_ergebnis=momentum_ergebnis,
        trendfolge_res=trendfolge_res,
        trendwende_res=trendwende_res,
        rotation_score=rotation_score,
    )

    print()
    print("  KAUFKANDIDATEN-BEWERTUNG")
    print("  " + "-" * 45)
    print(
        f"  Ergebnis: {kauf['Status']} "
        f"(Momentum {kauf['Momentum']})"
    )

    for grund in kauf["Gruende"]:
        print(f"    ✓ {grund}")

    for risiko in kauf["Risiken"]:
        print(f"    ⚠ {risiko}")

    if kauf["Status"] != "KEIN KAUF":
        KAUFKANDIDATEN_ERGEBNISSE.append(kauf)

    # --------------------------------------------------------
    # 5) Short
    # --------------------------------------------------------

    try:
        res, grund = _pruefe_short_setup(
            ticker,
            sektor,
            "EU" if ist_eu else "US",
            data.copy(),
            eu_close if ist_eu else spy_close,
            marktumfeld_baerisch=False,
            sektor_momentum=None,
        )

        if res:
            print(
                f"  SHORT: TREFFER - "
                f"{res.get('Setup_Typ')} | "
                f"Kurs {res.get('Kurs')} | "
                f"Stop {res.get('Stop')} | "
                f"TP1 {res.get('TP1')} "
                f"(CRV {res.get('CRV1')}) | "
                f"Qualitaet {res.get('Setup_Qualitaet')}"
            )
        else:
            print(
                f"  SHORT: kein Kandidat "
                f"(Stufe: {grund})"
            )

    except Exception as e:
        print(
            f"  SHORT: Fehler "
            f"({type(e).__name__}: {e})"
        )


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def parse_ticker_args(args):
    """Akzeptiert sowohl Leerzeichen als auch Kommas.

    Beispiele:
        AVGO ANET VRT
        AVGO,ANET,VRT
        AVGO, ANET, VRT
    """
    ticker_liste = []

    for arg in args:
        teile = arg.split(",")

        for ticker in teile:
            ticker = ticker.strip().rstrip(",").upper()

            if ticker:
                ticker_liste.append(ticker)

    return ticker_liste


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ticker_liste = parse_ticker_args(sys.argv[1:])
    else:
        ticker_liste = TICKER_DEFAULT

    # Doppelte Ticker entfernen, Reihenfolge behalten.
    ticker_liste = list(dict.fromkeys(ticker_liste))

    print(
        f"EINZEL-CHECK {datetime.date.today().isoformat()} - "
        f"{len(ticker_liste)} Titel: "
        f"{', '.join(ticker_liste)}"
    )

    print(
        "Hinweis: Rotations-Filter bewusst umgangen. "
        "Die Kaufkandidatenlogik verwendet Rotation nur als "
        "Zusatzinformation; Momentum allein ist KEIN Kauf.\n"
    )

    spy_close = get_benchmark_close()
    eu_close = get_eu_benchmark_close()

    scores, sektor_5t = lade_rotation_scores()

    for ticker in ticker_liste:
        try:
            pruefe(
                ticker,
                spy_close,
                eu_close,
                scores,
                sektor_5t,
            )
        except Exception as e:
            print(
                f"\nFEHLER BEI {ticker}: "
                f"{type(e).__name__}: {e}"
            )

        print()

    # ========================================================
    # GESAMTERGEBNIS
    # ========================================================

    print()
    print("=" * 62)
    print("KAUFKANDIDATEN DES CHECKS")
    print("=" * 62)

    if not KAUFKANDIDATEN_ERGEBNISSE:
        print("Keine Kaufkandidaten gefunden.")

    else:
        sortiert = sorted(
            KAUFKANDIDATEN_ERGEBNISSE,
            key=lambda x: (
                0 if x["Status"] == "KAUFKANDIDAT A" else 1,
                -x["Score"],
            ),
        )

        for kandidat in sortiert:
            print(
                f"{kandidat['Ticker']:8} "
                f"{kandidat['Status']:18} "
                f"Momentum {kandidat['Momentum']}"
            )

            for grund in kandidat["Gruende"]:
                print(f"    ✓ {grund}")

            for risiko in kandidat["Risiken"]:
                print(f"    ⚠ {risiko}")

            print()

    print("=" * 62)
    print("ENDE EINZEL-CHECK")
    print("=" * 62)
