"""
News aggregation: RSS feeds + NewsAPI, with optional FinBERT sentiment scoring.

Environment:
  NEWS_API_KEY — optional; enables NewsAPI for English-language news

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
    max_retries: int = 5,
) -> list[dict]:
    """Fetch English-language news from NewsAPI for a search query, retrying on 429."""
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
    delay = 2.0
    for attempt in range(max_retries):
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
                retry_after = float(exc.response.headers.get("Retry-After", delay))
                logger.info(
                    "NewsAPI rate-limited for '%s' (attempt %d/%d) — waiting %.0fs",
                    query, attempt + 1, max_retries, retry_after,
                )
                time.sleep(retry_after)
                delay *= 2
            else:
                logger.warning("NewsAPI error for '%s': %s", query, exc)
                return []
        except Exception as exc:
            logger.warning("NewsAPI error for '%s': %s", query, exc)
            return []
    logger.warning("NewsAPI gave up for '%s' after %d attempts", query, max_retries)
    return []


def get_news(
    tickers: list[str],
    feeds: list[str],
    names: dict[str, str] | None = None,
    max_age_hours: int = 72,
    use_newsapi: bool = True,
) -> dict[str, list[dict]]:
    """
    Unified news fetch: RSS feeds + optional NewsAPI.

    tickers:  list of Yahoo Finance symbols
    feeds:    RSS feed URLs to poll
    names:    optional {ticker: "Company Name"} for better keyword matching
    Returns:  {ticker: [article_dicts]}
    """
    result = fetch_rss(feeds, tickers, names=names, max_age_hours=max_age_hours)

    if use_newsapi:
        t0 = time.monotonic()
        for ticker in tickers:
            stem = ticker.rsplit(".", 1)[0].split("-")[0]
            articles = fetch_newsapi(stem, max_age_hours=max_age_hours)
            if articles:
                existing = result.get(ticker, [])
                seen = {a["headline"] for a in existing}
                for a in articles:
                    if a["headline"] not in seen:
                        existing.append(a)
                        seen.add(a["headline"])
                if existing:
                    result[ticker] = existing
        elapsed = time.monotonic() - t0
        logger.info("NewsAPI fetch complete: %d tickers in %.1fs", len(tickers), elapsed)

    return result


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
