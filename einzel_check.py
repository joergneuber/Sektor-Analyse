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
# Verbindliche Regel:
#   A -> Liste behalten + separate A-Meldung
#   B -> Liste behalten + last_candidate_date aktualisieren
#   C -> Liste behalten + last_candidate_date aktualisieren
#   KEIN KANDIDAT -> Liste aufnehmen/behalten + last_candidate_date NICHT aktualisieren
#   >45 Tage ohne A/B/C -> entfernen
#   technische/strukturelle Ungültigkeit -> entfernen
BEOBACHTUNG_MAX_TAGE_OHNE_KANDIDAT = 45
BEOBACHTUNGS_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "einzel_check_beobachtung.json",
)

# Google-Drive-Ordner des Projekts. Wird nur für den Auswertungs-Fallback
# benötigt; die eigentliche Watchlist bleibt die JSON-Datei.
FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"

# Historische Zeitreihe des Einzel-Checks.
# Pro Ticker und Check-Tag wird genau ein Snapshot gespeichert.
# Die Datei ist append-or-update (dedupliziert nach Datum + Ticker) und
# veraendert weder die A/B/C-Logik noch die bestehende Beobachtungsliste.
HISTORIE_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "einzel_check_historie.jsonl",
)


def _historie_normalisiere_wert(value):
    """Macht pandas/numpy-Werte JSON-sicher; unbekannte Werte werden None."""
    try:
        if value is None:
            return None
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def lade_historie_snapshot_map():
    """Laedt vorhandene Snapshots und dedupliziert nach Datum + Ticker."""
    snapshots = {}
    if not os.path.exists(HISTORIE_DATEI):
        return snapshots

    try:
        with open(HISTORIE_DATEI, "r", encoding="utf-8") as f:
            for zeile in f:
                zeile = zeile.strip()
                if not zeile:
                    continue
                try:
                    eintrag = json.loads(zeile)
                except json.JSONDecodeError:
                    continue
                key = (eintrag.get("Datum"), eintrag.get("Ticker"))
                if key[0] and key[1]:
                    snapshots[key] = eintrag
    except OSError:
        pass

    return snapshots


def speichere_historie_snapshot(snapshot):
    """Speichert einen Tages-Snapshot atomar und dedupliziert ihn."""
    snapshots = lade_historie_snapshot_map()
    key = (snapshot.get("Datum"), snapshot.get("Ticker"))
    if not key[0] or not key[1]:
        return

    snapshots[key] = snapshot
    sortiert = sorted(
        snapshots.values(),
        key=lambda x: (x.get("Datum", ""), x.get("Ticker", "")),
    )

    temp_datei = HISTORIE_DATEI + ".tmp"
    with open(temp_datei, "w", encoding="utf-8") as f:
        for eintrag in sortiert:
            f.write(json.dumps(eintrag, ensure_ascii=False, sort_keys=True) + "\n")

    os.replace(temp_datei, HISTORIE_DATEI)


def _crv_aus_resultat(res):
    """Gibt CRV1/CRV2/max CRV fuer die Historie zurueck."""
    if not res:
        return None, None, None
    werte = []
    result = []
    for key in ("CRV1", "CRV2"):
        value = _historie_normalisiere_wert(res.get(key))
        result.append(value)
        if isinstance(value, (int, float)):
            werte.append(float(value))
    return result[0], result[1], (max(werte) if werte else None)


def schreibe_historie_snapshot(
    ticker,
    data,
    sektor_info,
    momentum_ergebnis,
    trendfolge_res,
    trendwende_res,
    kauf,
    vorheriger_status,
):
    """Persistiert alle fuer die spaetere B->A-/B->C-Analyse relevanten Werte."""
    datum = datetime.date.today().isoformat()
    close = high = low = None
    try:
        close = float(data["Close"].iloc[-1])
        high = float(data["High"].iloc[-1])
        low = float(data["Low"].iloc[-1])
    except Exception:
        pass

    tf_crv1, tf_crv2, tf_crv_max = _crv_aus_resultat(trendfolge_res)
    tw_crv1, tw_crv2, tw_crv_max = _crv_aus_resultat(trendwende_res)
    sector_rs_info = momentum_ergebnis.get("sector_rs_info") or {}

    snapshot = {
        "Schema": 1,
        "Datum": datum,
        "Ticker": ticker,
        "Name": NAME_HINWEIS.get(ticker, ticker),
        "Status": kauf.get("Status"),
        "Vorheriger_Status": vorheriger_status,
        "Momentum": kauf.get("Momentum"),
        "Momentum_Score": momentum_ergebnis.get("score"),
        "Momentum_Max": momentum_ergebnis.get("max_score"),
        "Stoch": _historie_normalisiere_wert(momentum_ergebnis.get("stoch")),
        "Vol_Ratio": _historie_normalisiere_wert(momentum_ergebnis.get("vol_ratio")),
        "EMA50_Distance": _historie_normalisiere_wert(momentum_ergebnis.get("ema50_distance")),
        "Near_High": bool(momentum_ergebnis.get("near_high", False)),
        "Kurs": _historie_normalisiere_wert(close),
        "High": _historie_normalisiere_wert(high),
        "Low": _historie_normalisiere_wert(low),
        "Sektor": sektor_info.get("sektor"),
        "Sektor_ETF": sektor_info.get("etf"),
        "Sektor_RS": _historie_normalisiere_wert(sector_rs_info.get("outperformance")),
        "Sektor_RS_verfuegbar": bool(sector_rs_info.get("verfuegbar", False)),
        "Trendfolge_Setup": trendfolge_res.get("Setup_Typ") if trendfolge_res else None,
        "Trendfolge_Status": trendfolge_res.get("Status2") if trendfolge_res else None,
        "Trendfolge_CRV1": tf_crv1,
        "Trendfolge_CRV2": tf_crv2,
        "Trendfolge_CRV_Max": tf_crv_max,
        "Trendwende_Setup": trendwende_res.get("Setup_Typ") if trendwende_res else None,
        "Trendwende_CRV1": tw_crv1,
        "Trendwende_CRV2": tw_crv2,
        "Trendwende_CRV_Max": tw_crv_max,
        "Gruende": kauf.get("Gruende", []),
        "Risiken": kauf.get("Risiken", []),
    }

    try:
        speichere_historie_snapshot(snapshot)
    except OSError as exc:
        print(f"  WARNUNG: Historien-Snapshot konnte nicht gespeichert werden: {exc}")


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


def _normalisiere_beobachtungseintrag(ticker, eintrag):
    """Migriert alte Watchlist-Einträge auf das persistente Datumsfeld."""
    if not isinstance(eintrag, dict):
        eintrag = {}

    status = str(eintrag.get("status", "")).strip().upper()
    letzter_check = eintrag.get("letzter_check")
    last_candidate_date = eintrag.get("last_candidate_date")
    beobachtung_start_date = eintrag.get("beobachtung_start_date")

    if not last_candidate_date and status in ("KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C"):
        # Alte Versionen speicherten das letzte A/B/C-Datum in
        # ``letzter_check``. Dieses Datum wird einmalig übernommen.
        # Bei KEIN KANDIDAT darf ``letzter_check`` NICHT als A/B/C-Datum
        # interpretiert werden.
        if isinstance(letzter_check, str):
            match = re.search(r"(20\d{2}-\d{2}-\d{2})", letzter_check)
            if match:
                last_candidate_date = match.group(1)

    if not beobachtung_start_date and status == "KEIN KANDIDAT":
        # Alte Watchlist-Einträge ohne Startdatum können ihren bekannten
        # letzten Check als konservativen Beobachtungsbeginn verwenden.
        if isinstance(letzter_check, str):
            match = re.search(r"(20\d{2}-\d{2}-\d{2})", letzter_check)
            if match:
                beobachtung_start_date = match.group(1)

    return {
        "status": status or "UNBEKANNT",
        "letzter_check": letzter_check or "?",
        "last_candidate_date": last_candidate_date,
        "beobachtung_start_date": beobachtung_start_date,
    }


def _normalisiere_watchlist(liste):
    """Normalisiert alte Watchlist-Einträge ohne deren Alter vor der heutigen Prüfung zu bewerten."""
    normalisiert = {}
    for ticker, eintrag in liste.items():
        ticker = str(ticker).strip().upper()
        if not ticker:
            continue
        normalisiert[ticker] = _normalisiere_beobachtungseintrag(ticker, eintrag)
    return normalisiert


def _alter_seit_letztem_kandidaten(eintrag, heute=None):
    """Liefert Tage seit letztem A/B/C, sonst seit Beobachtungsbeginn."""
    heute = heute or datetime.date.today()
    if not isinstance(eintrag, dict):
        return None

    # Nach einem A/B/C zählt die Frist ab diesem letzten Kandidatenstatus.
    datum_text = eintrag.get("last_candidate_date")
    # Bei einem neu aufgenommenen KEIN-KANDIDAT ohne bisherigen A/B/C-Status
    # zählt die Frist ab dem ersten Beobachtungstag.
    if not datum_text:
        datum_text = eintrag.get("beobachtung_start_date")
    if not datum_text:
        return None

    try:
        datum = datetime.date.fromisoformat(str(datum_text))
    except ValueError:
        return None
    return (heute - datum).days


class TechnischUngueltigerTickerError(ValueError):
    """Belastbarer technischer Ausschluss: Symbol ist eindeutig ungültig/Delisting."""


def _ist_eindeutig_technisch_ungueltig(exc):
    """Erkennt nur belastbare Symbol-/Delisting-Faelle.

    Transport-, Rate-Limit-, Timeout-, HTTP- und sonstige temporaere
    Abruffehler duerfen niemals als Delisting gewertet werden.
    """
    if exc is None:
        return False

    # Unser eigener, bereits eindeutig klassifizierter Fehler hat Vorrang.
    # Damit kann die ursprüngliche Yahoo-Ausnahme sicher verpackt werden,
    # ohne dass die technische Klassifikation verloren geht.
    if isinstance(exc, TechnischUngueltigerTickerError):
        return True

    # Bekannte yfinance-Klassen sind staerker als freie Fehlermeldungen.
    cls_name = type(exc).__name__.lower()
    if cls_name in {
        "yftickermissingerror",
        "yfpricesmissingerror",
    }:
        text = str(exc).lower()
        # Auch bei YFPricesMissingError nur dann entfernen, wenn der
        # Fehlertext tatsaechlich auf fehlendes/Delisting-Symbol hindeutet.
        return any(marker in text for marker in (
            "delisted",
            "symbol may be delisted",
            "quote not found",
            "no price data found",
            "possibly delisted",
        ))

    text = str(exc).lower()

    # Temporäre/technische Transportfehler haben Vorrang: niemals löschen.
    temporaer = (
        "timeout", "timed out", "rate limit", "too many requests",
        "429", "connection", "connect", "connection reset",
        "connection aborted", "ssl", "proxy", "502", "503", "504",
        "bad gateway", "service unavailable", "temporarily",
        "temporary", "remote end closed",
    )
    if any(marker in text for marker in temporaer):
        return False

    # Freitext-Fallback nur fuer eindeutige Symbol-/Delisting-Hinweise.
    eindeutige_marker = (
        "quote not found for symbol",
        "no data found, symbol may be delisted",
        "symbol may be delisted",
        "possibly delisted",
        "invalid ticker",
        "invalid symbol",
        "delisted",
    )
    return any(marker in text for marker in eindeutige_marker)



def lade_beobachtung_aus_letzter_auswertung():
    """
    Konservativer Fallback nur bei fehlender JSON-Watchlist.
    Es werden ausschließlich explizit als B/C ausgewiesene Titel übernommen.
    Eine vorhandene, aber leere JSON-Datei ist ein gültiger Zustand und löst
    diesen Fallback NICHT aus.
    """
    try:
        service = get_drive_service()
        query = (
            f"name contains 'Auswertung(' and '{FOLDER_ID}' in parents "
            "and trashed = false"
        )
        antwort = service.files().list(
            q=query, spaces="drive",
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc", pageSize=20,
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
        marker = "Einzel-Check-Beobachtungsliste"
        start = text.find(marker)
        if start < 0:
            return {}
        block = text[start:]
        ende = re.search(r"\n\s*\d+\.\s+", block)
        if ende:
            block = block[:ende.start()]

        # Nur B/C als konservativer Fallback. A und KEIN KANDIDAT dürfen
        # aus einer alten Auswertung nicht wieder in die Watchlist gelangen.
        ergebnis = {}
        muster_zeile = re.compile(
            r"^\s*([A-Za-z0-9.\-]+)\s+(KAUFKANDIDAT\s+[BC])\s+letzter Check\s+(20\d{2}-\d{2}-\d{2})\s*$",
            re.IGNORECASE,
        )
        muster_inline = re.compile(
            r"Ticker:\s*([A-Za-z0-9.\-]+)\s*\|\s*Status:\s*(KAUFKANDIDAT\s+[BC])",
            re.IGNORECASE,
        )

        for zeile in block.splitlines():
            match = muster_zeile.match(zeile)
            if match:
                ticker, status, datum = match.groups()
                ergebnis[ticker.strip().upper()] = {
                    "status": re.sub(r"\s+", " ", status.strip().upper()),
                    "letzter_check": datum,
                    "last_candidate_date": datum,
                }
                continue

            match = muster_inline.search(zeile)
            if match:
                ticker, status = match.groups()
                ergebnis[ticker.strip().upper()] = {
                    "status": re.sub(r"\s+", " ", status.strip().upper()),
                    # Inline-Fallback ohne Datum wird bewusst nicht auf
                    # "heute" datiert: unbekanntes Alter darf die 45-Tage-
                    # Frist nicht künstlich zurücksetzen.
                    "letzter_check": "aus letzter Auswertung",
                    "last_candidate_date": None,
                }

        if ergebnis:
            print(
                f"INFO: Konservativer Fallback aus letzter Auswertung "
                f"({datei['name']}) übernommen: {len(ergebnis)} B/C-Titel."
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


def aktualisiere_beobachtungsliste(ticker, status, grund=None):
    """Pflegt die persistente Watchlist nach der verbindlichen Zielregel."""
    liste = _normalisiere_watchlist(lade_beobachtungsliste())
    heute = datetime.date.today().isoformat()
    war_bereits_drin = ticker in liste

    if status == "KAUFKANDIDAT A":
        liste[ticker] = {
            "status": status,
            "letzter_check": heute,
            "last_candidate_date": heute,
        }
        speichere_beobachtungsliste(liste)
        print(
            f"  BEOBACHTUNGSLISTE: {ticker} "
            f"{'beibehalten' if war_bereits_drin else 'aufgenommen'} -> {status}"
        )
        return

    if status in ("KAUFKANDIDAT B", "KAUFKANDIDAT C"):
        liste[ticker] = {
            "status": status,
            "letzter_check": heute,
            "last_candidate_date": heute,
        }
        speichere_beobachtungsliste(liste)
        print(
            f"  BEOBACHTUNGSLISTE: {ticker} "
            f"{'aktualisiert' if war_bereits_drin else 'aufgenommen'} -> {status}"
        )
        return

    if status == "KEIN KANDIDAT":
        if war_bereits_drin:
            eintrag = liste[ticker]
            eintrag["status"] = status
            eintrag["letzter_check"] = heute
        else:
            # Variante B: Jeder technisch gültige, tatsächlich geprüfte Titel
            # wird auch bei KEIN KANDIDAT neu in die Beobachtungsliste aufgenommen.
            eintrag = {
                "status": status,
                "letzter_check": heute,
                "last_candidate_date": None,
                "beobachtung_start_date": heute,
            }
            liste[ticker] = eintrag

        alter = _alter_seit_letztem_kandidaten(eintrag)

        if alter is not None and alter > BEOBACHTUNG_MAX_TAGE_OHNE_KANDIDAT:
            del liste[ticker]
            speichere_beobachtungsliste(liste)
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} entfernt -> {alter} Tage "
                f"ohne A/B/C (> {BEOBACHTUNG_MAX_TAGE_OHNE_KANDIDAT} Tage)"
            )
        else:
            speichere_beobachtungsliste(liste)
            quelle = "beibehalten" if war_bereits_drin else "aufgenommen"
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} {quelle} -> {status} "
                f"(last_candidate_date bleibt {eintrag.get('last_candidate_date', '?')}, "
                f"Start {eintrag.get('beobachtung_start_date', '?')})"
            )
        return

    # Technischer/struktureller Ausschluss darf einen vorhandenen Titel
    # entfernen. Ein temporärer Kursdaten-/Yahoo-Fehler wird niemals mit
    # diesem Status aufgerufen.
    if grund or status == "TECHNISCH UNGÜLTIG":
        if war_bereits_drin:
            del liste[ticker]
            speichere_beobachtungsliste(liste)
            print(
                f"  BEOBACHTUNGSLISTE: {ticker} entfernt -> {status}"
                + (f" ({grund})" if grund else "")
            )
        return

    print(
        f"  WARNUNG: Unbekannter Status für {ticker}: {status} - "
        "Watchlist-Eintrag bleibt unverändert."
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
    except Exception as exc:
        if _ist_eindeutig_technisch_ungueltig(exc):
            raise TechnischUngueltigerTickerError(
                f"Technisch ungültiger Ticker / mögliches Delisting: {exc}"
            ) from exc
        raise RuntimeError(f"Yahoo-Kursdatenabruf vorübergehend fehlgeschlagen: {exc}") from exc

    if data.empty:
        # Leere Antwort allein ist kein sicherer Delisting-Nachweis.
        raise RuntimeError("Yahoo lieferte vorübergehend keine Kursdaten")

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
A_MELDUNGEN = []
A_AUFSTIEGE_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    f"Einzel_Check_Aufstiege({datetime.date.today().isoformat()}).txt",
)
A_MELDUNGEN_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    f"Einzel_Check_A_Meldungen({datetime.date.today().isoformat()}).txt",
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

    try:
        data = hole_kursdaten(ticker)
    except Exception as exc:
        # Temporärer Yahoo-/Netzwerk-/Datenfehler: NICHT als Delisting oder
        # technische Ungültigkeit behandeln und daher niemals die Watchlist löschen.
        print(
            f"  TRENDWENDE/SHORT: Kursdaten vorübergehend nicht verfügbar "
            f"({type(exc).__name__}: {exc}) - Watchlist unverändert"
        )
        return

    if data is None or data.empty:
        print(
            "  TRENDWENDE/SHORT: Kursdaten nicht verfügbar "
            "- Watchlist unverändert"
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

    # Persistente Beobachtungsliste nach der verbindlichen Watchlist-Regel.
    # Vor dem Update den alten Status lesen, damit echte B/C -> A-Aufstiege
    # separat von den allgemeinen aktuellen A-Meldungen erkannt werden.
    vorherige_liste = lade_beobachtungsliste()
    vorheriger_status = vorherige_liste.get(ticker, {}).get("status")

    # Historien-Snapshot: unabhaengig davon, ob der Titel A/B/C/kein Kandidat ist.
    # Dadurch koennen spaeter echte B-Episoden, Rueckfaelle und A-Aufstiege
    # zeitlich rekonstruiert werden, ohne die bestehende Klassifikation zu veraendern.
    schreibe_historie_snapshot(
        ticker=ticker,
        data=data,
        sektor_info=sektor_info,
        momentum_ergebnis=momentum_ergebnis,
        trendfolge_res=trendfolge_res,
        trendwende_res=trendwende_aktien_res,
        kauf=kauf,
        vorheriger_status=vorheriger_status,
    )

    if kauf["Status"] == "KAUFKANDIDAT A":
        A_MELDUNGEN.append({
            "Ticker": ticker,
            "Name": klarname or ticker,
            "Vorheriger_Status": vorheriger_status or "nicht in Watchlist",
            "Datum": datetime.date.today().isoformat(),
            "Momentum": kauf.get("Momentum", "n/a"),
        })

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

    # Die JSON ist die alleinige maßgebliche Watchlist, sobald sie existiert.
    # Ein Fallback aus der letzten Auswertung darf NUR bei fehlender JSON-Datei
    # greifen. Eine vorhandene leere JSON-Datei ist ein bewusst gültiger Zustand
    # und darf nicht durch alte B/C-Einträge wieder aufgefüllt werden.
    if not os.path.exists(BEOBACHTUNGS_DATEI):
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
    print(
        f"Historie: {HISTORIE_DATEI} "
        "(1 Snapshot pro Ticker/Tag, ohne A/B/C-Logikaenderung)\n"
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
        except ValueError as e:
            if _ist_eindeutig_technisch_ungueltig(e):
                aktualisiere_beobachtungsliste(
                    ticker,
                    "TECHNISCH UNGÜLTIG",
                    grund=str(e),
                )
            else:
                print(f"\nDATENFEHLER BEI {ticker}: {type(e).__name__}: {e} - Watchlist unverändert.")
        except Exception as e:
            print(
                f"\nDATENFEHLER BEI {ticker}: "
                f"{type(e).__name__}: {e} - Watchlist unverändert."
            )

        print()

    # ========================================================
    # GESAMTERGEBNIS
    # ========================================================

    print()
    print("=" * 62)
    # Tagesdatei 1: echte B/C -> A-Aufstiege.
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

    # Tagesdatei 2: alle aktuellen A-Kandidaten. Diese Information ist bewusst
    # getrennt von den echten B/C -> A-Aufstiegen.
    try:
        if A_MELDUNGEN:
            with open(A_MELDUNGEN_DATEI, "w", encoding="utf-8") as f:
                f.write("EINZEL-CHECK: AKTUELLE KAUFKANDIDAT-A-MELDUNGEN\n")
                f.write("==============================================\n\n")
                for eintrag in A_MELDUNGEN:
                    f.write(
                        f"Name: {eintrag['Name']} | Ticker: {eintrag['Ticker']} | "
                        f"Vorheriger Status: {eintrag['Vorheriger_Status']} | "
                        f"Datum: {eintrag['Datum']} | Momentum: {eintrag['Momentum']}\n\n"
                    )
            print(f"A-MELDUNGEN_DATEI={A_MELDUNGEN_DATEI}")
        else:
            try:
                os.remove(A_MELDUNGEN_DATEI)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"WARNUNG: A-Meldungsdatei konnte nicht geschrieben werden: {exc}")

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
    print(
        "A/B/C = aufnehmen/behalten | KEIN KANDIDAT = aufnehmen/behalten | "
        ">45 Tage ohne A/B/C = entfernen | technisch ungültig/Delisting = entfernen"
    )
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
