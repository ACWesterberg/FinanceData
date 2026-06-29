"""
OHLCV price fetching with SQLite caching.

Priority:
  Nordic tickers → Alpha Vantage (25/day free tier) → yfinance fallback
  US tickers     → yfinance batch download

All results are cached; stale-check is per-ticker per day.

Environment:
  ALPHA_VANTAGE_KEY  — optional; enables AV for Nordic tickers
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pandas as pd
import yfinance as yf

from .cache import get_cache

logger = logging.getLogger(__name__)

_AV_FREE_TIER_DAILY_LIMIT = 25
_AV_CACHE_TTL_HOURS = 4

# In-memory short-TTL caches (per-process; fine for intraday use)
_av_mem: dict[str, tuple[datetime, pd.DataFrame]] = {}
_vix_cache: tuple[Optional[datetime], Optional[float]] = (None, None)
_sector_mem: dict[str, str] = {}


# ── Column normalisation ──────────────────────────────────────────────────────

def _std_cols(df: pd.DataFrame) -> pd.DataFrame:
    rename = {c: c.capitalize() for c in df.columns if c.lower() in ("open", "high", "low", "close", "volume")}
    df = df.rename(columns=rename)
    cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    return df[cols].copy()


# ── Alpha Vantage ─────────────────────────────────────────────────────────────

def _fetch_alpha_vantage(ticker: str) -> Optional[pd.DataFrame]:
    api_key = os.environ.get("ALPHA_VANTAGE_KEY", "")
    if not api_key:
        return None

    cache = get_cache()
    now = datetime.utcnow()

    cached = _av_mem.get(ticker)
    if cached:
        ts, df = cached
        if (now - ts).total_seconds() < _AV_CACHE_TTL_HOURS * 3600:
            return df.copy()

    daily_count = cache.get_rate_count("alpha_vantage")
    if daily_count >= _AV_FREE_TIER_DAILY_LIMIT:
        logger.warning("Alpha Vantage daily limit reached (%d) — skipping %s", _AV_FREE_TIER_DAILY_LIMIT, ticker)
        return None

    symbol = ticker.split(".")[0]
    url = (
        "https://www.alphavantage.co/query"
        f"?function=TIME_SERIES_DAILY_ADJUSTED&symbol={symbol}&outputsize=full&apikey={api_key}"
    )
    try:
        cache.increment_rate_count("alpha_vantage")
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        ts_key = "Time Series (Daily)"
        if ts_key not in data:
            note = data.get("Note") or data.get("Information", "")
            logger.warning("Alpha Vantage unexpected response for %s: %s", ticker, note[:120])
            return None

        rows = [
            {
                "Date": pd.to_datetime(d),
                "Open": float(v["1. open"]),
                "High": float(v["2. high"]),
                "Low": float(v["3. low"]),
                "Close": float(v["5. adjusted close"]),
                "Volume": float(v["6. volume"]),
            }
            for d, v in data[ts_key].items()
        ]
        df = pd.DataFrame(rows).set_index("Date").sort_index()
        _av_mem[ticker] = (now, df)
        return df
    except Exception as exc:
        logger.error("Alpha Vantage error for %s: %s", ticker, exc)
        return None


# ── yfinance helpers ──────────────────────────────────────────────────────────

def _yf_single(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        return df if not df.empty else None
    except Exception as exc:
        logger.error("yfinance error for %s: %s", ticker, exc)
        return None


def _yf_batch(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers,
            period=period,
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.error("yfinance batch error: %s", exc)
        return {}

    results: dict[str, pd.DataFrame] = {}
    if len(tickers) == 1:
        df = _std_cols(raw)
        if not df.empty:
            results[tickers[0]] = df
        return results

    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
            df = _std_cols(df)
            if not df.empty:
                results[t] = df
        except Exception:
            pass
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def get_prices(
    ticker: str,
    market: str = "us",
    period: str = "1y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV for a single ticker. Checks SQLite cache first.
    Nordic tickers try Alpha Vantage first, then yfinance.
    Returns a DataFrame with Open/High/Low/Close/Volume columns or None.
    """
    cache = get_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cached_date = cache.latest_price_date(ticker)

    if cached_date and cached_date >= today:
        rows = cache.get_prices(ticker)
        if rows:
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df.pop("date"))
            return df.rename(columns=str.capitalize)

    df: Optional[pd.DataFrame] = None
    if market == "nordic":
        yf_ticker = ticker.replace(".STO", ".ST") if ".STO" in ticker else ticker
        df = _fetch_alpha_vantage(yf_ticker)
        if df is None or df.empty:
            df = _yf_single(yf_ticker, period, interval)
    else:
        df = _yf_single(ticker, period, interval)

    if df is None or df.empty:
        return None

    df = _std_cols(df).sort_index()
    _persist_df(ticker, df, cache)
    return df


def get_prices_batch(
    tickers: list[str],
    market: str = "us",
    period: str = "1y",
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Batch-fetch OHLCV for many tickers using a single yf.download call.
    Results are persisted to the cache. Returns {ticker: DataFrame}.
    """
    cache = get_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    to_fetch = tickers if force_refresh else [
        t for t in tickers
        if (cache.latest_price_date(t) or "") < today
    ]

    results: dict[str, pd.DataFrame] = {}

    # Serve already-cached tickers from DB
    for t in tickers:
        if t not in to_fetch:
            rows = cache.get_prices(t)
            if rows:
                df = pd.DataFrame(rows)
                df.index = pd.to_datetime(df.pop("date"))
                results[t] = df.rename(columns=str.capitalize)

    if not to_fetch:
        return results

    batch = _yf_batch(to_fetch, period=period)
    for t, df in batch.items():
        _persist_df(t, df, cache)
        results[t] = df

    return results


def get_prices_since(
    tickers: list[str],
    since: str,
    force_refresh: bool = False,
) -> dict[str, bool]:
    """
    Ensure prices are loaded back to `since` (YYYY-MM-DD) for each ticker.
    Returns {ticker: success}. Primarily used by Fond-style weekly runs.
    """
    cache = get_cache()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    to_fetch = []
    for t in tickers:
        if force_refresh:
            to_fetch.append(t)
            continue
        latest = cache.latest_price_date(t)
        if latest is None or latest < today:
            to_fetch.append(t)

    if not to_fetch:
        return {t: True for t in tickers}

    lookback_days = (datetime.utcnow() - datetime.strptime(since, "%Y-%m-%d")).days + 10
    period = f"{lookback_days}d"

    results: dict[str, bool] = {}
    try:
        raw = yf.download(
            to_fetch,
            start=since,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.error("yfinance download error: %s", exc)
        return {t: False for t in to_fetch}

    if raw.empty:
        return {t: False for t in to_fetch}

    # Handle yfinance MultiIndex layout differences
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = raw.columns.get_level_values(0).unique().tolist()
        closes_df = raw["Close"] if "Close" in lvl0 else raw.swaplevel(axis=1)["Close"]
    else:
        closes_df = raw[["Close"]].rename(columns={"Close": to_fetch[0]})

    for sym in to_fetch:
        try:
            if sym not in closes_df.columns:
                results[sym] = False
                continue
            series = closes_df[sym].dropna()
            if series.empty:
                results[sym] = False
                continue

            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    lvl0 = raw.columns.get_level_values(0).unique().tolist()
                    df_sym = raw.xs(sym, axis=1, level=1) if "Close" in lvl0 else raw.xs(sym, axis=1, level=0)
                else:
                    df_sym = raw.copy()
                if isinstance(df_sym.columns, pd.MultiIndex):
                    df_sym.columns = df_sym.columns.get_level_values(0)
            except Exception:
                df_sym = series.to_frame("Close")

            if "Close" in df_sym.columns:
                df_sym = df_sym[~df_sym["Close"].isna()]
            df_sym.index = df_sym.index.strftime("%Y-%m-%d")

            rows = [
                {
                    "date": str(idx),
                    "open": float(row["Open"]) if "Open" in row and not pd.isna(row["Open"]) else None,
                    "high": float(row["High"]) if "High" in row and not pd.isna(row["High"]) else None,
                    "low": float(row["Low"]) if "Low" in row and not pd.isna(row["Low"]) else None,
                    "close": float(row["Close"]) if not pd.isna(row["Close"]) else None,
                    "volume": float(row["Volume"]) if "Volume" in row and not pd.isna(row["Volume"]) else None,
                }
                for idx, row in df_sym.iterrows()
                if "Close" in row and not pd.isna(row["Close"])
            ]
            if rows:
                get_cache().save_prices(sym, rows)
                results[sym] = True
            else:
                results[sym] = False
        except Exception:
            results[sym] = False

    for t in tickers:
        if t not in results:
            results[t] = get_cache().latest_price_date(t) is not None

    return results


def get_current_price(ticker: str, market: str = "us") -> Optional[float]:
    df = get_prices(ticker, market=market, period="5d")
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def get_vix() -> Optional[float]:
    global _vix_cache
    ts, val = _vix_cache
    if ts and (datetime.utcnow() - ts).total_seconds() < 3600:
        return val
    try:
        df = yf.Ticker("^VIX").history(period="2d", interval="1d")
        val = float(df["Close"].iloc[-1]) if not df.empty else None
    except Exception as exc:
        logger.warning("VIX fetch error: %s", exc)
        val = None
    _vix_cache = (datetime.utcnow(), val)
    return val


def get_sector(ticker: str) -> str:
    if ticker in _sector_mem:
        return _sector_mem[ticker]
    yf_ticker = ticker.replace(".STO", ".ST") if ".STO" in ticker else ticker
    try:
        sector = yf.Ticker(yf_ticker).info.get("sector") or "Unknown"
    except Exception:
        sector = "Unknown"
    _sector_mem[ticker] = sector
    return sector


# ── Internal helpers ──────────────────────────────────────────────────────────

def _persist_df(ticker: str, df: pd.DataFrame, cache) -> None:
    """Write a standardised OHLCV DataFrame to the price cache."""
    df_str = df.copy()
    df_str.index = df_str.index.strftime("%Y-%m-%d")
    rows = [
        {
            "date": str(idx),
            "open": float(row["Open"]) if "Open" in row and not pd.isna(row.get("Open")) else None,
            "high": float(row["High"]) if "High" in row and not pd.isna(row.get("High")) else None,
            "low": float(row["Low"]) if "Low" in row and not pd.isna(row.get("Low")) else None,
            "close": float(row["Close"]) if not pd.isna(row.get("Close")) else None,
            "volume": float(row["Volume"]) if "Volume" in row and not pd.isna(row.get("Volume")) else None,
        }
        for idx, row in df_str.iterrows()
        if "Close" in row and not pd.isna(row.get("Close"))
    ]
    if rows:
        cache.save_prices(ticker, rows)
