"""
trendline_utils.py

Zentrale Trendlinien-Erkennung fuer Long- und Short-Scanner.

Die beiden Richtungen verwenden bewusst dieselbe Methodik:
- lookback=120
- Swing-Pivots via scipy.signal.argrelextrema
- mindestens 3 Beruehrungspunkte
- 1 % Toleranz
- robuste lineare Regression mit Ausreisser-Filter
- Ausbruchskerzen (letzte 3) werden nicht zur Linienbildung verwendet
- ueberdurchschnittliches Volumen in den letzten 3 Kerzen
- nach dem Ausbruch darf kein Rueckfall auf die falsche Seite erfolgen

Nur die Richtung ist unterschiedlich:
Long  = fallende Widerstandslinie -> Ausbruch nach oben
Short = steigende Stuetzlinie       -> Ausbruch nach unten
"""

import numpy as np
from scipy.signal import argrelextrema


def _robuste_trendlinie(x, y, max_iterationen=2, ausreisser_schwelle=2.5):
    """Robuste lineare Regression mit iterativer Ausreisser-Entfernung."""
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)

    for _ in range(max_iterationen):
        if len(x_arr) < 3:
            break
        slope, intercept = np.polyfit(x_arr, y_arr, 1)
        residuen = np.abs(y_arr - (slope * x_arr + intercept))
        mad = np.median(residuen)
        if mad <= 0:
            break
        maske = residuen <= (ausreisser_schwelle * mad)
        if maske.all() or int(maske.sum()) < 3:
            break
        x_arr, y_arr = x_arr[maske], y_arr[maske]

    slope, intercept = np.polyfit(x_arr, y_arr, 1)
    return slope, intercept, len(x_arr)


def _kein_rueckfall_seit_ausbruch(closes, linie_werte, heute_pos, richtung,
                                  fenster_tage=3):
    """Prueft frischen Richtungswechsel und verhindert anschliessenden Rueckfall."""
    for i in range(1, fenster_tage + 1):
        pos = heute_pos - i
        pos_davor = pos - 1
        if pos_davor < 0:
            break

        if richtung == "long":
            wechsel = (
                closes[pos_davor] <= linie_werte[pos_davor]
                and closes[pos] > linie_werte[pos]
            )
            korrekt = np.asarray(closes[pos:heute_pos + 1]) > np.asarray(
                linie_werte[pos:heute_pos + 1]
            )
        else:
            wechsel = (
                closes[pos_davor] >= linie_werte[pos_davor]
                and closes[pos] < linie_werte[pos]
            )
            korrekt = np.asarray(closes[pos:heute_pos + 1]) < np.asarray(
                linie_werte[pos:heute_pos + 1]
            )

        if wechsel:
            return bool(np.all(korrekt))

    return False


def check_trendline_breakout(data, lookback=120, order=5, touch_tolerance=0.01):
    """Long: fallende Widerstandslinie wird nach oben durchbrochen."""
    return _check_trendline(data, "long", lookback, order, touch_tolerance)


def check_trendline_breakdown(data, lookback=120, order=5, touch_tolerance=0.01):
    """Short: steigende Stuetzlinie wird nach unten durchbrochen."""
    return _check_trendline(data, "short", lookback, order, touch_tolerance)


def _check_trendline(data, richtung, lookback, order, touch_tolerance):
    fenster = data.iloc[-lookback:] if len(data) > lookback else data.copy()
    if len(fenster) < 10:
        return False, None

    # Die letzten drei Kerzen koennen den Ausbruch enthalten und bleiben
    # deshalb aus der Linienbildung heraus.
    suchbereich = fenster.iloc[:-3]
    if len(suchbereich) < 10:
        return False, None

    if richtung == "long":
        werte = suchbereich["High"].values
        idx_swings = argrelextrema(werte, np.greater_equal, order=order)[0]
    else:
        werte = suchbereich["Low"].values
        idx_swings = argrelextrema(werte, np.less_equal, order=order)[0]

    if len(idx_swings) < 3:
        return False, None

    x = idx_swings.astype(float)
    y = werte[idx_swings]
    slope, intercept, verwendete_punkte = _robuste_trendlinie(x, y)

    # Long: fallende Widerstandslinie.
    # Short: steigende Stuetzlinie.
    if richtung == "long" and slope >= 0:
        return False, None
    if richtung == "short" and slope <= 0:
        return False, None
    if verwendete_punkte < 3:
        return False, None

    linie_bei_punkten = slope * x + intercept
    beruehrungen = int(
        np.sum(
            np.abs(y - linie_bei_punkten)
            <= (np.abs(linie_bei_punkten) * touch_tolerance)
        )
    )
    if beruehrungen < 3:
        return False, None

    heute_pos = len(fenster) - 1
    linie_heute = slope * heute_pos + intercept
    close_heute = fenster["Close"].iloc[-1]

    alle_positionen = np.arange(len(fenster))
    linie_werte_alle = slope * alle_positionen + intercept

    kein_rueckfall = _kein_rueckfall_seit_ausbruch(
        fenster["Close"].values,
        linie_werte_alle,
        heute_pos,
        richtung,
        fenster_tage=3,
    )

    if "Vol_SMA20" in fenster.columns:
        volumen_ok = any(
            fenster["Volume"].iloc[-1 - i] > fenster["Vol_SMA20"].iloc[-1 - i]
            for i in range(0, 3)
        )
    elif "Vol_Ratio" in fenster.columns:
        volumen_ok = any(
            fenster["Vol_Ratio"].iloc[-1 - i] > 1.0
            for i in range(0, 3)
        )
    else:
        volumen_ok = False

    if richtung == "long":
        ausbruch = (
            bool(close_heute > linie_heute)
            and kein_rueckfall
            and bool(volumen_ok)
        )
    else:
        ausbruch = (
            bool(close_heute < linie_heute)
            and kein_rueckfall
            and bool(volumen_ok)
        )

    return ausbruch, (float(linie_heute) if ausbruch else None)
