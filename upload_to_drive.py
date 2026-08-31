import os
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

print(f"Aktuelles Arbeitsverzeichnis: {os.getcwd()}")
print("Gefundene Dateien im Ordner:")
print(os.listdir('.'))


def get_drive_service():
    """Baut den Drive-Service auf und sorgt dafür, dass der Access-Token
    aktuell ist - inklusive klarer Fehlermeldungen, falls das Refreshen
    fehlschlägt (z.B. weil das Secret unvollständig ist oder der Google-
    OAuth-Client noch im "Testing"-Status mit 7-Tage-Ablauf steht)."""
    token_str = os.environ.get("GDRIVE_TOKEN")

    if not token_str:
        print("FEHLER: Umgebungsvariable GDRIVE_TOKEN nicht gefunden!")
        raise EnvironmentError("GDRIVE_TOKEN ist nicht gesetzt.")

    try:
        token_data = json.loads(token_str)
        # NEU (09.08.2026): zeigt den im Token gespeicherten Scope im Log an -
        # Google speichert ihn beim Erstellen mit ab, wird nirgends im Code selbst
        # festgelegt. Damit muss niemand mehr in alten Dateien danach suchen -
        # steht beim naechsten Lauf einfach direkt im GitHub-Actions-Log.
        print(f"DEBUG: Im GDRIVE_TOKEN gespeicherter Scope: {token_data.get('scope', token_data.get('scopes', 'NICHT GEFUNDEN'))}")
    except Exception as e:
        print(f"FEHLER beim Parsen des Tokens (kein gültiges JSON): {e}")
        raise

    # Prüfen, ob alle für ein automatisches Refresh nötigen Felder vorhanden sind.
    # Fehlt eines davon, kann die Bibliothek einen abgelaufenen Access-Token
    # NICHT automatisch erneuern - das ist die häufigste Ursache dafür, dass
    # ein Upload manuell kurz nach dem Erzeugen des Tokens klappt, beim
    # automatischen Cron-Lauf Stunden später aber stillschweigend scheitert.
    required_fields = ["refresh_token", "client_id", "client_secret", "token_uri"]
    fehlende_felder = [f for f in required_fields if not token_data.get(f)]
    if fehlende_felder:
        print(f"FEHLER: GDRIVE_TOKEN fehlen folgende Felder für ein automatisches "
              f"Token-Refresh: {fehlende_felder}. Ohne diese kann der Token nach "
              f"Ablauf (Access-Token: ~1 Stunde) nicht erneuert werden.")
        raise EnvironmentError(f"GDRIVE_TOKEN unvollständig: {fehlende_felder} fehlen.")

    try:
        creds = Credentials.from_authorized_user_info(token_data)
    except Exception as e:
        print(f"FEHLER beim Erstellen der Credentials aus dem Token: {e}")
        raise

    # Access-Token aktiv erneuern, falls abgelaufen oder ungültig - statt
    # darauf zu vertrauen, dass die Bibliothek das beim ersten API-Call
    # automatisch und stillschweigend erledigt.
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            print("Access-Token abgelaufen - versuche Refresh...")
            try:
                creds.refresh(Request())
                print("Token-Refresh erfolgreich.")
            except Exception as e:
                print(f"FEHLER beim Token-Refresh: {e}")
                print("Mögliche Ursachen: Refresh-Token widerrufen/abgelaufen "
                      "(z.B. 7-Tage-Ablauf bei OAuth-Client im 'Testing'-Status "
                      "in der Google Cloud Console), falsche client_id/client_secret "
                      "im Secret, oder Google-Server-Problem.")
                raise
        else:
            print("FEHLER: Token ist ungültig und kann nicht automatisch erneuert "
                  "werden (kein Refresh-Token vorhanden oder anderer Grund).")
            raise EnvironmentError("GDRIVE_TOKEN: Credentials ungültig, kein Refresh möglich.")

    return build('drive', 'v3', credentials=creds)


# Arbeitsverzeichnis festlegen
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def upload_file(filename, folder_id, service):
    # Sicherheitsreihenfolge: Die bestehende Datei wird NICHT vor dem Upload
    # gelöscht. Ein transienter Drive-/SSL-Timeout darf niemals dazu führen,
    # dass der letzte erfolgreiche Stand verloren geht. Erst nach bestätigtem
    # Upload werden vorhandene gleichnamige Altdateien entfernt.
    try:
        query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        alte_dateien = service.files().list(
            q=query, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc"
        ).execute().get("files", [])
    except Exception as e:
        alte_dateien = []
        print(f"  WARNUNG: Konnte vorhandene Versionen von '{filename}' nicht prüfen ({e}) - Upload wird trotzdem versucht.")

    file_metadata = {'name': filename, 'parents': [folder_id]}
    letzter_fehler = None
    neue_datei = None

    for versuch in range(1, 4):
        try:
            # Pro Versuch einen frischen Upload-Stream erzeugen.
            media = MediaFileUpload(filename, resumable=True)
            neue_datei = service.files().create(
                body=file_metadata, media_body=media, fields='id'
            ).execute(num_retries=3)
            if not neue_datei.get('id'):
                raise RuntimeError("Google Drive meldete keine Datei-ID nach dem Upload.")
            print(f"Datei '{filename}' erfolgreich hochgeladen. ID: {neue_datei['id']} (Versuch {versuch}/3)")
            break
        except Exception as e:
            letzter_fehler = e
            # Bei einem Timeout kann Google den Upload bereits angenommen haben,
            # bevor die Antwort den Runner erreicht. Vor einem erneuten Upload
            # deshalb prüfen, ob bereits eine neue gleichnamige Datei entstanden ist.
            try:
                aktuelle_dateien = service.files().list(
                    q=query, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc"
                ).execute().get("files", [])
                alte_ids = {alt.get('id') for alt in alte_dateien}
                neue_kandidaten = [f for f in aktuelle_dateien if f.get('id') not in alte_ids]
                if neue_kandidaten:
                    neue_datei = neue_kandidaten[0]
                    print(f"  INFO: Upload wurde trotz Fehlermeldung offenbar bereits angenommen. Verwende neue Datei-ID {neue_datei['id']}.")
                    break
            except Exception as pruef_fehler:
                print(f"  DEBUG: Prüfung auf bereits angenommenen Upload nicht möglich ({pruef_fehler}).")

            if versuch < 3:
                wartezeit = 2 ** (versuch - 1)
                print(f"  WARNUNG: Upload von '{filename}' fehlgeschlagen ({e}). Retry {versuch + 1}/3 in {wartezeit}s...")
                import time
                time.sleep(wartezeit)
            else:
                raise

    neue_id = neue_datei['id']

    # Erst jetzt die alten gleichnamigen Dateien löschen. Dadurch bleibt der
    # letzte funktionierende Stand bei einem Upload-Timeout erhalten.
    for alt in alte_dateien:
        if alt.get('id') == neue_id:
            continue
        try:
            service.files().delete(fileId=alt['id']).execute(num_retries=3)
            print(f"  (alte Version von '{filename}' gelöscht, ID: {alt['id']})")
        except Exception as e:
            # Ein Aufräumfehler darf einen bereits erfolgreichen Upload nicht
            # nachträglich zum Workflow-Abbruch machen.
            print(f"  WARNUNG: Alte Version von '{filename}' konnte nicht gelöscht werden ({e}).")


if __name__ == '__main__':
    # HIER DEINE FOLDER-ID EINTRAGEN
    FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'

    # Service EINMAL aufbauen (inkl. Refresh-Check) statt bei jedem Upload neu -
    # spart unnötige Refresh-Versuche und macht Fehler früher sichtbar.
    drive_service = get_drive_service()

    print("Suche nach neuen Dateien zum Hochladen...")
    found = False
    for filename in os.listdir('.'):
        # Scannt nach Performance, Setups (CSV) ODER Briefing (TXT) - "in" statt
        # "startswith", damit auch Trendwende_Setups(...).csv und
        # Trendwende_Briefing(...).txt erfasst werden (eigener Scanner, eigene
        # Dateien, siehe trendwende_scanner.py).
        if ("Performance" in filename or "Setups" in filename or "Langfrist_Bewertung" in filename) and filename.endswith(".csv"):
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID, drive_service)
            found = True
        elif (filename.startswith("Einzel_Check_Aufstiege(") or
              filename.startswith("Einzel_Check_A_Meldungen(")) and filename.endswith(".txt"):
            # Einzel-Check-A-Informationen: echte B/C -> A-Aufstiege und
            # separate Meldung aller aktuellen A-Kandidaten.
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID, drive_service)
            found = True
        elif filename == "einzel_check_beobachtung.json":
            # Persistente Beobachtungsliste des manuellen Einzel-Checks:
            # A/B/C und bereits beobachtete KEIN-KANDIDAT-Titel bleiben erhalten;
            # >45 Tage ohne A/B/C oder technische Ausschlüsse werden entfernt.
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID, drive_service)
            found = True
        elif ("Briefing" in filename or "Auswertung" in filename) and filename.endswith(".txt"):
            # "Auswertung" (NEU): die von claude_auswertung.py erzeugte fertige
            # Daten-Übersicht, landet genau wie die anderen Text-Dateien direkt
            # im selben Drive-Ordner.
            print(f"Lade '{filename}' hoch...")
            upload_file(filename, FOLDER_ID, drive_service)
            found = True

    if not found:
        print("Keine passenden Dateien zum Hochladen gefunden.")
