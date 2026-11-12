"""
hebeltrader_einzel_check.py

Täglicher HEBELTRADER -> Kandidaten -> Einzel-Check Pipeline.

Ablauf:
1. Börsenmedien HEBELTRADER-Seite nach der neuesten Ausgabe prüfen.
2. Nur eine tatsächlich neue Ausgabe verarbeiten.
3. Teaser der Ausgabe mehrstufig durch LLM auf mögliche Aktien/Yahoo-Ticker
   untersuchen (bewusst recall-lastig: eher mehr als weniger Kandidaten).
4. Kandidaten deterministisch gegen Yahoo Finance validieren.
5. Alle validierten Kandidaten gesammelt durch einzel_check.py schicken.
6. Ausgabe + maschinenlesbare Ergebnisse persistent als
   hebeltrader_einzel_check.json speichern.
7. Die Datei wird anschließend per upload_to_drive.py nach Google Drive gespiegelt.

Wichtig:
- Es wird niemals ein HEBELTRADER-Produkt/Derivat automatisch gehandelt.
- Die Kandidaten sind Recherche-/Prüfkandidaten für den bestehenden Einzel-Check.
- Bei Fehlern wird der bisherige persistente Stand NICHT überschrieben.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import requests
import yfinance as yf
from lxml import html
from zoneinfo import ZoneInfo

try:
    from groq import Groq
except Exception:
    Groq = None


HEBELTRADER_INDEX_URL = "https://www.boersenmedien.de/hebeltrader"
STATE_FILE = "hebeltrader_einzel_check.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139 Safari/537.36"
)
BERLIN = ZoneInfo("Europe/Berlin")

# Bewusst großzügig. Die zweite LLM-Stufe und Yahoo-Validierung reduzieren
# Halluzinationen; die recall-lastige Vorauswahl soll dagegen keine plausible
# Aktie zu früh verlieren.
MAX_LLM_KANDIDATEN = 25
MAX_VALIDIERTE_KANDIDATEN = 25

# Bekannte Nicht-Aktien, die der Kandidaten-LLM gelegentlich als Ticker nennt.
AUSSCHLUSS_TICKER = {
    "SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "XLK", "XLF", "XLE", "XLI",
    "XLV", "XLY", "XLP", "XLC", "XLB", "XLU", "XLRE", "XRT", "AIQ", "BOTZ",
    "GDX", "GLD", "SLV", "USO", "UNG", "TLT", "VIX", "^GSPC", "^NDX",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})


def berlin_now() -> dt.datetime:
    return dt.datetime.now(BERLIN)


def _request(url: str) -> str:
    last = None
    for attempt in range(1, 4):
        try:
            response = SESSION.get(url, timeout=25)
            response.raise_for_status()
            if not response.text.strip():
                raise RuntimeError("Leere HTTP-Antwort")
            return response.text
        except Exception as exc:
            last = exc
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"HTTP-Abruf fehlgeschlagen: {url} ({last})")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def lade_neueste_ausgabe() -> dict[str, Any]:
    source = _request(HEBELTRADER_INDEX_URL)
    tree = html.fromstring(source)

    candidates = []
    for a in tree.xpath("//a[@href]"):
        href = a.get("href") or ""
        label = _clean(" ".join(a.itertext()))
        m = re.search(
            r"/produkt/hebeltrader/hebeltrader-(\d+)26-[^/?#]+\.html",
            href,
            flags=re.I,
        )
        if not m:
            continue
        nr = int(m.group(1))
        if href.startswith("/"):
            href = "https://www.boersenmedien.de" + href
        candidates.append((nr, label, href))

    if not candidates:
        raise RuntimeError("Keine HEBELTRADER-Ausgabe auf der Börsenmedien-Seite gefunden.")

    nr, label, url = max(candidates, key=lambda x: x[0])
    issue_html = _request(url)
    issue_tree = html.fromstring(issue_html)

    h1 = _clean(" ".join(issue_tree.xpath("//h1[1]//text()")))
    h2 = _clean(" ".join(issue_tree.xpath("//h2[1]//text()")))
    date_text = _clean(" ".join(issue_tree.xpath("//body//text()")))

    # Die Produktseite enthält öffentlich den redaktionellen Teaser, aber nicht
    # das gekaufte PDF. Wir verwenden ausschließlich den frei sichtbaren Inhalt.
    paragraphs = []
    for p in issue_tree.xpath("//main//p | //article//p | //p"):
        value = _clean(" ".join(p.itertext()))
        if value and value not in paragraphs:
            paragraphs.append(value)

    teaser_parts = []
    if h2:
        teaser_parts.append(h2)
    for p in paragraphs:
        if len(p) >= 50 and p not in teaser_parts:
            teaser_parts.append(p)

    teaser = "\n".join(teaser_parts[:8]).strip()
    if not teaser:
        # Fallback: OpenGraph/meta description
        metas = issue_tree.xpath(
            "//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='description']/@content"
            " | //meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='og:description']/@content"
        )
        teaser = _clean(metas[0]) if metas else ""

    if not teaser:
        raise RuntimeError(f"HEBELTRADER {nr}/26: kein öffentlich sichtbarer Teaser gefunden.")

    datum = ""
    m_date = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_text)
    if m_date:
        datum = m_date.group(1)

    return {
        "issue_number": nr,
        "issue_label": f"HEBELTRADER {nr}/26",
        "issue_url": url,
        "issue_date": datum,
        "page_title": h1,
        "headline": h2,
        "teaser": teaser,
        "detected_at": berlin_now().isoformat(),
    }


def lade_state(state_path: str | None = None) -> dict[str, Any]:
    target_path = state_path or STATE_FILE
    if not os.path.exists(target_path):
        return {}
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        raise RuntimeError(f"{target_path} ist nicht lesbares JSON: {exc}") from exc


def _groq_client() -> Any:
    if Groq is None:
        raise RuntimeError("Python-Paket 'groq' ist nicht installiert.")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY fehlt.")
    return Groq(api_key=key)


def _json_from_response(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    # Erlaube Modelle, vor/nach dem JSON etwas Text zu setzen.
    start_obj = raw.find("{")
    start_arr = raw.find("[")
    starts = [x for x in (start_obj, start_arr) if x >= 0]
    if not starts:
        raise ValueError("Keine JSON-Struktur in LLM-Antwort gefunden.")
    start = min(starts)
    raw = raw[start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Bis zur letzten schließenden Klammer abschneiden.
        end = max(raw.rfind("}"), raw.rfind("]"))
        if end < 0:
            raise
        return json.loads(raw[: end + 1])


def _llm_call(client: Any, system: str, user: str) -> Any:
    """Ruft Groq auf; wiederholt ausschließlich json_validate_failed genau einmal."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                temperature=0.1,
                max_tokens=5000,
                response_format={"type": "json_object"},
                messages=messages,
            )
            return _json_from_response(response.choices[0].message.content or "")
        except Exception as exc:
            # Groq weist ungültig generiertes JSON bereits als HTTP 400 zurück.
            # Andere API-/Netzwerk-/Parserfehler werden nicht erneut ausgeführt.
            error_text = str(exc).lower()
            error_body = getattr(exc, "body", None)
            if error_body is not None:
                error_text += " " + repr(error_body).lower()
            is_json_validation_error = (
                getattr(exc, "status_code", None) == 400
                and "json_validate_failed" in error_text
            )
            if attempt == 0 and is_json_validation_error:
                messages = messages + [{
                    "role": "user",
                    "content": (
                        "Dein vorheriger Generierungsversuch wurde wegen ungültigem JSON " 
                        "zurückgewiesen. Wiederhole die Aufgabe. Gib ausschließlich ein "
                        "syntaktisch gültiges JSON-Objekt zurück, ohne Markdown-Codezaun " 
                        "oder zusätzlichen Text. Achte auf korrekt geschlossene und "
                        "escaped Anführungszeichen sowie gültige JSON-Zeichenfolgen."
                    ),
                }]
                continue
            raise

    # Der Loop kann regulär nur mit Rückgabe oder Exception enden.
    raise RuntimeError("Groq-Generierung unerwartet ohne Ergebnis beendet.")


def extrahiere_kandidaten(issue: dict[str, Any]) -> list[dict[str, Any]]:
    client = _groq_client()

    system_1 = """
Du arbeitest als forensischer Aktien-Rechercheassistent für einen technischen
Einzel-Check. Deine Aufgabe ist NICHT, eine Kaufentscheidung zu treffen.
Ermittle aus einem öffentlich sichtbaren HEBELTRADER-Teaser ein möglichst
vollständiges Kandidatenuniversum.

Regeln:
- Recall ist wichtiger als Precision: lieber plausible Kandidaten zu viel als
  eine plausible Aktie zu wenig.
- Liefere 10 bis 25 Kandidaten, sofern die Faktenlage das zulässt.
- Berücksichtige: wahrscheinlichster Basiswert, direkte Peers/Konkurrenten,
  relevante Zulieferer, Profiteure derselben Nachfrage, angrenzende
  Halbleiter-/Technologie-Werte und Unternehmen, die sehr gut zu den
  beschriebenen Kennzahlen passen.
- Keine ETFs, Indizes, Optionsscheine oder Derivate.
- Ticker müssen als Yahoo-Finance-Symbol angegeben werden (z.B. MU, NVDA,
  AMD, ASML.AS). Nicht raten, wenn ein Ticker völlig unbekannt ist.
- Trenne "primary" (wahrscheinlicher Basiswert) von "peer", "supplier",
  "beneficiary" und "thematic".
- "confidence" ist nur die Stärke der Zuordnung zur Story, NICHT die
  technische Kaufwahrscheinlichkeit.
- Nutze ausschließlich die übergebenen Fakten und allgemeines
  Unternehmens-/Tickerwissen; erfinde keine konkreten Quartalszahlen.
- Antworte ausschließlich als JSON-Objekt mit Schlüssel "candidates".
"""

    user_1 = f"""
HEBELTRADER: {issue['issue_label']}
Erscheinungsdatum: {issue.get('issue_date') or 'unbekannt'}
Überschrift: {issue.get('headline') or issue.get('page_title')}

Öffentlicher Teaser:
{issue['teaser']}

Erzeuge das breite Kandidatenuniversum.
JSON:
{{
  "candidates": [
    {{
      "name": "Unternehmen",
      "ticker": "YF-TICKER",
      "role": "primary|peer|supplier|beneficiary|thematic",
      "confidence": 0.0,
      "evidence": "kurze sachliche Begründung aus Teaser/Fakten"
    }}
  ]
}}
"""
    first = _llm_call(client, system_1, user_1)
    first_candidates = first.get("candidates", []) if isinstance(first, dict) else []

    # Zweite, bewusst unabhängige Audit-Stufe: Sie darf Kandidaten aussortieren,
    # aber nur bei fehlender Plausibilität. Sie darf keine neuen Ticker erfinden.
    audit_input = json.dumps(first_candidates[:MAX_LLM_KANDIDATEN], ensure_ascii=False)
    system_2 = """
Du bist die zweite, konservative Forensik-Stufe. Prüfe ein bereits erzeugtes
Kandidatenuniversum gegen einen HEBELTRADER-Teaser.

Entferne nur Kandidaten, die klar nicht zum beschriebenen Geschäftsmodell,
Sektor oder zur Story passen, oder offensichtlich kein handelbarer
Aktienticker sind. Bei Unsicherheit BEHALTE den Kandidaten und senke die
confidence. Die Aufgabe ist recall-lastig. Erfinde KEINE neuen Kandidaten.

Antworte ausschließlich als JSON:
{"candidates":[{"name":"...","ticker":"...","role":"...","confidence":0.0,"audit":"..."}]}
"""

    user_2 = f"""
TEASER:
{issue['teaser']}

ZU AUDITIERENDE KANDIDATEN:
{audit_input}
"""
    audited = _llm_call(client, system_2, user_2)
    candidates = audited.get("candidates", []) if isinstance(audited, dict) else []

    normalized = []
    seen = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        name = _clean(str(item.get("name") or ticker))
        if not ticker or ticker in seen or ticker in AUSSCHLUSS_TICKER:
            continue
        if not re.fullmatch(r"[A-Z0-9^._-]{1,16}", ticker):
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except Exception:
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        normalized.append({
            "name": name,
            "ticker": ticker,
            "role": str(item.get("role") or "thematic"),
            "confidence": confidence,
            "evidence": _clean(str(item.get("evidence") or item.get("audit") or "")),
            "audit": _clean(str(item.get("audit") or "")),
        })
        seen.add(ticker)

    # Höhere Story-Relevanz zuerst, aber keine harte Schwelle außer der
    # Validierung. So bleibt das Universum bewusst breit.
    role_order = {"primary": 0, "peer": 1, "supplier": 2, "beneficiary": 3, "thematic": 4}
    normalized.sort(key=lambda x: (role_order.get(x["role"], 9), -x["confidence"], x["ticker"]))
    return normalized[:MAX_LLM_KANDIDATEN]


def normalisiere_yahoo_alias(item: dict[str, Any]) -> dict[str, Any]:
    """Korrigiert ausschließlich drei bekannte Tipp-/Aliasfälle bei passendem Firmennamen.

    Die Prüfung des Namens verhindert, dass ein gleichlautender Ticker eines
    anderen Unternehmens pauschal auf einen fremden Basiswert umgebogen wird.
    Der ursprünglich vom LLM gelieferte Ticker bleibt nachvollziehbar erhalten.
    """
    ticker = str(item.get("ticker") or "").strip().upper()
    name = _clean(str(item.get("name") or "")).casefold()
    aliases = {
        "02208.HK": ("2208.HK", (r"goldwind", r"金風", r"金风")),
        "SZL.NS": ("SUZLON.NS", (r"suzlon",)),
        # Wortgrenze verhindert insbesondere eine Fehlzuordnung von AbbVie.
        "ABB": ("ABBN.SW", (r"\babb(?:\s+ltd\.?)?\b",)),
    }
    alias = aliases.get(ticker)
    if not alias:
        return item
    replacement, expected_name_patterns = alias
    if not any(re.search(pattern, name, flags=re.I) for pattern in expected_name_patterns):
        item["yahoo_alias_review"] = (
            f"Ticker-Alias {ticker} nicht automatisch angewendet: "
            "Unternehmensname passt nicht eindeutig."
        )
        return item
    item.setdefault("ticker_original", ticker)
    item["ticker"] = replacement
    item["ticker_alias_applied"] = f"{ticker} -> {replacement}"
    return item


def yahoo_validiere(kandidaten: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = []
    for item in kandidaten:
        normalisiere_yahoo_alias(item)
    for item in kandidaten:
        ticker = item["ticker"]
        try:
            # Fuenf Handelssitzungen sind keine fuenf Kalendertage.
            # 1mo stellt ausreichend Provider-Abdeckung bereit, damit das
            # kalenderbasierte Quality-Gate seine 5 abgeschlossenen Sessions
            # einschließlich erwarteter Feiertags-/Wochenendstruktur pruefen kann.
            hist = yf.Ticker(ticker).history(period="1mo", auto_adjust=False)
            if hist is None or hist.empty:
                item["yahoo_valid"] = False
                item["yahoo_reason"] = "keine Yahoo-Kursdaten"
                continue
            from ohlcv_quality_gate import quality_checked_frame
            checked = quality_checked_frame(
                hist, ticker, window_sessions=5, require_structure=True,
                full_history=True,
            )
            if checked.empty or checked["Close"].isna().iloc[-1]:
                quality = checked.attrs.get("ohlcv_quality", {})
                reason_codes = quality.get("reason_codes", [])
                reason_suffix = (
                    f" ({', '.join(str(code) for code in reason_codes)})"
                    if reason_codes else ""
                )
                item["yahoo_valid"] = False
                item["yahoo_reason"] = (
                    f"OHLCV-Quality-Gate INCOMPLETE{reason_suffix}"
                )
                continue
            close = checked["Close"]
            item["yahoo_valid"] = True
            item["yahoo_last_close"] = float(close.iloc[-1])
            item["yahoo_last_date"] = str(close.index[-1].date())
            valid.append(item)
        except Exception as exc:
            item["yahoo_valid"] = False
            item["yahoo_reason"] = f"{type(exc).__name__}: {exc}"

    # Primär-/Peer-Kandidaten nicht durch reine Confidence-Sortierung verdrängen.
    role_order = {"primary": 0, "peer": 1, "supplier": 2, "beneficiary": 3, "thematic": 4}
    valid.sort(key=lambda x: (role_order.get(x["role"], 9), -x["confidence"], x["ticker"]))
    return valid[:MAX_VALIDIERTE_KANDIDATEN]


def yahoo_ablehnungen(kandidaten: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Liefert alle tatsächlich von Yahoo/Quality-Gate abgelehnten Kandidaten."""
    return [
        dict(item) for item in kandidaten
        if item.get("yahoo_valid") is False
    ]


def _parse_einzel_ergebnisse(stdout: str, tickers: list[str]) -> list[dict[str, Any]]:
    """Liest Ergebnisse ausschließlich aus dem jeweiligen Ticker-Abschnitt.

    Die Ausgabe von einzel_check.py enthält viele Titel aus der Beobachtungsliste.
    Ein Ticker-Abschnitt beginnt mit einer eigenen Kopfzeile
    ``TICKER - Firmenname (Sektor: ...)`` und endet unmittelbar vor der nächsten
    solchen Kopfzeile. Fehlt innerhalb dieses Abschnitts eine Ergebniszeile,
    darf niemals das Ergebnis des nachfolgenden Titels übernommen werden.
    """
    result = []
    lines = stdout.splitlines(keepends=True)
    text = "".join(lines)

    # Ausschließlich echte Titel-Kopfzeilen erfassen. Die Regex ist absichtlich
    # enger als eine Suche nach einem Tickerwort: Listen, DEBUG-Zeilen und
    # Firmenname-Hinweise sind keine Abschnittsgrenzen.
    header_pattern = re.compile(
        r"^\s*(?P<ticker>[A-Z0-9^][A-Z0-9.^_-]*)\s+-\s+.+?\s+\(Sektor:",
        re.IGNORECASE | re.MULTILINE,
    )
    headers = list(header_pattern.finditer(text))
    blocks_by_ticker: dict[str, list[str]] = {}
    for index, header in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[header.start():block_end]
        key = header.group("ticker").upper()
        blocks_by_ticker.setdefault(key, []).append(block)

    result_pattern = re.compile(
        r"^\s*Ergebnis:\s*(KAUFKANDIDAT [ABC]|KEIN KANDIDAT)"
        r"\s*\(Momentum\s*([^)]+)\)",
        re.IGNORECASE | re.MULTILINE,
    )

    def technical_lines(block: str) -> list[str]:
        found = []
        for line in block.splitlines():
            clean = _clean(line)
            if not clean:
                continue
            if re.search(
                r"(?:TRENDFOLGE:|Setup:|Kurs |Stop |TP1 |TP2 |RSI |MACD |"
                r"TRENDWENDE .*TREFFER|KAUFKANDIDATEN-BEWERTUNG|Ergebnis:|✓|⚠|"
                r"Daten von Alpaca leer|Kursdaten nicht verfügbar)",
                clean,
                re.IGNORECASE,
            ):
                found.append(clean)
        return found[:30]

    for ticker in tickers:
        ticker_key = str(ticker).strip().upper()
        ticker_blocks = blocks_by_ticker.get(ticker_key, [])
        if not ticker_blocks:
            result.append({
                "ticker": ticker,
                "status": "NICHT AUSGELESEN",
                "momentum": None,
                "gruende": [],
                "risiken": [],
                "technischer_zustand": [],
                "auslesegrund": "Kein eigener Ticker-Abschnitt im Einzel-Check-Output gefunden.",
            })
            continue

        if len(ticker_blocks) != 1:
            result.append({
                "ticker": ticker,
                "status": "NICHT AUSGELESEN",
                "momentum": None,
                "gruende": [],
                "risiken": [],
                "technischer_zustand": [],
                "auslesegrund": (
                    f"Mehrdeutig: {len(ticker_blocks)} eigene Ticker-Abschnitte gefunden."
                ),
            })
            continue

        block = ticker_blocks[0]
        matches = list(result_pattern.finditer(block))
        if len(matches) != 1:
            reason = (
                "Kein Ergebnis im eigenen Ticker-Abschnitt gefunden."
                if not matches
                else f"Mehrdeutig: {len(matches)} Ergebniszeilen im eigenen Ticker-Abschnitt gefunden."
            )
            result.append({
                "ticker": ticker,
                "status": "NICHT AUSGELESEN",
                "momentum": None,
                "gruende": [],
                "risiken": [],
                "technischer_zustand": technical_lines(block),
                "auslesegrund": reason,
            })
            continue

        match = matches[0]
        tail = block[match.end():]
        reasons = [_clean(x) for x in re.findall(r"^\s*✓\s*(.+)$", tail, re.MULTILINE)]
        risks = [_clean(x) for x in re.findall(r"^\s*⚠\s*(.+)$", tail, re.MULTILINE)]
        result.append({
            "ticker": ticker,
            "status": match.group(1).upper(),
            "momentum": _clean(match.group(2)),
            "gruende": reasons[:6],
            "risiken": risks[:6],
            "technischer_zustand": technical_lines(block),
        })
    return result


def wiederaufbereite_gespeicherten_state(state: dict[str, Any]) -> dict[str, Any]:
    """Reparst den gespeicherten Roh-Output ohne externe Abfragen.

    Ausschließlich die parser-eigenen Felder in ``candidates[*].einzel_check``
    werden aktualisiert. Kandidaten-, Issue- und sonstige Metadaten bleiben
    erhalten. Die Funktion ist deterministisch und damit idempotent.
    """
    if not isinstance(state, dict):
        raise ValueError("Gespeicherter HEBELTRADER-State ist kein JSON-Objekt.")

    stdout = state.get("einzel_check_stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        raise ValueError("Gespeicherter State enthält keinen verwendbaren einzel_check_stdout.")

    candidates = state.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gespeicherter State enthält keine Kandidatenliste.")

    tickers: list[str] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"Kandidat an Position {index} ist kein JSON-Objekt.")
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError(f"Kandidat an Position {index} hat keinen Ticker.")
        if ticker in seen:
            raise ValueError(f"Ticker {ticker} ist in der Kandidatenliste mehrfach vorhanden.")
        seen.add(ticker)
        tickers.append(ticker)

    parsed_rows = _parse_einzel_ergebnisse(stdout, tickers)
    parsed_by_ticker = {str(row.get("ticker") or "").strip().upper(): row for row in parsed_rows}
    if set(parsed_by_ticker) != set(tickers) or len(parsed_rows) != len(tickers):
        raise RuntimeError("Parser lieferte keine eindeutige 1:1-Zuordnung zur gespeicherten Kandidatenliste.")

    # Diese Felder werden vollständig vom Parser verantwortet. Alte, inzwischen
    # überholte Werte (insbesondere Status/Momentum/Auslesegrund) dürfen nicht
    # aus dem fehlerhaften gespeicherten JSON stehen bleiben.
    parser_fields = {
        "ticker", "status", "momentum", "gruende", "risiken",
        "technischer_zustand", "auslesegrund",
    }
    repaired = dict(state)
    repaired_candidates = []
    for candidate in candidates:
        ticker = str(candidate["ticker"]).strip().upper()
        previous_check = candidate.get("einzel_check")
        check = dict(previous_check) if isinstance(previous_check, dict) else {}
        for field in parser_fields:
            check.pop(field, None)
        check.update(parsed_by_ticker[ticker])
        check["ticker"] = ticker
        if candidate.get("name"):
            check["name"] = candidate["name"]

        updated_candidate = dict(candidate)
        updated_candidate["einzel_check"] = check
        repaired_candidates.append(updated_candidate)

    repaired["candidates"] = repaired_candidates

    # Trade Story traversiert den gesamten JSON-Baum. Da ``llm_candidates``
    # dieselben Ticker zusätzlich enthalten kann, würde ein dort verbliebener
    # Altstatus den gerade korrigierten Status wegen gleicher Quellenpriorität
    # wieder überschreiben. Deshalb werden ausschließlich die vom Parser
    # verantworteten Felder für passende Ticker auch dort synchronisiert.
    llm_candidates = repaired.get("llm_candidates")
    if isinstance(llm_candidates, list):
        repaired_checks = {
            str(candidate.get("ticker") or "").strip().upper(): candidate["einzel_check"]
            for candidate in repaired_candidates
            if isinstance(candidate, dict)
            and isinstance(candidate.get("einzel_check"), dict)
        }
        synchronized_llm_candidates = []
        for index, candidate in enumerate(llm_candidates):
            if not isinstance(candidate, dict):
                synchronized_llm_candidates.append(candidate)
                continue
            ticker = str(candidate.get("ticker") or "").strip().upper()
            parsed_check = repaired_checks.get(ticker)
            if parsed_check is None:
                synchronized_llm_candidates.append(candidate)
                continue
            updated_candidate = dict(candidate)
            previous_check = candidate.get("einzel_check")
            check = dict(previous_check) if isinstance(previous_check, dict) else {}
            for field in parser_fields:
                check.pop(field, None)
                if field in parsed_check:
                    check[field] = parsed_check[field]
            updated_candidate["einzel_check"] = check
            synchronized_llm_candidates.append(updated_candidate)
        repaired["llm_candidates"] = synchronized_llm_candidates

    return repaired


def reparsiere_gespeicherten_state(state_path: str | None = None) -> dict[str, Any]:
    """Sichere Einmal-Wiederaufbereitung mit Backup und atomarem Austausch."""
    target_path = state_path or STATE_FILE
    state = lade_state(target_path)
    repaired = wiederaufbereite_gespeicherten_state(state)
    backup_path = target_path + ".pre-reparse.bak"
    if os.path.exists(backup_path):
        raise FileExistsError(
            f"Backup existiert bereits ({backup_path}); vorhandene Sicherung wird nicht überschrieben."
        )
    shutil.copy2(target_path, backup_path)
    # Der atomare Austausch ersetzt den Zielpfad erst, wenn die vollständige
    # neue JSON-Datei geschrieben wurde. Das Original bleibt bei Fehlern intakt.
    speichere_atomar(repaired, target_path)
    return repaired


def fuehre_einzel_check_aus(tickers: list[str], kandidaten: list[dict[str, Any]] | None = None) -> tuple[int, str, list[dict[str, Any]]]:
    if not tickers:
        return 0, "", []
    # einzel_check.py erwartet mehrere Ticker als getrennte Kommandozeilen-
    # argumente, also genau mit Leerzeichen zwischen den Tickern: 
    #   python einzel_check.py MU AMD AVGO
    # Die Liste wird NICHT in eine einzelne Zeichenkette gepackt, weil der
    # bestehende Parser Leerzeichen als Argumentgrenzen von sys.argv erwartet.
    command = [sys.executable, "einzel_check.py", *tickers]
    name_map = {str(x.get("ticker", "")).upper(): str(x.get("name", "")).strip()
                for x in (kandidaten or [])}
    print("Starte Einzel-Check:")
    print("  " + " | ".join(
        f"{name_map.get(t, '') or t} ({t})" for t in tickers
    ))
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=45 * 60,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if stderr:
        stdout += "\n\n[STDERR]\n" + stderr
    return completed.returncode, stdout, _parse_einzel_ergebnisse(stdout, tickers)


def speichere_atomar(daten: dict[str, Any], state_path: str | None = None) -> None:
    target_path = state_path or STATE_FILE
    temp = target_path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(temp, target_path)


def markiere_hebeltrader_quelle(ticker_liste: list[str], issue_label: str) -> None:
    """Persistiert die HEBELTRADER-Herkunft separat vom Kandidatenstatus.

    Die Status-/Bereinigungslogik von einzel_check.py bleibt autoritativ.
    Diese Funktion ergänzt ausschließlich das Herkunftsfeld in der bereits
    gepflegten Beobachtungsliste. Manuelle Ticker erhalten dadurch keinerlei
    HEBELTRADER-Markierung.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "einzel_check_beobachtung.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            liste = json.load(f)
        if not isinstance(liste, dict):
            return
        changed = False
        for ticker in ticker_liste:
            key = str(ticker).strip().upper()
            if key in liste and isinstance(liste[key], dict):
                if liste[key].get("quelle") != issue_label:
                    liste[key]["quelle"] = issue_label
                    changed = True
        if changed:
            temp = path + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(liste, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(temp, path)
    except Exception as exc:
        raise RuntimeError(f"HEBELTRADER-Quelle konnte nicht in der Beobachtungsliste gespeichert werden: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    argument_parser = argparse.ArgumentParser(description="HEBELTRADER-Kandidaten und Einzel-Check")
    argument_parser.add_argument(
        "--reparse-stored",
        action="store_true",
        help="Gespeicherten Einzel-Check-Roh-Output erneut parsen; keine LLM-/Yahoo-Abfragen.",
    )
    args = argument_parser.parse_args(argv)

    if args.reparse_stored:
        repaired = reparsiere_gespeicherten_state()
        statuses = {}
        for candidate in repaired["candidates"]:
            status = candidate.get("einzel_check", {}).get("status", "NICHT AUSGELESEN")
            statuses[status] = statuses.get(status, 0) + 1
        print(
            f"Gespeicherter HEBELTRADER-State reparst: {repaired.get('issue_label', 'unbekannte Ausgabe')} | "
            f"{len(repaired['candidates'])} Kandidaten | Statusverteilung: {statuses}"
        )
        print(f"Backup: {STATE_FILE}.pre-reparse.bak")
        return 0

    issue = lade_neueste_ausgabe()
    state = lade_state()
    previous_nr = int(state.get("issue_number", 0) or 0)

    print(
        f"Neueste HEBELTRADER-Ausgabe: {issue['issue_label']} "
        f"({issue.get('issue_date') or 'ohne Datum'})"
    )
    if issue["issue_number"] <= previous_nr:
        print(
            f"Bereits verarbeitet (persistenter Stand {previous_nr}/26). "
            "Kein erneuter Einzel-Check."
        )
        return 0

    kandidaten = extrahiere_kandidaten(issue)
    print(f"LLM-Kandidaten: {len(kandidaten)}")
    valid = yahoo_validiere(kandidaten)
    abgelehnt = yahoo_ablehnungen(kandidaten)
    print(f"Yahoo-validierte Kandidaten: {len(valid)}")
    print(f"Yahoo-abgelehnte Kandidaten: {len(abgelehnt)}")
    if abgelehnt:
        print("Yahoo-Ablehnungsdetails:")
        for item in abgelehnt:
            print(
                f"  - {item.get('name', '?')} | "
                f"{item.get('ticker_original', item.get('ticker', '?'))}"
                f" -> {item.get('ticker', '?')}: "
                f"{item.get('yahoo_reason', 'unbekannter Ablehnungsgrund')}"
            )
    alias_hinweise = [x for x in kandidaten if x.get("ticker_alias_applied") or x.get("yahoo_alias_review")]
    for item in alias_hinweise:
        print(
            "Ticker-Alias: "
            f"{item.get('ticker_alias_applied') or item.get('yahoo_alias_review')}"
        )
    if not valid:
        rejection_details = [
            f"{item.get('ticker', '?')}: {item.get('yahoo_reason', 'unbekannter Ablehnungsgrund')}"
            for item in kandidaten
        ]
        details = "; ".join(rejection_details) or "keine Kandidaten geliefert"
        print("Yahoo-Validierung ohne Treffer: " + details)
        raise RuntimeError(
            "Keine Yahoo-validierten Kandidaten. "
            f"Pruefgruende: {details}. "
            "Der persistente Stand bleibt unverändert."
        )

    tickers = [x["ticker"] for x in valid]
    rc, stdout, checks = fuehre_einzel_check_aus(tickers, valid)

    # Ein nicht-null Returncode ist ein harter Fehler. Die Einzelprüfung darf
    # aber einzelne Datenfehler intern abfangen; genau diese Rohmeldung bleibt
    # dann in der Ergebnisdatei sichtbar.
    if rc != 0:
        raise RuntimeError(
            f"einzel_check.py endete mit Returncode {rc}; "
            "persistenter HEBELTRADER-Stand bleibt unverändert."
        )

    check_map = {x["ticker"]: x for x in checks}
    for candidate in valid:
        check = dict(check_map.get(
            candidate["ticker"],
            {"ticker": candidate["ticker"], "status": "NICHT AUSGELESEN"},
        ))
        # Name + Ticker bleiben auch im strukturierten Ergebnis immer gemeinsam
        # sichtbar; die bestehende A/B/C-Logik des Einzel-Checks wird dadurch
        # nicht verändert.
        check["name"] = candidate["name"]
        check["ticker"] = candidate["ticker"]
        candidate["einzel_check"] = check

    result = {
        "schema_version": 3,
        "issue_number": issue["issue_number"],
        "issue_label": issue["issue_label"],
        "issue_date": issue.get("issue_date"),
        "issue_url": issue["issue_url"],
        "headline": issue.get("headline") or issue.get("page_title"),
        "teaser": issue["teaser"],
        "processed_at": berlin_now().isoformat(),
        "candidate_count_llm": len(kandidaten),
        "candidate_count_yahoo_valid": len(valid),
        "candidate_count_yahoo_rejected": len(abgelehnt),
        # Rückwärtskompatibel: candidates enthält weiterhin ausschließlich
        # Yahoo-validierte Werte, wie es die bestehenden Verbraucher erwarten.
        "candidates": valid,
        # Neue Diagnosefelder: künftig lassen sich alle LLM-Kandidaten und
        # sämtliche Yahoo-/Quality-Gate-Ablehnungen nachträglich rekonstruieren.
        "llm_candidates": kandidaten,
        "rejected_candidates": abgelehnt,
        # Vollständiger Einzel-Check-Output bleibt für spätere forensische
        # Rückprüfung erhalten; die Auswertung verwendet die strukturierten
        # Kernfelder oben.
        "einzel_check_stdout": stdout,
    }
    markiere_hebeltrader_quelle(tickers, issue["issue_label"])
    speichere_atomar(result)
    print(
        f"Gespeichert: {STATE_FILE} | {issue['issue_label']} | "
        f"{len(valid)} Kandidaten"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FEHLER: {type(exc).__name__}: {exc}")
        raise
