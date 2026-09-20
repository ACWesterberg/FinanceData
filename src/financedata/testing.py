"""Small deterministic fake implementing the APIs used by sibling projects."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class FakeFinanceData:
    """In-memory fixture suitable for downstream unit tests (no network or SQLite)."""

    prices: dict[str, pd.DataFrame] = field(default_factory=dict)
    news: dict[str, list[dict]] = field(default_factory=dict)
    fundamentals: dict[str, dict] = field(default_factory=dict)
    live_prices: dict[str, float | None] = field(default_factory=dict)
    fx_rates: dict[tuple[str, str], float] = field(default_factory=dict)

    def get_prices_since(self, tickers: list[str], since: str, force_refresh: bool = False) -> dict[str, bool]:
        return {ticker: ticker in self.prices for ticker in tickers}

    def get_news_cached(self, tickers: list[str], **kwargs) -> dict[str, list[dict]]:
        return {ticker: self.news[ticker] for ticker in tickers if self.news.get(ticker)}

    def get_fundamentals(self, tickers: list[str], **kwargs) -> dict[str, dict]:
        return {ticker: self.fundamentals[ticker] for ticker in tickers if ticker in self.fundamentals}

    def get_live_price(self, ticker: str, ttl_minutes: int = 10) -> float | None:
        return self.live_prices.get(ticker)

    def get_live_prices(self, tickers: list[str], ttl_minutes: int = 10) -> dict[str, float | None]:
        return {ticker: self.live_prices.get(ticker) for ticker in tickers}

    def get_fx_rate(self, base: str, quote: str = "SEK", *, on: str | None = None) -> float | None:
        return 1.0 if base == quote else self.fx_rates.get((base, quote))
