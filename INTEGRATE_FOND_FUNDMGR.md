# Integrating financedata into Fond/ai-fund-manager

## Context

`financedata` is a shared market-data library at `~/Github/FinanceData`.
It replaces the fetch+cache layer in `src/fundmgr/data/` with a single shared
SQLite cache at `~/.financedata/cache.db`.

**What stays in ai-fund-manager:** the `Store` class and all its portfolio tables
(positions, transactions, nav_history, recommendations, learnings, etc.), the
`TickerFeatures` dataclass, LLM prompt building, guardrails, CLI, scheduler.

**What moves to financedata:** the raw fetch + cache logic inside
`src/fundmgr/data/prices.py`, `news.py`, `macro_context.py`, `fundamentals.py`.

The `Store` still exists — it just loses its `price_cache`, `fundamentals_cache`,
`news_cache`, and `benchmark_cache` tables (those move to the shared DB).
Portfolio state (positions, cash, recommendations, etc.) stays exactly where it is.

---

## Step 1 — Install

```bash
cd ~/Github/Fond/ai-fund-manager
uv add --editable ../../FinanceData
```

Or if you prefer a fixed reference in `pyproject.toml`:

```toml
dependencies = [
    ...
    "financedata @ file:///home/pi/Github/FinanceData",
]
```

Then `uv sync`.

---

## Step 2 — Replace `src/fundmgr/data/prices.py`

The key public functions called elsewhere in the project are:
- `fetch_and_cache_prices(tickers, store, lookback_days, force_refresh)` → `dict[str, bool]`
- `compute_features(ticker, store, since_date)` → `TickerFeatures | None`
- `build_all_features(tickers, store, cfg, fetch_result)` → `dict[str, TickerFeatures]`

Keep `TickerFeatures` and all the feature-building logic in this file. Only replace
the raw fetch + indicator helpers:

```python
# src/fundmgr/data/prices.py  (keep TickerFeatures dataclass and build_all_features)
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from financedata import get_prices_since, rsi, pct_return, ann_vol
from fundmgr.config import AppConfig, UniverseTicker
from fundmgr.state.store import Store


# ── TickerFeatures stays exactly as-is ────────────────────────────────────────
# (keep the full @dataclass definition here unchanged)


# ── Replace these three private helpers ──────────────────────────────────────
# DELETE: def _rsi(closes, period)       → use financedata.rsi(closes, period)
# DELETE: def _pct_return(closes, periods) → use financedata.pct_return(closes, periods)
# DELETE: def _ann_vol(closes, periods)  → use financedata.ann_vol(closes, periods)
# Keep:   def _count_trading_days_since   (not in financedata)
# Keep:   def _safe_float                 (not in financedata)


# ── Replace fetch_and_cache_prices ────────────────────────────────────────────
def fetch_and_cache_prices(
    tickers: list[UniverseTicker],
    store: Store,
    lookback_days: int = 252,
    force_refresh: bool = False,
) -> dict[str, bool]:
    """Delegate price fetching to financedata; return dict ticker -> success."""
    symbols = [t.yahoo_ticker for t in tickers]
    since = (datetime.utcnow() - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")

    # financedata writes to its own SQLite; still call store.save_prices so the
    # fund's store stays in sync for compute_features reads.
    results = get_prices_since(symbols, since=since, force_refresh=force_refresh)

    # Mirror to fund's own store so compute_features (which reads from store) works
    from financedata import get_cache
    fd_cache = get_cache()
    for sym, ok in results.items():
        if ok:
            rows = fd_cache.get_prices(sym, since_date=since)
            if rows:
                store.save_prices(sym, rows)

    return results
```

**Note on `compute_features`:** it calls `store.get_prices(...)` which still works
because the mirror step above keeps the fund's store populated. Alternatively, once
you've confirmed everything works, you can rewrite `compute_features` to read from
`financedata.get_cache()` directly and remove the mirror step.

**Update indicator calls in `compute_features`:**

```python
# Before:
rsi_14=_rsi(closes),
return_1d_pct=_pct_return(closes, 1),
vol_20d_ann_pct=_ann_vol(closes, 20),

# After:
from financedata import rsi as fd_rsi, pct_return as fd_pct, ann_vol as fd_vol
rsi_14=fd_rsi(closes),
return_1d_pct=fd_pct(closes, 1),
vol_20d_ann_pct=fd_vol(closes, 20),
```

---

## Step 3 — Replace `src/fundmgr/data/news.py`

The key public functions called elsewhere:
- `fetch_news(feeds, tickers, max_age_hours)` → `dict[str, list[dict]]`
- `score_and_cache_sentiment(ticker_news, store, model, device)` → `None`
- `check_news_triggers(feeds, tickers, held_tickers, store, cfg, max_age_hours)` → `list[dict]`
- `attach_sentiment_to_features(features, store, since_date)` → `None`

Replace the file with:

```python
# src/fundmgr/data/news.py
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from financedata import get_news as fd_get_news, score_sentiment, get_cache

from fundmgr.config import SentimentConfig, UniverseTicker
from fundmgr.state.store import Store

if TYPE_CHECKING:
    from fundmgr.data.prices import TickerFeatures


def fetch_news(
    feeds: list[str],
    tickers: list[UniverseTicker],
    max_age_hours: int = 72,
) -> dict[str, list[dict]]:
    symbols = [t.yahoo_ticker for t in tickers]
    names = {t.yahoo_ticker: t.name for t in tickers}
    return fd_get_news(symbols, feeds=feeds, names=names, max_age_hours=max_age_hours)


def score_and_cache_sentiment(
    ticker_news: dict[str, list[dict]],
    store: Store,
    model: str = "ProsusAI/finbert",
    device: str = "cpu",
) -> None:
    from financedata import score_and_save
    score_and_save(ticker_news, model=model, device=device)
    # Mirror scored headlines to fund's own store for attach_sentiment_to_features
    fd_cache = get_cache()
    now = datetime.utcnow().strftime("%Y-%m-%d")
    for ticker in ticker_news:
        rows = fd_cache.get_news(ticker, since_date=now)
        if rows:
            store.save_news_sentiment(ticker, rows)


def check_news_triggers(
    feeds: list[str],
    tickers: list[UniverseTicker],
    held_tickers: set[str],
    store: Store,
    cfg: SentimentConfig,
    max_age_hours: int = 8,
) -> list[dict]:
    if not cfg.enabled:
        return []

    ticker_news = fetch_news(feeds, tickers, max_age_hours=max_age_hours)
    if not ticker_news:
        return []

    all_items: list[tuple[str, dict]] = [
        (ticker, item)
        for ticker, items in ticker_news.items()
        for item in items
    ]
    if not all_items:
        return []

    headlines = [item["headline"] for _, item in all_items]
    scores = score_sentiment(headlines, model=cfg.model, device=cfg.device)
    scored = [(ticker, item, s) for (ticker, item), s in zip(all_items, scores)]

    triggers: list[dict] = []
    now = datetime.utcnow()
    cooldown_cutoff = (now - timedelta(hours=cfg.trigger_cooldown_hours)).isoformat()

    for ticker, item, score in scored:
        label = score["label"]
        val = score["score"]
        is_held = ticker in held_tickers

        if is_held and label == "negative" and val >= cfg.trigger_threshold_negative:
            pass
        elif label == "positive" and val >= cfg.trigger_threshold_positive:
            pass
        else:
            continue

        article_hash = hashlib.sha1(f"{ticker}:{item['headline']}".encode()).hexdigest()
        if store.has_triggered(article_hash):
            continue
        last = store.last_trigger_at(ticker)
        if last and last >= cooldown_cutoff:
            continue

        store.record_trigger(ticker, item["headline"], label, val, article_hash)
        triggers.append({
            "ticker": ticker,
            "headline": item["headline"],
            "sentiment_label": label,
            "sentiment_score": val,
            "is_held": is_held,
        })

    return triggers


def attach_sentiment_to_features(
    features: dict[str, "TickerFeatures"],
    store: Store,
    since_date: str,
) -> None:
    # This function reads from the fund's own store.news_cache (mirrored above)
    for ticker, feat in features.items():
        rows = store.get_recent_news(ticker, since_date=since_date)
        if not rows:
            continue
        feat.news_count = len(rows)
        score_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
        scores = [
            r["sentiment_score"] * score_map.get(r["sentiment_label"], 0.0)
            for r in rows
            if r["sentiment_score"] is not None
        ]
        if scores:
            avg = sum(scores) / len(scores)
            feat.sentiment_label = "positive" if avg > 0.1 else "negative" if avg < -0.1 else "neutral"
            feat.sentiment_score = round(abs(avg), 3)
```

---

## Step 4 — Replace `src/fundmgr/data/macro_context.py`

The callers use `fetch_macro_indicators()` and `build_macro_block(indicators, headlines)`.
Both are available directly from `financedata`:

```python
# src/fundmgr/data/macro_context.py
from financedata import (  # noqa: F401 — re-export for existing callers
    get_macro_indicators_cached as fetch_macro_indicators,
    build_macro_block,
    Indicator,
)

# fetch_macro_headlines is only used to pass to build_macro_block.
# The build_macro_block in financedata accepts an optional list of dicts
# with {"headline": ..., "source": ...} or objects with .title/.source attrs.
# If you still use fetch_macro_headlines elsewhere, keep it here:
from financedata.macro import _fetch_rss_headlines  # internal — copy if needed
```

If `fetch_macro_headlines` is called in other files, keep it as a local helper
or inline it. The shared library's `build_macro_block` accepts either format.

---

## Step 5 — Replace `src/fundmgr/data/fundamentals.py`

The key public functions:
- `fetch_and_cache_fundamentals(tickers, store, ttl_days, max_workers)` → `int`
- `apply_to_features(features, store)` → `None`

```python
# src/fundmgr/data/fundamentals.py
from __future__ import annotations

import math
from datetime import datetime

from financedata import get_fundamentals, ts_to_days
from fundmgr.state.store import Store


def fetch_and_cache_fundamentals(
    tickers: list[str],
    store: Store,
    ttl_days: int = 7,
    max_workers: int = 12,
) -> int:
    """Fetch stale fundamentals via financedata and mirror to fund's store."""
    # financedata writes to its own cache and returns the full dict
    data = get_fundamentals(tickers, ttl_days=ttl_days, max_workers=max_workers)
    refreshed = 0
    for ticker, d in data.items():
        store.save_fundamentals(ticker, d)
        refreshed += 1
    return refreshed


def apply_to_features(features: dict, store: Store) -> None:
    """Pull cached fundamentals from fund's store and attach to TickerFeatures."""
    cached = store.get_all_fundamentals(list(features.keys()))

    for ticker, feat in features.items():
        data = cached.get(ticker)
        if not data:
            continue

        def _safe(val, scale=1.0):
            try:
                if val is None:
                    return None
                f = float(val) * scale
                return None if math.isnan(f) or math.isinf(f) else round(f, 2)
            except (TypeError, ValueError):
                return None

        feat.pe_ratio       = _safe(data.get("pe_ratio"))
        feat.forward_pe     = _safe(data.get("forward_pe"))
        feat.pb_ratio       = _safe(data.get("pb_ratio"))
        feat.ev_to_ebitda   = _safe(data.get("ev_to_ebitda"))
        feat.price_to_sales = _safe(data.get("price_to_sales"))
        feat.beta           = _safe(data.get("beta"))
        feat.analyst_count  = data.get("analyst_count")

        for frac_key, attr in (
            ("profit_margin",   "profit_margin_pct"),
            ("gross_margin",    "gross_margin_pct"),
            ("roe",             "roe_pct"),
            ("revenue_growth",  "revenue_growth_pct"),
            ("earnings_growth", "earnings_growth_pct"),
            ("dividend_yield",  "dividend_yield_pct"),
        ):
            raw = data.get(frac_key)
            setattr(feat, attr, round(raw * 100, 1) if raw is not None else None)

        mc = data.get("market_cap")
        feat.market_cap_msek = round(mc / 1e6, 0) if mc is not None else None

        high = data.get("fifty_two_week_high")
        if high and high > 0 and feat.last_price:
            feat.pct_from_52w_high = round((feat.last_price / high - 1) * 100, 1)

        target = data.get("analyst_target_price")
        if target and feat.last_price and feat.last_price > 0:
            feat.analyst_target_pct = round((target / feat.last_price - 1) * 100, 1)

        feat.days_to_earnings = ts_to_days(data.get("earnings_timestamp"))
        feat.days_to_ex_div   = ts_to_days(data.get("ex_div_timestamp"))
```

---

## Step 6 — Store cleanup (optional, do later)

After the migration is working, you can remove the now-redundant cache tables from
`fundmgr/state/store.py`:

- `price_cache` table and its methods (`save_prices`, `get_prices`, `latest_price_date`, `save_benchmark`, `get_benchmark`)
- `fundamentals_cache` table and its methods (`save_fundamentals`, `get_fundamentals`, etc.)
- `news_cache` table (keep `news_triggers` — that's portfolio state, not market data)

Don't do this in the same PR as the integration — verify everything runs first,
then clean up the store in a follow-up.

---

## Step 7 — Smoke test

```bash
uv run fund status   # should work normally
uv run fund run      # full weekly run — watch for any import errors
```

Check the shared cache was populated:

```bash
uv run python -c "
from financedata import get_cache
c = get_cache()
import sqlite3
conn = sqlite3.connect(c.db_path)
for t in ['prices', 'fundamentals', 'news']:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n} rows')
"
```
