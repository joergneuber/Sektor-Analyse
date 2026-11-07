# OHLCV Quality Gate – versionierte Sonderregeln

Stand: 07.11.2026
Quality-Gate-Regelversion: `1.3`
Geltungsbereich: tägliche OHLCV-Balken (`timeframe=1d`)

## Grundsatz

`SPECIAL_CASE` ist keine pauschale Freigabeklasse. Eine Freigabe ist nur
möglich, wenn die Registry auf eine bekannte, versionierte Regel verweist und
alle Bedingungen dieser Regel erfüllt sind:

- Instrument und Datum liegen im Regelbereich.
- `classification=SPECIAL_CASE`.
- `usage_decision` ist `ALLOW` oder `ALLOW_WITH_RULE`.
- `approved_by` und `evidence_ref` sind nicht leer.
- `rule_version` stimmt exakt mit einer bekannten Regel überein.
- `allowed_reason_codes` entspricht exakt dem für Instrument/Datum
  freigegebenen Reason-Code-Satz.
- `timeframe=1d`.
- Bei negativen WTI-Werten muss zusätzlich jeder negative Feldwert innerhalb
  einer Toleranz von 0,005 USD/bbl dem fest im Code hinterlegten, geprüften
  historischen Beobachtungswert entsprechen.

Ein unbekannter Regelname, ein falsches Instrument, ein falsches Datum, ein
abweichender negativer Wert oder ein nicht erlaubter Reason-Code bleibt
fail-closed.

## Regel 1: CME Settlement außerhalb der Intraday-Range

- Regelversion: `CME_SETTLEMENT_CLOSE_OUTSIDE_RANGE_V1`
- Instrumente: `CL=F`, `GC=F`, `SI=F`, `HG=F`, `PL=F`, `PA=F`
- Zeitfenster: 2000-01-01 bis 2099-12-31
- Erlaubte Reason-Codes:
  - `HIGH_BELOW_OPEN_OR_CLOSE`
  - `LOW_ABOVE_OPEN_OR_CLOSE`
- Alle übrigen strukturellen Fehler bleiben gesperrt.
- Die Regel gibt keine anderen Instrumente frei und erlaubt keine negativen
  Preise.

## Regel 2: Historische negative NYMEX-WTI-Preise

- Regelversion: `WTI_NEGATIVE_SETTLEMENT_2020_V2`
- Instrument: ausschließlich `CL=F`, Yahoo-Frontmonat-Abbildung des NYMEX
  WTI May-2020-Kontrakts `CLK20`.
- Daten: ausschließlich 2020-04-20 und 2020-04-21.
- Die tatsächlich negativen Preise sind durch CFTC/EIA und zeitgenössische
  Marktdaten belegt. Die Regel erlaubt nicht pauschal alle negativen Werte.

| Sitzung | Feld | Geprüfter negativer Wert (USD/bbl) | Zulässiger Reason-Code | Evidenz |
|---|---|---:|---|---|
| 2020-04-20 | Low | -40,32 | `LOW_NON_POSITIVE` | CFTC / EIA |
| 2020-04-20 | Close | -37,63 | `CLOSE_NON_POSITIVE` | CFTC Settlement |
| 2020-04-21 | Open | -14,00 | `OPEN_NON_POSITIVE` | ICIS zeitgenössische Marktübersicht |
| 2020-04-21 | Low | -16,74 | `LOW_NON_POSITIVE` | ICIS; CFTC bestätigt erneute negative Trades über Nacht |

Die Werte müssen je Feld innerhalb von 0,005 USD/bbl mit dem im Code
festgelegten Wert übereinstimmen. Nullpreise bleiben ungültig. Positive OHLC-
Felder werden weiterhin den normalen Strukturprüfungen unterzogen; eine
positive High-Angabe wird durch diese Negativpreisregel nicht gesondert
bestätigt.

### Evidenzabgrenzung

- CFTC-Interimsbericht: bestätigt den 20.04.2020 Intraday-Tiefstpreis
  -40,32 USD/bbl, Settlement -37,63 USD/bbl sowie erneute negative Trades
  während der Overnight-Sitzung 20./21.04. und Settlement 10,01 USD/bbl am
  21.04.2020.
- EIA: bestätigt -40,32 USD/bbl am 20.04. und dass Preise während eines Teils
  des folgenden Handelstages negativ blieben.
- ICIS, 21.04.2020: berichtet für den auslaufenden Mai-WTI-Kontrakt Low
  -16,74 USD/bbl und Open -14,00 USD/bbl; die veröffentlichte Momentaufnahme
  nennt zudem High 2,54 USD/bbl um 05:18 GMT. Diese Momentaufnahme ist keine
  vollständige Tages-HOHLC-Bestätigung.
- Der Yahoo-Wert High 13,86 USD/bbl am 21.04. wird nicht als unabhängig
  bestätigt behauptet. Er wird nicht durch die Negativpreis-Ausnahme
  legitimiert, sondern muss lediglich die gewöhnlichen OHLC-Strukturregeln
  erfüllen.

### Konsequenz für ATR

Die ATR-Berechnung verwendet dieselbe Instrument-/Datums-/Feld-/Wertprüfung.
Ein abweichender negativer OHLC-Wert wird nicht als gültige True Range
verwendet. Ohne Ticker oder gültigen Registry-Nachweis bleibt ATR `INCOMPLETE`.

## Audit-Regel

Änderungen an Instrumenten, Zeiträumen, erwarteten Feldwerten, Toleranzen,
Reason-Codes oder Regelversionen sind Codeänderungen und müssen erneut
forensisch geprüft und getestet werden.
