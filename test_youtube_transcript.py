import sys
from pathlib import Path

import requests


VIDEO_ID = "WOps6_bjhZ0"
URL = f"https://youtube-transcript.ai/transcript/{VIDEO_ID}.txt"

OUTPUT_FILE = Path("youtube_transcript_test.txt")


def main():
    print("=" * 70)
    print("YouTube Transcript Test")
    print("=" * 70)
    print(f"Video-ID: {VIDEO_ID}")
    print(f"URL:      https://www.youtube.com/watch?v={VIDEO_ID}")
    print(f"Quelle:   {URL}")
    print()

    try:
        response = requests.get(
            URL,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )
    except requests.RequestException as exc:
        print(f"FEHLER: HTTPS-Abruf fehlgeschlagen: {exc}")
        sys.exit(1)

    print(f"HTTP-Status: {response.status_code}")
    print(f"Content-Type: {response.headers.get('content-type', '')}")
    print(f"Antwortgroesse: {len(response.text):,} Zeichen")
    print()

    if response.status_code != 200:
        print("FEHLER: Transcript konnte nicht abgerufen werden.")
        print(response.text[:1000])
        sys.exit(1)

    transcript = response.text.strip()

    if not transcript:
        print("FEHLER: Leere Transcript-Antwort.")
        sys.exit(1)

    # Sicherheitscheck: Es sollte tatsächlich ein Transcript
    # und nicht nur eine Fehlerseite zurückgekommen sein.
    if "Transcript:" not in transcript:
        print("WARNUNG: Antwort sieht nicht wie das erwartete Transcript aus.")
        print(transcript[:1000])
        sys.exit(1)

    OUTPUT_FILE.write_text(transcript, encoding="utf-8")

    print("SUCCESS: Transcript erfolgreich abgerufen.")
    print(f"Gespeichert in: {OUTPUT_FILE}")
    print()
    print("-" * 70)
    print("ERSTE 2.000 ZEICHEN")
    print("-" * 70)
    print(transcript[:2000])
    print()
    print("-" * 70)
    print("TEST ERFOLGREICH")
    print("-" * 70)


if __name__ == "__main__":
    main()
