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

import datetime as dt
import json
import os
import re
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
MAX_LLM_KANDIDATEN = 18
MAX_VALIDIERTE_KANDIDATEN = 15

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


def lade_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        raise RuntimeError(f"{STATE_FILE} ist nicht lesbares JSON: {exc}") from exc


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
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        temperature=0.1,
        max_tokens=5000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return _json_from_response(response.choices[0].message.content or "")


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
- Liefere 10 bis 18 Kandidaten, sofern die Faktenlage das zulässt.
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


def yahoo_validiere(kandidaten: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = []
    for item in kandidaten:
        ticker = item["ticker"]
        try:
            hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
            if hist is None or hist.empty:
                item["yahoo_valid"] = False
                item["yahoo_reason"] = "keine Yahoo-Kursdaten"
                continue
            close = hist["Close"].dropna()
            if close.empty:
                item["yahoo_valid"] = False
                item["yahoo_reason"] = "keine gültigen Schlusskurse"
                continue
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


def _parse_einzel_ergebnisse(stdout: str, tickers: list[str]) -> list[dict[str, Any]]:
    result = []
    # Einzel-Check druckt pro Titel einen klaren Kopf und später die
    # KAUFKANDIDATEN-BEWERTUNG. Wir extrahieren nur diese maschinenlesbaren
    # Kernfelder; die vollständige Rohprüfung bleibt zusätzlich gespeichert.
    for ticker in tickers:
        pattern = re.compile(
            rf"(?ms)^\s*{re.escape(ticker)}(?:\s+-[^\n]*)?\s+\(Sektor:.*?^\s*Ergebnis:\s*(KAUFKANDIDAT [ABC]|KEIN KANDIDAT)"
            rf"\s*\(Momentum\s*([^)]+)\)(.*?)(?=^\s*=+\s*$|\Z)"
        )
        m = pattern.search(stdout)
        if not m:
            result.append({
                "ticker": ticker,
                "status": "NICHT AUSGELESEN",
                "momentum": None,
                "gruende": [],
                "risiken": [],
            })
            continue
        block = m.group(0)
        tail = m.group(3)
        reasons = [_clean(x) for x in re.findall(r"^\s*✓\s*(.+)$", tail, re.M)]
        risks = [_clean(x) for x in re.findall(r"^\s*⚠\s*(.+)$", tail, re.M)]
        technical_lines = []
        for line in block.splitlines():
            clean = _clean(line)
            if not clean:
                continue
            if re.search(r"(?:TRENDFOLGE:|Setup:|Kurs |Stop |TP1 |TP2 |RSI |MACD |TRENDWENDE .*TREFFER|KAUFKANDIDATEN-BEWERTUNG|Ergebnis:|✓|⚠)", clean, re.I):
                technical_lines.append(clean)
        result.append({
            "ticker": ticker,
            "status": m.group(1),
            "momentum": _clean(m.group(2)),
            "gruende": reasons[:6],
            "risiken": risks[:6],
            "technischer_zustand": technical_lines[:30],
        })
    return result


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


def speichere_atomar(daten: dict[str, Any]) -> None:
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(temp, STATE_FILE)


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


def main() -> int:
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
    print(f"Yahoo-validierte Kandidaten: {len(valid)}")
    if not valid:
        raise RuntimeError(
            "Keine Yahoo-validierten Kandidaten. Der persistente Stand bleibt unverändert."
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
        "candidates": valid,
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
