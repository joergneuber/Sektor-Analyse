# Quellenwert-Datenvertrag – Sektor-Analyse

**Version:** 1.0 – 12.10.2026  
**Geltungsbereich:** numerische Fakten in `Briefing`, `Makro_Briefing`, `Edelmetalle_Briefing` und der daraus erzeugten Gemini-Tagesauswertung.

## 1. Grundsatz

Eine Zahl ist nur dann vergleichbar, wenn **Kennzahl, Messart, Einheit und Zeitbasis** übereinstimmen. Quelle und Datenstand müssen rückverfolgbar sein. Ein Zahlenwert allein ist kein vollständiger Fakt.

Python-Scanner und Quelldateien liefern die Fakten. Gemini darf sie erklären, aber keine abweichenden Werte als bestätigt darstellen. Bei einem Konflikt zwischen zwei Python-Quellen wird nicht automatisch eine Quelle zum Sieger erklärt. Der Konflikt muss als `NICHT FINAL` ausgewiesen werden, bis Berechnung und Zeitfenster übereinstimmen.

## 2. Pflichtfelder pro Quellenwert

| Feld | Bedeutung | Beispiel |
|---|---|---|
| `key` | stabile Kennungs-ID | `market.vix.price` |
| `source` | Datei/API/Serie | `Makro_Briefing` / `^VIX` |
| `observed_at` | Datenstand bzw. Abrufzeit | `2026-10-12`, optional mit Uhrzeit/Zeitzone |
| `unit` | Einheit | `index_points`, `USD/hour`, `%` |
| `measurement` | konkrete Messart | `price`, `index_level`, `yoy_rate`, `return_4w`, `distance_52w_low` |
| `temporal_basis` | Zeitbasis | `intraday`, `last_completed_close`, `monthly_observation` |
| `calculation` | Formel oder `direct` | `close[-1] / close[-21] - 1` |
| `status` | Datenstatus | `REAL`, `REAL_HEUTE`, `UNAVAILABLE`, `PROXY` |

Fehlt ein Pflichtfeld, darf der Wert nicht stillschweigend als vollständig bestätigt gelten.

## 3. Kurse: Live/Intraday vs. Schlusskurs

- `REAL_HEUTE` mit Hinweis „laufender/aktueller Tageswert“ ist **kein offizieller Schlusskurs**.
- `last_completed_close` bezeichnet den letzten vollständig abgeschlossenen Handelstag.
- Die Werte dürfen nebeneinander in der Auswertung stehen, müssen aber mit Zeitbasis und Datenstand bezeichnet werden.
- Sie dürfen nicht als identische Sollwerte gegeneinander geprüft werden.
- Ein aktueller Kursvergleich ist nur numerisch belastbar, wenn Quelle und Ausgabe auf denselben Zeitpunkt bzw. dieselbe Snapshot-Basis bezogen sind. Bei fehlender Zeitbasis: `NICHT VERIFIZIERT`, nicht automatisch `PASS`.

### Beispiele aus den Fakten vom 12.10.2026

| Kennzahl | Laufender Wert | Letzter Schlusskurs | Bewertung |
|---|---:|---:|---|
| Russell 2000 | 2.803,382324 (12.10.) | 2.806,98 (09.10.) | Unterschied durch Zeitbasis plausibel |
| VIX | 15,410000 (12.10.) | 14,84 (09.10.) | Unterschied durch Zeitbasis plausibel |
| Palladium | 1.155,00 (12.10.) | 1.140,50 (09.10.) | Unterschied durch Zeitbasis plausibel |

Die Werte sind nicht „falsch“, nur weil sie voneinander abweichen. Falsch wäre, sie ohne Zeitbasis als dieselbe Kennzahl zum selben Zeitpunkt zu vergleichen.

## 4. Historische Renditen

**4W-Konvention:**

\[
R_{4W}=\left(\frac{C_t}{C_{t-20}}-1\right)\times100
\]

- `C_t` ist der letzte **abgeschlossene** Tages-Schlusskurs.
- `C_{t-20}` ist der Schlusskurs 20 abgeschlossene Handelssitzungen zuvor.
- Das benötigt **21 Schlusskurse** für 20 Renditeintervalle.
- Offene/aktuelle Tageskerzen werden ausgeschlossen.
- Beide Scanner-/Briefingpfade müssen diese gleiche Konvention verwenden.

Eine 4W-Rendite und der prozentuale Abstand zum 52W-Tief sind unterschiedliche Messarten. Die 52W-Distanz darf nie als 4W-Rendite geprüft oder ersetzt werden.

### Offener Quellkonflikt im Lauf vom 12.10.2026

| Metall | Markt-Briefing | Edelmetalle-Briefing | Status |
|---|---:|---:|---|
| Gold | −3,6 % | −4,6 % | `NICHT FINAL` |
| Silber | −4,2 % | −7,1 % | `NICHT FINAL` |
| Platin | −4,2 % | −4,6 % | `NICHT FINAL` |
| Palladium | −10,8 % | −11,6 % | `NICHT FINAL` |

**Verbindliche Ausgaberegel:** Für Gold, Silber, Platin und Palladium hat das `Edelmetalle-Briefing` bei der 4W-Rendite Vorrang, sofern dort ein expliziter, numerisch brauchbarer 4W-Wert vorhanden ist. Das `Markt-Briefing` ist ausschließlich Fallback, wenn der entsprechende Wert im Edelmetalle-Briefing fehlt oder unbrauchbar ist. Abweichungen werden protokolliert und müssen fachlich untersucht werden; sie verhindern aber nicht die Ausgabe des priorisierten Edelmetallwerts. Die Reparatur, spätere Schlusskorrektur und Quellenbindungs-Gates müssen dieselbe Regel verwenden. Wenn beide Quellen keinen brauchbaren Wert liefern, darf kein Zahlenwert erfunden werden und das Gate muss den fehlenden Wert melden.

## 5. Makrodaten: Messwert und Veränderungsrate trennen

CPI/Core CPI und PCE/Core PCE enthalten mindestens zwei getrennte Messarten:

- **Index-Level:** z. B. CPI `334,1310`.
- **YOY-Rate:** z. B. CPI `+3,35 %`.

Die vier Felder sind eigenständig und dürfen nur über exakte Feldnamen zugeordnet werden. `CPI` darf nicht als Teil von `Core CPI` gefunden werden; dasselbe gilt für `PCE` und `Core PCE`.

| Feld | Index-Level | YOY |
|---|---:|---:|
| CPI | 334,1310 | +3,35 % |
| Core CPI | 337,7650 | +2,45 % |
| PCE | 131,5790 | +3,42 % |
| Core PCE | 130,4550 | +3,01 % |

Stundenlohn ist ein **USD/Stunde-Niveauwert**. Eine Lohnwachstumsrate wie `+4,2 %` ist keine alternative Darstellung dieses Niveaus. Beim Prüfen/Reparieren wird nur ein explizit mit `USD`/`$` gekennzeichneter Stundenlohnwert mit der USD/Stunde-Quelle verglichen.

## 6. Rundung und Toleranz

Verglichen wird mit der Präzision, die in der Ausgabe tatsächlich angezeigt wird. Die Toleranz ist höchstens eine halbe Einheit der letzten angezeigten Dezimalstelle, mit kleinem Gleitkomma-Floor.

Beispiele:

- Quelle `3,129000`, Ausgabe `3,13`: akzeptierte Rundung.
- Quelle `6,773500`, Ausgabe `6,77`: akzeptierte Rundung.
- Quelle `1,120574`, Ausgabe `1,1206`: akzeptierte Rundung.
- Quelle `2.806,98`, Ausgabe `2.803,38`: echter Konflikt, keine Rundung.

## 7. Gate-Verhalten

- `PASS` nur, wenn die relevanten Werte und ihre Zuordnungen geprüft wurden und keine offenen Konflikte verbleiben.
- Unterschiedliche Zeitbasen: entweder getrennt und nachvollziehbar darstellen oder als nicht vergleichbar markieren.
- Fehlende Datenbasis, fehlende Zeitbasis und widersprüchliche Quellen: dokumentierter Fehlerstatus; kein `PASS`.
- Quellenkonflikte dürfen nicht durch eine automatische Überschreibung verborgen werden.
- Der abschließende Gate-Test muss den tatsächlich gespeicherten Endstand prüfen.

## 8. Implementierte Korrekturen in dieser Arbeitsversion

1. Gemeinsame Hilfsfunktionen für Dezimalpräzision, Zeitbasisvergleich, exakte Feldnamen und 4W-Extraktion.
2. CPI/Core CPI und PCE/Core PCE werden über exakte Zeilenlabels getrennt.
3. Rundungstoleranz orientiert sich an der angezeigten Präzision statt an einer pauschalen `1e-8`-Grenze.
4. Der 4W-Prüfer liest nur den direkt mit `4W` verknüpften Prozentwert; die 52W-Distanz wird nicht mehr als 4W-Rendite gelesen.
5. Abweichende 4W-Werte der zwei Briefings lösen einen Quellenkonflikt aus und werden nicht automatisch überschrieben.
6. Stundenlohnprüfung/-reparatur unterscheidet USD/Stunde von prozentualen Veränderungen.
7. `get_kurzfrist_kontext_text` und `get_markt_kurzfrist_kontext_text` verwenden abgeschlossene Tageskerzen und dieselbe 20-Intervall-Formel.
8. Die falsche Abschnittsgrenze `NÄCHSTE KATALYSOREN` wurde auf `NÄCHSTE KATALYSATOREN` korrigiert.

Diese Arbeitsversion ist **noch nicht produktionsfreigegeben**. Die Tests und der vollständige Workflow müssen nach Einbindung in die GitHub-Umgebung erneut laufen.
