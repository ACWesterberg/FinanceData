"""
Fundamentals cache: valuation, quality, growth, and analyst data from yfinance .info.
Fetched in parallel threads; cached in SQLite with a 7-day TTL.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import yfinance as yf

from .cache import get_cache

logger = logging.getLogger(__name__)

# yfinance info key → our cache key
_FIELD_MAP: dict[str, str] = {
    "trailingPE":                       "pe_ratio",
    "forwardPE":                        "forward_pe",
    "priceToBook":                      "pb_ratio",
    "enterpriseToEbitda":               "ev_to_ebitda",
    "priceToSalesTrailingTwelveMonths": "price_to_sales",
    "marketCap":                        "market_cap",
    "profitMargins":                    "profit_margin",
    "grossMargins":                     "gross_margin",
    "operatingMargins":                 "operating_margin",
    "ebitdaMargins":                    "ebitda_margin",
    "returnOnEquity":                   "roe",
    "returnOnAssets":                   "roa",
    "freeCashflow":                     "free_cash_flow",
    "operatingCashflow":                "operating_cash_flow",
    "totalDebt":                        "total_debt",
    "totalCash":                        "total_cash",
    "debtToEquity":                     "debt_to_equity",
    "revenueGrowth":                    "revenue_growth",
    "earningsGrowth":                   "earnings_growth",
    "beta":                             "beta",
    "fiftyTwoWeekHigh":                 "fifty_two_week_high",
    "fiftyTwoWeekLow":                  "fifty_two_week_low",
    "dividendYield":                    "dividend_yield",
    "targetMeanPrice":                  "analyst_target_price",
    "numberOfAnalystOpinions":          "analyst_count",
    "currency":                         "currency",
    "earningsTimestamp":                "earnings_timestamp",
    "exDividendDate":                   "ex_div_timestamp",
    "website":                          "website",
    "sector":                           "sector",
}

# Fields yfinance returns as a 0-1 fraction rather than a percentage. Values are
# cached raw, so a consumer rendering them as percentages multiplies by 100.
_FRACTION_FIELDS = {
    "profit_margin", "gross_margin", "operating_margin", "ebitda_margin",
    "roe", "roa", "revenue_growth", "earnings_growth", "dividend_yield",
}


def ts_to_days(ts) -> int | None:
    """Convert Unix timestamp to calendar days from today (None if in the past)."""
    if ts is None:
        return None
    try:
        today = datetime.utcnow().date()
        target = datetime.utcfromtimestamp(float(ts)).date()
        delta = (target - today).days
        return delta if delta >= 0 else None
    except (TypeError, ValueError, OSError):
        return None


def _safe(val) -> float | None:
    try:
        if val is None:
            return None
        f = float(val)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _fetch_one(ticker: str) -> tuple[str, dict | None]:
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("quoteType") == "NONE":
            return ticker, None
        data: dict = {}
        for yf_key, our_key in _FIELD_MAP.items():
            raw = info.get(yf_key)
            if our_key in ("currency", "website", "sector"):
                data[our_key] = raw if isinstance(raw, str) else None
            elif our_key == "analyst_count":
                data[our_key] = int(raw) if raw is not None else None
            else:
                data[our_key] = _safe(raw)
        return ticker, data
    except Exception as exc:
        logger.debug("fundamentals fetch error for %s: %s", ticker, exc)
        return ticker, None


def get_fundamentals(
    tickers: list[str],
    ttl_days: int = 7,
    max_workers: int = 12,
) -> dict[str, dict]:
    """
    Return fundamentals for all tickers. Stale/missing entries are refreshed
    via parallel yfinance calls.

    Returns {ticker: {pe_ratio, forward_pe, pb_ratio, ...}} — raw stored values.
    Fraction fields (profit_margin, roe, etc.) are stored as decimals (0.15 = 15%).
    """
    cache = get_cache()
    stale = cache.get_stale_fundamentals(tickers, ttl_days=ttl_days)

    if stale:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, t): t for t in stale}
            for fut in as_completed(futures):
                ticker, data = fut.result()
                if data is not None:
                    cache.save_fundamentals(ticker, data)

    return cache.get_all_fundamentals(tickers)
