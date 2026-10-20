#!/usr/bin/env python3
"""Offline Investment Radar: summarize existing project result files only.

No scanners, APIs, Gemini calls, or main workflow are invoked.
"""
from __future__ import annotations
import argparse, csv, json
from pathlib import Path
from datetime import date


def newest(folder: Path, pattern: str):
    files = sorted(folder.glob(pattern), key=lambda p: p.name, reverse=True)
    return files[0] if files else None


def read_csv(path):
    if not path: return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        sample=f.read(4096); f.seek(0)
        dialect=csv.Sniffer().sniff(sample, delimiters=';,\t')
        return list(csv.DictReader(f, dialect=dialect))


def read_json(path):
    if not path: return {}
    try: return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError): return {}


def read_text(path):
    return path.read_text(encoding='utf-8-sig', errors='replace') if path else ''


# Transparent keyword rules: these create research hypotheses, not causal claims.
MACRO_RULES = [
    {
        "id": "inflation_cost_pressure",
        "label": "Inflations-/Kostendruck",
        "keywords": ("CPI:", "Core CPI:", "PCE:", "Core PCE:", "PPI:", "Prices="),
        "sectors": ("Rohstoffe", "Energie", "Grundstoffe", "Materials"),
        "note": "Preis-/Inflationsdaten können Kosten- und Preissetzungsmacht-Themen berühren.",
    },
    {
        "id": "manufacturing_cycle",
        "label": "Industrie-/Auftragszyklus",
        "keywords": ("ISM Manufacturing PMI:", "Industrieproduktion:", "New Orders="),
        "sectors": ("Industrie", "Rohstoffe", "Grundstoffe", "Materials", "Industrials"),
        "note": "Konjunktur- und Auftragsindikatoren können zyklische Sektoren betreffen.",
    },
    {
        "id": "rates_real_yields",
        "label": "Zins-/Realzinsumfeld",
        "keywords": ("Fed Funds Effective Rate:", "ECB Deposit Facility Rate:", "Realzins 10Y TIPS:", "US 10Y Treasury:"),
        "sectors": ("Immobilien", "Versorger", "Technologie", "Gold", "Edelmetalle"),
        "note": "Zinsänderungen können Bewertungs- und Finanzierungssensitivität beeinflussen.",
    },
    {
        "id": "labor_consumption",
        "label": "Arbeitsmarkt-/Konsumumfeld",
        "keywords": ("Arbeitslosenquote:", "NFP / Nonfarm Payrolls:", "Consumer Sentiment:"),
        "sectors": ("Konsum", "Basiskonsum", "Zyklischer Konsum", "Consumer Discretionary", "Consumer Staples"),
        "note": "Arbeitsmarkt- und Stimmungsdaten können konsumbezogene Sektoren betreffen.",
    },
]


def macro_hypotheses(macro_text: str, perf_rows: list[dict], perf_eu_rows: list[dict],
                     setup_rows: list[dict], story_candidates: list[dict]) -> list[dict]:
    """Find rule-based macro/sector overlaps using only text already in result files."""
    sectors = {}
    for row in [*perf_rows, *perf_eu_rows]:
        sector = str(row.get("Sektor", "")).strip()
        if sector:
            sectors.setdefault(sector.casefold(), []).append(row)
    setups_by_sector = {}
    for row in setup_rows:
        sector = str(row.get("Sektor", "")).strip()
        if sector:
            setups_by_sector.setdefault(sector.casefold(), []).append(row)
    story_by_sector = {}
    for row in story_candidates:
        sector = str(row.get("Sektor", "")).strip()
        if sector:
            story_by_sector.setdefault(sector.casefold(), []).append(row)

    hypotheses = []
    for rule in MACRO_RULES:
        evidence = [line.strip() for line in macro_text.splitlines()
                   if any(keyword.casefold() in line.casefold() for keyword in rule["keywords"])]
        if not evidence:
            continue
        for sector in rule["sectors"]:
            perf_matches = sectors.get(sector.casefold(), [])
            setup_matches = setups_by_sector.get(sector.casefold(), [])
            story_matches = story_by_sector.get(sector.casefold(), [])
            # Emit only if the supplied files actually contain this sector.
            if not (perf_matches or setup_matches or story_matches):
                continue
            perf_summary = []
            for row in perf_matches[:3]:
                perf_summary.append({"ticker": row.get("Ticker", "?"),
                                    "5T": row.get("5T", ""), "30T": row.get("30T", ""),
                                    "60T": row.get("60T", ""),
                                    "rotation_score": row.get("Rotation-Score", "")})
            setup_tickers = sorted({str(r.get("Ticker", "?")).strip() for r in setup_matches if r.get("Ticker")})
            story_tickers = sorted({str(r.get("ticker", "?")).strip() for r in story_matches if r.get("ticker")})
            hypotheses.append({"rule": rule["id"], "theme": rule["label"], "sector": sector,
                              "macro_evidence": evidence[:4], "performance": perf_summary,
                              "setup_tickers": setup_tickers[:8], "story_tickers": story_tickers[:8],
                              "interpretation": rule["note"]})
    return hypotheses


def build_report(folder: Path) -> str:
    folder=Path(folder)
    story=newest(folder, 'Trade_Story_Universum(*).json')
    setup=newest(folder, 'Setups(*).csv')
    metals=newest(folder, 'Edelmetalle_Setups(*).csv')
    turn=newest(folder, 'Trendwende_Setups(*).csv')
    short=newest(folder, 'Short_Setups(*).csv')
    perf=newest(folder, 'Performance(*).csv')
    perf_eu=newest(folder, 'Performance_EU(*).csv')
    macro=newest(folder, 'Makro_Briefing(*).txt')
    structure=newest(folder, 'Struktur_Trend_Briefing(*).txt')
    bitcoin=newest(folder, 'Trade_Story_Bitcoin(*).json')

    story_data=read_json(story)
    candidates=story_data.get('candidates', []) if isinstance(story_data,dict) else []
    valid=[c for c in candidates if str(c.get('trade_story_status','')).upper() == 'VALIDE SETUP']
    sector_counts={}
    for c in valid:
        sector=str(c.get('Sektor') or 'SEKTOR NICHT ANGEGEBEN')
        sector_counts[sector]=sector_counts.get(sector,0)+1
    sorted_sectors=sorted(sector_counts.items(), key=lambda x:(-x[1],x[0]))

    perf_rows=read_csv(perf); perf_eu_rows=read_csv(perf_eu)
    setup_rows=read_csv(setup)
    hypotheses=macro_hypotheses(macro_text if "macro_text" in locals() else read_text(macro), perf_rows, perf_eu_rows, setup_rows, candidates)
    def perf_lines(rows, label):
        out=[]
        for r in rows:
            ticker=r.get('Ticker','?'); sector=r.get('Sektor','?')
            rot=r.get('Rotation-Score','')
            p30=r.get('30T',''); p60=r.get('60T','')
            out.append(f'- {label} {ticker} ({sector}): Rotation-Score {rot or "nicht verfügbar"}; 30T {p30 or "n.v."}; 60T {p60 or "n.v."}')
        return out

    macro_text=read_text(macro)
    macro_status=[]
    for line in macro_text.splitlines():
        if any(key in line.upper() for key in ['STATUS=UNAVAILABLE','STATUS=PROXY','STATUS=REAL_CACHED']):
            macro_status.append(line.strip())
    macro_status=macro_status[:8]
    btc=read_json(bitcoin)
    btc_lines=[]
    for key in ('pi_cycle_bottom','sma50w'):
        item=btc.get(key,{}) if isinstance(btc,dict) else {}
        if item: btc_lines.append(f'- {key}: {item.get("message") or item.get("state") or "Status vorhanden"}')

    lines=[f'INVESTMENT RADAR | {date.today().isoformat()}',
           'Modus: OFFLINE-Auswertung vorhandener Ergebnisdateien; keine neuen Datenabrufe/Scanner/Gemini.', '',
           '1. SETUP-UNIVERSUM']
    lines.append(f'- Quelle: {story.name if story else "nicht vorhanden"}')
    lines.append(f'- Kandidaten gesamt im Trade-Story-Universum: {len(candidates)}')
    lines.append(f'- Als VALIDE SETUP markiert: {len(valid)}')
    if sorted_sectors:
        lines.append('- Sektorverteilung der validen Setups:')
        lines.extend(f'  - {s}: {n}' for s,n in sorted_sectors[:12])
    else: lines.append('- Keine validen Setups oder keine lesbaren Sektorfelder gefunden.')

    lines.append('- Valide Kandidaten (Ticker | Sektor | Setup | CRV1):')
    for c in valid[:10]:
        lines.append(f'  - {c.get("ticker","?")} | {c.get("Sektor") or "Sektor n.v."} | {c.get("Setup_Typ") or "Setup n.v."} | CRV1 {c.get("CRV1") or "n.v."}')
    lines += ['', '2. PERFORMANCE-KONTEXT (separat, kein automatischer Kausal-Link)']
    lines += perf_lines(perf_rows,'US') or ['- US-Performance-Datei nicht vorhanden/leer.']
    lines += perf_lines(perf_eu_rows,'EU') or ['- EU-Performance-Datei nicht vorhanden/leer.']
    lines += ['', '3. WEITERE VORHANDENE SIGNALPAKETE']
    for label,path,rows in [('Normale Setups',setup,read_csv(setup)),('Trendwende',turn,read_csv(turn)),('Edelmetalle',metals,read_csv(metals)),('Short',short,read_csv(short))]:
        lines.append(f'- {label}: Datei {path.name if path else "nicht vorhanden"}; Zeilen {len(rows)}')
    lines += ['', '4. MAKRO-/STRUKTUR-STATUS']
    lines.append(f'- Makro: {macro.name if macro else "nicht vorhanden"}')
    lines.append(f'- Struktur/Trend: {structure.name if structure else "nicht vorhanden"}')
    lines.append('- Im Makrobriefing explizit markierte Datenstatus (Auszug):')
    lines.extend(f'  - {x}' for x in macro_status) if macro_status else lines.append('  - Keine passenden Statuszeilen im Briefing gefunden.')
    lines += ['', '5. MAKRO-SEKTOR-ÜBERSCHNEIDUNGEN (regelbasierte Beobachtungshypothesen)']
    if hypotheses:
        for h in hypotheses:
            lines.append(f'- Thema: {h["theme"]} | Sektor: {h["sector"]}')
            lines.append(f'  - Makro-Beleg: {" || ".join(h["macro_evidence"])}')
            lines.append(f'  - Einordnung: {h["interpretation"]}')
            if h["performance"]:
                lines.append(f'  - Vorhandene Performance-Zeilen: {json.dumps(h["performance"], ensure_ascii=False)}')
            if h["setup_tickers"]:
                lines.append(f'  - Setups im Sektor: {", ".join(h["setup_tickers"])}')
            if h["story_tickers"]:
                lines.append(f'  - Trade-Story-Kandidaten im Sektor: {", ".join(h["story_tickers"])}')
    else:
        lines.append('- Keine Überschneidung der transparenten Makro-Regeln mit den vorhandenen Sektor-/Setup-Daten gefunden.')
    lines.append('- Diese Überschneidungen sind Rechercheansätze, kein Nachweis eines Wirkungszusammenhangs und kein Handelssignal.')
    lines += ['', '6. BITCOIN-KONTEXT']
    lines.append(f'- Quelle: {bitcoin.name if bitcoin else "nicht vorhanden"}')
    lines.extend(btc_lines or ['- Kein verwertbarer Bitcoin-Status im JSON gefunden.'])
    lines += ['', '7. RADAR-EINORDNUNG',
      '- Die gelieferten Dateien zeigen parallel vorhandene Setups, Sektor-Performance, Makro-/Strukturkontext und Bitcoin-Status.',
      '- Ein belastbarer Zusammenhang zwischen Makrofaktor, Sektor und Einzelticker ist aus diesen aggregierten Dateien allein nicht bewiesen.',
      '- Keine Kaufs-/Verkaufsempfehlung. Fehlende oder nicht zuordenbare Werte werden nicht ergänzt.']
    return '\n'.join(lines)+'\n'


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input-dir', required=True, help='Ordner mit bereits erzeugten Ergebnisdateien')
    ap.add_argument('--output', help='Berichtspfad; Standard: Investment_Radar_Bericht.txt im Eingabeordner')
    args=ap.parse_args()
    folder=Path(args.input_dir)
    output=Path(args.output) if args.output else folder/'Investment_Radar_Bericht.txt'
    report=build_report(folder)
    output.write_text(report,encoding='utf-8')
    print(f'Investment Radar Bericht geschrieben: {output}')

if __name__=='__main__': main()
