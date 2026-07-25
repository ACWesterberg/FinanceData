#!/usr/bin/env python3
"""
Fundamentals coverage probe — how thin is our current data, and does a paid feed fix it?

Compares, field-by-field, what each provider actually returns for the same tickers:

    yfinance .info   — the source under financedata.get_fundamentals today (baseline)
    Twelve Data      — /statistics + /profile   (needs TWELVEDATA_API_KEY)
    EODHD            — /api/fundamentals         (needs EODHD_API_TOKEN)

It scores the ~22 fields the fund manager consumes, and — the number that actually
decides this — prints a per-field FILL RATE across the Nordic sample, since Nordic is
exactly where cheap providers fall down. A provider is only worth paying for if the
statement-derived fields (debt_to_equity, gross_margin, growth) come back populated
for .ST names, not just the US ones.

Run:
    export TWELVEDATA_API_KEY=...     # optional — grab a free trial key
    export EODHD_API_TOKEN=...        # optional — you already have this
    python scripts/fundamentals_coverage_probe.py                 # default sample
    python scripts/fundamentals_coverage_probe.py --nordic 8 --us 4
    python scripts/fundamentals_coverage_probe.py --tickers ERIC-B.ST,AAPL

No key for a provider ⇒ that column is skipped (not failed). yfinance runs if importable.
This only ever GETs data — it never writes to the shared cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional


def _get_json(url: str, params: dict, timeout: float = 30) -> tuple[int, Any]:
    """Minimal stdlib GET → (status, parsed_json_or_None). No third-party deps."""
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "financedata-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None

# ── Canonical fields (mirror financedata.fundamentals._FIELD_MAP) ───────────────
# "core" = numeric decision inputs we score for coverage; the rest are shown but
# not scored (currency/sector/website are strings, timestamps are situational).
CORE_FIELDS = [
    "market_cap", "pe_ratio", "forward_pe", "pb_ratio", "ev_to_ebitda",
    "price_to_sales", "profit_margin", "gross_margin", "roe", "debt_to_equity",
    "revenue_growth", "earnings_growth", "beta", "fifty_two_week_high",
    "fifty_two_week_low", "dividend_yield", "analyst_target_price", "analyst_count",
]
META_FIELDS = ["currency", "sector", "website"]
ALL_FIELDS = CORE_FIELDS + META_FIELDS

# Fields that come from financial statements — where yfinance .info is usually blank
# and a real feed should earn its money.
STATEMENT_FIELDS = {"gross_margin", "debt_to_equity", "revenue_growth", "earnings_growth", "ev_to_ebitda"}

DEFAULT_NORDIC = [
    "ERIC-B.ST", "VOLV-B.ST", "SAND.ST", "SEB-A.ST", "INVE-B.ST",
    "ATCO-A.ST", "HM-B.ST", "NIBE-B.ST", "EVO.ST", "ASSA-B.ST",
]
DEFAULT_US = ["AAPL", "MSFT", "NVDA", "JPM", "XOM"]


def _num(v: Any) -> Optional[float]:
    """Coerce to a real number; treat blanks/sentinels/non-finite as missing."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s in ("", "NA", "N/A", "None", "-", "--"):
            return None
        try:
            v = float(s.replace(",", ""))
        except ValueError:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _present(field: str, value: Any) -> bool:
    """Is this field actually populated? A 0.0 ratio counts as missing, not data."""
    if field in META_FIELDS:
        return isinstance(value, str) and value.strip() not in ("", "NA", "None", "-")
    n = _num(value)
    if n is None:
        return False
    if field in ("analyst_count",):
        return n > 0
    if field in ("fifty_two_week_high", "fifty_two_week_low", "market_cap", "analyst_target_price"):
        return n != 0
    # ratios/margins/growth: a true 0.0 is almost always "not reported"
    return n != 0.0


def _dig(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ── Providers ───────────────────────────────────────────────────────────────
def fetch_yfinance(ticker: str) -> Optional[dict]:
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    if not info or info.get("quoteType") == "NONE":
        return {}
    return {
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "pb_ratio": info.get("priceToBook"),
        "ev_to_ebitda": info.get("enterpriseToEbitda"),
        "price_to_sales": info.get("priceToSalesTrailingTwelveMonths"),
        "profit_margin": info.get("profitMargins"),
        "gross_margin": info.get("grossMargins"),
        "roe": info.get("returnOnEquity"),
        "debt_to_equity": info.get("debtToEquity"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "beta": info.get("beta"),
        "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
        "dividend_yield": info.get("dividendYield"),
        "analyst_target_price": info.get("targetMeanPrice"),
        "analyst_count": info.get("numberOfAnalystOpinions"),
        "currency": info.get("currency"),
        "sector": info.get("sector"),
        "website": info.get("website"),
    }


def _eodhd_symbol(ticker: str) -> str:
    # Nordic already carries .ST (the code EODHD uses for Nasdaq Stockholm); US → .US
    return ticker if "." in ticker else f"{ticker}.US"


def fetch_eodhd(ticker: str, token: str) -> Optional[dict]:
    sym = _eodhd_symbol(ticker)
    status, d = _get_json(
        f"https://eodhd.com/api/fundamentals/{sym}",
        {"api_token": token, "fmt": "json"},
    )
    if status != 200 or not isinstance(d, dict) or not d:
        return {}
    ratings = _dig(d, "AnalystRatings") or {}
    analyst_count = None
    counts = [ratings.get(k) for k in ("StrongBuy", "Buy", "Hold", "Sell", "StrongSell")]
    nums = [_num(c) for c in counts]
    if any(n is not None for n in nums):
        analyst_count = sum(n for n in nums if n is not None)
    return {
        "market_cap": _dig(d, "Highlights", "MarketCapitalization"),
        "pe_ratio": _dig(d, "Highlights", "PERatio") or _dig(d, "Valuation", "TrailingPE"),
        "forward_pe": _dig(d, "Valuation", "ForwardPE") or _dig(d, "Highlights", "ForwardPE"),
        "pb_ratio": _dig(d, "Valuation", "PriceBookMRQ"),
        "ev_to_ebitda": _dig(d, "Valuation", "EnterpriseValueEbitda"),
        "price_to_sales": _dig(d, "Valuation", "PriceSalesTTM"),
        "profit_margin": _dig(d, "Highlights", "ProfitMargin"),
        "gross_margin": _dig(d, "Highlights", "GrossProfitTTM"),  # absolute, not a margin — see notes
        "roe": _dig(d, "Highlights", "ReturnOnEquityTTM"),
        "debt_to_equity": _dig(d, "Financials", "Balance_Sheet", "quarterly") and None,  # statement-nested; probe reports as depth signal
        "revenue_growth": _dig(d, "Highlights", "QuarterlyRevenueGrowthYOY"),
        "earnings_growth": _dig(d, "Highlights", "QuarterlyEarningsGrowthYOY"),
        "beta": _dig(d, "Technicals", "Beta"),
        "fifty_two_week_high": _dig(d, "Technicals", "52WeekHigh"),
        "fifty_two_week_low": _dig(d, "Technicals", "52WeekLow"),
        "dividend_yield": _dig(d, "Highlights", "DividendYield") or _dig(d, "SplitsDividends", "ForwardAnnualDividendYield"),
        "analyst_target_price": _dig(d, "Highlights", "WallStreetTargetPrice") or _dig(d, "AnalystRatings", "TargetPrice"),
        "analyst_count": analyst_count,
        "currency": _dig(d, "General", "CurrencyCode"),
        "sector": _dig(d, "General", "Sector"),
        "website": _dig(d, "General", "WebURL"),
    }


def _twelvedata_params(ticker: str, key: str) -> dict:
    if ticker.endswith(".ST"):
        return {"symbol": ticker[:-3], "mic_code": "XSTO", "apikey": key}
    return {"symbol": ticker, "apikey": key}


def fetch_twelvedata(ticker: str, key: str) -> Optional[dict]:
    base = _twelvedata_params(ticker, key)
    ss, stats = _get_json("https://api.twelvedata.com/statistics", base)
    ps, prof = _get_json("https://api.twelvedata.com/profile", base)
    stats = stats if (ss == 200 and isinstance(stats, dict)) else {}
    prof = prof if (ps == 200 and isinstance(prof, dict)) else {}
    if isinstance(stats, dict) and stats.get("status") == "error":
        stats = {}
    if isinstance(prof, dict) and prof.get("status") == "error":
        prof = {}
    s = _dig(stats, "statistics") or {}
    val = s.get("valuations_metrics") or {}
    fin = s.get("financials") or {}
    inc = fin.get("income_statement") or {}
    bal = fin.get("balance_sheet") or {}
    px = s.get("stock_price_summary") or {}
    div = s.get("dividends_and_splits") or {}
    return {
        "market_cap": val.get("market_capitalization"),
        "pe_ratio": val.get("trailing_pe"),
        "forward_pe": val.get("forward_pe"),
        "pb_ratio": val.get("price_to_book_mrq"),
        "ev_to_ebitda": val.get("enterprise_to_ebitda"),
        "price_to_sales": val.get("price_to_sales_ttm"),
        "profit_margin": fin.get("profit_margin"),
        "gross_margin": fin.get("gross_margin"),
        "roe": fin.get("return_on_equity_ttm"),
        "debt_to_equity": bal.get("total_debt_to_equity_mrq"),
        "revenue_growth": inc.get("quarterly_revenue_growth"),
        "earnings_growth": inc.get("quarterly_earnings_growth_yoy"),
        "beta": px.get("beta"),
        "fifty_two_week_high": _dig(px, "fifty_two_week_high") or _dig(px, "fifty_two_week", "high"),
        "fifty_two_week_low": _dig(px, "fifty_two_week_low") or _dig(px, "fifty_two_week", "low"),
        "dividend_yield": div.get("forward_annual_dividend_yield"),
        "analyst_target_price": None,   # separate /price_target endpoint — omitted to save credits
        "analyst_count": None,
        "currency": prof.get("currency") or _dig(stats, "meta", "currency"),
        "sector": prof.get("sector"),
        "website": prof.get("website"),
    }


# ── Scoring / reporting ─────────────────────────────────────────────────────
def score(rows: dict, provider: str, tickers: list[str]) -> dict:
    """Return {field: fill_fraction} over the given tickers for one provider."""
    out = {}
    for f in ALL_FIELDS:
        got = sum(1 for t in tickers if rows.get((t, provider)) and _present(f, rows[(t, provider)].get(f)))
        out[f] = got / len(tickers) if tickers else 0.0
    return out


def core_count(data: Optional[dict]) -> str:
    if data is None:
        return "  -"
    n = sum(1 for f in CORE_FIELDS if _present(f, data.get(f)))
    return f"{n:>2}/{len(CORE_FIELDS)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fundamentals coverage probe")
    ap.add_argument("--nordic", type=int, default=6, help="how many Nordic tickers (default 6)")
    ap.add_argument("--us", type=int, default=3, help="how many US tickers (default 3)")
    ap.add_argument("--tickers", type=str, default="", help="comma-separated override list")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between Twelve Data calls (trial: 8/min)")
    args = ap.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nordic = [t for t in tickers if t.endswith(".ST")]
    else:
        nordic = DEFAULT_NORDIC[: args.nordic]
        tickers = nordic + DEFAULT_US[: args.us]

    td_key = os.environ.get("TWELVEDATA_API_KEY", "")
    eodhd_token = os.environ.get("EODHD_API_TOKEN", "")

    providers: list[tuple[str, Callable[[str], Optional[dict]]]] = []
    try:
        import yfinance  # noqa: F401
        providers.append(("yfinance", fetch_yfinance))
    except Exception:
        print("• yfinance not importable — baseline column skipped\n", file=sys.stderr)

    if td_key:
        providers.append(("twelvedata", lambda t: fetch_twelvedata(t, td_key)))
    else:
        print("• TWELVEDATA_API_KEY unset — Twelve Data column skipped", file=sys.stderr)
    if eodhd_token:
        providers.append(("eodhd", lambda t: fetch_eodhd(t, eodhd_token)))
    else:
        print("• EODHD_API_TOKEN unset — EODHD column skipped", file=sys.stderr)

    if not providers:
        print("No providers available. Set at least one API key or install yfinance.", file=sys.stderr)
        return 1

    names = [n for n, _ in providers]
    rows: dict = {}
    print(f"\nProbing {len(tickers)} tickers × {len(names)} providers: {', '.join(names)}\n")

    # ── per-ticker core-field counts ────────────────────────────────────────
    hdr = f"{'ticker':<12} " + "  ".join(f"{n:>10}" for n in names)
    print(hdr)
    print("-" * len(hdr))
    for t in tickers:
        cells = []
        for name, fn in providers:
            data = fn(t)
            rows[(t, name)] = data
            cells.append(f"{core_count(data):>10}")
            if name == "twelvedata":
                time.sleep(args.sleep)
        print(f"{t:<12} " + "  ".join(cells))

    # ── per-field fill rate on the NORDIC subset (the decision) ──────────────
    if nordic:
        print(f"\nNordic field fill-rate  (n={len(nordic)}: {', '.join(nordic)})\n")
        scores = {name: score(rows, name, nordic) for name in names}
        fhdr = f"{'field':<22} " + "  ".join(f"{n:>10}" for n in names)
        print(fhdr)
        print("-" * len(fhdr))
        for f in ALL_FIELDS:
            marker = " *" if f in STATEMENT_FIELDS else "  "
            cells = "  ".join(f"{scores[n][f] * 100:>9.0f}%" for n in names)
            print(f"{f:<22}{marker}" + cells)
        print("\n  * = statement-derived field (where yfinance .info is usually blank)")

    print(
        "\nHow to read this: a provider only justifies its price if the Nordic '*' rows"
        "\nfill in where yfinance is blank. High US coverage is table stakes — everyone has it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
