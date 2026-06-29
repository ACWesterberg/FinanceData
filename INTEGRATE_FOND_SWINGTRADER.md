# Integrating financedata into Fond/swing-trader

## Context

`financedata` is a shared market-data library at `~/Github/FinanceData`.
It replaces the duplicated indicator math and RSS-fetching code inside
`src/swingmgr/data/intraday.py` and `src/swingmgr/data/news.py`.

**What stays in swing-trader:** the `IntradayFeatures` dataclass, the
`fetch_intraday_features` function (it does more than just data fetching — it
structures the result for the LLM), the `fetch_all` orchestrator, and the
`_batch_screen` stage-1 screener (it can now call into financedata's shared cache).

**What moves to financedata:** the private indicator functions (`_rsi`, `_atr`,
`_vwap`, `_key_levels`, `_daily_score`) and the RSS fetching in `news.py`.

---

## Step 1 — Install

```bash
cd ~/Github/Fond/swing-trader
uv add --editable ../../FinanceData
```

Or in `pyproject.toml`:

```toml
dependencies = [
    ...
    "financedata @ file:///home/pi/Github/FinanceData",
]
```

---

## Step 2 — Update `src/swingmgr/data/intraday.py`

### 2a — Replace private indicator helpers

At the top of the file, replace the four private functions with imports:

```python
# REMOVE these four function definitions:
# def _rsi(close, period=14) -> pd.Series: ...
# def _atr(high, low, close, period=14) -> pd.Series: ...
# def _vwap(bars) -> float | None: ...
# def _key_levels(daily, last_price) -> tuple: ...

# ADD these imports instead:
from financedata import rsi_series as _rsi_series, atr as _atr, vwap as _vwap, key_levels as _key_levels
```

**Then update the call sites inside `fetch_intraday_features`:**

```python
# Before:
rsi_daily_series = _rsi(daily["Close"], rsi_period)
rsi_1h_series = _rsi(h1["Close"], rsi_period)

# After:
from financedata import rsi_series as _rsi_fn
rsi_daily_series = _rsi_fn(daily["Close"], rsi_period)
rsi_1h_series = _rsi_fn(h1["Close"], rsi_period)
```

```python
# Before:
atr_series = _atr(daily["High"], daily["Low"], daily["Close"], atr_period)

# After (same signature — drop-in replacement):
atr_series = _atr(daily["High"], daily["Low"], daily["Close"], atr_period)
```

```python
# Before:
vwap_today = _vwap(today_bars)

# After (same signature — drop-in replacement):
vwap_today = _vwap(today_bars)
```

```python
# Before:
support, resistance = _key_levels(daily, last_price)

# After (same signature — drop-in replacement):
support, resistance = _key_levels(daily, last_price)
```

`_atr`, `_vwap`, and `_key_levels` are drop-in replacements. Only `_rsi` changes
because the shared library separates `rsi()` (returns the latest float) from
`rsi_series()` (returns the full Series, which is what this file needs).

### 2b — Replace `_daily_score`

```python
# REMOVE:
# def _daily_score(close, volume, rsi_period, ma_short, ma_long) -> float: ...

# ADD import:
from financedata import daily_momentum_score as _daily_score
```

`daily_momentum_score(close, volume, rsi_period, ma_short, ma_long)` has the same
signature and same scoring logic — it was extracted directly from this file.

### 2c — Update `_batch_screen` to use the shared price cache

The batch screener currently calls `yf.download` directly. You can leave it as-is
(it still works), or optionally route through the financedata cache so prices
downloaded during screening are available to other projects:

```python
# Optional: replace the yf.download call inside _batch_screen with:
from financedata import get_prices_batch

# Instead of:
raw = yf.download(chunk, period=f"{lookback_days}d", ...)

# Use (this writes to shared cache automatically):
batch_result = get_prices_batch(chunk, market="nordic", period=f"{lookback_days}d")
# batch_result is {ticker: DataFrame} — reconstruct close_df/vol_df from it:
close_df = pd.DataFrame({t: df["Close"] for t, df in batch_result.items()})
vol_df   = pd.DataFrame({t: df["Volume"] for t, df in batch_result.items()})
```

This is optional — the screener works fine either way. The benefit is that prices
fetched during stage-1 screening are cached so stage-2's `fetch_intraday_features`
daily lookback doesn't re-download the same data.

---

## Step 3 — Replace `src/swingmgr/data/news.py`

The current `news.py` has `fetch_headlines` (flat list of all headlines) and
`filter_for_ticker` (keyword match). The shared library does both in one call and
uses better word-boundary matching.

```python
# src/swingmgr/data/news.py
from __future__ import annotations

from dataclasses import dataclass

from financedata import fetch_rss, SWEDISH_RSS_FEEDS


@dataclass
class Headline:
    title: str
    source: str
    published_at: str | None = None
    url: str | None = None


def fetch_headlines(
    feeds: list[str],
    max_age_hours: int = 24,
    max_per_feed: int = 5,   # kept for API compat; not enforced by shared lib
) -> list[Headline]:
    """
    Fetch recent headlines from RSS feeds.
    Returns a flat list of Headline objects (all tickers merged).
    """
    # fetch_rss needs at least one ticker to build a keyword map.
    # For a global "all headlines" fetch, pass a placeholder and return everything.
    ticker_news = fetch_rss(
        feeds=feeds,
        tickers=["__all__"],
        names={"__all__": ""},   # empty name → no keyword filter
        max_age_hours=max_age_hours,
    )
    # Flatten all matched articles to Headline objects
    seen: set[str] = set()
    results: list[Headline] = []
    for articles in ticker_news.values():
        for a in articles:
            key = a.get("headline", "")
            if key and key not in seen:
                seen.add(key)
                results.append(Headline(
                    title=a["headline"],
                    source=a.get("source", ""),
                    published_at=a.get("published_at"),
                    url=a.get("source_url"),
                ))
    results.sort(key=lambda h: h.published_at or "", reverse=True)
    return results


def filter_for_ticker(headlines: list[Headline], ticker: str) -> list[Headline]:
    """
    Keyword filter for a single ticker. Kept for callers that still use this pattern.
    For new code, prefer financedata.get_news() which does matching during fetch.
    """
    from financedata import build_keyword_map
    import re
    kmap = build_keyword_map([ticker])
    kws = kmap.get(ticker, [])
    if not kws:
        return []
    return [
        h for h in headlines
        if any(re.search(r"\b" + re.escape(kw) + r"\b", h.title.lower()) for kw in kws)
    ]
```

**Alternatively** — if the callers of `fetch_headlines` + `filter_for_ticker` always
pair them (fetch all, then filter per ticker), replace both call sites with a single
`financedata.get_news(tickers, feeds)` call which does matching during the fetch pass
and avoids re-parsing feeds per-ticker.

---

## Step 4 — Smoke test

```bash
uv run python -c "
import sys
from financedata import rsi_series, atr, vwap, key_levels, daily_momentum_score
import pandas as pd
import numpy as np

# Minimal sanity check with synthetic data
closes = pd.Series([10.0 + i * 0.1 + (i % 3) * 0.2 for i in range(30)])
high = closes + 0.5
low = closes - 0.5
volume = pd.Series([1000.0 + i * 10 for i in range(30)])

print('RSI series tail:', rsi_series(closes).iloc[-3:].values)
print('ATR latest:', atr(high, low, closes).iloc[-1])
print('Daily score:', daily_momentum_score(closes, volume))
print('All indicator imports OK')
"
```

Then run a normal swing scan and confirm the output is unchanged.

---

## What can be deleted after migration

- The `_rsi`, `_atr`, `_vwap`, `_key_levels`, `_daily_score` function bodies in
  `src/swingmgr/data/intraday.py` (replaced by financedata imports)
- The original `fetch_headlines` + `filter_for_ticker` implementation in
  `src/swingmgr/data/news.py` (replaced by the wrapper above or direct `get_news` calls)

Keep `IntradayFeatures`, `fetch_intraday_features`, `fetch_all`, `_batch_screen`,
`_quiet_yf` — those are swing-trader-specific.
