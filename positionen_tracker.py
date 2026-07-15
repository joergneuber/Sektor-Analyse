import os
import io
import json
import datetime
import pandas as pd
import yfinance as yf
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- KONFIGURATION ---
FOLDER_ID = '1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL'
LOKALE_DATEI = 'Offene_Positionen.csv'          # lokale Arbeitsdatei (für analyse.py)
DRIVE_NAME = 'Offene_Positionen'                # Anzeigename in Drive (native Google-Sheets-Datei,
                                                 # ohne .csv-Endung, damit sie sich direkt per Doppelklick
                                                 # in Sheets öffnen und bearbeiten lässt - keine separate
                                                 # Kopie mehr, wie es bei einer echten .csv-Datei passiert)
DRIVE_NAME_ALT = 'Offene_Positionen.csv'        # Alter Name (Übergang von der ersten Version)
SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
ANLEITUNG_TICKER = 'ANLEITUNG'  # Sentinel-Wert: Zeilen mit diesem Ticker werden
                                 # nie als echte Position verarbeitet, dienen nur
                                 # als sichtbarer Hinweistext im Sheet selbst
SPALTEN = [
    'Ticker', 'Name', 'Sektor', 'Markt', 'Waehrung',
    'Einstiegsdatum', 'Einstieg', 'Stop', 'TP1', 'TP2',
    'Status', 'Ausstiegsdatum', 'Ausstiegskurs',
    'Aktueller_Kurs', 'Performance_Seit_Einstieg%',
    'Produkt_Typ', 'Emittent', 'Hebel',
    'OS_Einstiegskurs', 'OS_Manueller_Kurs',
    'OS_Performance%', 'OS_Quelle'
]
NUMERISCHE_SPALTEN = [
    'Einstieg', 'Stop', 'TP1', 'TP2', 'Ausstiegskurs', 'Aktueller_Kurs', 'Performance_Seit_Einstieg%',
    'Hebel', 'OS_Einstiegskurs', 'OS_Manueller_Kurs', 'OS_Performance%'
]

alpaca_client = StockHistoricalDataClient(os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'))


def get_drive_service():
    """Baut den Drive-Service auf und erneuert den Access-Token aktiv,
    falls abgelaufen (siehe upload_to_drive.py für ausführliche Begründung)."""
    token_str = os.environ.get("GDRIVE_TOKEN")
    if not token_str:
        print("FEHLER: Umgebungsvariable GDRIVE_TOKEN nicht gefunden!")
        raise EnvironmentError("GDRIVE_TOKEN ist nicht gesetzt.")

    try:
        token_data = json.loads(token_str)
    except Exception as e:
        print(f"FEHLER beim Parsen des Tokens: {e}")
        raise

    required_fields = ["refresh_token", "client_id", "client_secret", "token_uri"]
    fehlende_felder = [f for f in required_fields if not token_data.get(f)]
    if fehlende_felder:
        print(f"FEHLER: GDRIVE_TOKEN fehlen folgende Felder: {fehlende_felder}")
        raise EnvironmentError(f"GDRIVE_TOKEN unvollständig: {fehlende_felder} fehlen.")

    creds = Credentials.from_authorized_user_info(token_data)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            print("Access-Token abgelaufen - versuche Refresh...")
            creds.refresh(Request())
            print("Token-Refresh erfolgreich.")
        else:
            raise EnvironmentError("GDRIVE_TOKEN: Credentials ungültig, kein Refresh möglich.")

    return build('drive', 'v3', credentials=creds)


def finde_datei(service, folder_id):
    """Sucht nach der Positionen-Datei unter dem aktuellen (native Sheet) oder
    dem alten Namen (rohe .csv aus der ersten Version). Gibt (file_id, mime_type)
    zurück, oder (None, None), falls noch keine Datei existiert."""
    query = (
        f"(name='{DRIVE_NAME}' or name='{DRIVE_NAME_ALT}') "
        f"and '{folder_id}' in parents and trashed=false"
    )
    ergebnis = service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    treffer = ergebnis.get('files', [])
    if not treffer:
        return None, None
    # Falls beide Namen existieren (Übergangsfall), das native Sheet bevorzugen
    for f in treffer:
        if f['mimeType'] == SHEET_MIME:
            return f['id'], f['mimeType']
    return treffer[0]['id'], treffer[0]['mimeType']


def _datum_vereinheitlichen(wert):
    """Wandelt einen einzelnen Datumswert - egal in welchem Format Google
    Sheets ihn beim CSV-Export geschrieben hat (z.B. '15/7', '2026-07-15',
    '7/15/2026') - ins einheitliche Format TT.MM.JJJJ um. Unbekannte/leere
    Werte bleiben unverändert, statt verworfen zu werden."""
    if wert is None or (isinstance(wert, float) and pd.isna(wert)):
        return wert
    text = str(wert).strip()
    if text == "" or text.lower() == "nan":
        return text
    try:
        datum = pd.to_datetime(text, dayfirst=True, errors='raise')
        if datum.year < 1970:
            # Ursprungswert hatte keine Jahresangabe (z.B. '15/7') - Pandas
            # setzt dann ein unplausibles Platzhalter-Jahr; aktuelles Jahr
            # ist die sinnvollste Annahme für ein Positions-Tracking
            datum = datum.replace(year=datetime.datetime.now().year)
        return datum.strftime("%d.%m.%Y")
    except Exception:
        return text  # Unbekanntes Format - lieber unverändert lassen als verwerfen


def normalisiere_daten(df):
    """Vereinheitlicht Einstiegsdatum/Ausstiegsdatum auf TT.MM.JJJJ, egal wie
    Google Sheets sie exportiert hat - sonst wechselt die Anzeige im Briefing
    je nach Gebietsschema-Einstellung des Sheets (z.B. '15/7' statt einem
    festen, lesbaren Format)."""
    for spalte in ['Einstiegsdatum', 'Ausstiegsdatum']:
        if spalte in df.columns:
            df[spalte] = df[spalte].apply(_datum_vereinheitlichen)
    return df


def normalisiere_zahlen(df):
    """Wandelt Zahlen mit Komma als Dezimaltrennzeichen (z.B. '47,77' aus einem
    deutsch lokalisierten Google Sheet exportiert) ins Punkt-Format um, das
    Python für float()-Umwandlungen braucht. Ohne diese Bereinigung schlägt
    jede float()-Umwandlung fehl, sobald das Sheet auf ein Gebietsschema mit
    Komma-Dezimaltrennzeichen eingestellt ist - unabhängig davon, ob die
    Zahlen manuell eingetragen oder vom Skript selbst geschrieben wurden."""
    for spalte in NUMERISCHE_SPALTEN:
        if spalte in df.columns:
            df[spalte] = df[spalte].apply(
                lambda v: str(v).replace(',', '.') if isinstance(v, str) and v.strip() != "" else v
            )
    return df


def ergaenze_neue_zeilen(df):
    """Vervollständigt Zeilen, bei denen nur Ticker, Einstieg und Stop manuell
    eingetragen wurden (Status-Feld noch leer). Automatisch abgeleitet:
    - Markt: aus dem Ticker-Suffix (.DE -> DAX, sonst US)
    - Waehrung: aus dem Markt (US -> USD, DAX -> EUR)
    - Name: per yfinance-Firmennamen-Abruf (best effort, Fallback: Ticker)
    - Einstiegsdatum: heutiges Datum
    - Status: 'Offen'
    - TP1/TP2 (NUR falls leer): grobe 2:1/3:1-Chance-Risiko-Schätzung aus
      Einstieg/Stop - KEINE echte technische Zielberechnung wie im Scanner
      (dort EMA/Fib/Realitäts-Deckel-basiert), nur ein Platzhalter, damit das
      Briefing nicht leer bleibt. Bei Bedarf manuell überschreiben.
    Sektor wird bewusst NICHT automatisch ermittelt (keine zuverlässige
    Zuordnung ohne Duplizierung der kompletten Sektor-Listen aus analyse.py).
    Die Anleitungszeile (Ticker == ANLEITUNG_TICKER) wird dabei ignoriert."""
    heute = datetime.datetime.now().strftime("%d.%m.%Y")

    for idx, row in df.iterrows():
        ticker = str(row['Ticker']).strip()
        if not ticker or ticker.lower() == 'nan' or ticker.upper() == ANLEITUNG_TICKER:
            continue

        status_leer = str(row['Status']).strip() == "" or str(row['Status']).strip().lower() == "nan"
        if not status_leer:
            continue  # Zeile schon aktiviert (offen/gestoppt) oder manuell gepflegt - nicht anfassen

        einstieg_vorhanden = not pd.isna(row['Einstieg']) and str(row['Einstieg']).strip() not in ("", "nan")
        stop_vorhanden = not pd.isna(row['Stop']) and str(row['Stop']).strip() not in ("", "nan")
        if not (einstieg_vorhanden and stop_vorhanden):
            continue  # Noch nicht genug für eine neue Position (Einstieg/Stop fehlen)

        print(f"DEBUG: Neue Zeile erkannt für {ticker} - ergänze automatisch ableitbare Felder...")

        # Markt-Erkennung über das Ticker-Suffix: Jeder Ticker MIT Punkt-Suffix
        # (.DE Xetra, .PA Paris, .AS Amsterdam, .MI Mailand, .L London, ...)
        # ist ein europäischer Titel und läuft über yfinance. Nur suffixlose
        # Ticker gelten als US-Titel (Alpaca). Die Waehrung wird grob aus dem
        # Suffix abgeleitet (.L = GBP-Sonderfall, sonst EUR fuer EU-Boersen).
        ticker_upper = ticker.upper()
        if '.' in ticker_upper:
            markt = 'DAX' if ticker_upper.endswith('.DE') else 'EU'
            waehrung = 'GBP' if ticker_upper.endswith('.L') else 'EUR'
        else:
            markt = 'US'
            waehrung = 'USD'

        try:
            info = yf.Ticker(ticker).info
            name = info.get('longName', ticker)
            if not name:
                name = ticker
        except Exception as e:
            print(f"DEBUG: Firmenname für {ticker} konnte nicht ermittelt werden ({e}) - nutze Ticker als Name.")
            name = ticker

        einstieg = float(row['Einstieg'])
        stop = float(row['Stop'])
        risiko = einstieg - stop

        tp1_leer = pd.isna(row['TP1']) or str(row['TP1']).strip() in ("", "nan")
        tp2_leer = pd.isna(row['TP2']) or str(row['TP2']).strip() in ("", "nan")
        if risiko > 0:
            if tp1_leer:
                df.at[idx, 'TP1'] = round(einstieg + 2 * risiko, 2)
            if tp2_leer:
                df.at[idx, 'TP2'] = round(einstieg + 3 * risiko, 2)
        elif tp1_leer or tp2_leer:
            print(f"DEBUG: {ticker} -> Stop liegt nicht unter dem Einstieg, TP1/TP2 können nicht geschätzt werden.")

        df.at[idx, 'Markt'] = markt
        df.at[idx, 'Waehrung'] = waehrung
        df.at[idx, 'Name'] = name
        if pd.isna(row['Einstiegsdatum']) or str(row['Einstiegsdatum']).strip() in ("", "nan"):
            df.at[idx, 'Einstiegsdatum'] = heute
        df.at[idx, 'Status'] = 'Offen'

    return df


def stelle_anleitung_sicher(df):
    """Stellt sicher, dass die Hinweiszeile (Ticker == ANLEITUNG_TICKER) an
    erster Stelle steht - unabhängig davon, ob die Datei ganz neu angelegt
    wurde oder schon vor Einführung dieser Funktion existierte. Läuft bei
    JEDEM Aufruf, nicht nur bei der Erstanlage, damit bestehende Dateien
    (wie die aus früheren Versionen) die Anleitung nachträglich bekommen."""
    # Defensiv: fehlende Spalten (z.B. neue Optionsschein-Felder bei einer
    # Datei mit altem Schema) hier zentral nachrüsten, bevor irgendein
    # Spaltenzugriff stattfindet
    for spalte in SPALTEN:
        if spalte not in df.columns:
            df[spalte] = ""
    df = df[SPALTEN]

    # Typ-Absicherung in BEIDE Richtungen (neuere Pandas-Versionen verweigern
    # dtype-fremde Zuweisungen hart statt still zu konvertieren):
    # 1. Nicht-Zahlen-Spalten -> object, NaN -> "" (String-Zuweisungen wie
    #    Datum '15.07.2026' in leere float64-NaN-Spalten crashen sonst)
    # 2. Zahlen-Spalten -> explizit numerisch (der Sheets-Export liefert alles
    #    als Text; eine spätere float-Zuweisung wie Aktueller_Kurs = 53.77 in
    #    eine str-typisierte Spalte crasht sonst genauso). errors='coerce'
    #    macht Leerstrings/Unlesbares zu NaN - beim CSV-Schreiben wieder leer.
    for spalte in df.columns:
        if spalte not in NUMERISCHE_SPALTEN:
            df[spalte] = df[spalte].astype(object)
            df[spalte] = df[spalte].where(pd.notna(df[spalte]), "")
        else:
            df[spalte] = pd.to_numeric(df[spalte], errors='coerce')

    vorhanden = (
        df['Ticker'].astype(str).str.strip().str.upper() == ANLEITUNG_TICKER
    ).any() if not df.empty else False

    if vorhanden:
        return df

    print("DEBUG: Anleitungszeile fehlt - wird ergänzt.")
    anleitung = {spalte: "" for spalte in SPALTEN}
    anleitung['Ticker'] = ANLEITUNG_TICKER
    anleitung['Name'] = (
        "NEUE POSITION: nur Ticker, Einstieg und Stop ausfuellen, Status-Feld LEER LASSEN. "
        "Rest wird automatisch ergaenzt (Name, Markt, Waehrung, Einstiegsdatum, Status=Offen). "
        "TP1/TP2 werden nur grob geschaetzt (2:1/3:1 Chance-Risiko) falls leer - bei Bedarf "
        "manuell mit echten Werten aus Setups.csv ueberschreiben. Sektor wird NICHT automatisch "
        "ermittelt, bleibt leer, falls nicht manuell eingetragen. Diese Zeile nicht loeschen "
        "oder als Position befuellen (Ticker-Wert 'ANLEITUNG' wird ignoriert)."
    )
    anleitung['Sektor'] = (
        "OPTIONSSCHEIN (optional): Produkt_Typ = 'Optionsschein' setzen, dann zusaetzlich "
        "Emittent, Hebel und OS_Einstiegskurs ausfuellen - die Performance des Scheins wird "
        "dann automatisch aus Hebel x Aktienkursbewegung GESCHAETZT (OS_Quelle = 'geschaetzt'). "
        "Traegst du stattdessen den aktuellen Schein-Kurs in OS_Manueller_Kurs ein, wird DIESER "
        "Wert bevorzugt fuer OS_Performance% genutzt (OS_Quelle = 'manuell') - genauer, aber muss "
        "von dir selbst nachgetragen werden. Ohne Angaben bleibt Produkt_Typ leer = normale Aktie."
    )
    return pd.concat([pd.DataFrame([anleitung]), df], ignore_index=True)[SPALTEN]


def lade_positionen_herunter(service, file_id, mime_type):
    """Lädt die bestehende Positionen-Datei aus Drive herunter und gibt sie als
    DataFrame zurück. Bei einer nativen Google-Sheets-Datei wird der Inhalt per
    Export als CSV abgerufen (get_media funktioniert bei nativen Google-Typen
    nicht); bei einer rohen .csv (Übergangsfall von der ersten Version) wird
    stattdessen direkt heruntergeladen. Legt eine leere Struktur an, falls die
    Datei noch nicht existiert (erster Lauf) oder leer/beschädigt ist."""
    if file_id is None:
        print(f"DEBUG: {DRIVE_NAME} existiert noch nicht in Drive - starte mit leerer Liste.")
        return pd.DataFrame(columns=SPALTEN)

    try:
        if mime_type == SHEET_MIME:
            request = service.files().export(fileId=file_id, mimeType='text/csv')
        else:
            request = service.files().get_media(fileId=file_id)

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        fertig = False
        while not fertig:
            _, fertig = downloader.next_chunk()
        buffer.seek(0)

        # Native Sheets-Exporte sind komma-getrennt, die alte rohe CSV war
        # semikolon-getrennt - beides abfangen
        inhalt = buffer.getvalue().decode('utf-8-sig')
        sep = ';' if inhalt.count(';') > inhalt.count(',') else ','
        df = pd.read_csv(io.StringIO(inhalt), sep=sep)

        for spalte in SPALTEN:
            if spalte not in df.columns:
                df[spalte] = ""
        return df[SPALTEN]
    except Exception as e:
        print(f"FEHLER beim Herunterladen/Parsen von {DRIVE_NAME}: {e}. Starte mit leerer Liste.")
        return pd.DataFrame(columns=SPALTEN)


def hole_aktuellen_kurs(ticker, markt):
    """Holt den letzten verfügbaren Schlusskurs - via Alpaca für US-Werte
    (suffixlose Ticker), via yfinance für alle europäischen Titel (Ticker mit
    Punkt-Suffix: .DE, .PA, .AS, .MI, .L, ...), inkl. NaN-Bereinigung.
    Sicherheitsnetz: Ein Ticker MIT Suffix läuft immer über yfinance, selbst
    wenn im Markt-Feld (manuell) US steht - Alpaca kennt keine EU-Ticker."""
    try:
        if markt == 'US' and '.' not in str(ticker):
            start_date = datetime.datetime.now() - datetime.timedelta(days=10)
            request = StockBarsRequest(symbol_or_symbols=[ticker], start=start_date, timeframe=TimeFrame.Day)
            bars = alpaca_client.get_stock_bars(request)
            hist = bars.df
            if hist.empty:
                return None
            hist = hist.reset_index(level=0, drop=True)
            if 'close' in hist.columns:
                hist = hist.rename(columns={'close': 'Close'})
            return float(hist['Close'].iloc[-1])
        else:  # Europäische Börsen (jedes Punkt-Suffix) via yfinance
            hist = yf.Ticker(ticker).history(period="10d")
            if hist.empty:
                return None
            hist = hist.dropna(subset=['Close'])
            if hist.empty:
                return None
            return float(hist['Close'].iloc[-1])
    except Exception as e:
        print(f"FEHLER beim Kursabruf für {ticker} ({markt}): {e}")
        return None


def aktualisiere_positionen(df):
    """Prüft jede offene Position gegen den aktuellen Kurs und den Stop.
    Wird der Stop erreicht oder unterschritten, wechselt der Status auf
    'Gestoppt' und Ausstiegsdatum/-kurs werden gesetzt. Für alle offenen
    Positionen wird zusätzlich der aktuelle Kurs und die Performance seit
    Einstieg als Info-Spalte ergänzt (für die Briefing-Anzeige)."""
    heute = datetime.datetime.now().strftime("%d.%m.%Y")

    for idx, row in df.iterrows():
        if str(row['Status']).strip().lower() != 'offen':
            continue

        ticker = row['Ticker']
        markt = row['Markt']
        stop = float(row['Stop'])
        einstieg = float(row['Einstieg'])

        aktueller_kurs = hole_aktuellen_kurs(ticker, markt)
        if aktueller_kurs is None:
            print(f"DEBUG: Kein aktueller Kurs für {ticker} verfügbar - Status bleibt unverändert.")
            continue

        performance = round(((aktueller_kurs - einstieg) / einstieg) * 100, 2) if einstieg > 0 else 0.0
        df.at[idx, 'Aktueller_Kurs'] = aktueller_kurs
        df.at[idx, 'Performance_Seit_Einstieg%'] = performance

        if aktueller_kurs <= stop:
            print(f"DEBUG: {ticker} -> Stop erreicht/unterschritten (Kurs={aktueller_kurs}, Stop={stop}). Status -> Gestoppt.")
            df.at[idx, 'Status'] = 'Gestoppt'
            df.at[idx, 'Ausstiegsdatum'] = heute
            df.at[idx, 'Ausstiegskurs'] = aktueller_kurs

    return df


def berechne_optionsschein_performance(df):
    """Berechnet für Positionen mit Produkt_Typ = 'Optionsschein' die Performance
    des Scheins selbst (nicht der Aktie). Zwei Quellen, manueller Kurs hat Vorrang:
    - OS_Manueller_Kurs vorhanden: echte Performance daraus, OS_Quelle = 'manuell'
      (präziser, da der tatsächliche Schein-Kurs verwendet wird statt einer
      linearen Näherung - erfasst Spread, Restlaufzeit, Volatilität automatisch)
    - sonst, falls Hebel + OS_Einstiegskurs vorhanden: GESCHÄTZTE Performance aus
      Hebel x Aktienkursbewegung, OS_Quelle = 'geschätzt' (vereinfachte lineare
      Näherung - reale Scheine bewegen sich nicht exakt linear zum Hebel)
    Gilt nur für Zeilen mit Status = 'Offen' und echten Werten in Aktueller_Kurs
    (wird vorher von aktualisiere_positionen gesetzt)."""
    # Defensiv: Falls die eingelesene Datei die Optionsschein-Spalten (noch)
    # nicht kennt (altes Schema, manuell bearbeitete Datei), hier nachrüsten
    # statt mit KeyError abzubrechen
    for spalte in SPALTEN:
        if spalte not in df.columns:
            df[spalte] = ""

    for idx, row in df.iterrows():
        ticker = str(row['Ticker']).strip()
        if not ticker or ticker.lower() == 'nan' or ticker.upper() == ANLEITUNG_TICKER:
            continue
        if str(row['Status']).strip().lower() != 'offen':
            continue

        produkt_typ = str(row['Produkt_Typ']).strip().lower()
        if produkt_typ != 'optionsschein':
            continue

        os_manuell = row['OS_Manueller_Kurs']
        os_manuell_vorhanden = not pd.isna(os_manuell) and str(os_manuell).strip() not in ("", "nan")

        os_einstieg = row['OS_Einstiegskurs']
        os_einstieg_vorhanden = not pd.isna(os_einstieg) and str(os_einstieg).strip() not in ("", "nan")

        if os_manuell_vorhanden and os_einstieg_vorhanden:
            os_manuell_f = float(os_manuell)
            os_einstieg_f = float(os_einstieg)
            if os_einstieg_f > 0:
                performance = round(((os_manuell_f - os_einstieg_f) / os_einstieg_f) * 100, 2)
                df.at[idx, 'OS_Performance%'] = performance
                df.at[idx, 'OS_Quelle'] = 'manuell'
                print(f"DEBUG: {ticker} -> OS-Performance aus manuellem Kurs: {performance}%")
            continue

        hebel = row['Hebel']
        hebel_vorhanden = not pd.isna(hebel) and str(hebel).strip() not in ("", "nan")
        aktien_performance = row['Performance_Seit_Einstieg%']
        aktien_performance_vorhanden = not pd.isna(aktien_performance) and str(aktien_performance).strip() not in ("", "nan")

        if hebel_vorhanden and aktien_performance_vorhanden:
            hebel_f = float(hebel)
            performance = round(hebel_f * float(aktien_performance), 2)
            df.at[idx, 'OS_Performance%'] = performance
            df.at[idx, 'OS_Quelle'] = 'geschätzt'
            print(f"DEBUG: {ticker} -> OS-Performance geschätzt (Hebel {hebel_f}x): {performance}%")
        else:
            print(f"DEBUG: {ticker} -> Produkt_Typ=Optionsschein, aber weder OS_Manueller_Kurs noch (Hebel+OS_Einstiegskurs) vollständig - keine OS-Performance berechenbar.")

    return df


def hochladen(service, lokale_datei, folder_id, alte_file_id):
    """Lädt die aktualisierte Datei als NATIVE Google-Sheets-Datei nach Drive
    hoch - dafür wird die alte Datei (falls vorhanden) gelöscht und komplett
    neu angelegt, mit CSV-Inhalt als Upload-Medium und Sheets-Ziel-MIME-Typ.
    Das ist der zuverlässigste Weg laut Drive-API, eine CSV in ein natives
    Sheet zu konvertieren (ein reines In-Place-Update per media_body auf eine
    bestehende Sheets-Datei ist laut Drive-API-Doku nicht garantiert). Der
    Nutzer kann die entstehende Datei direkt in Google Sheets öffnen und
    bearbeiten - keine separate Kopie mehr wie bei einer rohen .csv."""
    if alte_file_id:
        service.files().delete(fileId=alte_file_id).execute()
        print(f"Alte Datei (ID: {alte_file_id}) gelöscht, wird neu angelegt.")

    media = MediaIoBaseUpload(io.FileIO(lokale_datei, 'rb'), mimetype='text/csv', resumable=True)
    file_metadata = {'name': DRIVE_NAME, 'parents': [folder_id], 'mimeType': SHEET_MIME}
    neue_datei = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print(f"Datei '{DRIVE_NAME}' als Google Sheet in Drive angelegt (ID: {neue_datei.get('id')}).")


if __name__ == '__main__':
    print("Positions-Tracker gestartet...")
    service = get_drive_service()

    file_id, mime_type = finde_datei(service, FOLDER_ID)
    df = lade_positionen_herunter(service, file_id, mime_type)
    df = normalisiere_zahlen(df)
    df = normalisiere_daten(df)
    df = stelle_anleitung_sicher(df)

    df = ergaenze_neue_zeilen(df)

    anzahl_offen = len(df[df['Status'].astype(str).str.strip().str.lower() == 'offen']) if not df.empty else 0
    print(f"DEBUG: {anzahl_offen} offene Position(en) zur Prüfung gefunden.")

    if anzahl_offen > 0:
        df = aktualisiere_positionen(df)
        df = berechne_optionsschein_performance(df)

    # Immer lokal speichern (auch bei 0 offenen Positionen), damit
    # analyse.py die Datei für den Briefing-Abschnitt einlesen kann
    df.to_csv(LOKALE_DATEI, index=False, sep=';', encoding='utf-8-sig')
    hochladen(service, LOKALE_DATEI, FOLDER_ID, file_id)

    print("Positions-Tracker abgeschlossen.")
