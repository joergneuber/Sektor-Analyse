"""
Isolierter Gemini-API-Belastungstest.

Teststufe:
    ca. 50.000 Input-Tokens

Bewusst KEIN:
- Produktionscode
- Quota-Scheduler
- Retry
- Modellwechsel
- A1/A2/A3-Kontext
- File-Upload

Es wird genau EIN generate_content()-Request gesendet.
"""

import os
import sys
from google import genai


MODEL = os.getenv("GEMINI_DIAGNOSE_MODEL", "gemini-3.5-flash")

# Zielgröße des Tests.
TARGET_WORDS = 38_000


def build_test_prompt() -> str:
    """
    Erzeugt deterministischen, aber inhaltlich einfachen Testtext.
    Die Wiederholung ist absichtlich gewählt: Wir testen die
    Request-Größe, nicht die fachliche Verarbeitung.
    """
    block = (
        "Dies ist ein kontrollierter Gemini API Belastungstest. "
        "Der Text dient ausschließlich dazu, einen Request mit "
        "ungefähr fünfzigtausend Input-Tokens zu erzeugen. "
        "Die Daten enthalten keine Trading- oder Projektdaten. "
        "Bitte verarbeite den gesamten bereitgestellten Kontext. "
    )

    parts = []
    while len(" ".join(parts).split()) < TARGET_WORDS:
        parts.append(block)

    return "\n".join(parts)


def main() -> int:
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print(
            "FEHLER: GEMINI_API_KEY ist nicht gesetzt.",
            flush=True,
        )
        return 2

    print(
        f"GEMINI-DIAGNOSE: Modell={MODEL}",
        flush=True,
    )

    print(
        f"GEMINI-DIAGNOSE: Zielgröße ungefähr {TARGET_WORDS:,} Wörter.",
        flush=True,
    )

    prompt = build_test_prompt()

    word_count = len(prompt.split())
    char_count = len(prompt)

    print(
        f"GEMINI-DIAGNOSE: erzeugte Wörter={word_count:,}",
        flush=True,
    )

    print(
        f"GEMINI-DIAGNOSE: erzeugte Zeichen={char_count:,}",
        flush=True,
    )

    try:
        client = genai.Client(api_key=api_key)

        # Nur Messung der tatsächlichen Tokenzahl.
        # Diese Messung wird NICHT an generate_content() gesendet.
        token_info = client.models.count_tokens(
            model=MODEL,
            contents=prompt,
        )

        measured_tokens = getattr(
            token_info,
            "total_tokens",
            None,
        )

        print(
            f"GEMINI-DIAGNOSE: count_tokens={measured_tokens}",
            flush=True,
        )

        print(
            "GEMINI-DIAGNOSE: jetzt EINMALIG generate_content()...",
            flush=True,
        )

        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
        )

        print(
            "GEMINI-DIAGNOSE: generate_content() erfolgreich.",
            flush=True,
        )

        response_text = getattr(response, "text", "")

        print(
            f"GEMINI-DIAGNOSE: Antwort={response_text!r}",
            flush=True,
        )

        usage = getattr(
            response,
            "usage_metadata",
            None,
        )

        if usage is not None:
            print(
                "GEMINI-DIAGNOSE: usage_metadata="
                f"prompt_token_count="
                f"{getattr(usage, 'prompt_token_count', None)}, "
                f"candidates_token_count="
                f"{getattr(usage, 'candidates_token_count', None)}, "
                f"total_token_count="
                f"{getattr(usage, 'total_token_count', None)}",
                flush=True,
            )

        return 0

    except Exception as exc:
        print(
            "GEMINI-DIAGNOSE: API-FEHLER: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        for attr in (
            "code",
            "status_code",
        ):
            value = getattr(
                exc,
                attr,
                None,
            )

            if value is not None:
                print(
                    f"GEMINI-DIAGNOSE: {attr}={value}",
                    flush=True,
                )

        return 1


if __name__ == "__main__":
    sys.exit(main())