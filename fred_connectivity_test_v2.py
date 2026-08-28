#!/usr/bin/env python3
"""
Isolierter FRED-Konnektivitätstest.
Nur Python-Standardbibliothek: keine externen Pakete erforderlich.
"""

import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TIMEOUT = 10

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

TESTS = [
    ("FRED_ROOT", "https://fred.stlouisfed.org/"),
    ("FRED_SERIES_EXAMPLE", "https://fred.stlouisfed.org/series/CPIAUCSL"),
    ("FRED_API_ROOT", "https://api.stlouisfed.org/"),
]


def fetch(name, url):
    print(f"--- {name} ---", flush=True)
    print(f"URL={url}", flush=True)

    started = time.monotonic()

    request = Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(4096)
            elapsed = time.monotonic() - started

            print(f"HTTP_STATUS={response.status}", flush=True)
            print(f"FINAL_URL={response.geturl()}", flush=True)
            print(f"CONTENT_SAMPLE_BYTES={len(body)}", flush=True)
            print(f"SECONDS={elapsed:.2f}", flush=True)

            return True

    except HTTPError as exc:
        elapsed = time.monotonic() - started
        print("HTTP_STATUS=ERROR", flush=True)
        print(f"ERROR_TYPE=HTTPError", flush=True)
        print(f"HTTP_CODE={exc.code}", flush=True)
        print(f"ERROR={exc}", flush=True)
        print(f"SECONDS={elapsed:.2f}", flush=True)
        return False

    except URLError as exc:
        elapsed = time.monotonic() - started
        print("HTTP_STATUS=ERROR", flush=True)
        print(f"ERROR_TYPE=URLError", flush=True)
        print(f"ERROR={exc}", flush=True)
        print(f"SECONDS={elapsed:.2f}", flush=True)
        return False

    except TimeoutError as exc:
        elapsed = time.monotonic() - started
        print("HTTP_STATUS=ERROR", flush=True)
        print("ERROR_TYPE=TimeoutError", flush=True)
        print(f"ERROR={exc}", flush=True)
        print(f"SECONDS={elapsed:.2f}", flush=True)
        return False

    except Exception as exc:
        elapsed = time.monotonic() - started
        print("HTTP_STATUS=ERROR", flush=True)
        print(f"ERROR_TYPE={type(exc).__name__}", flush=True)
        print(f"ERROR={exc}", flush=True)
        print(f"SECONDS={elapsed:.2f}", flush=True)
        return False

    finally:
        print("", flush=True)


def main():
    print("=== FRED DIRECT CONNECTIVITY TEST v2 ===", flush=True)
    print(f"TIMEOUT={TIMEOUT}s", flush=True)
    print("DEPENDENCIES=PYTHON_STANDARD_LIBRARY_ONLY", flush=True)
    print("MODE=NETWORK_ONLY_NO_PRODUCTION_CHANGES", flush=True)
    print("", flush=True)

    results = []
    started = time.monotonic()

    for name, url in TESTS:
        results.append(fetch(name, url))

    elapsed = time.monotonic() - started
    successful = sum(results)

    print("=== SUMMARY ===", flush=True)
    print(f"SUCCESSFUL_REQUESTS={successful}/{len(results)}", flush=True)
    print(f"TOTAL_SECONDS={elapsed:.2f}", flush=True)

    if successful == len(results):
        print("RESULT=GREEN_FRED_CONNECTIVITY", flush=True)
        return 0

    print("RESULT=RED_FRED_CONNECTIVITY", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
