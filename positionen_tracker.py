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
DATEINAME = 'Offene_Positionen.csv'
SPALTEN = [
    'Ticker', 'Name', 'Sektor', 'Markt', 'Waehrung',
    'Einstiegsdatum', 'Einstieg', 'Stop', 'TP1', 'TP2',
    'Status', 'Ausstiegsdatum', 'Ausstiegskurs',
    'Aktueller_Kurs', 'Performance_Seit_Einstieg%'
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


def finde_datei_id(service, dateiname, folder_id):
    """Sucht eine Datei mit exaktem Namen im Zielordner, gibt die File-ID zurück
    oder None, falls sie (noch) nicht existiert."""
    query = f"name='{dateiname}' and '{folder_id}' in parents and trashed=false"
    ergebnis = service.files().list(q=query, fields="files(id, name)").execute()
    treffer = ergebnis.get('files', [])
    if treffer:
        return treffer[0]['id']
    return None


def lade_positionen_herunter(service, file_id):
    """Lädt die bestehende Offene_Positionen.csv aus Drive herunter und
    gibt sie als DataFrame zurück. Legt eine leere Struktur an, falls die
    Datei noch nicht existiert (erster Lauf) oder leer/beschädigt ist."""
    if file_id is None:
        print(f"DEBUG: {DATEINAME} existiert noch nicht in Drive - starte mit leerer Liste.")
        return pd.DataFrame(columns=SPALTEN)

    try:
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        fertig = False
        while not fertig:
            _, fertig = downloader.next_chunk()
        buffer.seek(0)
        df = pd.read_csv(buffer, sep=';', encoding='utf-8-sig')

        # Sicherstellen, dass alle erwarteten Spalten existieren (falls die
        # Datei manuell bearbeitet wurde und Spalten fehlen)
        for spalte in SPALTEN:
            if spalte not in df.columns:
                df[spalte] = ""
        return df[SPALTEN]
    except Exception as e:
        print(f"FEHLER beim Herunterladen/Parsen von {DATEINAME}: {e}. Starte mit leerer Liste.")
        return pd.DataFrame(columns=SPALTEN)


def hole_aktuellen_kurs(ticker, markt):
    """Holt den letzten verfügbaren Schlusskurs - via Alpaca für US-Werte,
    via yfinance für DAX-Werte (inkl. NaN-Bereinigung, siehe scan_setups_fixed.py)."""
    try:
        if markt == 'US':
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
        else:  # DAX
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
    heute = datetime.datetime.now().strftime("%Y-%m-%d")

    for idx, row in df.iterrows():
        if str(row['Status']).strip() != 'Offen':
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


def hochladen(service, dateiname, folder_id, file_id):
    """Lädt die aktualisierte Datei nach Drive hoch - überschreibt die
    bestehende Datei in-place (update), falls sie schon existiert, statt
    jeden Tag eine neue Datei mit demselben Namen anzulegen."""
    media = MediaIoBaseUpload(io.FileIO(dateiname, 'rb'), mimetype='text/csv', resumable=True)
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"Datei '{dateiname}' in Drive aktualisiert (ID: {file_id}).")
    else:
        file_metadata = {'name': dateiname, 'parents': [folder_id]}
        neue_datei = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print(f"Datei '{dateiname}' neu in Drive angelegt (ID: {neue_datei.get('id')}).")


if __name__ == '__main__':
    print("Positions-Tracker gestartet...")
    service = get_drive_service()

    file_id = finde_datei_id(service, DATEINAME, FOLDER_ID)
    df = lade_positionen_herunter(service, file_id)

    anzahl_offen = len(df[df['Status'] == 'Offen']) if not df.empty else 0
    print(f"DEBUG: {anzahl_offen} offene Position(en) zur Prüfung gefunden.")

    if anzahl_offen > 0:
        df = aktualisiere_positionen(df)

    # Immer lokal speichern (auch bei 0 offenen Positionen), damit
    # analyse.py die Datei für den Briefing-Abschnitt einlesen kann
    df.to_csv(DATEINAME, index=False, sep=';', encoding='utf-8-sig')
    hochladen(service, DATEINAME, FOLDER_ID, file_id)

    print("Positions-Tracker abgeschlossen.")
