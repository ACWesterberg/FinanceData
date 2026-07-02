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
| `FRED_API_KEY` | No | Enables US macro data from FRED (Fed rate, CPI, 10Y, unemployment) |

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

```python
from financedata import get_news, score_sentiment, score_and_save, SWEDISH_RSS_FEEDS

# Fetch news for a list of tickers from RSS + NewsAPI
articles = get_news(
    tickers=["AAPL", "VOLV-B.ST"],
    feeds=SWEDISH_RSS_FEEDS,          # or your own list of RSS URLs
    names={"VOLV-B.ST": "Volvo"},     # optional: improves keyword matching
    max_age_hours=72,
    use_newsapi=True,                 # requires NEWS_API_KEY
)
# → {"VOLV-B.ST": [{"headline": "...", "source_url": "...", "published_at": "...", "source": "..."}, ...]}

# Score headlines with FinBERT (requires pip install financedata[sentiment])
scores = score_sentiment(["Volvo beats earnings", "Market crash incoming"])
# → [{"label": "positive", "score": 0.97}, {"label": "negative", "score": 0.91}]

# Score + save to cache in one step
score_and_save(articles)
```

NewsAPI retries on 429 with exponential backoff (up to 5 attempts). Total fetch time is logged at INFO level.

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
