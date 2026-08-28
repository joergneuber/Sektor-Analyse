#!/usr/bin/env python3
"""
ISOLIERTER PARSER-DIAGNOSETEST v3

Ziel:
Die bestehende _te_calendar_actual()-Logik in makro_szenario.py
gezielt untersuchen, ohne den Produktionscode zu veraendern.

Der Test:
1. liest die Funktion als AST aus der Produktionsdatei,
2. zeigt ihre genaue Quelltextstruktur,
3. extrahiert Regex-/String-Literale,
4. ruft die Funktion weiterhin gegen frisches TE-HTML auf,
5. prueft bekannte reale Marker aus dem vorherigen Lauf.

Keine Werte werden erfunden oder als Actual akzeptiert.
"""

from pathlib import Path
import ast
import importlib.util
import inspect
import re
import sys
import requests

TARGET_FILE = Path("makro_szenario.py")
PMI_URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tradingeconomics.com/",
}


def compact(text, limit=500):
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def source_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def literal_strings(node):
    values = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
    return values


def main():
    print("=== TE ISM SERVICES PARSER DIAGNOSE v3 ===")
    print(f"TARGET_FILE={TARGET_FILE}")
    print(f"REFERENCE={REFERENCE}")
    print(f"URL={PMI_URL}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    source = TARGET_FILE.read_text(encoding="utf-8")

    try:
        tree = ast.parse(source)
    except Exception as exc:
        print(f"SOURCE_SYNTAX_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_SOURCE")
        return 1

    node = source_function(tree, "_te_calendar_actual")

    if node is None:
        print("FUNCTION=_te_calendar_actual NOT_FOUND")
        print("RESULT=RED_FUNCTION_MISSING")
        return 1

    print(f"FUNCTION=FOUND")
    print(f"FUNCTION_LINE_START={node.lineno}")
    print(f"FUNCTION_LINE_END={getattr(node, 'end_lineno', '?')}")

    fn_source = ast.get_source_segment(source, node) or ""
    print(f"FUNCTION_SOURCE_LENGTH={len(fn_source)}")

    print("")
    print("=== FUNCTION SOURCE ===")
    print(fn_source)

    print("")
    print("=== STRING / REGEX LITERALS ===")
    strings = literal_strings(node)

    for i, value in enumerate(strings, 1):
        print(f"LITERAL_{i}={value!r}")

    print("")
    print("=== FRESH TRADING ECONOMICS HTML ===")

    try:
        response = requests.get(
            PMI_URL,
            headers=HEADERS,
            timeout=30,
            allow_redirects=True,
        )
    except Exception as exc:
        print(f"HTTP_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_HTTP")
        return 1

    html = response.text

    print(f"HTTP_STATUS={response.status_code}")
    print(f"FINAL_URL={response.url}")
    print(f"HTML_LENGTH={len(html)}")

    print("")
    print("=== KNOWN TE MARKER CHECKS ===")

    markers = [
        "54.1",
        "54.10",
        "57.2",
        "47.4",
        "70.3",
        "2026-08-05",
        "Jul 2026",
        "July 2026",
        "ISM Services PMI",
        "Non Manufacturing PMI",
    ]

    for marker in markers:
        count = html.lower().count(marker.lower())
        print(f"MARKER={marker!r} HITS={count}")

        if count:
            pos = html.lower().find(marker.lower())
            print(
                "CONTEXT="
                + compact(html[max(0, pos - 350):pos + 650], 1000)
            )

    print("")
    print("=== PRODUCTION PARSER CALL ===")

    spec = importlib.util.spec_from_file_location(
        "macro_under_test",
        TARGET_FILE,
    )

    if spec is None or spec.loader is None:
        print("RESULT=RED_IMPORT_SPEC")
        return 1

    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"IMPORT_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_IMPORT")
        return 1

    parser = getattr(module, "_te_calendar_actual", None)

    if parser is None:
        print("RESULT=RED_FUNCTION_MISSING")
        return 1

    try:
        result = parser(html, REFERENCE)
        print(f"PARSER_RETURN={result!r}")
    except Exception as exc:
        print(f"PARSER_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_PARSER_EXCEPTION")
        return 1

    print("")
    print("=== DIRECT FUNCTION CODE INSPECTION ===")

    try:
        runtime_source = inspect.getsource(parser)
        print(runtime_source)
    except Exception as exc:
        print(f"RUNTIME_SOURCE_ERROR={type(exc).__name__}: {exc}")

    print("")
    print("=== DIAGNOSIS RESULT ===")
    if result is None:
        print("PARSER_CURRENTLY_RETURNS=None")
        print("KNOWN_TE_MARKERS_PRESENT=True/see marker counts above")
        print("RESULT=GREEN_PARSER_BUG_REPRODUCED")
        return 0

    print("PARSER_CURRENTLY_RETURNS_NON_NONE")
    print("RESULT=GREEN_PARSER_BEHAVIOR_CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
