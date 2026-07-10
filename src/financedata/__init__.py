"""
financedata — shared market data service for DeepSwing and Fond.

Install (editable):
    pip install -e /path/to/FinanceData

Environment variables:
    FINANCEDATA_DB       — SQLite cache path (default: ~/.financedata/cache.db)
    ALPHA_VANTAGE_KEY    — Alpha Vantage API key (Nordic price fallback, 25 req/day)
    NEWS_API_KEY         — NewsAPI key (English-language news)
    FINNHUB_API_KEY      — Finnhub key (per-ticker US company-news fallback)
    FRED_API_KEY         — FRED API key (US macro indicators)
    EODHD_API_TOKEN      — EODHD token (broker universe refresh)
"""
from .cache import DataCache, get_cache
from .prices import (
    get_prices,
    get_prices_batch,
    get_prices_since,
    get_current_price,
    get_vix,
    get_sector,
)
from .indicators import (
    rsi,
    rsi_series,
    atr,
    vwap,
    sma,
    ema,
    ann_vol,
    pct_return,
    key_levels,
    daily_momentum_score,
)
from .news import (
    get_news,
    get_news_cached,
    get_market_headlines,
    fetch_rss,
    fetch_newsapi,
    fetch_finnhub_news,
    fetch_yfinance_news,
    newsapi_available,
    score_sentiment,
    score_and_save,
    build_keyword_map,
    SWEDISH_RSS_FEEDS,
)
from .macro import (
    get_macro_context,
    get_macro_indicators_cached,
    fetch_macro_indicators,
    build_macro_block,
    Indicator,
)
from .fundamentals import get_fundamentals, ts_to_days
from .insider import get_insider_summary
from .fx import get_fx_rate, to_sek
from .live import get_live_price, get_live_prices, get_live_price_detail
from .universe import (
    refresh_universe,
    get_universe,
    get_universe_symbols,
    get_universe_updates,
    export_universe,
    last_refresh as last_universe_refresh,
    MONTROSE_COUNTRIES,
)

__all__ = [
    # cache
    "DataCache", "get_cache",
    # prices
    "get_prices", "get_prices_batch", "get_prices_since",
    "get_current_price", "get_vix", "get_sector",
    # indicators
    "rsi", "rsi_series", "atr", "vwap", "sma", "ema",
    "ann_vol", "pct_return", "key_levels", "daily_momentum_score",
    # news
    "get_news", "get_news_cached", "get_market_headlines",
    "fetch_rss", "fetch_newsapi", "fetch_finnhub_news", "fetch_yfinance_news",
    "newsapi_available",
    "score_sentiment", "score_and_save", "build_keyword_map", "SWEDISH_RSS_FEEDS",
    # macro
    "get_macro_context", "get_macro_indicators_cached", "fetch_macro_indicators",
    "build_macro_block", "Indicator",
    # fundamentals
    "get_fundamentals", "ts_to_days",
    # insider
    "get_insider_summary",
    # fx
    "get_fx_rate", "to_sek",
    # live prices
    "get_live_price", "get_live_prices", "get_live_price_detail",
    # universe
    "refresh_universe", "get_universe", "get_universe_symbols",
    "get_universe_updates", "export_universe", "last_universe_refresh",
    "MONTROSE_COUNTRIES",
]
