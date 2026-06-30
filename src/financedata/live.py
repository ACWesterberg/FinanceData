"""
Intraday / live price fetching with short-TTL SQLite cache.

get_live_price(ticker, ttl_minutes=10) -> float | None
get_live_prices(tickers, ttl_minutes=10) -> dict[str, float | None]

Prices are returned in the stock's native currency — FX conversion is the
caller's responsibility (use financedata.to_sek / get_fx_rate).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

import yfinance as yf

from .cache import get_cache

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 10


def get_live_price(
    ticker: str,
    ttl_minutes: int = _DEFAULT_TTL,
) -> float | None:
    """
    Return the latest price for ticker in its native currency.

    Tries yfinance fast_info.last_price first, falls back to the last daily bar.
    Cached with ttl_minutes TTL. Pass ttl_minutes=0 to force a fresh fetch.

    Returns None if unavailable. Never converts currency.

    Note: right at market open yfinance may return yesterday's close as
    last_price. Callers that need to distinguish fresh from stale should check
    the price_time exposed via get_live_price_detail().
    """
    cache = get_cache()

    if ttl_minutes > 0:
        cached = cache.get_live_price(ticker, ttl_minutes)
        if cached is not None:
            return cached[0]

    price, price_time = _fetch(ticker)
    if price is None:
        return None

    cache.save_live_price(ticker, price, price_time)
    return price


def get_live_prices(
    tickers: list[str],
    ttl_minutes: int = _DEFAULT_TTL,
) -> dict[str, float | None]:
    """
    Batch live price fetch. Returns {ticker: price | None}.
    Cached results are served from SQLite; only stale/missing tickers hit yfinance.
    """
    cache = get_cache()
    results: dict[str, float | None] = {}
    to_fetch: list[str] = []

    if ttl_minutes > 0:
        for ticker in tickers:
            cached = cache.get_live_price(ticker, ttl_minutes)
            if cached is not None:
                results[ticker] = cached[0]
            else:
                to_fetch.append(ticker)
    else:
        to_fetch = list(tickers)

    for ticker in to_fetch:
        price, price_time = _fetch(ticker)
        results[ticker] = price
        if price is not None:
            cache.save_live_price(ticker, price, price_time)

    return results


def get_live_price_detail(
    ticker: str,
    ttl_minutes: int = _DEFAULT_TTL,
) -> dict | None:
    """
    Like get_live_price but also returns price_time so callers can detect
    stale open-bar prices.

    Returns {"price": float, "price_time": str | None} or None.
    """
    cache = get_cache()

    if ttl_minutes > 0:
        cached = cache.get_live_price(ticker, ttl_minutes)
        if cached is not None:
            return {"price": cached[0], "price_time": cached[1]}

    price, price_time = _fetch(ticker)
    if price is None:
        return None

    cache.save_live_price(ticker, price, price_time)
    return {"price": price, "price_time": price_time}


def _fetch(ticker: str) -> tuple[float | None, str | None]:
    t = yf.Ticker(ticker)

    # fast_info is cheaper than a full history pull
    try:
        price = t.fast_info.last_price
        if price is not None and not math.isnan(float(price)):
            price_time = _fast_info_time(t)
            return float(price), price_time
    except Exception:
        pass

    # Fallback: last bar from recent daily history
    try:
        df = t.history(period="2d", interval="1d", auto_adjust=True)
        if not df.empty:
            price_time = df.index[-1].isoformat()
            return float(df["Close"].iloc[-1]), price_time
    except Exception as exc:
        logger.warning("live price fetch error for %s: %s", ticker, exc)

    return None, None


def _fast_info_time(t: yf.Ticker) -> str | None:
    try:
        for attr in ("last_trade_time", "regular_market_time"):
            rmt = getattr(t.fast_info, attr, None)
            if rmt is not None:
                return rmt.isoformat() if hasattr(rmt, "isoformat") else str(rmt)
    except Exception:
        pass
    return datetime.utcnow().isoformat()
