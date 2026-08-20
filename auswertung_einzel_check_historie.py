"""
Auswertung der historischen Einzel-Check-Zeitreihe.

Zweck:
- B-Episoden erkennen
- B -> A, B -> C, B -> KEIN KANDIDAT und offene B-Episoden messen
- Kursbewegung zwischen erstem B und Folgeereignis auswerten
- Noch KEINE neue Trading-Regel ableiten

Eingabe:
    einzel_check_historie.jsonl

Aufruf:
    python auswertung_einzel_check_historie.py
    python auswertung_einzel_check_historie.py --datei /pfad/einzel_check_historie.jsonl
"""

import argparse
import json
import os
from collections import Counter

import pandas as pd


def lade_snapshots(datei):
    rows = []
    if not os.path.exists(datei):
        return rows
    with open(datei, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def episode_statusfolge(gruppe):
    return " -> ".join(gruppe["Status"].tolist())


def baue_b_episoden(df):
    episoden = []
    if df.empty:
        return episoden

    for ticker, g in df.groupby("Ticker", sort=True):
        g = g.sort_values("Datum").reset_index(drop=True)
        i = 0
        while i < len(g):
            if g.at[i, "Status"] != "KAUFKANDIDAT B":
                i += 1
                continue

            start = i
            j = i + 1
            while j < len(g) and g.at[j, "Status"] == "KAUFKANDIDAT B":
                j += 1

            start_row = g.iloc[start]
            next_row = g.iloc[j] if j < len(g) else None
            end_b = g.iloc[j - 1]

            event = next_row["Status"] if next_row is not None else "OFFEN"
            kurs_start = start_row.get("Kurs")
            kurs_event = next_row.get("Kurs") if next_row is not None else None
            pct_to_event = None
            if pd.notna(kurs_start) and pd.notna(kurs_event) and float(kurs_start) != 0:
                pct_to_event = (float(kurs_event) / float(kurs_start) - 1.0) * 100.0

            episode = {
                "Ticker": ticker,
                "B_Start": start_row["Datum"],
                "Letzter_B": end_b["Datum"],
                "B_Tage": j - start,
                "Folgeereignis": event,
                "Folgedatum": next_row["Datum"] if next_row is not None else None,
                "Kurs_B_Start": kurs_start,
                "Kurs_Folgeereignis": kurs_event,
                "Kursbewegung_B_bis_Ereignis_%": pct_to_event,
                "Momentum_B_Start": start_row.get("Momentum"),
                "Stoch_B_Start": start_row.get("Stoch"),
                "Vol_Ratio_B_Start": start_row.get("Vol_Ratio"),
                "EMA50_Distance_B_Start": start_row.get("EMA50_Distance"),
                "Near_High_B_Start": start_row.get("Near_High"),
                "Sektor_RS_B_Start": start_row.get("Sektor_RS"),
                "Sektor": start_row.get("Sektor"),
            }
            episoden.append(episode)
            i = j

    return episoden


def drucke_auswertung(df, episoden):
    print("=" * 78)
    print("EINZEL-CHECK HISTORIEN-AUSWERTUNG")
    print("=" * 78)
    print(f"Snapshots: {len(df)}")
    print(f"Ticker:    {df['Ticker'].nunique() if not df.empty else 0}")
    if not df.empty:
        print(f"Zeitraum:  {df['Datum'].min()} bis {df['Datum'].max()}")
    print()

    if df.empty:
        print("Noch keine Historie vorhanden.")
        print("Die Datei wird ab dem naechsten Einzel-Check automatisch aufgebaut.")
        return

    print("STATUS-VERTEILUNG")
    print("-" * 78)
    for status, count in df["Status"].value_counts().items():
        print(f"{status:20} {count:6}")
    print()

    print("B-EPISODEN")
    print("-" * 78)
    if not episoden:
        print("Noch keine B-Episode vorhanden.")
        return

    counter = Counter(e["Folgeereignis"] for e in episoden)
    for event, count in counter.items():
        print(f"B -> {event:18} {count:6}")

    geschlossen = [e for e in episoden if e["Folgeereignis"] != "OFFEN"]
    b_a = [e for e in geschlossen if e["Folgeereignis"] == "KAUFKANDIDAT A"]
    print()
    print(f"Geschlossene B-Episoden: {len(geschlossen)}")
    print(f"Davon B -> A:            {len(b_a)}")
    if geschlossen:
        print(f"B -> A Quote:            {len(b_a) / len(geschlossen) * 100:.1f}%")

    bewegungen = [e["Kursbewegung_B_bis_Ereignis_%"] for e in b_a if e["Kursbewegung_B_bis_Ereignis_%"] is not None]
    if bewegungen:
        print(f"Median Kurs B -> A:      {pd.Series(bewegungen).median():+.2f}%")
        print(f"Durchschnitt B -> A:     {pd.Series(bewegungen).mean():+.2f}%")
        print(f"Bestes B -> A:           {max(bewegungen):+.2f}%")
        print(f"Schlechtestes B -> A:    {min(bewegungen):+.2f}%")

    print()
    print("EPISODENDETAILS")
    print("-" * 78)
    for e in episoden:
        bewegung = "n/a" if e["Kursbewegung_B_bis_Ereignis_%"] is None else f"{e['Kursbewegung_B_bis_Ereignis_%']:+.2f}%"
        print(
            f"{e['Ticker']:8} {e['B_Start']} -> {e['Folgedatum'] or 'offen':10} "
            f"B-Tage={e['B_Tage']:2} | {e['Folgeereignis']:18} | Kurs={bewegung}"
        )

    print()
    print("HINWEIS")
    print("-" * 78)
    print("Diese Auswertung beschreibt nur historische Beobachtungen.")
    print("Sie veraendert keine A/B/C-Regel und erzeugt keinen automatischen Kauf-Trigger.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--datei",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "einzel_check_historie.jsonl"),
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Optionaler Exportpfad fuer die B-Episoden.",
    )
    args = parser.parse_args()

    rows = lade_snapshots(args.datei)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["Datum", "Ticker"], keep="last")
        df = df.sort_values(["Ticker", "Datum"]).reset_index(drop=True)

    episoden = baue_b_episoden(df)
    drucke_auswertung(df, episoden)

    if args.csv:
        pd.DataFrame(episoden).to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\nB-Episoden-CSV: {args.csv}")


if __name__ == "__main__":
    main()
