"""
Macro context: central bank rates (FRED, Riksbank, ECB) + equity/commodity/FX
indicators via yfinance. Results are cached in SQLite with a 6-hour TTL.

Environment:
  FRED_API_KEY — optional; enables US FRED series (FEDFUNDS, CPI, 10Y, UNRATE)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import yfinance as yf

from .cache import get_cache

logger = logging.getLogger(__name__)

_MACRO_CACHE_TTL_HOURS = 6.0

# ── yfinance macro indicators ─────────────────────────────────────────────────

@dataclass
class Indicator:
    label: str
    category: str       # index | commodity | fx | rate
    unit: str = ""
    price: Optional[float] = None
    change_5d_pct: Optional[float] = None


_INDICATOR_DEFS: list[tuple[str, str, str, str]] = [
    ("^GSPC",    "S&P 500",      "index",     ""),
    ("^IXIC",    "Nasdaq",       "index",     ""),
    ("^GDAXI",   "DAX",          "index",     ""),
    ("^OMXSPI",  "OMXSPI",       "index",     ""),
    ("BZ=F",     "Brent crude",  "commodity", "$/bbl"),
    ("GC=F",     "Gold",         "commodity", "$/oz"),
    ("HG=F",     "Copper",       "commodity", "$/lb"),
    ("EURSEK=X", "EUR/SEK",      "fx",        ""),
    ("USDSEK=X", "USD/SEK",      "fx",        ""),
    ("NOKSEK=X", "NOK/SEK",      "fx",        ""),
    ("^TNX",     "US 10Y yield", "rate",      "%"),
    ("^VIX",     "VIX",          "index",     ""),
]


def fetch_macro_indicators() -> list[Indicator]:
    """Fetch current prices and 5-day changes for key macro indicators via yfinance."""
    symbols = [s for s, *_ in _INDICATOR_DEFS]
    result: list[Indicator] = []
    try:
        data = yf.download(symbols, period="10d", progress=False, auto_adjust=True)
        closes = data["Close"] if "Close" in data.columns else data
    except Exception:
        return result

    for symbol, label, category, unit in _INDICATOR_DEFS:
        ind = Indicator(label=label, category=category, unit=unit)
        try:
            series = closes[symbol].dropna()
            if len(series) >= 2:
                ind.price = float(series.iloc[-1])
                lookback = min(5, len(series) - 1)
                base = float(series.iloc[-1 - lookback])
                if base != 0:
                    ind.change_5d_pct = (ind.price - base) / base * 100
        except Exception:
            pass
        result.append(ind)
    return result


# ── Central bank rates ────────────────────────────────────────────────────────

def _fetch_fred(series_id: str) -> Optional[float]:
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        return None
    since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}"
        f"&file_type=json&sort_order=desc&limit=1&observation_start={since}"
    )
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if obs and obs[0].get("value") not in (".", None):
            return float(obs[0]["value"])
    except Exception as exc:
        logger.debug("FRED error for %s: %s", series_id, exc)
    return None


def _fetch_riksbank_rate() -> Optional[float]:
    try:
        resp = httpx.get(
            "https://api.riksbank.se/swea/v1/Observations/SECBREPOEFF/2023-01-01",
            timeout=10,
        )
        resp.raise_for_status()
        observations = resp.json()
        if observations:
            return float(observations[-1].get("value", 0))
    except Exception as exc:
        logger.debug("Riksbank API error: %s", exc)
    return None


def _fetch_ecb_rate() -> Optional[float]:
    try:
        resp = httpx.get(
            "https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.4F.KR.DFR.LEV"
            "?lastNObservations=1&format=jsondata",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        series = data.get("dataSets", [{}])[0].get("series", {})
        if series:
            obs = next(iter(series.values())).get("observations", {})
            if obs:
                latest = max(obs.keys(), key=int)
                return float(obs[latest][0])
    except Exception as exc:
        logger.debug("ECB API error: %s", exc)
    return None


# ── Cached context builders ───────────────────────────────────────────────────

def get_macro_context(market: str) -> str:
    """
    Return a short text macro summary for 'us' or 'nordic'.
    Cached in SQLite for 6 hours.
    """
    cache = get_cache()
    cached = cache.get_macro(market, max_age_hours=_MACRO_CACHE_TTL_HOURS)
    if cached:
        return cached.get("text", "")

    if market == "us":
        text = _build_us_macro()
    else:
        text = _build_nordic_macro()

    cache.save_macro(market, {"text": text})
    return text


def _build_us_macro() -> str:
    parts = []
    if (ffr := _fetch_fred("FEDFUNDS")) is not None:
        parts.append(f"Fed Funds Rate: {ffr:.2f}%")
    if (cpi := _fetch_fred("CPIAUCSL")) is not None:
        parts.append(f"CPI (latest): {cpi:.1f}")
    if (t10y := _fetch_fred("DGS10")) is not None:
        parts.append(f"10Y Treasury: {t10y:.2f}%")
    if (unemp := _fetch_fred("UNRATE")) is not None:
        parts.append(f"Unemployment: {unemp:.1f}%")
    return ("US Macro | " + " | ".join(parts)) if parts else "US macro data unavailable."


def _build_nordic_macro() -> str:
    parts = []
    if (rate := _fetch_riksbank_rate()) is not None:
        parts.append(f"Riksbank Rate: {rate:.2f}%")
    if (ecb := _fetch_ecb_rate()) is not None:
        parts.append(f"ECB Rate: {ecb:.2f}%")
    return ("Nordic Macro | " + " | ".join(parts)) if parts else "Nordic macro data unavailable."


def get_macro_indicators_cached() -> list[Indicator]:
    """Fetch macro indicators with SQLite caching (6h TTL)."""
    cache = get_cache()
    cached = cache.get_macro("indicators", max_age_hours=_MACRO_CACHE_TTL_HOURS)
    if cached and "indicators" in cached:
        return [
            Indicator(**ind) for ind in cached["indicators"]
        ]
    indicators = fetch_macro_indicators()
    cache.save_macro("indicators", {
        "indicators": [
            {"label": i.label, "category": i.category, "unit": i.unit,
             "price": i.price, "change_5d_pct": i.change_5d_pct}
            for i in indicators
        ]
    })
    return indicators


def build_macro_block(indicators: list[Indicator], headlines: list | None = None) -> str:
    """Format indicators (and optional headlines) into an LLM prompt block."""
    if not indicators:
        return ""

    lines = [f"## Global Macro Context  (fetched {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"]
    categories = [
        ("index",     "Equity indices"),
        ("commodity", "Commodities"),
        ("fx",        "FX vs SEK"),
        ("rate",      "Rates"),
    ]
    for cat_key, cat_label in categories:
        cat_inds = [i for i in indicators if i.category == cat_key and i.price is not None]
        if not cat_inds:
            continue
        parts = []
        for ind in cat_inds:
            chg = f" ({ind.change_5d_pct:+.1f}% 5d)" if ind.change_5d_pct is not None else ""
            unit = f" {ind.unit}" if ind.unit else ""
            parts.append(f"{ind.label} {ind.price:.2f}{unit}{chg}")
        lines.append(f"  {cat_label}: {' | '.join(parts)}")

    if headlines:
        lines.append("")
        lines.append("  Recent global headlines:")
        for h in headlines[:12]:
            if hasattr(h, "title"):
                lines.append(f"    [{getattr(h, 'source', 'News')[:20]}] {h.title}")
            elif isinstance(h, dict):
                lines.append(f"    [{h.get('source', 'News')[:20]}] {h.get('headline', h.get('title', ''))}")

    lines.append("\n  Use this context to inform your macro overlay.")
    return "\n".join(lines)
