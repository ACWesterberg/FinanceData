# Data-contract and provenance audit

## Scope and compatibility baseline

The package already exports the APIs used by ai-fund-manager: price loading,
indicators, cache access, news and sentiment, fundamentals, live quotes, and FX.
Their legacy signatures and essential return shapes are now pinned by contract
tests. The legacy functions continue returning their original dictionaries,
scalars, or pandas objects.

## Findings

* **Prices/live/FX:** price bars retain effective market dates and live quotes
  retain `price_time`, but callers generally cannot inspect provider, retrieval
  time, or fallback decisions. Spot FX has a same-UTC-day TTL; historical FX is
  permanent. Daily prices use the latest cached market date as freshness.
* **Fundamentals:** the cache is a single current snapshot per ticker with a
  seven-day default TTL. It cannot truthfully answer point-in-time queries.
* **News/sentiment:** an empty-fetch log correctly prevents repeated calls for
  genuine empty results, but the legacy API previously made cache hits, empty
  refreshes, rate limits, failures, partial coverage, and stale data
  indistinguishable. Article `published_at` and row `fetched_at` correctly
  represent different concepts.
* **Macro:** macro data is a current snapshot with a six-hour default TTL. Like
  fundamentals it is not a historical store.
* **Cache:** schema creation and two idempotent column migrations existed, but
  there was no explicit schema version. Connections already transact and roll
  back atomically; a busy timeout was missing for concurrent sibling processes.

## Implemented slice

This change adds opt-in result/provenance contracts and news diagnostics while
leaving `get_news_cached(...) -> dict[str, list[dict]]` unchanged. Diagnostics
include providers, requested/effective windows, retrieval time, cache state,
completeness, and warnings. Refresh exceptions may safely return explicitly
labelled stale cache data. Current-only fundamentals now have an explicit
`as_of` API which refuses to relabel current values as historical. The SQLite
schema is versioned and connections wait for short-lived write contention.

## Next slices

1. Add equivalent opt-in provenance for prices, live quotes, FX, and macro.
2. Replace the fundamentals snapshot table with filing-period observations if a
   historical provider is selected; do not synthesize history from snapshots.
3. Add migration tests starting from every released schema and consider WAL
   after measuring multi-process deployment behavior.
