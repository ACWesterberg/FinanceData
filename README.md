# FinanceData

Shared market data library for DeepSwing, FundManager, and SwingTrader.

Provides a single SQLite cache at `~/.financedata/cache.db` shared across all projects, so fetching prices for one app doesn't re-fetch them for another.

---

## Installation

**pip project (DeepSwing):**
```bash
pip install -e ~/Github/FinanceData
```

**uv project (FundManager, SwingTrader):**
```bash
uv add --editable ~/Github/FinanceData
uv sync
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `FINANCEDATA_DB` | No | SQLite path (default: `~/.financedata/cache.db`) |
| `ALPHA_VANTAGE_KEY` | No | Enables Alpha Vantage for Nordic daily prices (25 req/day free) |
| `NEWS_API_KEY` | No | Enables NewsAPI for English-language news |
| `FINNHUB_API_KEY` | No | Enables Finnhub per-ticker company-news fallback (US-focused, 60 req/min free) |
| `FINNHUB_RATE_LIMIT_PER_MIN` | No | Finnhub per-minute request cap, shared across processes (default `60`) |
| `FRED_API_KEY` | No | Enables US macro data from FRED (Fed rate, CPI, 10Y, unemployment) |
| `EODHD_API_TOKEN` | For universe refresh | EODHD token used to refresh the broker-tradable instrument universe |
| `NEWSAPI_SLOW_THRESHOLD_SECONDS` | No | Trip the NewsAPI breaker when a batch stalls this long on 429 backoff (default `8`, `0` disables) |
| `NEWSAPI_COOLDOWN_MINUTES` | No | How long the breaker skips NewsAPI once tripped (default `20`) |

Everything works without API keys — they just unlock additional data sources.

---

## API reference

### Historical prices

```python
from financedata import get_prices, get_prices_batch, get_prices_since

# Single ticker — returns pd.DataFrame with Open/High/Low/Close/Volume, or None
df = get_prices("AAPL")
df = get_prices("VOLV-B.ST", market="nordic", period="2y")

# Batch — returns {ticker: DataFrame}
dfs = get_prices_batch(["AAPL", "MSFT", "NVDA"], period="1y")

# Ensure prices are loaded back to a date — returns {ticker: bool}
# Used for weekly fund runs that need a fixed history window
ok = get_prices_since(["AAPL", "MSFT"], since="2025-01-01")
```

`market="nordic"` routes through Alpha Vantage first (if key is set), falling back to yfinance. All results are cached in SQLite; stale tickers are the only ones re-fetched.

---

### Live / intraday prices

```python
from financedata import get_live_price, get_live_prices, get_live_price_detail

# Single ticker — native currency, 10-min cache
price = get_live_price("AAPL")           # float or None
price = get_live_price("AAPL", ttl_minutes=0)  # force fresh fetch

# Batch
prices = get_live_prices(["AAPL", "MSFT", "VOLV-B.ST"])
# → {"AAPL": 213.5, "MSFT": 420.1, "VOLV-B.ST": 28.4}

# With timestamp — useful for detecting stale open-bar prices
detail = get_live_price_detail("AAPL")
# → {"price": 213.5, "price_time": "2026-06-29T14:32:00"}
```

Uses `fast_info.last_price` (cheap), falls back to last daily bar. Prices are in the stock's native currency — use `to_sek` to convert.

**Note:** right at market open yfinance sometimes returns yesterday's close as `last_price`. Use `get_live_price_detail` to check `price_time` if freshness matters.

---

### FX rates

```python
from financedata import get_fx_rate, to_sek

# Rate for a currency pair (base → SEK by default)
rate = get_fx_rate("DKK")          # DKK→SEK, e.g. 1.481
rate = get_fx_rate("USD")          # USD→SEK, e.g. 10.32
rate = get_fx_rate("EUR", "USD")   # EUR→USD

# Same-day spot is cached in SQLite (TTL = same trading day)
# Historical rates are cached permanently
rate = get_fx_rate("USD", on="2025-12-31")   # that day's close

# base == quote always returns 1.0 (no network call)
get_fx_rate("SEK", "SEK")  # → 1.0

# Convert an amount to SEK
sek = to_sek(1000.0, "DKK")       # float or None
sek = to_sek(500.0, "USD", on="2025-12-31")
```

Returns `None` if the rate can't be resolved — never silently returns `1.0`. Currency mapping (exchange → currency) stays in the calling project.

---

### Technical indicators

All pure functions — no I/O, no caching. Input is `pd.Series` or `pd.DataFrame` with a `DatetimeIndex`.

```python
from financedata import rsi, rsi_series, atr, vwap, sma, ema, ann_vol, pct_return, key_levels, daily_momentum_score

df = get_prices("AAPL")
closes = df["Close"]

rsi(closes)                  # float or None — latest RSI(14)
rsi(closes, period=9)        # custom period
rsi_series(closes)           # full pd.Series of RSI values

atr(df["High"], df["Low"], df["Close"])   # pd.Series of ATR(14)

sma(closes, 20)              # float or None
ema(closes, 50)              # float or None

ann_vol(closes)              # annualised vol % over last 20 bars, or None
pct_return(closes, 5)        # % return over last 5 bars, or None

support, resistance = key_levels(df, last_price=closes.iloc[-1])

# Composite screening score (higher = more interesting momentum setup)
score = daily_momentum_score(closes, df["Volume"])

# Intraday VWAP from intraday bars
vwap(intraday_df)            # float or None
```

---

### Fundamentals

```python
from financedata import get_fundamentals

# Fetch for a list of tickers — stale entries (>7 days) are refreshed automatically
data = get_fundamentals(["AAPL", "MSFT", "VOLV-B.ST"], ttl_days=7)
# → {"AAPL": {"pe_ratio": 28.3, "forward_pe": 24.1, "pb_ratio": 42.0, ...}, ...}

# ts_to_days converts a Unix timestamp field to days-since-epoch (used internally)
from financedata import ts_to_days
```

Fetching is parallelised across tickers. Returns raw yfinance `.info` fields.

---

### News

News is aggregated from up to four sources, in order: **RSS** (Nordic) → **NewsAPI** (English, `NEWS_API_KEY`) → **Finnhub** (per-ticker US company news, `FINNHUB_API_KEY`) → **yfinance/Yahoo** (universal free backstop). The fallback chain is what lets US tickers — which have no Nordic RSS coverage — still get news.

```python
from financedata import (
    get_news, get_news_cached, get_market_headlines,
    score_sentiment, score_and_save, SWEDISH_RSS_FEEDS,
)

# One-shot fetch (RSS + NewsAPI + optional per-ticker fallback)
articles = get_news(
    tickers=["AAPL", "VOLV-B.ST"],
    feeds=SWEDISH_RSS_FEEDS,          # or your own list of RSS URLs
    names={"VOLV-B.ST": "Volvo"},     # optional: improves keyword matching
    max_age_hours=72,
    use_newsapi=True,                 # requires NEWS_API_KEY
    use_fallback=True,                # Finnhub (US) then yfinance for empty tickers
    market="us",                      # "us" always tries Finnhub; None infers US per
                                      # ticker (no suffix = US); "nordic" skips Finnhub
)
# → {"VOLV-B.ST": [{"headline": "...", "source_url": "...", "published_at": "...", "source": "..."}, ...]}
```

**Prefer `get_news_cached` for shared, deduplicated fetching.** It's a TTL read-through over the shared SQLite cache, so one project's fetch satisfies another's later read (e.g. the fund's morning scan warms the cache for DeepSwing's post-screen survivors — no re-query).

```python
# Only stale/missing tickers (older than ttl_hours) hit the network; the rest
# are served from the shared cache. feeds defaults to SWEDISH_RSS_FEEDS for
# nordic/None markets and [] for "us".
articles = get_news_cached(
    tickers=["AAPL", "MSFT"],
    market="us",
    ttl_hours=6,          # per-ticker cache freshness window
    use_fallback=True,    # on by default here
)
```

Market-wide (not ticker-filtered) macro/geopolitical headlines — Nordic from RSS, US from a broad NewsAPI query — cached per market (default 30-min TTL):

```python
headlines = get_market_headlines("nordic")   # or "us"
# → [{"headline": "...", "source": "...", "published_at": "..."}, ...] newest-first
```

Sentiment scoring is unchanged:

```python
scores = score_sentiment(["Volvo beats earnings", "Market crash incoming"])
# → [{"label": "positive", "score": 0.97}, {"label": "negative", "score": 0.91}]
score_and_save(articles)   # scores headlines and upserts them into the shared cache
```

NewsAPI retries on 429 with exponential backoff (up to 5 attempts). If a batch stalls on backoff, a **breaker** trips and subsequent calls skip NewsAPI (RSS/fallback only) for `NEWSAPI_COOLDOWN_MINUTES` so one throttled ticker can't stall a whole scan. Check `newsapi_available()` to see the current state.

Finnhub calls are governed by a **shared per-minute limiter** (`FINNHUB_RATE_LIMIT_PER_MIN`, default 60) tracked in the SQLite cache, so DeepSwing and the Fond apps together stay under the free-tier cap. When the minute budget is spent, Finnhub is skipped and the ticker falls straight through to yfinance — no 429s.

**FinBERT** is optional — install with `pip install financedata[sentiment]`. Falls back to neutral scores if not installed.

---

### Macro

```python
from financedata import get_macro_context, get_macro_indicators_cached, build_macro_block, Indicator

# Short text summary for LLM prompts — cached 6 hours
text = get_macro_context("us")      # Fed rate, CPI, 10Y, unemployment
text = get_macro_context("nordic")  # Riksbank rate, ECB rate

# Structured indicator list — cached 6 hours
indicators = get_macro_indicators_cached()
# → [Indicator(label="S&P 500", category="index", price=5432.1, change_5d_pct=+1.2), ...]

# Format into an LLM prompt block
block = build_macro_block(indicators, headlines=[...])
```

`Indicator` fields: `label`, `category` (`index/commodity/fx/rate`), `unit`, `price`, `change_5d_pct`.

---

### Insider activity

```python
from financedata import get_insider_summary

# Returns a short text summary of recent insider transactions
summary = get_insider_summary("AAPL", market="us")
summary = get_insider_summary("VOLV-B.ST", market="nordic")
# → "3 insider purchases in last 90 days, largest: ..."
```

US market uses SEC EDGAR. Nordic market uses FI Insynsregistret.

---

### Universe (broker-tradable instruments)

FinanceData maintains one complete, up-to-date universe of the instruments
tradable in the 18 countries covered by the brokerage (Montrose), sourced from
EODHD and stored in the shared cache. **Other projects never re-fetch it** — they
read the full set, or pull only what changed since their last sync.

```python
from financedata import (
    refresh_universe, get_universe, get_universe_symbols,
    get_universe_updates, export_universe, last_universe_refresh,
    MONTROSE_COUNTRIES,
)
```

**Refreshing (run on a schedule on the Pi — this is the only step that hits EODHD):**

```python
# Reconciles the source into the cache: inserts new listings, updates changed
# ones, and flags vanished ones as removed (never deletes). Needs EODHD_API_TOKEN.
summary = refresh_universe()
# → {"refreshed_at": ..., "added": 12, "removed": 3, "modified": 5,
#    "unchanged": 41000, "total_active": 41017}

refresh_universe(include_etfs=True)                 # include ETFs too
refresh_universe(countries=["Sweden", "Norway"])    # limit the refresh scope
```

Or from the shell (ideal for cron / a systemd timer):

```bash
export EODHD_API_TOKEN=...
python -m financedata.universe refresh          # fetch + reconcile
python -m financedata.universe status           # show last refresh summary
python -m financedata.universe export out.csv   # dump the cached universe
```

**Reading the universe (no network — pure cache reads):**

```python
rows = get_universe()                                   # list[dict], all countries
rows = get_universe(["Sweden", "United States"])        # filtered
df   = get_universe(as_dataframe=True)                  # pandas DataFrame
tickers = get_universe_symbols(["Sweden"])              # just the tickers
```

Each row has: `key`, `ticker`, `company_name`, `exchange`, `exchange_code`,
`mic`, `country`, `isin`, `security_type`, `currency`, `status`, `first_seen`,
`last_seen`, `updated_at`. `key` (`"<exchange_code>:<ticker>"`) is the stable
identity across refreshes.

**Incremental updates — how downstream projects stay in sync:**

Each project persists the `as_of` timestamp it last saw and passes it back as
`since`, so it only ever processes deltas — additions, removals, and field
changes (e.g. a re-ticker or ISIN correction):

```python
delta = get_universe_updates(since=my_last_sync)   # optionally countries=[...]
# → {"since": ..., "as_of": ...,
#    "added":    [row, ...],   # new to the broker
#    "removed":  [row, ...],   # delisted / no longer tradable (status='removed')
#    "modified": [row, ...]}   # existing listing whose fields changed

for row in delta["added"]:    add_to_my_universe(row)
for row in delta["removed"]:  drop_from_my_universe(row["key"])
for row in delta["modified"]: update_my_universe(row)

my_last_sync = delta["as_of"]   # persist for next time
```

`last_universe_refresh()` returns metadata about the most recent refresh run.

---

### Cache

```python
from financedata import get_cache

cache = get_cache()   # singleton — same instance everywhere in the process
```

Direct cache access is rarely needed. The DB path is controlled by `FINANCEDATA_DB` (default `~/.financedata/cache.db`). All projects on the same machine share one DB automatically.

---

## Updating on the Pi

After pushing changes from your Mac:

```bash
cd ~/Github/FinanceData && git pull
```

No reinstall needed — all projects use editable installs.
