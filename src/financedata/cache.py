"""
Shared SQLite cache for all financedata modules.

DB path: $FINANCEDATA_DB (default: ~/.financedata/cache.db)
All data tables live here; portfolio state stays in each project's own DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL NOT NULL,
    volume      REAL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker      TEXT PRIMARY KEY,
    data_json   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_snapshots (
    market      TEXT PRIMARY KEY,
    data_json   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    headline        TEXT NOT NULL,
    summary         TEXT,
    source_url      TEXT,
    published_at    TEXT,
    sentiment_label TEXT,
    sentiment_score REAL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news (ticker, fetched_at);

CREATE TABLE IF NOT EXISTS insider_cache (
    cache_key   TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

-- Persisted daily API quota counters (survive process restarts)
CREATE TABLE IF NOT EXISTS rate_limits (
    provider    TEXT NOT NULL,
    date        TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    pair        TEXT NOT NULL,
    date        TEXT NOT NULL,
    rate        REAL NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (pair, date)
);

CREATE TABLE IF NOT EXISTS live_prices (
    ticker      TEXT PRIMARY KEY,
    price       REAL NOT NULL,
    price_time  TEXT,
    fetched_at  TEXT NOT NULL
);

-- Records the last time news was fetched for a ticker (or reserved market key),
-- so the read-through cache knows a ticker was checked even when it had no news.
CREATE TABLE IF NOT EXISTS news_fetch_log (
    ticker      TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL
);
"""


def _default_db_path() -> Path:
    path = Path(os.environ.get("FINANCEDATA_DB", "~/.financedata/cache.db")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class DataCache:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or _default_db_path()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn) -> None:
        """Lightweight, idempotent migrations for DBs created before a column existed."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(news)").fetchall()}
        if "source" not in cols:
            conn.execute("ALTER TABLE news ADD COLUMN source TEXT")
        if "summary" not in cols:
            conn.execute("ALTER TABLE news ADD COLUMN summary TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Prices ────────────────────────────────────────────────────────────────

    def save_prices(self, ticker: str, rows: list[dict]) -> None:
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO prices "
                "(ticker, date, open, high, low, close, volume, fetched_at) "
                "VALUES (:ticker, :date, :open, :high, :low, :close, :volume, :fetched_at)",
                [{**r, "ticker": ticker, "fetched_at": now} for r in rows],
            )

    def get_prices(self, ticker: str, since_date: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if since_date:
                rows = conn.execute(
                    "SELECT date, open, high, low, close, volume FROM prices "
                    "WHERE ticker = ? AND date >= ? ORDER BY date ASC",
                    (ticker, since_date),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT date, open, high, low, close, volume FROM prices "
                    "WHERE ticker = ? ORDER BY date ASC",
                    (ticker,),
                ).fetchall()
        return [dict(r) for r in rows]

    def latest_price_date(self, ticker: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM prices WHERE ticker = ?", (ticker,)
            ).fetchone()
        return row["d"] if row and row["d"] else None

    # ── Fundamentals ──────────────────────────────────────────────────────────

    def save_fundamentals(self, ticker: str, data: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fundamentals (ticker, data_json, fetched_at) VALUES (?, ?, ?)",
                (ticker, json.dumps(data), datetime.utcnow().isoformat()),
            )

    def get_fundamentals(self, ticker: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM fundamentals WHERE ticker = ?", (ticker,)
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def get_stale_fundamentals(self, tickers: list[str], ttl_days: int = 7) -> list[str]:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=ttl_days)).isoformat()
        with self._conn() as conn:
            fresh = {
                r["ticker"]
                for r in conn.execute(
                    "SELECT ticker FROM fundamentals WHERE fetched_at > ?", (cutoff,)
                ).fetchall()
            }
        return [t for t in tickers if t not in fresh]

    def get_all_fundamentals(self, tickers: list[str]) -> dict[str, dict]:
        if not tickers:
            return {}
        placeholders = ",".join("?" * len(tickers))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT ticker, data_json FROM fundamentals WHERE ticker IN ({placeholders})",
                tickers,
            ).fetchall()
        return {r["ticker"]: json.loads(r["data_json"]) for r in rows}

    # ── Macro ─────────────────────────────────────────────────────────────────

    def save_macro(self, market: str, data: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO macro_snapshots (market, data_json, fetched_at) VALUES (?, ?, ?)",
                (market, json.dumps(data), datetime.utcnow().isoformat()),
            )

    def get_macro(self, market: str, max_age_hours: float = 6.0) -> dict | None:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM macro_snapshots WHERE market = ? AND fetched_at > ?",
                (market, cutoff),
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    # ── News ──────────────────────────────────────────────────────────────────

    def save_news(self, ticker: str, items: list[dict]) -> None:
        """Upsert articles for a ticker, keyed by headline. Tolerates items missing
        sentiment/source fields (e.g. raw fetches before scoring). Re-seen headlines
        have their fetched_at refreshed (so they stay inside the cache TTL window)
        and any newly-provided fields filled in, without creating duplicate rows."""
        if not items:
            return
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            existing = {
                r["headline"]
                for r in conn.execute(
                    "SELECT headline FROM news WHERE ticker = ?", (ticker,)
                ).fetchall()
            }
            inserts, updates = [], []
            for item in items:
                headline = item.get("headline")
                if not headline:
                    continue
                row = {
                    "ticker": ticker,
                    "headline": headline,
                    "summary": item.get("summary"),
                    "source_url": item.get("source_url"),
                    "source": item.get("source"),
                    "published_at": item.get("published_at"),
                    "sentiment_label": item.get("sentiment_label"),
                    "sentiment_score": item.get("sentiment_score"),
                    "fetched_at": now,
                }
                (updates if headline in existing else inserts).append(row)

            if inserts:
                conn.executemany(
                    "INSERT INTO news "
                    "(ticker, headline, summary, source_url, source, published_at, "
                    "sentiment_label, sentiment_score, fetched_at) "
                    "VALUES (:ticker, :headline, :summary, :source_url, :source, :published_at, "
                    ":sentiment_label, :sentiment_score, :fetched_at)",
                    inserts,
                )
            if updates:
                conn.executemany(
                    "UPDATE news SET "
                    "fetched_at = :fetched_at, "
                    "summary = COALESCE(:summary, summary), "
                    "source_url = COALESCE(:source_url, source_url), "
                    "source = COALESCE(:source, source), "
                    "published_at = COALESCE(:published_at, published_at), "
                    "sentiment_label = COALESCE(:sentiment_label, sentiment_label), "
                    "sentiment_score = COALESCE(:sentiment_score, sentiment_score) "
                    "WHERE ticker = :ticker AND headline = :headline",
                    updates,
                )

    def get_news(self, ticker: str, since_date: str) -> list[dict]:
        """Return stored articles for a ticker with fetched_at >= since_date.
        since_date may be a date ('YYYY-MM-DD') or a full ISO timestamp."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT headline, summary, source_url, source, published_at, "
                "sentiment_label, sentiment_score, fetched_at FROM news "
                "WHERE ticker = ? AND fetched_at >= ? ORDER BY fetched_at DESC",
                (ticker, since_date),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_news_fetched(self, ticker: str) -> None:
        """Record that news was just fetched for a ticker (or reserved market key)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO news_fetch_log (ticker, fetched_at) VALUES (?, ?)",
                (ticker, datetime.utcnow().isoformat()),
            )

    def get_stale_news_tickers(self, tickers: list[str], ttl_hours: float = 6.0) -> list[str]:
        """Tickers whose last news fetch is older than ttl_hours (or never fetched)."""
        from datetime import timedelta
        if not tickers:
            return []
        cutoff = (datetime.utcnow() - timedelta(hours=ttl_hours)).isoformat()
        with self._conn() as conn:
            fresh = {
                r["ticker"]
                for r in conn.execute(
                    "SELECT ticker FROM news_fetch_log WHERE fetched_at > ?", (cutoff,)
                ).fetchall()
            }
        return [t for t in tickers if t not in fresh]

    # ── Insider ───────────────────────────────────────────────────────────────

    def save_insider(self, cache_key: str, summary: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO insider_cache (cache_key, summary, fetched_at) VALUES (?, ?, ?)",
                (cache_key, summary, datetime.utcnow().isoformat()),
            )

    def get_insider(self, cache_key: str, max_age_hours: float = 24.0) -> str | None:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT summary FROM insider_cache WHERE cache_key = ? AND fetched_at > ?",
                (cache_key, cutoff),
            ).fetchone()
        return row["summary"] if row else None

    # ── Rate limits ───────────────────────────────────────────────────────────

    def increment_rate_count(self, provider: str, period: str | None = None) -> int:
        """Increment and return the counter for (provider, period). period defaults
        to today's date (daily quota, e.g. Alpha Vantage); pass a finer key such as
        a minute bucket for per-minute limits (e.g. Finnhub)."""
        period = period or datetime.utcnow().date().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO rate_limits (provider, date, count) VALUES (?, ?, 1) "
                "ON CONFLICT(provider, date) DO UPDATE SET count = count + 1",
                (provider, period),
            )
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE provider = ? AND date = ?",
                (provider, period),
            ).fetchone()
        return row["count"] if row else 1

    def get_rate_count(self, provider: str, period: str | None = None) -> int:
        period = period or datetime.utcnow().date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE provider = ? AND date = ?",
                (provider, period),
            ).fetchone()
        return row["count"] if row else 0

    def prune_rate_counts(self, provider: str, keep_period: str) -> None:
        """Drop stale rate-limit rows for a provider (buckets older than keep_period),
        so per-minute windows don't accumulate rows indefinitely."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM rate_limits WHERE provider = ? AND date < ?",
                (provider, keep_period),
            )

    # ── FX rates ──────────────────────────────────────────────────────────────

    def save_fx_rate(self, pair: str, date: str, rate: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates (pair, date, rate, fetched_at) VALUES (?, ?, ?, ?)",
                (pair, date, rate, datetime.utcnow().isoformat()),
            )

    def get_fx_rate(self, pair: str, date: str, *, spot: bool = True) -> float | None:
        """
        spot=True  → only return if fetched today (same-day TTL for spot rates)
        spot=False → historical; return regardless of fetch age
        """
        with self._conn() as conn:
            if spot:
                today = datetime.utcnow().strftime("%Y-%m-%d")
                row = conn.execute(
                    "SELECT rate FROM fx_rates WHERE pair = ? AND date = ? AND fetched_at >= ?",
                    (pair, date, today),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT rate FROM fx_rates WHERE pair = ? AND date = ?",
                    (pair, date),
                ).fetchone()
        return row["rate"] if row else None

    # ── Live prices ───────────────────────────────────────────────────────────

    def save_live_price(self, ticker: str, price: float, price_time: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO live_prices (ticker, price, price_time, fetched_at) VALUES (?, ?, ?, ?)",
                (ticker, price, price_time, datetime.utcnow().isoformat()),
            )

    def get_live_price(self, ticker: str, ttl_minutes: int = 10) -> tuple[float, str | None] | None:
        """Returns (price, price_time) if within TTL, else None."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(minutes=ttl_minutes)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT price, price_time FROM live_prices WHERE ticker = ? AND fetched_at > ?",
                (ticker, cutoff),
            ).fetchone()
        return (row["price"], row["price_time"]) if row else None


_instance: DataCache | None = None


def get_cache() -> DataCache:
    global _instance
    if _instance is None:
        _instance = DataCache()
    return _instance
