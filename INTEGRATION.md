# financedata — Integration Overview

`financedata` is a shared market-data service for DeepSwing and Fond.
It replaces duplicated fetch + cache code across all three projects with a single
installable Python package backed by a shared SQLite database at
`~/.financedata/cache.db`.

## Per-project guides

Each guide has exact before/after code for every file that changes:

| Project | Guide |
|---|---|
| DeepSwing | [INTEGRATE_DEEPSWING.md](INTEGRATE_DEEPSWING.md) |
| Fond/ai-fund-manager | [INTEGRATE_FOND_FUNDMGR.md](INTEGRATE_FOND_FUNDMGR.md) |
| Fond/swing-trader | [INTEGRATE_FOND_SWINGTRADER.md](INTEGRATE_FOND_SWINGTRADER.md) |

## What this package provides

```
src/financedata/
├── cache.py        — SQLite store (shared across all projects on the Pi)
├── prices.py       — yfinance batch fetch + Alpha Vantage fallback
├── indicators.py   — RSI, ATR, VWAP, SMA, EMA, ann_vol, momentum score
├── news.py         — RSS + NewsAPI aggregator + FinBERT sentiment
├── macro.py        — FRED + Riksbank + ECB + yfinance indices (6h TTL)
├── fundamentals.py — yfinance .info, parallel fetch (7-day TTL)
└── insider.py      — SEC EDGAR + FI Insynsregistret (24h TTL)
```

## Environment variables (set in systemd unit or .env on the Pi)

```bash
FINANCEDATA_DB=/home/pi/.financedata/cache.db   # shared across all projects
ALPHA_VANTAGE_KEY=...    # optional, 25/day free tier for Nordic prices
NEWS_API_KEY=...         # optional, English-language news via NewsAPI
FRED_API_KEY=...         # optional, US macro indicators (FEDFUNDS, CPI, 10Y, UNRATE)
```

## Key improvements over the old per-project code

1. **Alpha Vantage counter persists across restarts** — the old DeepSwing counter
   was in-memory and reset on every process restart. 25 restarts could burn all
   625 possible daily requests. Now tracked in SQLite.

2. **Shared price cache** — morning fetch by Fond means DeepSwing's later scan
   reads from DB, no duplicate yfinance calls.

3. **Better news keyword matching** — uses word-boundary regex (`\bvolvo\b`) instead
   of substring search, dramatically fewer false matches.

4. **FinBERT is optional** — `pip install financedata[sentiment]` enables it.
   Falls back to neutral scores if not installed (DeepSwing doesn't need torch).
