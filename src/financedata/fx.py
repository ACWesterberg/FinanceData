"""
FX rate fetching and conversion, cached in the shared SQLite store.

get_fx_rate(base, quote="SEK", *, on=None) -> float | None
to_sek(amount, currency, *, on=None) -> float | None
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import yfinance as yf

from .cache import get_cache

logger = logging.getLogger(__name__)

# In-process memo: "{BASEQUOTE}:{date}" -> rate
_memo: dict[str, float] = {}


def get_fx_rate(
    base: str,
    quote: str = "SEK",
    *,
    on: str | None = None,
) -> float | None:
    """
    Return the exchange rate base→quote (e.g. get_fx_rate("DKK") → ~1.48).

    on=None        → latest spot, same-day cache TTL
    on="YYYY-MM-DD" → that day's close, cached permanently

    Returns None if the rate can't be resolved — never silently returns 1.0.
    base == quote returns 1.0 (no network call).
    """
    base = base.upper()
    quote = quote.upper()

    if base == quote:
        return 1.0

    pair = f"{base}{quote}"
    date = on or datetime.utcnow().strftime("%Y-%m-%d")
    memo_key = f"{pair}:{date}"

    if memo_key in _memo:
        return _memo[memo_key]

    cache = get_cache()
    cached = cache.get_fx_rate(pair, date, spot=(on is None))
    if cached is not None:
        _memo[memo_key] = cached
        return cached

    rate = _fetch_yf(pair, on)
    if rate is None:
        return None

    cache.save_fx_rate(pair, date, rate)
    _memo[memo_key] = rate
    return rate


def to_sek(amount: float, currency: str, *, on: str | None = None) -> float | None:
    """
    Convert amount in currency to SEK.
    Returns None if the rate is unavailable — never silently mis-converts.
    """
    rate = get_fx_rate(currency, "SEK", on=on)
    if rate is None:
        return None
    return amount * rate


def _fetch_yf(pair: str, on: str | None) -> float | None:
    ticker_sym = f"{pair}=X"
    try:
        t = yf.Ticker(ticker_sym)
        if on:
            end = (datetime.strptime(on, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            df = t.history(start=on, end=end, interval="1d", auto_adjust=True)
        else:
            df = t.history(period="2d", interval="1d", auto_adjust=True)

        if df.empty:
            logger.warning("FX: no data returned for %s (on=%s)", pair, on)
            return None

        rate = float(df["Close"].iloc[-1])
        if math.isnan(rate):
            return None
        return rate
    except Exception as exc:
        logger.warning("FX fetch error for %s: %s", pair, exc)
        return None
