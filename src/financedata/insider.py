"""
Insider trading data: SEC EDGAR Form 4 (US) + FI Insynsregistret (Nordic).
Results are cached in SQLite for 24 hours.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta

import httpx

from .cache import get_cache

logger = logging.getLogger(__name__)

_CACHE_TTL_HOURS = 24.0

_FI_COLUMN_ALIASES: dict[str, list[str]] = {
    "issuer": ["Emittent", "Issuer", "emittent", "issuer", "Bolag"],
    "date":   ["Handelsdatum", "TransactionDate", "Datum", "Date", "Transaction date"],
    "person": ["Person", "Insider", "Namn", "Name"],
    "type":   ["Transaktionstyp", "TransactionType", "Typ", "Type", "Transaction type"],
    "volume": ["Volym", "Volume", "Antal", "Quantity"],
    "price":  ["Kurs", "Price", "Pris"],
}


def get_insider_summary(ticker: str, market: str) -> str:
    """
    Return a short insider-activity summary for a ticker.
    market: "us" → SEC EDGAR | "nordic" → FI Insynsregistret
    Cached 24 hours in SQLite.
    """
    cache_key = f"{market}:{ticker}"
    cache = get_cache()
    cached = cache.get_insider(cache_key, max_age_hours=_CACHE_TTL_HOURS)
    if cached is not None:
        return cached

    text = _fetch_sec_edgar(ticker) if market == "us" else _fetch_fi_insynsregistret(ticker)
    cache.save_insider(cache_key, text)
    return text


def _fetch_sec_edgar(ticker: str) -> str:
    try:
        since = _days_ago(30)
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{ticker}%22&dateRange=custom&startdt={since}&forms=4"
        )
        resp = httpx.get(
            url, timeout=10,
            headers={"User-Agent": "financedata/1.0 contact@example.com"},
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            return f"No recent insider filings found for {ticker} (SEC EDGAR)."
        summaries = []
        for hit in hits[:5]:
            src = hit.get("_source", {})
            summaries.append(
                f"{src.get('file_date', '?')}: "
                f"{src.get('display_names', ['?'])[0]} — Form 4"
            )
        return "SEC Insider Activity:\n" + "\n".join(summaries)
    except Exception as exc:
        logger.debug("SEC EDGAR error for %s: %s", ticker, exc)
        return f"Insider data unavailable for {ticker}."


def _resolve_col(row: dict, canonical: str) -> str:
    for alias in _FI_COLUMN_ALIASES.get(canonical, []):
        val = row.get(alias, "")
        if val:
            return val.strip()
    return "?"


def _detect_delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return ";" if line.count(";") >= line.count(",") else ","
    return ";"


def _decode_fi_response(content: bytes) -> str:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")


def _fetch_fi_insynsregistret(ticker: str) -> str:
    try:
        url = "https://fi.se/contentassets/2c7b86aa49b74e37b3eb8fe91da5ccbc/insynsregistret.csv"
        resp = httpx.get(url, timeout=15, headers={"User-Agent": "financedata/1.0"})
        resp.raise_for_status()

        text = _decode_fi_response(resp.content)
        delimiter = _detect_delimiter(text)
        ticker_base = ticker.split(".")[0].upper()
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

        since = datetime.utcnow() - timedelta(days=30)
        matches = []
        for row in reader:
            issuer = _resolve_col(row, "issuer").upper()
            if ticker_base not in issuer:
                continue
            date_str = _resolve_col(row, "date")
            try:
                if datetime.strptime(date_str[:10], "%Y-%m-%d") >= since:
                    matches.append(row)
            except ValueError:
                pass

        if not matches:
            return f"No recent insider activity for {ticker_base} (FI register)."

        summaries = [
            f"{_resolve_col(r, 'person')}: {_resolve_col(r, 'type')} — "
            f"{_resolve_col(r, 'volume')} shares @ {_resolve_col(r, 'price')}"
            for r in matches[:5]
        ]
        return "FI Insider Activity:\n" + "\n".join(summaries)
    except Exception as exc:
        logger.debug("FI Insynsregistret error for %s: %s", ticker, exc)
        return f"Nordic insider data unavailable for {ticker}."


def _days_ago(n: int) -> str:
    return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%d")
