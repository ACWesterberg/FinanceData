# Integrating financedata into DeepSwing

## Context

`financedata` is a shared market-data library that lives at `~/Github/FinanceData`.
It replaces the data-fetching and caching code in `src/data/` with a single shared
SQLite cache at `~/.financedata/cache.db` that all three projects (DeepSwing,
Fond/ai-fund-manager, Fond/swing-trader) read from.

**What stays in DeepSwing:** trading logic, scan/decision engine, backtester,
APScheduler setup, trade DB (`src/db.py`), universe CSV.

**What moves to financedata:** everything in `src/data/market_data.py`,
`src/data/news_fetcher.py`, `src/data/macro_data.py`, `src/data/insider_fetcher.py`.

---

## Step 1 — Install

```bash
cd ~/Github/DeepSwing
source venv/bin/activate
pip install -e ../FinanceData
```

Verify:

```bash
python -c "import financedata; print('OK')"
```

---

## Step 2 — Replace `src/data/market_data.py`

Delete the entire file and replace with this thin wrapper that re-exports from
`financedata` using the same function names DeepSwing calls today:

```python
# src/data/market_data.py
from __future__ import annotations
from typing import Optional
import pandas as pd

from financedata import (
    get_prices as _get_prices,
    get_prices_batch,
    get_current_price,
    get_vix,
    get_sector,
)


def fetch_ohlcv(
    ticker: str,
    market: str,
    period: str = "1y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    return _get_prices(ticker, market=market, period=period, interval=interval)


def fetch_batch_nordic(tickers: list[str]) -> dict[str, pd.DataFrame]:
    return get_prices_batch(tickers, market="nordic", period="1y")


def fetch_batch_us(tickers: list[str]) -> dict[str, pd.DataFrame]:
    return get_prices_batch(tickers, market="us", period="1y")


# get_current_price, get_vix, get_sector are identical signatures — import directly:
# from financedata import get_current_price, get_vix, get_sector
```

Any file that does `from src.data.market_data import get_current_price` will keep
working. Alternatively, update those call sites to `from financedata import
get_current_price` directly — either works.

**What you gain:** The Alpha Vantage daily counter is now persisted in SQLite and
survives process restarts. Previously, restarting `main.py` reset the counter to 0,
so 25 restarts in a day could burn 625 AV requests.

---

## Step 3 — Replace `src/data/news_fetcher.py`

Delete the file and replace with:

```python
# src/data/news_fetcher.py
from __future__ import annotations
from financedata import get_news, SWEDISH_RSS_FEEDS
import os


def fetch_news_for_ticker(ticker: str, market: str) -> list[dict]:
    """Fetch recent news articles for a given ticker."""
    feeds = SWEDISH_RSS_FEEDS if market == "nordic" else []
    use_newsapi = bool(os.environ.get("NEWS_API_KEY"))

    results = get_news(
        tickers=[ticker],
        feeds=feeds,
        max_age_hours=48,
        use_newsapi=use_newsapi,
    )
    # get_news returns {ticker: [articles]}; flatten to the list this project expects
    return results.get(ticker, [])
```

The returned article dicts have the same keys as before:
`{"title", "description", "source", "published", "url"}` — wait, the shared library
uses `{"headline", "source_url", "published_at", "source"}`. Check callers of
`fetch_news_for_ticker` in DeepSwing and update the key names they use:

| Old key | New key |
|---|---|
| `a["title"]` | `a["headline"]` |
| `a["description"]` | *(not present — was only a short extract)* |
| `a["url"]` | `a["source_url"]` |
| `a["published"]` | `a["published_at"]` |

Search for uses with:
```bash
grep -rn 'fetch_news_for_ticker\|a\["title"\]\|a\["description"\]' src/
```

---

## Step 4 — Replace `src/data/macro_data.py`

Delete the file and replace with:

```python
# src/data/macro_data.py
from financedata import get_macro_context  # noqa: F401 — re-export for callers
```

`get_macro_context(market)` has the identical signature and returns the same
`"US Macro | Fed Funds Rate: ..." / "Nordic Macro | ..."` string format.

The SQLite cache replaces the in-memory `_macro_cache` dict — so the 6-hour TTL
now also survives restarts.

---

## Step 5 — Replace `src/data/insider_fetcher.py`

Delete the file and replace with:

```python
# src/data/insider_fetcher.py
from financedata import get_insider_summary  # noqa: F401 — re-export for callers
```

Identical signature: `get_insider_summary(ticker, market) -> str`.
The 24-hour cache is now in SQLite rather than in-memory.

---

## Step 6 — Environment variables

Make sure these are set in the systemd service or `.env` file on the Pi.
DeepSwing's existing `settings` already reads `ALPHA_VANTAGE_API_KEY` and
`NEWS_API_KEY`. The shared library reads slightly different names:

| DeepSwing `settings` attribute | financedata env var |
|---|---|
| `settings.alpha_vantage_api_key` | `ALPHA_VANTAGE_KEY` |
| `settings.news_api_key` | `NEWS_API_KEY` |
| `settings.fred_api_key` | `FRED_API_KEY` |

Easiest fix: add these to the systemd unit or `.env`:

```bash
ALPHA_VANTAGE_KEY=${ALPHA_VANTAGE_API_KEY}   # alias if you don't want to rename
NEWS_API_KEY=...
FRED_API_KEY=...
FINANCEDATA_DB=/home/pi/.financedata/cache.db
```

Or update `src/data/market_data.py` to set the env var from settings at import time:

```python
import os
from config.settings import settings
os.environ.setdefault("ALPHA_VANTAGE_KEY", settings.alpha_vantage_api_key or "")
os.environ.setdefault("NEWS_API_KEY", settings.news_api_key or "")
os.environ.setdefault("FRED_API_KEY", settings.fred_api_key or "")
```

---

## Step 7 — Smoke test

```bash
source venv/bin/activate
python -c "
from financedata import get_prices, get_macro_context, get_insider_summary
df = get_prices('AAPL', market='us', period='5d')
print('Prices:', df.shape if df is not None else 'None')
print('Macro:', get_macro_context('us')[:60])
print('Insider:', get_insider_summary('AAPL', 'us')[:60])
"
```

---

## Files to delete after migration

- `src/data/market_data.py` (replaced by wrapper or direct imports)
- `src/data/news_fetcher.py` (replaced by wrapper)
- `src/data/macro_data.py` (replaced by one-liner re-export)
- `src/data/insider_fetcher.py` (replaced by one-liner re-export)

Keep `src/data/universe.py` — that's DeepSwing-specific (ticker universe CSV logic).
