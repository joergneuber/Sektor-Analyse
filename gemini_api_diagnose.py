"""
Isolierter Gemini-API-Diagnosetest.

Verwendet bewusst NICHT den Produktionscode und NICHT den Quota-/Retry-Scheduler.
Getestet wird nur:
    GEMINI_API_KEY -> google-genai -> generate_content()

Der Test ist absichtlich klein, damit ein 503 nicht mit der Größe des
Produktions-Requests verwechselt werden kann.
"""

import os
import sys
from google import genai


MODEL = os.getenv("GEMINI_DIAGNOSE_MODEL", "gemini-3.5-flash")
PROMPT = "Antworte ausschließlich mit: TEST OK"


def main() -> int:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("FEHLER: GEMINI_API_KEY ist nicht gesetzt.", flush=True)
        return 2

    print(f"GEMINI-DIAGNOSE: Modell={MODEL}", flush=True)
    print("GEMINI-DIAGNOSE: kleiner GenerateContent-Test wird gesendet.", flush=True)

    try:
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=MODEL,
            contents=PROMPT,
        )

        usage = getattr(response, "usage_metadata", None)
        print("GEMINI-DIAGNOSE: HTTP/API-Aufruf erfolgreich.", flush=True)
        print(f"GEMINI-DIAGNOSE: Antwort={getattr(response, 'text', '')!r}", flush=True)

        if usage is not None:
            print(
                "GEMINI-DIAGNOSE: usage_metadata="
                f"prompt_token_count={getattr(usage, 'prompt_token_count', None)}, "
                f"candidates_token_count={getattr(usage, 'candidates_token_count', None)}, "
                f"total_token_count={getattr(usage, 'total_token_count', None)}",
                flush=True,
            )

        return 0

    except Exception as exc:
        print(
            f"GEMINI-DIAGNOSE: API-FEHLER: {type(exc).__name__}: {exc}",
            flush=True,
        )

        # Falls das SDK einen numerischen Statuscode bereitstellt,
        # zusätzlich explizit ausgeben.
        for attr in ("code", "status_code"):
            value = getattr(exc, attr, None)
            if value is not None:
                print(f"GEMINI-DIAGNOSE: {attr}={value}", flush=True)

        return 1


if __name__ == "__main__":
    sys.exit(main())
