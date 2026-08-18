"""
einzel_check.py
Version 12.08.2026

Einzelprüfung beliebiger Ticker gegen die bestehenden Strategien.

WICHTIG:
- Trendfolge, Trendwende und Short werden NICHT verändert.
- Der Kaufkandidaten-Algorithmus ist bewusst strenger als die frühere
  Momentum-Punktesumme.
- Momentum allein ist KEIN Kauf.
- KAUFKANDIDAT A = bestätigtes technisches Setup + CRV >= 1.0.
- KAUFKANDIDAT B = starke Vorbereitung / Trigger-Nähe, aber noch KEIN Sofortkauf.
- KAUFKANDIDAT C = frühe technische Vorbereitung, noch weiter vom Trigger entfernt.
- Alles andere = KEIN KANDIDAT.
- Dieser Einzelcheck ist KEIN Sektor-Rotationsscanner.
- Die Sektorzuordnung erfolgt automatisch aus analyse.py.
- Die Sektor-Relative-Stärke wird direkt gegen den passenden Sektor-ETF
  berechnet und ist NICHT davon abhängig, ob der Sektor in einer
  Performance-/Rotationsdatei vorhanden ist.

Aufruf:
    python einzel_check.py GM F CMI BWA PCAR
    python einzel_check.py AVGO,ANET,VRT,DELL
"""

import datetime
import io
import json
import os
import re
import sys

import pandas as pd
import yfinance as yf

from googleapiclient.http import MediaIoBaseDownload

from analyse import (
    analyze_a_setup,
    analyze_a_setup_eu,
    get_benchmark_close,
    get_eu_benchmark_close,
    sektoren_map,
    sektoren_aktien,
    dax_aktien,
    eu_sektoren_etf,
)
from trendwende_scanner import _pruefe_trendwende, _indikatoren_berechnen
from short_scanner import _pruefe_short_setup
from edelmetalle_scanner import SPANNEN_POSITION_MAX
from upload_to_drive import get_drive_service


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
# Extrem überkaufte Titel dürfen trotz bestätigtem Setup nicht A werden.
KAUF_A_MAX_STOCH = 95.0


# Cache für Sektor-ETF-Kurse.
# Wichtig: Bei mehreren Titeln desselben Sektors wird der ETF nur einmal
# geladen.
SEKTOR_ETF_CACHE = {}

# Persistente Beobachtungsliste im gleichen Verzeichnis wie dieses Skript.
# Regel:
#   A -> entfernen
#   B -> aufnehmen / aktualisieren
#   C -> aufnehmen / aktualisieren
#   KEIN KANDIDAT -> entfernen
BEOBACHTUNGS_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "einzel_check_beobachtung.json",
)

# Google-Drive-Ordner des Projekts. Wird nur für den Auswertungs-Fallback
# benötigt; die eigentliche Watchlist bleibt die JSON-Datei.
FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"


def lade_beobachtungsliste():
    """Lädt die aktuell persistierte Einzel-Check-Beobachtungsliste."""
    if not os.path.exists(BEOBACHTUNGS_DATEI):
        return {}

    try:
        with open(BEOBACHTUNGS_DATEI, "r", encoding="utf-8") as f:
            daten = json.load(f)
        return daten if isinstance(daten, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        print(
            "  WARNUNG: Beobachtungsliste konnte nicht gelesen werden - "
            "starte mit leerer Liste."
        )
        return {}


def lade_beobachtung_aus_letzter_auswertung():
    """
    Fallback-Quelle für die Wiedervorlage:
    liest die letzte Auswertung*.txt aus Google Drive und extrahiert
    ausschließlich B/C-Titel aus Abschnitt 4
    ``Einzel-Check-Beobachtungsliste``.

    Die Auswertung ist damit nur eine Rückfallebene, wenn keine lokale
    bzw. synchronisierte JSON-Beobachtungsliste vorhanden ist.
    """
    try:
        service = get_drive_service()

        query = (
            f"name contains 'Auswertung(' and '{FOLDER_ID}' in parents "
            "and trashed = false"
        )

        antwort = service.files().list(
            q=query,
            spaces="drive",
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=20,
        ).execute()

        treffer = antwort.get("files", [])
        if not treffer:
            return {}

        datei = treffer[0]
        request = service.files().get_media(fileId=datei["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)

        fertig = False
        while not fertig:
            _, fertig = downloader.next_chunk()

        text = buffer.getvalue().decode("utf-8-sig", errors="replace")

        marker = "Einzel-Check-Beobachtungsliste:"
        start = text.find(marker)
        if start < 0:
            return {}

        block = text[start + len(marker):]

        # Abschnitt 4 endet beim nächsten nummerierten Hauptabschnitt.
        ende = re.search(r"\n\s*\d+\.\s+", block)
        if ende:
            block = block[:ende.start()]

        muster = re.compile(
            r"Ticker:\s*([A-Za-z0-9.\-]+)\s*\|\s*"
            r"Status:\s*(KAUFKANDIDAT\s+[BC])",
            re.IGNORECASE,
        )

        ergebnis = {}
        for match in muster.finditer(block):
            ticker = match.group(1).strip().upper()
            status = re.sub(r"\s+", " ", match.group(2).strip().upper())
            ergebnis[ticker] = {
                "status": status,
                "letzter_check": "aus letzter Auswertung",
            }

        if ergebnis:
            print(
                f"INFO: Beobachtungsliste aus letzter Auswertung "
                f"({datei['name']}) übernommen: "
                f"{len(ergebnis)} B/C-Titel."
            )

        return ergebnis

    except Exception as e:
        print(
            "WARNUNG: Letzte Auswertung konnte nicht als "
            f"Fallback gelesen werden ({type(e).__name__}: {e})."
        )
        return {}


def speichere_beobachtungsliste(liste):
    """Speichert die Beobachtungsliste atomar."""
    temp_datei = BEOBACHTUNGS_DATEI + ".tmp"

    with open(temp_datei, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=2)
        f.write("\n")

    os.replace(temp_datei, BEOBACHTUNGS_DATEI)


def aktualisiere_beobachtungsliste(ticker, status):
    """
    Pflegt die persistente Beobachtungsliste nach der vereinbarten Regel:

      A -> entfernen
      B -> aufnehmen / aktualisieren
      C -> aufnehmen / aktualisieren
      KEIN KANDIDAT -> entfernen
    """
    liste = lade_beobachtungsliste()
    heute = datetime.date.today().isoformat()

    if status in ("KAUFKANDIDAT B", "KAUFKANDIDAT C"):
        war_bereits_drin = ticker in liste
        liste[ticker] = {
            "status": status,
            "letzter_check": heute,
        }
        speichere_beobachtungsliste(liste)

        if war_bereits_drin:
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} aktualisiert -> {status}"
            )
        else:
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} aufgenommen -> {status}"
            )

    else:
        war_bereits_drin = ticker in liste
        if war_bereits_drin:
            del liste[ticker]
            speichere_beobachtungsliste(liste)
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} entfernt -> {status}"
            )
        else:
            # Datei trotzdem anlegen/aktualisieren, damit nach einem Lauf
            # mit ausschließlich A/kein Kandidat eine gültige leere Liste
            # existiert.
            speichere_beobachtungsliste(liste)
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} nicht enthalten -> {status}"
            )


# ============================================================
# TICKER / NAMEN
# ============================================================

TICKER_DEFAULT = [
    "GM", "F", "CMI", "BWA", "PCAR",
    "BABA", "NEM", "ALB", "SIX2.DE", "DRH.F", "ENR.DE",
]

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
    "GILD": "Gilead Sciences",
}


# ============================================================
# AUTOMATISCHE SEKTORZUORDNUNG
# ============================================================

def finde_us_sektor(ticker):
    """
    Ermittelt den US-Sektor direkt aus analyse.py.

    sektoren_aktien ist absichtlich die Quelle der Wahrheit.
    Falls ein Titel mehreren ETFs zugeordnet ist, wird der erste Treffer
    gemäß der Reihenfolge in analyse.py verwendet.

    Beispiel:
        GILD -> XLV -> Gesundheit

    Das ist ausdrücklich KEINE Top-Sektor-/Rotationsprüfung.
    """
    treffer = []

    for etf, ticker_liste in sektoren_aktien.items():
        if ticker in ticker_liste:
            sektor = sektoren_map.get(etf)

            if sektor:
                treffer.append((etf, sektor))

    if not treffer:
        return None, None, []

    erster_etf, erster_sektor = treffer[0]

    return erster_sektor, erster_etf, treffer


def finde_eu_sektor(ticker):
    """
    Ermittelt die EU-Sektorzuordnung direkt aus ``dax_aktien`` in
    ``analyse.py`` – analog zur bestehenden US-Logik.

    WICHTIG:
    - Mehrfachzuordnungen sind ausdrücklich erlaubt.
    - Alle Treffer werden gesammelt.
    - Der erste Treffer bleibt der primäre Sektor, damit sich die
      bestehende Bewertungslogik nicht ungewollt ändert.
    - Die vollständige Trefferliste steht zusätzlich für Transparenz
      und spätere Sektorvergleiche zur Verfügung.

    Beispiel (wenn P911.DE zusätzlich unter "Automobil" geführt wird):
        P911.DE -> Industrie / EXH4.DE
                 -> Automobil / EXV5.DE

    Rückgabe:
        (erster_sektor, erster_etf, alle_treffer)
    """
    treffer = []

    for sektor, ticker_liste in dax_aktien.items():
        if ticker not in ticker_liste:
            continue

        etfs = [
            etf
            for etf, etf_sektor in eu_sektoren_etf.items()
            if etf_sektor == sektor
        ]

        # Genau wie bei der US-Logik: Nur eine verwertbare Kombination
        # aus Sektor und ETF kommt in die Trefferliste.
        if etfs:
            treffer.append((etfs[0], sektor))

    if not treffer:
        return None, None, []

    erster_etf, erster_sektor = treffer[0]
    return erster_sektor, erster_etf, treffer


def finde_sektor_information(ticker):
    """
    Einheitliche automatische Sektorermittlung.

    Rückgabe:
        {
            "sektor": ...,
            "etf": ...,
            "eu": bool,
            "alle_treffer": [...]
        }

    Keine Performance-Datei notwendig.
    Keine Rotationsdatei notwendig.
    """
    ist_eu = "." in ticker

    if ist_eu:
        sektor, etf, treffer = finde_eu_sektor(ticker)

        return {
            "sektor": sektor or "N/A",
            "etf": etf,
            "eu": True,
            "alle_treffer": treffer,
        }

    sektor, etf, treffer = finde_us_sektor(ticker)

    return {
        "sektor": sektor or "N/A",
        "etf": etf,
        "eu": False,
        "alle_treffer": treffer,
    }


# ============================================================
# KURSDATEN
# ============================================================

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


def berechne_5t_performance_aus_daten(data):
    """Berechnet die 5-Tage-Performance aus bereits geladenen Kursdaten."""
    try:
        if data is None or len(data) < 6:
            return None

        close_aktuell = float(data["Close"].iloc[-1])
        close_vor_5 = float(data["Close"].iloc[-6])

        if close_vor_5 <= 0:
            return None

        return (close_aktuell / close_vor_5 - 1.0) * 100.0

    except Exception:
        return None


# ============================================================
# SEKTOR-RELATIVE-STÄRKE
# ============================================================

def hole_sektor_etf_5t(etf):
    """
    Lädt die 5-Tage-Performance eines Sektor-ETFs direkt über yfinance.

    Kein Zugriff auf Performance(*).csv und kein Zugriff auf
    Performance_EU(*).csv.

    Dadurch funktioniert der Sektor-RS auch dann, wenn der Sektor im
    normalen Rotationslauf nicht unter den Top-Sektoren steht.
    """
    if not etf:
        return None

    if etf in SEKTOR_ETF_CACHE:
        return SEKTOR_ETF_CACHE[etf]

    try:
        hist = yf.Ticker(etf).history(period="3mo")

        if hist.empty or "Close" not in hist.columns:
            SEKTOR_ETF_CACHE[etf] = None
            return None

        hist = hist.dropna(subset=["Close"])

        if len(hist) < 6:
            SEKTOR_ETF_CACHE[etf] = None
            return None

        aktuell = float(hist["Close"].iloc[-1])
        vor_5 = float(hist["Close"].iloc[-6])

        if vor_5 <= 0:
            SEKTOR_ETF_CACHE[etf] = None
            return None

        wert = (aktuell / vor_5 - 1.0) * 100.0

        SEKTOR_ETF_CACHE[etf] = wert
        return wert

    except Exception:
        SEKTOR_ETF_CACHE[etf] = None
        return None


def berechne_sektor_rs(ticker_5t, sektor_etf, sektor):
    """
    Aktie vs. zugehöriger Sektor-ETF.

    Rückgabe:
        {
            "verfuegbar": bool,
            "aktie_5t": float | None,
            "sektor_5t": float | None,
            "outperformance": float | None,
            "positiv": bool | None,
            "text": str
        }

    Wichtig:
    Das Ergebnis ist reine Zusatzinformation für den Einzelcheck.
    Es ist KEIN Rotationsfilter.
    """
    if ticker_5t is None:
        return {
            "verfuegbar": False,
            "aktie_5t": None,
            "sektor_5t": None,
            "outperformance": None,
            "positiv": None,
            "text": "Aktien-5T-Performance nicht verfügbar",
        }

    if not sektor_etf:
        return {
            "verfuegbar": False,
            "aktie_5t": ticker_5t,
            "sektor_5t": None,
            "outperformance": None,
            "positiv": None,
            "text": f"Kein Sektor-ETF für '{sektor}' hinterlegt",
        }

    sektor_5t = hole_sektor_etf_5t(sektor_etf)

    if sektor_5t is None:
        return {
            "verfuegbar": False,
            "aktie_5t": ticker_5t,
            "sektor_5t": None,
            "outperformance": None,
            "positiv": None,
            "text": f"{sektor_etf}: 5T-Daten nicht verfügbar",
        }

    outperformance = ticker_5t - sektor_5t
    positiv = outperformance > 0

    return {
        "verfuegbar": True,
        "aktie_5t": ticker_5t,
        "sektor_5t": sektor_5t,
        "outperformance": outperformance,
        "positiv": positiv,
        "text": (
            f"Aktie {ticker_5t:+.1f}% vs. "
            f"{sektor} ({sektor_etf}) {sektor_5t:+.1f}% "
            f"= {outperformance:+.1f} %-Pkt."
        ),
    }


# ============================================================
# MOMENTUM
# ============================================================

def momentum_ausbruch_score(ticker, data, sektor, sektor_etf):
    """
    Berechnet den Momentum-Ausbruch-Score.

    Vier Kernkriterien:
      1. Stochastik > 80
      2. Kurs nahe am 3-Monats-Hoch (1 % Toleranz)
      3. Volumen > 1.5x SMA20
      4. Kurs mindestens 5 % über EMA50

    Der Sektor-RS wird separat direkt gegen den passenden Sektor-ETF
    berechnet. Er gehört NICHT zum 4-Punkte-Kernscore.
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
                "sector_rs_info": {
                    "verfuegbar": False,
                    "text": "zu wenig Kurshistorie",
                },
                "text": (
                    "  MOMENTUM-AUSBRUCH-SCORE: "
                    "zu wenig Kurshistorie"
                ),
            }

        kurs = float(df["Close"].iloc[-1])
        stoch = float(df["Stoch_K"].iloc[-1])
        vol_ratio = float(df["Vol_Ratio"].iloc[-1])
        ema50 = float(df["EMA50"].iloc[-1])

        ema_distance = (
            (kurs - ema50) / ema50 * 100.0
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
                f"Volumenanstieg "
                f"(>{MOMENTUM_VOL_SCHWELLE:.1f}x SMA20)",
                p3,
                f"{vol_ratio:.2f}x",
            ),
            (
                f"Abstand EMA50 "
                f"(>={MOMENTUM_EMA50_MIN:.0f}%)",
                p4,
                f"{ema_distance:+.1f}%",
            ),
        ]

        core_score = sum(
            1 for _, ok, _ in punkte if ok
        )

        aktie_5t = berechne_5t_performance_aus_daten(df)

        sector_rs_info = berechne_sektor_rs(
            aktie_5t,
            sektor_etf,
            sektor,
        )

        zeilen = [
            f"  MOMENTUM-AUSBRUCH-SCORE: "
            f"{core_score}/4"
        ]

        for name, ok, detail in punkte:
            zeilen.append(
                f"    {'✓' if ok else '–'} "
                f"{name}: {detail}"
            )

        zeilen.append(
            f"    • Sektor-RS: "
            f"{sector_rs_info['text']}"
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
            "sector_rs": sector_rs_info.get("positiv"),
            "sector_rs_info": sector_rs_info,
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
            "sector_rs_info": {
                "verfuegbar": False,
                "text": "Fehler bei Sektor-RS",
            },
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
):
    """
    Klassifiziert den einzelnen Titel in A / B / C.

    A = bestätigtes technisches Setup:
        Trendfolge ODER reguläre Aktien-Trendwende
        UND mindestens ein CRV >= 1.0.

    B = starke Vorbereitung / Trigger-Nähe:
        kein bestätigtes Setup,
        Momentum >= 3/4
        UND 3M-Hoch-Nähe ODER Volumen-Ausbruch.
        B ist ausdrücklich KEIN Sofortkauf.

    C = früher technischer Kandidat:
        kein bestätigtes Setup,
        Momentum >= 2/4,
        aber noch keine ausreichende B-Trigger-Konstellation.

    Sektor-RS:
        Der Titel wird direkt mit seinem Sektor-ETF verglichen.
        Positive Relative Stärke bestätigt die Einstufung,
        negative Relative Stärke erzeugt eine Warnung.
        Der Sektor-RS ist KEIN Rotationsfilter und ersetzt kein Setup.
    """
    momentum = int(momentum_ergebnis.get("score", 0))
    momentum_max = int(momentum_ergebnis.get("max_score", 4))

    tf_ok = (
        trendfolge_res is not None
        and _crv_ok(trendfolge_res)
    )

    tw_ok = (
        trendwende_res is not None
        and _crv_ok(trendwende_res)
    )

    near_high = bool(momentum_ergebnis.get("near_high"))

    vol_ratio = momentum_ergebnis.get("vol_ratio")
    ema_distance = momentum_ergebnis.get("ema50_distance")

    sector_rs = momentum_ergebnis.get("sector_rs")
    sector_rs_info = momentum_ergebnis.get("sector_rs_info") or {}
    sector_rs_diff = sector_rs_info.get("outperformance")

    gruende = []
    risiken = []

    # --------------------------------------------------------
    # Sektor-RS als Bestätigung / Warnung
    # --------------------------------------------------------

    if sector_rs is True:
        if sector_rs_diff is not None:
            gruende.append(
                f"Sektor-RS positiv ({float(sector_rs_diff):+.1f} %-Pkt.)"
            )
        else:
            gruende.append("Sektor-RS positiv")

    elif sector_rs is False:
        if sector_rs_diff is not None:
            risiken.append(
                f"Sektor-RS negativ ({float(sector_rs_diff):+.1f} %-Pkt.)"
            )
        else:
            risiken.append("Sektor-RS negativ")

    else:
        risiken.append("Sektor-RS nicht verfügbar")

    # --------------------------------------------------------
    # A: bestätigtes Setup
    # --------------------------------------------------------

    stoch = momentum_ergebnis.get("stoch")
    stoch_a_ok = (
        stoch is not None
        and float(stoch) <= KAUF_A_MAX_STOCH
    )

    if (tf_ok or tw_ok) and stoch_a_ok:
        if tf_ok:
            gruende.append(
                "Trendfolge-Setup bestätigt, CRV >= 1.0"
            )

        if tw_ok:
            gruende.append(
                "Trendwende-Setup bestätigt, CRV >= 1.0"
            )

        if momentum >= 3:
            gruende.append(
                f"Momentum unterstützt das Setup ({momentum}/{momentum_max})"
            )

        crvs = []

        for setup_res in (trendfolge_res, trendwende_res):
            if setup_res:
                for key in ("CRV1", "CRV2"):
                    try:
                        value = setup_res.get(key)
                        if value is not None:
                            crvs.append(float(value))
                    except (TypeError, ValueError):
                        pass

        if crvs:
            gruende.append(
                f"bestes vorhandenes CRV {max(crvs):.2f}"
            )

        return {
            "Ticker": ticker,
            "Status": "KAUFKANDIDAT A",
            "Score": momentum,
            "Momentum": f"{momentum}/{momentum_max}",
            "Gruende": gruende,
            "Risiken": risiken,
        }

    # --------------------------------------------------------
    # B: starke Vorbereitung / Trigger abwarten
    # --------------------------------------------------------

    b_trigger = near_high

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
            "noch kein bestätigtes Trendfolge-/Trendwende-Setup"
        )
        risiken.append(
            "KEIN Sofortkauf – Trigger und CRV abwarten"
        )

        return {
            "Ticker": ticker,
            "Status": "KAUFKANDIDAT B",
            "Score": momentum,
            "Momentum": f"{momentum}/{momentum_max}",
            "Gruende": gruende,
            "Risiken": risiken,
        }

    # --------------------------------------------------------
    # C: frühe technische Vorbereitung
    # --------------------------------------------------------

    if momentum >= 2:
        gruende.append(
            f"technische Vorbereitung vorhanden ({momentum}/{momentum_max})"
        )

        if near_high:
            gruende.append(
                "Kurs bereits in Richtung 3-Monats-Hoch"
            )

        if vol_ratio is not None:
            try:
                if float(vol_ratio) > 1.0:
                    gruende.append(
                        f"erhöhte Volumenaktivität ({float(vol_ratio):.2f}x SMA20)"
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
            "noch kein bestätigtes Einstiegssignal"
        )
        risiken.append(
            "noch keine ausreichende B-Trigger-Konstellation"
        )

        return {
            "Ticker": ticker,
            "Status": "KAUFKANDIDAT C",
            "Score": momentum,
            "Momentum": f"{momentum}/{momentum_max}",
            "Gruende": gruende,
            "Risiken": risiken,
        }

    # --------------------------------------------------------
    # Kein Kandidat
    # --------------------------------------------------------

    risiken.append(
        f"Momentum zu schwach ({momentum}/{momentum_max})"
    )
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
        "Status": "KEIN KANDIDAT",
        "Score": momentum,
        "Momentum": f"{momentum}/{momentum_max}",
        "Gruende": gruende,
        "Risiken": risiken,
    }


# ============================================================
# EINZELPRÜFUNG
# ============================================================

KAUFKANDIDATEN_ERGEBNISSE = []
A_AUFSTIEGE = []
A_AUFSTIEGE_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    f"Einzel_Check_Aufstiege({datetime.date.today().isoformat()}).txt",
)


def pruefe(
    ticker,
    spy_close,
    eu_close,
):
    trendfolge_res = None
    trendwende_aktien_res = None

    sektor_info = finde_sektor_information(ticker)

    sektor = sektor_info["sektor"]
    sektor_etf = sektor_info["etf"]
    ist_eu = sektor_info["eu"]

    klarname = NAME_HINWEIS.get(ticker)

    kopf = ticker

    if klarname:
        kopf += f" - {klarname}"

    # --------------------------------------------------------
    # Sektor-Hinweis
    # --------------------------------------------------------

    if len(sektor_info["alle_treffer"]) > 1:
        treffer_text = ", ".join(
            f"{etf}={sec}"
            for etf, sec in sektor_info["alle_treffer"]
        )

        print(
            f"  Hinweis: {ticker} ist mehreren Sektoren "
            f"zugeordnet ({treffer_text}) - "
            f"verwende '{sektor}' / {sektor_etf} "
            f"(erster Treffer aus analyse.py)."
        )

    elif sektor != "N/A":
        print(
            f"  Sektor automatisch aus analyse.py: "
            f"{sektor}"
            + (
                f" | Sektor-ETF: {sektor_etf}"
                if sektor_etf
                else " | kein Sektor-ETF gefunden"
            )
        )

    else:
        print(
            f"  Hinweis: {ticker} konnte in der "
            f"automatischen Sektorzuordnung nicht gefunden werden."
        )

    print("=" * 62)
    print(
        f"{kopf}   (Sektor: {sektor}"
        + (
            f" | Sektor-ETF: {sektor_etf}"
            if sektor_etf
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
        print(
            "  TRENDWENDE/SHORT: "
            "keine Kursdaten"
        )
        return

    # --------------------------------------------------------
    # 2) Momentum + direkter Sektor-RS
    # --------------------------------------------------------

    momentum_ergebnis = momentum_ausbruch_score(
        ticker,
        data,
        sektor,
        sektor_etf,
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
            res, grund = _trendwende(
                spannen_max
            )

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

                # Für einen echten A-Kandidaten darf nur die reguläre
                # Aktien-Regel zählen. Metall-Regel bleibt Messung.
                if (
                    spannen_max is None
                    and trendwende_aktien_res is None
                ):
                    trendwende_aktien_res = res

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

    if (
        aktien_ok is False
        and metall_ok is True
    ):
        try:
            kurs = float(
                data["Close"].iloc[-1]
            )
            tief = float(
                data["Low"].min()
            )
            hoch = float(
                data["High"].max()
            )

            if hoch > tief:
                spannen_position = (
                    (kurs - tief)
                    / (hoch - tief)
                )
            else:
                spannen_position = 0.0

            print(
                "  >>> ABWEICHUNG: nur die "
                "Metall-Regel laesst diesen Titel zu "
                f"({(kurs / tief - 1) * 100:.1f}% "
                "ueber 52W-Tief, "
                f"Spannen-Position "
                f"{spannen_position:.0%})"
            )

        except Exception:
            print(
                "  >>> ABWEICHUNG: nur die "
                "Metall-Regel laesst diesen Titel zu."
            )

    elif (
        aktien_ok is True
        and metall_ok is False
    ):
        print(
            "  >>> ABWEICHUNG umgekehrt: "
            "nur die Aktien-Regel laesst "
            "diesen Titel zu."
        )

    # --------------------------------------------------------
    # 4) KAUFKANDIDATEN-BEWERTUNG
    # --------------------------------------------------------

    kauf = bewerte_kaufkandidat(
        ticker=ticker,
        momentum_ergebnis=momentum_ergebnis,
        trendfolge_res=trendfolge_res,
        trendwende_res=trendwende_aktien_res,
    )

    print()
    print(
        "  KAUFKANDIDATEN-BEWERTUNG"
    )
    print(
        "  " + "-" * 45
    )
    print(
        f"  Ergebnis: {kauf['Status']} "
        f"(Momentum {kauf['Momentum']})"
    )

    for grund in kauf["Gruende"]:
        print(
            f"    ✓ {grund}"
        )

    for risiko in kauf["Risiken"]:
        print(
            f"    ⚠ {risiko}"
        )

    # Persistente Beobachtungsliste:
    # A entfernt, B/C aufnehmen bzw. aktualisieren, KEIN KANDIDAT entfernen.
    # Vor dem Update den alten Status lesen, damit nur echte B/C -> A-Aufstiege
    # aus der Beobachtungsliste gemeldet werden.
    vorherige_liste = lade_beobachtungsliste()
    vorheriger_status = vorherige_liste.get(ticker, {}).get("status")
    if kauf["Status"] == "KAUFKANDIDAT A" and vorheriger_status in (
        "KAUFKANDIDAT B", "KAUFKANDIDAT C"
    ):
        A_AUFSTIEGE.append({
            "Ticker": ticker,
            "Name": klarname or ticker,
            "von": vorheriger_status,
            "Datum": datetime.date.today().isoformat(),
            "Momentum": kauf.get("Momentum", "n/a"),
        })
        print(
            f"  >>> AUFSTIEG AUS BEOBACHTUNGSLISTE: {klarname or ticker} "
            f"({ticker}) {vorheriger_status} -> KAUFKANDIDAT A"
        )

    aktualisiere_beobachtungsliste(
        ticker,
        kauf["Status"],
    )

    if kauf["Status"] != "KEIN KANDIDAT":
        KAUFKANDIDATEN_ERGEBNISSE.append(
            kauf
        )

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
    """
    Akzeptiert sowohl Leerzeichen als auch Kommas.

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
    # Bestehende Beobachtungstitel werden bei jedem Einzel-Check automatisch
    # erneut geprüft. ``--beobachtungsliste`` erzwingt zusätzlich den reinen
    # Watchlist-Modus; manuell angegebene Ticker bleiben trotzdem möglich.
    argumente = [arg for arg in sys.argv[1:] if arg != "--beobachtungsliste"]
    beobachtungsliste_modus = "--beobachtungsliste" in sys.argv[1:]

    beobachtung = lade_beobachtungsliste()

    # Die synchronisierte JSON ist die maßgebliche aktuelle Watchlist.
    # Nur wenn sie wirklich nicht vorhanden/leer ist, wird Punkt 4 der
    # letzten Auswertung als Fallback verwendet. So werden Titel, die im
    # selben Tag bereits als A/KEIN KANDIDAT entfernt wurden, nicht durch
    # eine ältere Auswertung wieder auferweckt.
    if not beobachtung:
        beobachtung = lade_beobachtung_aus_letzter_auswertung()
        if beobachtung:
            speichere_beobachtungsliste(beobachtung)

    beobachtete_ticker = list(beobachtung.keys())
    if beobachtete_ticker:
        print(
            "INFO: Aktive Beobachtungsliste für diesen Lauf: "
            + ", ".join(beobachtete_ticker)
        )

    if argumente:
        ticker_liste = parse_ticker_args(argumente)
    elif not beobachtungsliste_modus:
        ticker_liste = TICKER_DEFAULT
    else:
        ticker_liste = []

    # Bestehende B/C-Titel immer zur heutigen Prüfung hinzufügen.
    # Doppelte Ticker werden nur einmal geprüft; die Reihenfolge bleibt
    # manuelle Eingabe zuerst, danach Beobachtungsliste.
    ticker_liste = list(
        dict.fromkeys(ticker_liste + beobachtete_ticker)
    )

    print(
        f"EINZEL-CHECK "
        f"{datetime.date.today().isoformat()} - "
        f"{len(ticker_liste)} Titel: "
        f"{', '.join(ticker_liste)}"
    )

    print(
        "Hinweis: Dieser Einzel-Check ist KEIN "
        "Sektor-Rotationsscanner. "
        "Die Sektorzuordnung erfolgt automatisch aus "
        "analyse.py. Der Sektor-RS wird direkt gegen "
        "den zugehörigen Sektor-ETF berechnet. "
        "Momentum allein ist KEIN Kauf.\n"
    )

    spy_close = get_benchmark_close()
    eu_close = get_eu_benchmark_close()

    for ticker in ticker_liste:
        try:
            pruefe(
                ticker,
                spy_close,
                eu_close,
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
    # Tagesdatei fuer die fertige Auswertung: Nur echte B/C -> A-Aufstiege.
    try:
        if A_AUFSTIEGE:
            with open(A_AUFSTIEGE_DATEI, "w", encoding="utf-8") as f:
                f.write("EINZEL-CHECK: NEUE KAUFKANDIDAT-A-AUFSTIEGE\n")
                f.write("===========================================\n\n")
                for eintrag in A_AUFSTIEGE:
                    f.write(
                        f"Name: {eintrag['Name']} | Ticker: {eintrag['Ticker']} | "
                        f"Aufstieg: {eintrag['von']} -> KAUFKANDIDAT A | "
                        f"Datum: {eintrag['Datum']} | Momentum: {eintrag['Momentum']}\n\n"
                    )
            print(f"A-AUFSTIEGE_DATEI={A_AUFSTIEGE_DATEI}")
        else:
            try:
                os.remove(A_AUFSTIEGE_DATEI)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"WARNUNG: A-Aufstiegsdatei konnte nicht geschrieben werden: {exc}")

    print(
        "KAUFKANDIDATEN DES CHECKS"
    )
    print(
        "A = bestätigtes Setup + CRV >= 1.0 | "
        "B = starke Trigger-Nähe | C = frühe technische Vorbereitung"
    )
    print("=" * 62)

    if not KAUFKANDIDATEN_ERGEBNISSE:
        print(
            "Keine Kaufkandidaten gefunden."
        )

    else:
        rangfolge = {
            "KAUFKANDIDAT A": 0,
            "KAUFKANDIDAT B": 1,
            "KAUFKANDIDAT C": 2,
        }

        sortiert = sorted(
            KAUFKANDIDATEN_ERGEBNISSE,
            key=lambda x: (
                rangfolge.get(x["Status"], 9),
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
                print(
                    f"    ✓ {grund}"
                )

            for risiko in kandidat["Risiken"]:
                print(
                    f"    ⚠ {risiko}"
                )

            print()

    # ========================================================
    # BEOBACHTUNGSLISTE
    # ========================================================

    beobachtung = lade_beobachtungsliste()

    print()
    print("=" * 62)
    print("AKTUELLE EINZEL-CHECK-BEOBACHTUNGSLISTE")
    print("B/C = beobachten | A/KEIN KANDIDAT = automatisch entfernt")
    print("=" * 62)

    if not beobachtung:
        print("Beobachtungsliste ist leer.")
    else:
        for ticker, eintrag in sorted(beobachtung.items()):
            print(
                f"{ticker:8} "
                f"{eintrag.get('status', 'UNBEKANNT'):18} "
                f"letzter Check {eintrag.get('letzter_check', '?')}"
            )

    print("=" * 62)
    print(
        "ENDE EINZEL-CHECK"
    )
    print("=" * 62)
