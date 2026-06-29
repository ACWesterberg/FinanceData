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
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO news "
                "(ticker, headline, source_url, published_at, sentiment_label, sentiment_score, fetched_at) "
                "VALUES (:ticker, :headline, :source_url, :published_at, :sentiment_label, :sentiment_score, :fetched_at)",
                [{**item, "ticker": ticker, "fetched_at": now} for item in items],
            )

    def get_news(self, ticker: str, since_date: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT headline, published_at, sentiment_label, sentiment_score FROM news "
                "WHERE ticker = ? AND fetched_at >= ? ORDER BY fetched_at DESC",
                (ticker, since_date),
            ).fetchall()
        return [dict(r) for r in rows]

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

    def increment_rate_count(self, provider: str) -> int:
        today = datetime.utcnow().date().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO rate_limits (provider, date, count) VALUES (?, ?, 1) "
                "ON CONFLICT(provider, date) DO UPDATE SET count = count + 1",
                (provider, today),
            )
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE provider = ? AND date = ?",
                (provider, today),
            ).fetchone()
        return row["count"] if row else 1

    def get_rate_count(self, provider: str) -> int:
        today = datetime.utcnow().date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE provider = ? AND date = ?",
                (provider, today),
            ).fetchone()
        return row["count"] if row else 0


_instance: DataCache | None = None


def get_cache() -> DataCache:
    global _instance
    if _instance is None:
        _instance = DataCache()
    return _instance
