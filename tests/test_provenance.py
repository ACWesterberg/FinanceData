from pathlib import Path
import sqlite3

from financedata.cache import DataCache
from financedata.contracts import DataResult
from financedata import fundamentals, news


def test_news_cache_hit_diagnostics(monkeypatch, tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    cache.save_news("A", [{"headline": "A wins", "published_at": "2026-09-19 10:00"}])
    cache.mark_news_fetched("A")
    monkeypatch.setattr("financedata.cache.get_cache", lambda: cache)

    result = news.get_news_cached_with_provenance(["A"], feeds=[], use_newsapi=False, use_fallback=False)
    assert isinstance(result, DataResult)
    assert result.items["A"][0]["headline"] == "A wins"
    assert result.provenance["A"].cache_state == "cache_hit"
    assert result.provenance["A"].effective_start == "2026-09-19 10:00"


def test_news_empty_refresh_is_not_failure(monkeypatch, tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    monkeypatch.setattr("financedata.cache.get_cache", lambda: cache)
    monkeypatch.setattr(news, "get_news", lambda *args, **kwargs: {})
    result = news.get_news_cached_with_provenance(["A"], feeds=[], use_newsapi=False, use_fallback=False)
    assert result.items == {}
    assert result.provenance["A"].cache_state == "genuine_empty"
    assert result.provenance["A"].completeness == "empty"


def test_news_provider_failure_uses_stale_fallback(monkeypatch, tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    cache.save_news("A", [{"headline": "old"}])
    monkeypatch.setattr("financedata.cache.get_cache", lambda: cache)
    monkeypatch.setattr(news, "get_news", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    result = news.get_news_cached_with_provenance(["A"], feeds=[], use_newsapi=False, use_fallback=False)
    assert result.items["A"][0]["headline"] == "old"
    assert result.provenance["A"].cache_state == "stale_fallback"
    assert result.provenance["A"].completeness == "partial"


def test_swallowed_provider_failure_is_not_reported_as_empty(monkeypatch, tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    monkeypatch.setattr("financedata.cache.get_cache", lambda: cache)

    def provider_failure(*args, **kwargs):
        news._record_provider("yfinance", "failure", ticker="A", warning="offline")
        return {}

    monkeypatch.setattr(news, "get_news", provider_failure)
    result = news.get_news_cached_with_provenance(["A"], feeds=[], use_newsapi=False)
    assert result.provenance["A"].cache_state == "provider_failure"
    assert result.provenance["A"].warnings == ("yfinance: offline",)
    assert cache.get_news_fetch_time("A") is None


def test_partial_provider_coverage_is_explicit(monkeypatch, tmp_path: Path):
    cache = DataCache(tmp_path / "cache.db")
    monkeypatch.setattr("financedata.cache.get_cache", lambda: cache)

    def partial(*args, **kwargs):
        news._record_provider("rss:one", "success")
        news._record_provider("newsapi", "rate_limited", ticker="A", warning="HTTP 429")
        return {"A": [{"headline": "covered by RSS"}]}

    monkeypatch.setattr(news, "get_news", partial)
    result = news.get_news_cached_with_provenance(["A"], feeds=["one"])
    assert result.provenance["A"].cache_state == "partial_coverage"
    assert result.provenance["A"].completeness == "partial"


def test_future_cache_schema_is_not_silently_downgraded(tmp_path: Path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 99")
    try:
        DataCache(path)
    except RuntimeError as exc:
        assert "newer than supported" in str(exc)
    else:
        raise AssertionError("future schema should be rejected")


def test_historical_fundamentals_are_honestly_unsupported():
    result = fundamentals.get_fundamentals_with_provenance(["A"], as_of="2020-01-01")
    assert result.items == {}
    assert result.provenance["A"].historical_capability is False
    assert result.provenance["A"].cache_state == "unsupported"
