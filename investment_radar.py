#!/usr/bin/env python3
"""Offline Investment Radar: interpret existing project results; no scanners/APIs/Gemini."""
from __future__ import annotations
import argparse, csv, json, re
from pathlib import Path
from datetime import date

def newest(folder: Path, pattern: str):
    files=sorted(folder.glob(pattern), key=lambda p:p.name, reverse=True)
    return files[0] if files else None

def read_csv(path):
    if not path: return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        sample=f.read(4096); f.seek(0)
        try: dialect=csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error: dialect=csv.excel
        return list(csv.DictReader(f, dialect=dialect))

def read_json(path):
    if not path: return {}
    try: return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError,json.JSONDecodeError): return {}

def read_text(path):
    return path.read_text(encoding="utf-8-sig",errors="replace") if path else ""

def company_label(item):
    name=str(item.get("Name") or item.get("name") or item.get("Unternehmen") or item.get("Company") or "").strip()
    ticker=str(item.get("Ticker") or item.get("ticker") or item.get("Symbol") or "").strip()
    if name and ticker: return f"{name} ({ticker})"
    if ticker: return ticker
    return name or "Unternehmen nicht zugeordnet"

def build_report(folder: Path) -> str:
    folder=Path(folder)
    story=newest(folder,"Trade_Story_Universum(*).json")
    setup=newest(folder,"Setups(*).csv")
    metals=newest(folder,"Edelmetalle_Setups(*).csv")
    turn=newest(folder,"Trendwende_Setups(*).csv")
    short=newest(folder,"Short_Setups(*).csv")
    perf=newest(folder,"Performance(*).csv")
    perf_eu=newest(folder,"Performance_EU(*).csv")
    macro=newest(folder,"Makro_Briefing(*).txt")
    structure=newest(folder,"Struktur_Trend_Briefing(*).txt")
    bitcoin=newest(folder,"Trade_Story_Bitcoin(*).json")
    sd=read_json(story); candidates=sd.get("candidates",[]) if isinstance(sd,dict) else []
    # Combine radar universe and setup lists, deduplicated by ticker, retaining source evidence.
    pool=[]
    for c in candidates:
        if isinstance(c,dict): pool.append(dict(c))
    for path in (setup,metals,turn,short):
        for r in read_csv(path):
            if isinstance(r,dict): pool.append(dict(r))
    unique={}
    for c in pool:
        t=str(c.get("Ticker") or c.get("ticker") or c.get("Symbol") or "").strip().upper()
        if not t: continue
        if t not in unique: unique[t]=c
        else:
            for k,v in c.items():
                if (not unique[t].get(k)) and v: unique[t][k]=v
    # Keep every plausible candidate, but suppress explicitly invalid/blocked items.
    excluded={"UNGÜLTIG","INVALID","AUSGESCHLOSSEN","BLOCKIERT","KEIN SETUP"}
    picks=[]
    for t,c in unique.items():
        status=str(c.get("trade_story_status") or c.get("Status") or c.get("Status2") or "").strip().upper()
        if status in excluded: continue
        # Ignore data-only rows without a setup/thesis, unless explicitly marked valid.
        thesis=any(c.get(k) for k in ("Setup_Typ","Setup","These","Trade_Story","trade_story","CRV1","CRV"))
        if thesis or status=="VALIDE SETUP": picks.append(c)
    # Rank for display, not cap: valid setups first, then available CRV, then ticker.
    def rank(c):
        status=str(c.get("trade_story_status") or c.get("Status") or "").upper()
        try: crv=float(str(c.get("CRV1") or c.get("CRV") or "0").replace(",","."))
        except ValueError: crv=0
        return (status=="VALIDE SETUP",crv,company_label(c))
    picks.sort(key=rank,reverse=True)
    macro_text=read_text(macro); struct_text=read_text(structure)
    # Summarize only meaningful directional evidence, avoid copying whole source lists.
    context=[]
    for label,txt in (("Makro",macro_text),("Struktur/Trend",struct_text)):
        lines=[x.strip(" -•\t") for x in txt.splitlines() if x.strip()]
        # Keep concise evidence lines; source text itself is not dumped.
        relevant=[x for x in lines if re.search(r"(STATUS=|Trend|Risiko|bull|bear|Long|Short|Gold|Zins|Inflation|Sektor|Rotation)",x,re.I)]
        if relevant: context.append((label,relevant[:5]))
    lines=[f"INVESTMENT RADAR | {date.today().isoformat()}",
      "Fokus: Interpretation und Tradechancen. Quellen werden nicht als Dateninventar wiederholt.",
      "", "1. BLICKRICHTUNG"]
    if context:
        for label,items in context:
            lines.append(f"- {label}:")
            lines.extend(f"  - {x[:350]}" for x in items)
    else: lines.append("- Makro-/Strukturbriefing fehlt oder enthält keine eindeutig interpretierbaren Richtungszeilen.")
    lines += ["", "2. AUSSICHTSREICHE TRADECHANCEN"]
    if picks:
        for c in picks:
            label=company_label(c)
            setup_name=c.get("Setup_Typ") or c.get("Setup") or c.get("Signal") or "Setup/These aus Quelldaten"
            status=c.get("trade_story_status") or c.get("Status") or c.get("Status2") or "Status nicht eindeutig"
            crv=c.get("CRV1") or c.get("CRV") or "n.v."
            sector=c.get("Sektor") or c.get("Sector") or ""
            why=c.get("These") or c.get("Trade_Story") or c.get("trade_story") or c.get("Begründung") or c.get("Kommentar") or ""
            lines.append(f"- {label}: {setup_name}. Status: {status}. CRV1: {crv}." + (f" Bereich: {sector}." if sector else ""))
            if why: lines.append(f"  Einordnung: {str(why)[:600]}")
            conditions=c.get("Bedingung") or c.get("Trigger") or c.get("Next_Step") or c.get("Hinweis")
            if conditions: lines.append(f"  Weiter beobachten: {str(conditions)[:350]}")
    else: lines.append("- Aus den verfügbaren Dateien lässt sich aktuell keine ausreichend begründete Kandidaten-These ableiten.")
    lines += ["", "3. WAS DIE EINSCHÄTZUNG ÄNDERN WÜRDE"]
    change=[]
    for txt in (macro_text,struct_text):
        for line in txt.splitlines():
            s=line.strip()
            if s and re.search(r"(Risiko|Invalid|Unavailable|UNAVAILABLE|PROXY|Break|über|unter|Bestätigung|Widerstand|Unterstützung)",s,re.I):
                change.append(s)
    lines.extend(f"- {x[:350]}" for x in change[:8])
    if not change: lines.append("- In den Briefings sind keine expliziten Änderungsbedingungen zuverlässig erkennbar; neue bestätigte Setup-/Struktursignale würden die These neu bewerten.")
    lines += ["", "4. THESEN-/VERLAUFSNOTIZ", 
      "- Tagesbericht als datierter Snapshot. Für spätere Vergleiche sind Kandidaten, Blickrichtung und Bedingungen über die gespeicherten Tagesdateien gegenzuhalten.",
      "- Kandidaten sind Radar-Chancen, keine automatische Entry-Freigabe. Quelle und Status können unvollständig sein."]
    btc=read_json(bitcoin)
    if isinstance(btc,dict):
        vals=[]
        for k in ("pi_cycle_bottom","sma50w"):
            v=btc.get(k)
            if isinstance(v,dict) and (v.get("message") or v.get("state")): vals.append(f"{k}: {v.get('message') or v.get('state')}")
        if vals: lines += ["","Bitcoin-Kontext:"]+[f"- {v}" for v in vals]
    return "\n".join(lines)+"\n"

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-dir",required=True)
    ap.add_argument("--output",help="Berichtspfad")
    args=ap.parse_args()
    folder=Path(args.input_dir)
    output=Path(args.output) if args.output else folder/f"Investment_Radar_{date.today().isoformat()}.txt"
    output.write_text(build_report(folder),encoding="utf-8")
    print(f"Investment Radar Bericht geschrieben: {output}")
if __name__=="__main__": main()
