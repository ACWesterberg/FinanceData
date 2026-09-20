import inspect
from pathlib import Path

import pandas as pd

import financedata as fd
from financedata.cache import DataCache


def test_consumer_symbols_are_public():
    expected = {
        "get_prices_since", "rsi", "pct_return", "ann_vol", "get_cache",
        "get_news_cached", "score_sentiment", "score_and_save",
        "get_fundamentals", "ts_to_days", "get_live_price",
        "get_live_prices", "get_fx_rate",
    }
    assert expected <= set(fd.__all__)
    assert all(callable(getattr(fd, name)) for name in expected)


def test_legacy_signatures_are_stable():
    assert list(inspect.signature(fd.get_prices_since).parameters) == ["tickers", "since", "force_refresh"]
    assert list(inspect.signature(fd.get_news_cached).parameters) == [
        "tickers", "feeds", "names", "max_age_hours", "ttl_hours",
        "use_newsapi", "use_fallback", "market", "force_refresh",
    ]
    assert list(inspect.signature(fd.get_fx_rate).parameters) == ["base", "quote", "on"]
    assert list(inspect.signature(fd.get_live_price).parameters) == ["ticker", "ttl_minutes"]


def test_indicator_shapes():
    closes = pd.Series(range(1, 32), dtype=float)
    assert fd.rsi(closes) is None or isinstance(fd.rsi(closes), float)
    assert isinstance(fd.pct_return(closes, 5), float)
    assert isinstance(fd.ann_vol(closes), float)
    assert fd.ts_to_days("not-a-timestamp") is None


def test_cache_schema_version_and_empty_fetch_timestamp(tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    cache.mark_news_fetched("EMPTY")
    assert cache.get_news_fetch_time("EMPTY") is not None
    with cache._conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_rate_limit_claim_is_atomic_and_bounded(tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    assert cache.claim_rate_limit("provider", "window", 2)
    assert cache.claim_rate_limit("provider", "window", 2)
    assert not cache.claim_rate_limit("provider", "window", 2)
    assert cache.get_rate_count("provider", "window") == 2


def test_fake_preserves_essential_return_shapes():
    fake = fd.FakeFinanceData(live_prices={"A": 12.0}, fx_rates={("USD", "SEK"): 10.0})
    assert fake.get_prices_since(["A"], "2024-01-01") == {"A": False}
    assert fake.get_news_cached(["A"]) == {}
    assert fake.get_live_prices(["A", "B"]) == {"A": 12.0, "B": None}
    assert fake.get_fx_rate("USD") == 10.0
