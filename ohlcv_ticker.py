"""Shared canonical OHLCV ticker normalization and validation."""
from __future__ import annotations

import re


def normalize_market_ticker(value: object) -> str | None:
    """Return a canonical exchange symbol; reject prose/name fragments."""
    ticker = str(value or "").strip().upper()
    if not ticker or len(ticker) > 30 or any(ch.isspace() for ch in ticker):
        return None
    if not re.fullmatch(r"(?:\^[A-Z0-9][A-Z0-9._=/\-]{0,29}|[A-Z0-9][A-Z0-9._=/\-]{0,29})", ticker):
        return None
    return ticker
