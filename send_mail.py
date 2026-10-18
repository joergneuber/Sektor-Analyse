"""
Verschickt die fertige Auswertung(<Datum>).txt per E-Mail (SMTP) - wieder-
verwendet dieselbe Infrastruktur wie beim Mini-Daily-Gold-Projekt (gleiche
Secret-Namen, gleicher smtplib-Aufbau), hier als letzter Schritt in main.yml.

GEGENUEBER DER MINI-DAILY-GOLD-VORLAGE VEREINFACHT: Die Sektor-Auswertung
ist eine reine PLAIN-TEXT-Datei (bewusst kein Markdown/HTML - siehe die
Master-Anweisung fuer die Gemini-Auswertung), deshalb hier KEIN HTML-Teil
und KEINE Chart-Bilder wie beim Gold-Projekt - der Text passt unveraendert
als E-Mail-Body.

Benoetigte Secrets (dieselben NAMEN wie im Mini-Daily-Gold-Repo, aber
GitHub-Secrets sind repo-spezifisch - selbst bei identischem Wert muessen
sie hier im Neuber Macro & Markets-Repo separat eingetragen werden):
- SMTP_HOST (z.B. smtp.gmail.com)
- SMTP_PORT (z.B. 587)
- SMTP_USER (Absender-Adresse)
- SMTP_PASSWORD (App-Passwort, kein normales Kontopasswort)
- MAIL_EMPFAENGER (Ziel-Adresse)
"""

import os
import glob
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo


def finde_neueste_auswertung():
    """Bevorzugt exakt den vom Gemini-Schritt ausgegebenen Reportpfad.
    Ohne expliziten Pfad wird nur für lokale/manuelle Nutzung das alte
    datierte Dateimuster verwendet."""
    explizit = os.environ.get("GEMINI_AUSWERTUNG_DATEI", "").strip()
    if explizit:
        if not os.path.isfile(explizit):
            raise FileNotFoundError(
                f"Der vom Gemini-Schritt angegebene Report fehlt: {explizit}"
            )
        if os.path.getsize(explizit) == 0:
            raise ValueError(f"Der vom Gemini-Schritt angegebene Report ist leer: {explizit}")
        return explizit

    treffer = sorted(glob.glob("Auswertung(*).txt"))
    if not treffer:
        raise FileNotFoundError("Keine Auswertung(*).txt im Arbeitsverzeichnis gefunden.")
    return treffer[-1]


def pruefe_smtp_secrets():
    """NEU (09.08.2026, Anlass: main.yml scheiterte am 07.08. mit einem
    kryptischen `ValueError: invalid literal for int() with base 10: ''`
    mitten in smtplib - die eigentliche Ursache (alle 5 SMTP_*-Secrets
    fehlten schlicht im Neuber Macro & Markets-Repo, GitHub-Secrets sind repo-
    spezifisch und wurden nur im Mini-Daily-Gold-Repo angelegt) war aus
    dem Fehler selbst nicht ersichtlich - `${{ secrets.SMTP_HOST }}` etc.
    loesen sich bei fehlendem Secret zu einem LEEREN String auf, nicht zu
    einem Fehler, deshalb kam der Absturz erst spaeter bei int('').
    Prueft alle benoetigten Variablen VOR dem eigentlichen Mail-Versand
    und nennt fehlende Namen konkret, statt einen einzelnen kryptischen
    Fehler tief im smtplib-Aufruf zu produzieren."""
    benoetigt = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "MAIL_EMPFAENGER"]
    fehlend = [name for name in benoetigt if not os.environ.get(name)]
    if fehlend:
        raise EnvironmentError(
            "Folgende SMTP-Secrets fehlen oder sind leer: " + ", ".join(fehlend) + ". "
            "GitHub-Secrets sind repo-spezifisch - im Neuber Macro & Markets-Repo unter "
            "Settings > Secrets and variables > Actions neu anlegen, auch wenn "
            "dieselben Secrets im Mini-Daily-Gold-Repo bereits existieren."
        )


def main():
    pruefe_smtp_secrets()
    dateipfad = finde_neueste_auswertung()
    # utf-8-sig entfernt einen ggf. vorhandenen BOM am Dateianfang.
    with open(dateipfad, "r", encoding="utf-8-sig") as f:
        text = f.read()

    status_matches = re.findall(
        r"(?m)^FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text
    )
    if not status_matches:
        raise ValueError("Report enthält keinen gültigen FACHLICH_FINAL-Marker.")
    report_status = status_matches[-1]
    gemini_status = os.environ.get("GEMINI_STATUS", "SUCCESS").strip()
    if gemini_status != "SUCCESS":
        raise ValueError(f"Reportversand blockiert: GEMINI_STATUS={gemini_status}")
    expected_status = os.environ.get("GEMINI_FACHLICH_FINAL", "").strip()
    if expected_status and expected_status != report_status:
        raise ValueError(
            f"Statusabweichung: Workflow={expected_status}, Report={report_status}"
        )
    report_check = os.environ.get("GEMINI_REPORT_CHECK_OUTCOME", "success").strip()
    if report_check != "success":
        raise ValueError(f"Reportversand blockiert: Report-Check={report_check}")

    jetzt_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    fachlich_nicht_final = report_status == "NICHT_FINAL"

    msg = EmailMessage()
    status_label = "NICHT_FINAL – fachlich eingeschränkt" if fachlich_nicht_final else "FINAL"
    msg["Subject"] = (
        f"Neuber Macro & Markets - {status_label} - "
        f"{jetzt_berlin.strftime('%d.%m.%Y %H:%M')}"
    )
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_EMPFAENGER"]

    # Der Originalreport bleibt unverändert. Bei NICHT_FINAL wird eine
    # deutliche Kennzeichnung vorangestellt, damit der Empfänger den Status
    # auch im Mail-Body sofort erkennt.
    mail_body = text
    if fachlich_nicht_final:
        mail_body = (
            "ACHTUNG: FACHLICH NICHT_FINAL\n"
            "Die Auswertung wurde technisch erfolgreich erstellt, ist aber "
            "fachlich nicht final freigegeben. Bitte Befunde/Unsicherheiten im "
            "Report beachten. Dies ist KEIN technischer Gemini-Fallback.\n\n"
            + text
        )
    msg.set_content(mail_body)

    # Zusaetzlich als Anhang - falls ein Mail-Client den Body umbricht/
    # kuerzt, bleibt die Originaldatei komplett und mit Original-Dateinamen
    # (inkl. Datum) erhalten.
    msg.add_attachment(text.encode("utf-8"), maintype="text", subtype="plain",
                        filename=os.path.basename(dateipfad))

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)

    print(f"Mail versendet an {os.environ['MAIL_EMPFAENGER']} - Inhalt: {dateipfad}")


if __name__ == "__main__":
    main()
