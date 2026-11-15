"""Typisierte XLSX-Serialisierung fuer den Positions-Tracker."""
from __future__ import annotations

import datetime as dt
import io
import pandas as pd

DATE_COLUMNS = ("Einstiegsdatum", "Ausstiegsdatum")


def _parse_date(value, column: str, row_number: int):
    """Wandelt gueltige deutsche/ISO-Datumswerte in echte Excel-Datumszellen um."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    parsed = pd.to_datetime(
        text, errors="coerce", dayfirst=True,
        format="mixed"
    )
    if pd.isna(parsed):
        raise ValueError(
            f"Ungueltiges Datum in {column!r}, Datenzeile {row_number}: {value!r}"
        )
    return parsed.to_pydatetime().replace(tzinfo=None)


def erstelle_sheet_upload_stream(
    csv_pfad: str,
    numerische_spalten: list[str],
    datumsspalten: tuple[str, ...] = DATE_COLUMNS,
    erforderliche_spalten: tuple[str, ...] = (),
) -> io.BytesIO:
    """CSV mit deutschem Dezimalkomma in typisiertes XLSX ueberfuehren.

    Datumswerte werden echte datetime-Zellen, Zahlen echte numerische Zellen.
    Nichtleere fehlerhafte Werte und fehlende Pflichtspalten brechen ab.
    """
    df = pd.read_csv(
        csv_pfad, sep=";", decimal=",", encoding="utf-8-sig",
        keep_default_na=False, dtype=str,
    )
    missing = [c for c in (*erforderliche_spalten, *numerische_spalten, *datumsspalten)
               if c not in df.columns and c in erforderliche_spalten]
    if missing:
        raise ValueError("Erforderliche Spalten fehlen: " + ", ".join(missing))

    for spalte in numerische_spalten:
        if spalte not in df.columns:
            continue
        original = df[spalte].astype(str).str.strip()
        leer = original.eq("") | original.str.lower().isin({"nan", "none"})
        normalisiert = original.where(leer, original.str.replace(",", ".", regex=False))
        numerisch = pd.to_numeric(normalisiert.where(~leer, pd.NA), errors="coerce")
        ungueltig = (~leer) & numerisch.isna()
        if ungueltig.any():
            beispiele = original[ungueltig].head(5).tolist()
            raise ValueError(f"Ungueltige Werte in numerischer Spalte {spalte!r}: {beispiele}")
        df[spalte] = numerisch

    for spalte in datumsspalten:
        if spalte not in df.columns:
            continue
        df[spalte] = [
            _parse_date(value, spalte, index + 2)
            for index, value in enumerate(df[spalte].tolist())
        ]

    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl", datetime_format="dd.mm.yyyy") as writer:
        df.to_excel(writer, index=False, sheet_name="Offene_Positionen")
        ws = writer.sheets["Offene_Positionen"]
        for name in datumsspalten:
            if name in df.columns:
                col = df.columns.get_loc(name) + 1
                for row in range(2, len(df) + 2):
                    ws.cell(row=row, column=col).number_format = "dd.mm.yyyy"
    stream.seek(0)
    return stream
