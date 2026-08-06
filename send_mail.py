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
sie hier im Sektor-Analyse-Repo separat eingetragen werden):
- SMTP_HOST (z.B. smtp.gmail.com)
- SMTP_PORT (z.B. 587)
- SMTP_USER (Absender-Adresse)
- SMTP_PASSWORD (App-Passwort, kein normales Kontopasswort)
- MAIL_EMPFAENGER (Ziel-Adresse)
"""

import os
import glob
import smtplib
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo


def finde_neueste_auswertung():
    """Der Dateiname ist datiert (Auswertung(2026-08-08).txt), deshalb kein
    fester Name moeglich - gleiches Muster wie lade_rotation_scores() in
    einzel_check.py (sorted(glob.glob(...))[-1], das ISO-Datumsformat
    YYYY-MM-DD im Dateinamen sortiert korrekt chronologisch als String).
    Wirft bewusst einen Fehler statt still eine alte/leere Mail zu
    verschicken, wenn keine Datei gefunden wird (z.B. weil der Gemini-
    Schritt an diesem Tag fehlgeschlagen ist - continue-on-error laesst
    main.yml dann zwar weiterlaufen, aber es gibt schlicht nichts zu
    verschicken)."""
    treffer = sorted(glob.glob("Auswertung(*).txt"))
    if not treffer:
        raise FileNotFoundError(
            "Keine Auswertung(*).txt im Arbeitsverzeichnis gefunden - "
            "vermutlich ist der Gemini-Auswertung-Schritt heute fehlgeschlagen."
        )
    return treffer[-1]


def main():
    dateipfad = finde_neueste_auswertung()
    with open(dateipfad, "r", encoding="utf-8") as f:
        text = f.read()

    jetzt_berlin = datetime.now(ZoneInfo("Europe/Berlin"))

    msg = EmailMessage()
    msg["Subject"] = f"Neuber Macro & Markets - {jetzt_berlin.strftime('%d.%m.%Y %H:%M')}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_EMPFAENGER"]

    # NUR Klartext, bewusst kein HTML-Teil (anders als bei Mini Daily Gold):
    # die Auswertung ist explizit als reine Plain-Text-Datei ohne Markdown/
    # Formatierung angelegt, passt deshalb unveraendert als E-Mail-Body.
    msg.set_content(text)

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
