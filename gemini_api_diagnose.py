import os
import sys
import time

from google import genai


MODEL = "gemini-3.5-flash-lite"

TARGET_TOKENS = 150_000
MIN_TOKENS = 145_000
MAX_TOKENS = 155_000


def count_tokens(client, text):
    result = client.models.count_tokens(
        model=MODEL,
        contents=text,
    )
    return result.total_tokens


def main():
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        print("FEHLER: GEMINI_API_KEY ist nicht gesetzt.")
        sys.exit(1)

    print("=" * 70)
    print("GEMINI FLASH-LITE 150K DIAGNOSETEST")
    print("=" * 70)
    print(f"Modell: {MODEL}")
    print(f"Ziel: {TARGET_TOKENS:,} Input-Tokens")
    print(f"zulässiger Bereich: {MIN_TOKENS:,}–{MAX_TOKENS:,}")
    print()

    client = genai.Client(api_key=api_key)

    # Ein bewusst kurzer, deterministischer Textblock.
    # Die tatsächliche Tokenzahl wird NICHT geschätzt,
    # sondern mit count_tokens() kontrolliert.
    block = (
        "Dies ist ein deterministischer Gemini-Diagnosetest. "
        "Der Inhalt dient ausschließlich zur Erzeugung eines "
        "definierten großen Input-Kontextes. "
        "Es handelt sich nicht um echte Projektdaten. "
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ "
        "abcdefghijklmnopqrstuvwxyz "
        "0123456789 "
        "MARKET DATA TEST "
        "TECHNICAL ANALYSIS TEST "
        "HISTORICAL DATA TEST "
    )

    # Start deutlich unterhalb des Ziels.
    repetitions = 1000

    print("1. Aufbau des Testinputs")
    print("-" * 70)

    # Wir erhöhen schrittweise und kontrollieren jedes Mal die
    # tatsächliche Tokenzahl über count_tokens().
    for iteration in range(20):
        text = block * repetitions

        start = time.time()

        try:
            token_count = count_tokens(client, text)
        except Exception as exc:
            print("COUNT_TOKENS_FEHLER")
            print(type(exc).__name__)
            print(str(exc))
            sys.exit(2)

        elapsed = time.time() - start

        print(
            f"Iteration {iteration + 1:02d}: "
            f"{token_count:,} Tokens "
            f"({repetitions:,} Wiederholungen, "
            f"{elapsed:.2f}s)"
        )

        if MIN_TOKENS <= token_count <= MAX_TOKENS:
            break

        if token_count < TARGET_TOKENS:
            # proportional hochskalieren
            factor = TARGET_TOKENS / max(token_count, 1)

            # etwas konservativer aufrunden
            repetitions = max(
                repetitions + 1,
                int(repetitions * factor * 0.98),
            )
        else:
            # proportional reduzieren
            factor = TARGET_TOKENS / token_count

            repetitions = max(
                1,
                int(repetitions * factor * 0.98),
            )

    else:
        print()
        print("FEHLER: Zielbereich konnte nicht erreicht werden.")
        print(f"Letzte Tokenzahl: {token_count:,}")
        sys.exit(3)

    print()
    print(f"Finale Input-Tokens: {token_count:,}")
    print(f"Finale Textlänge: {len(text):,} Zeichen")
    print()

    # Absolute Sicherheitsprüfung.
    # Niemals einen Request außerhalb unseres geplanten Bereichs senden.
    if token_count < MIN_TOKENS or token_count > MAX_TOKENS:
        print("FEHLER: Input liegt außerhalb des erlaubten Bereichs.")
        sys.exit(4)

    print("2. EINMALIGER generate_content()-Aufruf")
    print("-" * 70)
    print(f"Modell: {MODEL}")
    print(f"Input-Tokens: {token_count:,}")
    print()
    print("Kein Retry")
    print("Kein Fallback")
    print("Kein Quota-Scheduler")
    print("Kein Modellwechsel")
    print()

    start = time.time()

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=text,
        )

        elapsed = time.time() - start

        print("GENERATE_CONTENT_ERFOLG")
        print(f"Dauer: {elapsed:.2f}s")
        print(f"Modell angefordert: {MODEL}")

        usage = getattr(response, "usage_metadata", None)

        if usage is not None:
            print()
            print("Usage-Metadaten:")
            print(usage)

        response_text = getattr(response, "text", None)

        if response_text:
            print()
            print("Antwortanfang:")
            print(response_text[:500])

        print()
        print("=" * 70)
        print("TEST ERFOLGREICH")
        print("=" * 70)

    except Exception as exc:
        elapsed = time.time() - start

        print("GENERATE_CONTENT_FEHLER")
        print(f"Dauer bis Fehler: {elapsed:.2f}s")
        print(f"Modell: {MODEL}")
        print(f"Input-Tokens: {token_count:,}")
        print(f"Exception-Typ: {type(exc).__name__}")
        print(f"Fehlermeldung: {exc}")

        for attribute in (
            "code",
            "status",
            "details",
            "response",
        ):
            value = getattr(exc, attribute, None)
            if value is not None:
                print(f"{attribute}: {value!r}")

        print()
        print("=" * 70)
        print("TEST MIT API-FEHLER BEENDET")
        print("=" * 70)

        sys.exit(5)


if __name__ == "__main__":
    main()