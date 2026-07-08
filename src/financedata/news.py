"""
News aggregation: RSS + NewsAPI + per-ticker fallbacks (Finnhub, yfinance),
with optional FinBERT sentiment scoring and a shared SQLite read-through cache.

Sources, in order of use:
  1. RSS feeds        — Nordic/Swedish coverage, no key required
  2. NewsAPI          — English-language, needs NEWS_API_KEY
  3. Finnhub          — per-ticker company news (US-focused), needs FINNHUB_API_KEY
  4. yfinance (Yahoo) — universal free backstop, no key required

Environment:
  NEWS_API_KEY                  — optional; enables NewsAPI (English news)
  FINNHUB_API_KEY               — optional; enables Finnhub per-ticker fallback
  FINNHUB_RATE_LIMIT_PER_MIN    — optional; Finnhub per-minute request cap (default 60)
  NEWSAPI_COOLDOWN_MINUTES      — optional; how long the breaker skips NewsAPI after a
                                  429 before retrying (default 20)

FinBERT is lazy-loaded; install `financedata[sentiment]` extras to enable it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import httpx

logger = logging.getLogger(__name__)

# ── NewsAPI rate-limit breaker ────────────────────────────────────────────────
# On a 429, trip a breaker so this batch and subsequent calls skip NewsAPI
# (RSS/fallbacks only) instead of retrying a quota that won't clear until
# tomorrow.
_newsapi_cooldown_until: Optional[datetime] = None


def _newsapi_cooldown_minutes() -> int:
    try:
        return int(os.environ.get("NEWSAPI_COOLDOWN_MINUTES", "20"))
    except ValueError:
        return 20


def newsapi_available() -> bool:
    """True when NEWS_API_KEY is set and the rate-limit breaker isn't tripped."""
    if not os.environ.get("NEWS_API_KEY"):
        return False
    if _newsapi_cooldown_until and datetime.now(timezone.utc) < _newsapi_cooldown_until:
        return False
    return True


def _trip_newsapi_breaker() -> None:
    """Skip NewsAPI for the rest of this batch and for NEWSAPI_COOLDOWN_MINUTES
    afterwards — a 429 almost always means the daily quota is spent, so there's
    no point hitting it again until the cooldown expires."""
    global _newsapi_cooldown_until
    cooldown = _newsapi_cooldown_minutes()
    _newsapi_cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=cooldown)
    logger.warning("NewsAPI rate-limited — skipping NewsAPI for %d min (RSS/fallback only)", cooldown)


# Swedish financial RSS feeds (used across both DeepSwing and Fond)
SWEDISH_RSS_FEEDS: list[str] = [
    "https://www.di.se/rss",
    "https://borsdata.se/rss",
    "https://www.redeye.se/rss",
]

# Generic words that appear in company names but match far too broadly
_GENERIC_WORDS = {
    "water", "group", "power", "solar", "media", "steel", "foods", "paper",
    "drugs", "stone", "cable", "fiber", "clean", "smart", "micro", "north",
    "south", "east", "west", "global", "digital", "capital", "holding",
    "holdings", "international", "services", "solutions", "technologies",
    "systems", "energy", "finance", "financial", "properties", "realty",
    "partners", "ventures", "industries", "resources", "networks",
    "communications", "healthcare", "pharma", "biotech", "management",
    "investment", "investments", "corporation", "company", "limited",
    "bancorp", "bancshares", "equities", "markets", "assets", "trust",
    "funds", "technology", "innovation", "innovations",
}


# ── Keyword map ───────────────────────────────────────────────────────────────

def build_keyword_map(
    tickers: list[str],
    names: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """
    Build {ticker: [keywords]} for headline matching.

    tickers: Yahoo Finance symbols (e.g. "VOLV-B.ST", "AAPL")
    names:   optional {ticker: company_name} for name-based keywords
    """
    kmap: dict[str, list[str]] = {}
    for t in tickers:
        kws: list[str] = []
        stem = t.rsplit(".", 1)[0]
        base = stem.split("-")[0].lower()
        stem_lower = stem.lower()
        if len(base) >= 4:
            kws.append(stem_lower)
            if base != stem_lower:
                kws.append(base)
        if names:
            name = names.get(t, "")
            for word in name.split():
                word = re.sub(r"[^a-z0-9]", "", word.lower())
                if len(word) >= 5 and word not in _GENERIC_WORDS:
                    kws.append(word)
        kmap[t] = list(set(kws))
    return kmap


def _match_tickers(text: str, keyword_map: dict[str, list[str]]) -> list[str]:
    text_lower = text.lower()
    return [
        ticker for ticker, kws in keyword_map.items()
        if any(re.search(r"\b" + re.escape(kw) + r"\b", text_lower) for kw in kws)
    ]


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_rss(
    feeds: list[str],
    tickers: list[str],
    names: dict[str, str] | None = None,
    max_age_hours: int = 72,
) -> dict[str, list[dict]]:
    """
    Pull RSS feeds and match headlines to tickers.
    Returns {ticker: [{headline, source_url, published_at}]}.
    """
    keyword_map = build_keyword_map(tickers, names)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ticker_news: dict[str, list[dict]] = {t: [] for t in tickers}

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.debug("RSS error (%s): %s", feed_url, exc)
            continue

        source_name = getattr(feed.feed, "title", feed_url.split("/")[2])[:30]

        for entry in feed.entries:
            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            text = f"{title} {summary}"
            link = getattr(entry, "link", "") or ""

            published_dt = None
            published_str = None
            for attr in ("published_parsed", "updated_parsed"):
                parsed = getattr(entry, attr, None)
                if parsed:
                    published_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                    published_str = published_dt.strftime("%Y-%m-%d %H:%M")
                    break

            if published_dt and published_dt < cutoff:
                continue

            for ticker in _match_tickers(text, keyword_map):
                ticker_news[ticker].append({
                    "headline": title[:500],
                    "source_url": link[:500],
                    "published_at": published_str,
                    "source": source_name,
                })

    return {k: v for k, v in ticker_news.items() if v}


def fetch_newsapi(
    query: str,
    max_age_hours: int = 48,
    page_size: int = 20,
) -> list[dict]:
    """Fetch English-language news from NewsAPI for a search query.

    Gives up immediately on 429 and trips the shared breaker (see
    `newsapi_available`) instead of retrying with backoff — NewsAPI's free
    tier is a daily quota, so a 429 won't clear within the same run and
    retrying just burns time. RSS and the per-ticker fallback sources still
    cover for it (see `get_news`'s `use_fallback`).
    """
    api_key = os.environ.get("NEWS_API_KEY", "")
    if not api_key:
        return []

    since = (datetime.utcnow() - timedelta(hours=max_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "q": query,
        "from": since,
        "sortBy": "relevancy",
        "language": "en",
        "pageSize": page_size,
        "apiKey": api_key,
    }
    try:
        resp = httpx.get("https://newsapi.org/v2/everything", params=params, timeout=10)
        resp.raise_for_status()
        return [
            {
                "headline": a.get("title", "")[:500],
                "source_url": a.get("url", "")[:500],
                "published_at": a.get("publishedAt", ""),
                "source": a.get("source", {}).get("name", "NewsAPI"),
            }
            for a in resp.json().get("articles", [])
        ]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            _trip_newsapi_breaker()
        else:
            logger.warning("NewsAPI error for '%s': %s", query, exc)
        return []
    except Exception as exc:
        logger.warning("NewsAPI error for '%s': %s", query, exc)
        return []


# ── Per-ticker fallback sources ───────────────────────────────────────────────

def _finnhub_limit_per_min() -> int:
    try:
        return int(os.environ.get("FINNHUB_RATE_LIMIT_PER_MIN", "60"))
    except ValueError:
        return 60


def _finnhub_rate_ok() -> bool:
    """Shared, cross-process per-minute limiter (Finnhub free tier = 60 req/min),
    tracked in the SQLite rate_limits table keyed by minute. Returns False when the
    current minute's budget is spent so callers skip Finnhub and fall to yfinance
    instead of triggering 429s. Best-effort: a small overshoot is possible when
    multiple processes race within the same minute, hence the default headroom."""
    from .cache import get_cache
    cache = get_cache()
    window = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    if cache.get_rate_count("finnhub", window) >= _finnhub_limit_per_min():
        return False
    cache.increment_rate_count("finnhub", window)
    cache.prune_rate_counts("finnhub", window)  # keep only the current minute bucket
    return True


def fetch_finnhub_news(ticker: str, max_age_days: int = 7, limit: int = 10) -> list[dict]:
    """Per-ticker company news via Finnhub (needs FINNHUB_API_KEY). US-focused.
    Subject to a shared per-minute rate limiter — returns [] when the key is unset,
    the minute budget is spent, or the request fails (so the caller falls back)."""
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return []
    if not _finnhub_rate_ok():
        logger.debug("Finnhub per-minute limit reached — skipping %s", ticker)
        return []
    try:
        today = datetime.now(timezone.utc).date()
        params = {
            "symbol": ticker,
            "from": (today - timedelta(days=max_age_days)).isoformat(),
            "to": today.isoformat(),
            "token": api_key,
        }
        resp = httpx.get("https://finnhub.io/api/v1/company-news", params=params, timeout=10.0)
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as exc:
        logger.debug("Finnhub news failed for %s: %s", ticker, exc)
        return []

    items: list[dict] = []
    for a in raw[:limit]:
        title = (a.get("headline") or "").strip()
        if not title:
            continue
        published = ""
        ts = a.get("datetime")
        if ts:
            try:
                published = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError, TypeError):
                published = ""
        items.append({
            "headline": title[:500],
            "source_url": (a.get("url") or "")[:500],
            "published_at": published,
            "source": a.get("source") or "Finnhub",
        })
    return items


def fetch_yfinance_news(ticker: str, limit: int = 10) -> list[dict]:
    """Per-ticker news via yfinance (Yahoo). No API key — the universal backstop.
    Handles both the newer ('content' envelope) and older (flat) yfinance schemas."""
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).news or []
    except Exception as exc:
        logger.debug("yfinance news failed for %s: %s", ticker, exc)
        return []

    items: list[dict] = []
    for entry in raw[:limit]:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if isinstance(content, dict):  # newer schema (yfinance >= ~0.2.40)
            title = (content.get("title") or "").strip()
            source = (content.get("provider") or {}).get("displayName") or "Yahoo Finance"
            url = ((content.get("canonicalUrl") or {}).get("url")
                   or (content.get("clickThroughUrl") or {}).get("url") or "")
            published = str(content.get("pubDate") or content.get("displayTime") or "")
            published = published.replace("T", " ").rstrip("Z")[:16]
        else:  # older flat schema
            title = (entry.get("title") or "").strip()
            source = entry.get("publisher") or "Yahoo Finance"
            url = entry.get("link") or ""
            ts = entry.get("providerPublishTime")
            published = ""
            if ts:
                try:
                    published = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError, TypeError):
                    published = ""
        if not title:
            continue
        items.append({
            "headline": title[:500],
            "source_url": (url or "")[:500],
            "published_at": published,
            "source": source,
        })
    return items


def _looks_us(ticker: str) -> bool:
    """Heuristic: US Yahoo symbols have no exchange suffix (AAPL, BRK-B), while
    non-US ones carry a dotted suffix (VOLV-B.ST, AZN.L, SAP.DE). Used to route
    the Finnhub fallback in mixed/global universes when no explicit market is given."""
    return "." not in ticker


def _fetch_fallback_news(ticker: str, market: str | None) -> list[dict]:
    """Free per-ticker news when the primary sources (RSS/NewsAPI) are empty.
    Finnhub (US-focused) is preferred for US tickers when a key is set; yfinance is
    the universal free backstop. With an explicit market="us" Finnhub is always
    tried; with market=None it's tried only for symbols that look US, so a global
    universe doesn't waste Finnhub calls on clearly non-US tickers."""
    try_finnhub = market == "us" or (market is None and _looks_us(ticker))
    if try_finnhub and os.environ.get("FINNHUB_API_KEY"):
        articles = fetch_finnhub_news(ticker)
        if articles:
            return articles
    return fetch_yfinance_news(ticker)


def get_news(
    tickers: list[str],
    feeds: list[str],
    names: dict[str, str] | None = None,
    max_age_hours: int = 72,
    use_newsapi: bool = True,
    use_fallback: bool = False,
    market: str | None = None,
) -> dict[str, list[dict]]:
    """
    Unified news fetch: RSS feeds + optional NewsAPI + optional per-ticker fallback.

    tickers:      list of Yahoo Finance symbols
    feeds:        RSS feed URLs to poll
    names:        optional {ticker: "Company Name"} for better keyword matching
    use_newsapi:  query NewsAPI per ticker (subject to the rate-limit breaker)
    use_fallback: for tickers still empty, try Finnhub (US, if keyed) then yfinance —
                  essential for US tickers, which have no Nordic RSS coverage
    market:       "us" | "nordic" | None; gates Finnhub to US in the fallback
    Returns:      {ticker: [article_dicts]}
    """
    result = fetch_rss(feeds, tickers, names=names, max_age_hours=max_age_hours)

    def _merge(ticker: str, articles: list[dict]) -> None:
        if not articles:
            return
        existing = result.get(ticker, [])
        seen = {a["headline"] for a in existing}
        for a in articles:
            if a["headline"] not in seen:
                existing.append(a)
                seen.add(a["headline"])
        if existing:
            result[ticker] = existing

    if use_newsapi and newsapi_available():
        t0 = time.monotonic()
        fetched = 0
        for ticker in tickers:
            if not newsapi_available():
                # A 429 tripped the breaker mid-batch — stop hitting NewsAPI for
                # the rest of these tickers; RSS/fallback still cover them below.
                break
            stem = ticker.rsplit(".", 1)[0].split("-")[0]
            _merge(ticker, fetch_newsapi(stem, max_age_hours=max_age_hours))
            fetched += 1
        elapsed = time.monotonic() - t0
        logger.info("NewsAPI fetch complete: %d/%d tickers in %.1fs", fetched, len(tickers), elapsed)

    if use_fallback:
        for ticker in tickers:
            if not result.get(ticker):
                _merge(ticker, _fetch_fallback_news(ticker, market))

    return result


# ── Read-through cache ────────────────────────────────────────────────────────

def get_news_cached(
    tickers: list[str],
    feeds: list[str] | None = None,
    names: dict[str, str] | None = None,
    max_age_hours: int = 72,
    ttl_hours: float = 6.0,
    use_newsapi: bool = True,
    use_fallback: bool = True,
    market: str | None = None,
    force_refresh: bool = False,
) -> dict[str, list[dict]]:
    """
    TTL read-through wrapper around get_news backed by the shared SQLite cache.

    Tickers fetched within ttl_hours are served from the cache; only stale/missing
    tickers hit the network. This lets one project's fetch satisfy another's later
    read (e.g. the fund's morning scan warms the cache for DeepSwing's survivors).

    feeds defaults to SWEDISH_RSS_FEEDS for nordic/None markets and [] for "us".
    Returns {ticker: [article_dicts]} (only tickers with articles are included).
    """
    from .cache import get_cache
    cache = get_cache()

    if feeds is None:
        feeds = [] if market == "us" else SWEDISH_RSS_FEEDS

    stale = tickers if force_refresh else cache.get_stale_news_tickers(tickers, ttl_hours=ttl_hours)
    stale_set = set(stale)

    result: dict[str, list[dict]] = {}
    cutoff = (datetime.utcnow() - timedelta(hours=ttl_hours)).isoformat()
    for ticker in tickers:
        if ticker in stale_set:
            continue
        rows = cache.get_news(ticker, since_date=cutoff)
        if rows:
            result[ticker] = rows

    if stale:
        fetched = get_news(
            stale,
            feeds=feeds,
            names=names,
            max_age_hours=max_age_hours,
            use_newsapi=use_newsapi,
            use_fallback=use_fallback,
            market=market,
        )
        for ticker in stale:
            articles = fetched.get(ticker, [])
            if articles:
                cache.save_news(ticker, articles)
                result[ticker] = articles
            cache.mark_news_fetched(ticker)

    return result


# ── Market-wide headlines ─────────────────────────────────────────────────────

# Broad market/macro/geopolitical query for US market-wide headlines (NewsAPI).
_US_MARKET_QUERY = (
    '"stock market" OR "Federal Reserve" OR "S&P 500" OR '
    'inflation OR "oil prices" OR geopolitics'
)


def _market_key(market: str) -> str:
    """Reserved pseudo-ticker used to cache market-wide headlines in the news table."""
    return f"__market_{market.lower()}__"


def get_market_headlines(
    market: str = "nordic",
    feeds: list[str] | None = None,
    max_age_hours: int = 24,
    limit: int = 20,
    ttl_hours: float = 0.5,
    force_refresh: bool = False,
) -> list[dict]:
    """
    Recent market-wide headlines — NOT filtered to any ticker. This is the macro /
    geopolitical environment signal. Nordic pulls the RSS feeds directly; US uses a
    broad NewsAPI query. Cached in shared SQLite per market (default 30 min TTL).
    Returns [{headline, source, published_at}] newest-first.
    """
    from .cache import get_cache
    cache = get_cache()
    key = _market_key(market)

    if not force_refresh and not cache.get_stale_news_tickers([key], ttl_hours=ttl_hours):
        cutoff = (datetime.utcnow() - timedelta(hours=ttl_hours)).isoformat()
        cached = cache.get_news(key, since_date=cutoff)
        if cached:
            return cached[:limit]

    if market == "us":
        items = _fetch_us_market_headlines(max_age_hours, limit)
    else:
        items = _fetch_rss_market_headlines(feeds or SWEDISH_RSS_FEEDS, max_age_hours, limit)

    if items:
        cache.save_news(key, items)
    cache.mark_news_fetched(key)
    logger.info("Market-wide news [%s]: %d headlines", market, len(items))
    return items


def _fetch_rss_market_headlines(feeds: list[str], max_age_hours: int, limit: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items: list[dict] = []
    seen: set[str] = set()

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.debug("Market RSS error (%s): %s", feed_url, exc)
            continue
        source = (getattr(feed.feed, "title", "") or feed_url.split("/")[2])[:30]
        for entry in getattr(feed, "entries", []):
            title = (getattr(entry, "title", "") or "").strip()
            if not title or title.lower() in seen:
                continue
            pub_dt = None
            pub_str = None
            for attr in ("published_parsed", "updated_parsed"):
                parsed = getattr(entry, attr, None)
                if parsed:
                    pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                    break
            if pub_dt and pub_dt < cutoff:
                continue
            seen.add(title.lower())
            items.append({"headline": title[:500], "source": source, "published_at": pub_str})

    items.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    return items[:limit]


def _fetch_us_market_headlines(max_age_hours: int, limit: int) -> list[dict]:
    if not newsapi_available():
        logger.info("US market news skipped — no NEWS_API_KEY (or breaker tripped)")
        return []
    articles = fetch_newsapi(_US_MARKET_QUERY, max_age_hours=max_age_hours, page_size=limit)

    items: list[dict] = []
    seen: set[str] = set()
    for a in articles:
        title = (a.get("headline") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        items.append({
            "headline": title[:500],
            "source": a.get("source", "NewsAPI"),
            "published_at": (a.get("published_at") or "").replace("T", " ").rstrip("Z")[:16],
        })
    items.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    return items[:limit]


# ── FinBERT sentiment ─────────────────────────────────────────────────────────

_finbert_pipeline = None


def _get_finbert(model: str = "ProsusAI/finbert", device: str = "cpu"):
    global _finbert_pipeline
    if _finbert_pipeline is None:
        try:
            from transformers import pipeline as hf_pipeline
            _finbert_pipeline = hf_pipeline(
                "sentiment-analysis",
                model=model,
                device=device,
                truncation=True,
                max_length=512,
            )
        except Exception as exc:
            logger.warning("FinBERT unavailable (%s) — using neutral fallback", exc)
            _finbert_pipeline = None
    return _finbert_pipeline


def score_sentiment(
    texts: list[str],
    model: str = "ProsusAI/finbert",
    device: str = "cpu",
) -> list[dict]:
    """
    Score each text with FinBERT.
    Returns [{label: positive|negative|neutral, score: float}].
    Falls back to neutral if FinBERT is unavailable.
    """
    if not texts:
        return []
    pipe = _get_finbert(model, device)
    if pipe is None:
        return [{"label": "neutral", "score": 0.5}] * len(texts)
    try:
        results = pipe(texts, batch_size=16)
        return [{"label": r["label"].lower(), "score": round(float(r["score"]), 4)} for r in results]
    except Exception as exc:
        logger.warning("Sentiment scoring error: %s", exc)
        return [{"label": "neutral", "score": 0.5}] * len(texts)


def score_and_save(
    ticker_news: dict[str, list[dict]],
    model: str = "ProsusAI/finbert",
    device: str = "cpu",
) -> None:
    """Score all fetched headlines and persist to the shared news cache."""
    from .cache import get_cache
    cache = get_cache()

    for ticker, items in ticker_news.items():
        if not items:
            continue
        headlines = [item["headline"] for item in items]
        scores = score_sentiment(headlines, model, device)
        enriched = [
            {**item, "sentiment_label": s["label"], "sentiment_score": s["score"]}
            for item, s in zip(items, scores)
        ]
        cache.save_news(ticker, enriched)


def article_hash(ticker: str, headline: str) -> str:
    return hashlib.sha1(f"{ticker}:{headline}".encode()).hexdigest()
