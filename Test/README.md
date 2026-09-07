# Testumgebung Optionsschein-Kurse

Diese Testumgebung arbeitet bewusst mit einer **physischen XLSX-Kopie** und verändert keine Produktivdatei.

## Dateien

- `Offene_Positionen_TEST.xlsx` – Testkopie der aktuellen `Offene_Positionen.xlsx`
- `optionsschein_kurse.py` – dieselbe Kurslogik wie die geplante Produktivdatei
- `test_optionsschein_kurse.py` – deterministischer Offline-Selbsttest

## Testablauf

```text
Offene_Positionen_TEST.xlsx
        |
        v
optionsschein_kurse.py
        |
        v
Offene_Positionen_TEST.xlsx
```

Der Selbsttest verwendet kontrollierte Testkurse und **keinen Internetzugriff**. Dadurch sind Zahlentypen, Formate, Performanceberechnung und das XLSX-Schema reproduzierbar prüfbar.

Der echte Börse-Stuttgart-Abruf ist ein separater Integrationstest. Er darf nicht Voraussetzung für den Offline-Selbsttest sein.
