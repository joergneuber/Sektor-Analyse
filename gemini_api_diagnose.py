import os
import sys
import time

from google import genai


MODEL = "gemini-3.5-flash-lite"
TARGET_TOKENS = 150_000


def main():
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        print("FEHLER: GEMINI_API_KEY ist nicht gesetzt.")
        sys.exit(1)

    print("=" * 70)
    print("GEMINI API DIAGNOSETEST")
    print("=" * 70)
    print(f"Modell: {MODEL}")
    print(f"Zielgröße: ca. {TARGET_TOKENS:,} Input-Tokens")
    print()

    client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------
    # Großen, deterministischen Testinput erzeugen.
    # Keine echten Projektdaten notwendig.
    # ------------------------------------------------------------
    block = (
        "Dies ist ein deterministischer API-Diagnosetest. "
        "Der Text dient ausschließlich dazu, einen großen Input-Kontext "
        "für Gemini zu erzeugen. Die inhaltliche Aussage dieses Textes "
        "ist für den Test nicht relevant. "
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ "
        "abcdefghijklmnopqrstuvwxyz "
        "0123456789 "
    )

    # Sicherheitsreserve: count_tokens() bestimmt die tatsächliche Größe.
    text = block * 180_000

    print("1. count_tokens()")
    print("-" * 70)

    start = time.time()

    try:
        token_result = client.models.count_tokens(
            model=MODEL,
            contents=text,
        )
    except Exception as exc:
        print("COUNT_TOKENS_FEHLER")
        print(type(exc).__name__)
        print(str(exc))
        sys.exit(2)

    elapsed = time.time() - start

    token_count = getattr(token_result, "total_tokens", None)

    print(f"Gemeldete Input-Tokens: {token_count}")
    print(f"Dauer count_tokens(): {elapsed:.2f}s")
    print()

    if token_count is None:
        print("FEHLER: total_tokens konnte nicht ermittelt werden.")
        print(repr(token_result))
        sys.exit(3)

    # ------------------------------------------------------------
    # Falls die erzeugte Menge zu klein ist, nicht künstlich
    # mehrfach senden. Der Test soll genau EINEN Request machen.
    # ------------------------------------------------------------
    if token_count < 140_000:
        print(
            f"WARNUNG: Input liegt mit {token_count:,} Tokens "
            "unterhalb des erwarteten Testbereichs."
        )

    if token_count > 170_000:
        print(
            f"WARNUNG: Input liegt mit {token_count:,} Tokens "
            "oberhalb des erwarteten Testbereichs."
        )

    print("2. EINMALIGER generate_content()-Aufruf")
    print("-" * 70)
    print(f"Modell VOR Request: {MODEL}")
    print("Kein Retry")
    print("Kein Fallback")
    print("Kein Scheduler")
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

        # Antwort nur sehr kurz ausgeben.
        response_text = getattr(response, "text", None)

        if response_text:
            print()
            print("Antwortanfang:")
            print(response_text[:500])

        # Nutzungsinformationen ausgeben, sofern vorhanden.
        usage = getattr(response, "usage_metadata", None)

        if usage is not None:
            print()
            print("Usage-Metadaten:")
            print(usage)

        print()
        print("=" * 70)
        print("TEST ERFOLGREICH ABGESCHLOSSEN")
        print("=" * 70)

    except Exception as exc:
        elapsed = time.time() - start

        print("GENERATE_CONTENT_FEHLER")
        print(f"Dauer bis Fehler: {elapsed:.2f}s")
        print(f"Modell angefordert: {MODEL}")
        print(f"Exception-Typ: {type(exc).__name__}")
        print(f"Fehlermeldung: {exc}")

        # Zusätzliche Attribute des Google-Fehlers ausgeben,
        # soweit vorhanden.
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

        sys.exit(4)


if __name__ == "__main__":
    main()