#!/usr/bin/env python3
"""
Fundamentals coverage probe — how thin is our current data, and does a paid feed fix it?

Compares, field-by-field, what each provider actually returns for the same tickers:

    yfinance .info   — the source under financedata.get_fundamentals today (baseline)
    Twelve Data      — /statistics + /profile   (needs TWELVEDATA_API_KEY)
    EODHD            — /api/fundamentals         (needs EODHD_API_TOKEN, Fundamentals plan)

It scores the ~18 numeric fields the fund manager consumes and prints a per-field FILL
RATE across the Nordic sample — the number that actually decides this, since Nordic is
where cheap feeds fall down. Crucially it ALSO prints a diagnostics section explaining
*why* any cell is empty (rate-limited vs symbol-not-found vs plan-restricted), so a 0%
is never mistaken for "bad data" when it's really "wrong token" or "throttled".

Run:
    export TWELVEDATA_API_KEY=...     # optional — free trial key
    export EODHD_API_TOKEN=...        # optional — must be a *Fundamentals*-plan token
    python scripts/fundamentals_coverage_probe.py                 # small default sample
    python scripts/fundamentals_coverage_probe.py --nordic 8 --us 4
    python scripts/fundamentals_coverage_probe.py --tickers ERIC-B.ST,AAPL --sleep 10

Notes:
- Twelve Data's trial is ~8 credits/min and /statistics is heavy; keep the sample small
  and --sleep high, or you'll see 'out of API credits' in diagnostics. 429s auto-retry once.
- A provider with no key is skipped (not failed). yfinance runs only if importable.
- This only ever GETs data — it never writes to the shared cache.
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
    """Stdlib GET → (status, parsed_json). Returns the error BODY too, so provider
    error messages ('symbol not found', 'out of credits') are visible, not swallowed."""
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "financedata-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, None
    except Exception as exc:
        return 0, {"_transport_error": str(exc)}


def _load_env(explicit: Optional[str] = None) -> list[str]:
    """Load KEY=VALUE lines from .env files into os.environ (without clobbering already-set
    vars). Searches: --env-file, ./.env, ../DeepSwing/.env, ../ai-fund-manager/.env."""
    from pathlib import Path
    cwd = Path.cwd()
    candidates = [Path(explicit)] if explicit else []
    candidates += [cwd / ".env", cwd.parent / "DeepSwing" / ".env", cwd.parent / "ai-fund-manager" / ".env"]
    loaded = []
    for p in candidates:
        if not p or not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        loaded.append(str(p))
    return loaded


# ── Canonical fields (mirror financedata.fundamentals._FIELD_MAP) ───────────────
CORE_FIELDS = [
    "market_cap", "pe_ratio", "forward_pe", "pb_ratio", "ev_to_ebitda",
    "price_to_sales", "profit_margin", "gross_margin", "roe", "debt_to_equity",
    "revenue_growth", "earnings_growth", "beta", "fifty_two_week_high",
    "fifty_two_week_low", "dividend_yield", "analyst_target_price", "analyst_count",
]
META_FIELDS = ["currency", "sector", "website"]
ALL_FIELDS = CORE_FIELDS + META_FIELDS
STATEMENT_FIELDS = {"gross_margin", "debt_to_equity", "revenue_growth", "earnings_growth", "ev_to_ebitda"}

DEFAULT_NORDIC = [
    "ERIC-B.ST", "VOLV-B.ST", "SAND.ST", "SEB-A.ST", "INVE-B.ST",
    "ATCO-A.ST", "HM-B.ST", "NIBE-B.ST", "EVO.ST", "ASSA-B.ST",
]
DEFAULT_US = ["AAPL", "MSFT", "NVDA", "JPM", "XOM"]


def _num(v: Any) -> Optional[float]:
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
    if field in META_FIELDS:
        return isinstance(value, str) and value.strip() not in ("", "NA", "None", "-")
    n = _num(value)
    if n is None:
        return False
    if field == "analyst_count":
        return n > 0
    if field in ("fifty_two_week_high", "fifty_two_week_low", "market_cap", "analyst_target_price"):
        return n != 0
    return n != 0.0


def _dig(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _td_error(body: Any) -> Optional[str]:
    """Return a short reason string if a Twelve Data body is an error, else None."""
    if isinstance(body, dict):
        if "_transport_error" in body:
            return f"transport: {body['_transport_error'][:50]}"
        if body.get("status") == "error":
            return f"{body.get('code', '?')}: {str(body.get('message', ''))[:70]}"
    return None


# ── Providers: each returns (flat_dict_or_None, note_or_None) ────────────────
def fetch_yfinance(ticker: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        import yfinance as yf
    except Exception:
        return None, "yfinance not importable"
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        return {}, f"error: {str(exc)[:60]}"
    if not info or info.get("quoteType") == "NONE":
        return {}, "no .info returned"
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
    }, None


def _eodhd_symbol(ticker: str) -> str:
    return ticker if "." in ticker else f"{ticker}.US"


def fetch_eodhd(ticker: str, token: str) -> tuple[Optional[dict], Optional[str]]:
    sym = _eodhd_symbol(ticker)
    status, d = _get_json(
        f"https://eodhd.com/api/fundamentals/{sym}",
        {"api_token": token, "fmt": "json"},
    )
    if status in (401, 402, 403):
        return {}, f"{status}: token lacks Fundamentals-plan access"
    if status == 404:
        return {}, "404: symbol not found on EODHD"
    if status != 200 or not isinstance(d, dict) or not d:
        msg = _dig(d, "_transport_error") if isinstance(d, dict) else None
        return {}, f"{status}: {msg or 'empty/unexpected response'}"
    ratings = _dig(d, "AnalystRatings") or {}
    nums = [_num(ratings.get(k)) for k in ("StrongBuy", "Buy", "Hold", "Sell", "StrongSell")]
    analyst_count = sum(n for n in nums if n is not None) if any(n is not None for n in nums) else None
    return {
        "market_cap": _dig(d, "Highlights", "MarketCapitalization"),
        "pe_ratio": _dig(d, "Highlights", "PERatio") or _dig(d, "Valuation", "TrailingPE"),
        "forward_pe": _dig(d, "Valuation", "ForwardPE") or _dig(d, "Highlights", "ForwardPE"),
        "pb_ratio": _dig(d, "Valuation", "PriceBookMRQ"),
        "ev_to_ebitda": _dig(d, "Valuation", "EnterpriseValueEbitda"),
        "price_to_sales": _dig(d, "Valuation", "PriceSalesTTM"),
        "profit_margin": _dig(d, "Highlights", "ProfitMargin"),
        "gross_margin": _dig(d, "Highlights", "GrossProfitTTM"),  # absolute, not a ratio — see notes
        "roe": _dig(d, "Highlights", "ReturnOnEquityTTM"),
        "debt_to_equity": None,  # statement-nested on EODHD; not a Highlights field
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
    }, None


def _parse_td(stats: Any, prof: Any) -> dict:
    s = _dig(stats, "statistics") or {}
    val = s.get("valuations_metrics") or {}
    fin = s.get("financials") or {}
    inc = fin.get("income_statement") or {}
    bal = fin.get("balance_sheet") or {}
    px = s.get("stock_price_summary") or {}
    div = s.get("dividends_and_splits") or {}
    prof = prof if isinstance(prof, dict) else {}
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


def _td_call(params: dict, backoff: float) -> tuple[Any, Optional[str]]:
    """One /statistics call with a single 429 retry. Returns (body, error_note)."""
    _, body = _get_json("https://api.twelvedata.com/statistics", params)
    err = _td_error(body)
    if err and err.startswith("429"):
        print(f"    …rate-limited, waiting {backoff:.0f}s and retrying once", file=sys.stderr)
        time.sleep(backoff)
        _, body = _get_json("https://api.twelvedata.com/statistics", params)
        err = _td_error(body)
    return body, err


def fetch_twelvedata(ticker: str, key: str, backoff: float) -> tuple[Optional[dict], Optional[str]]:
    # Primary Nordic mapping is mic_code=XSTO; if TD says 'not found', retry with exchange=Stockholm.
    if ticker.endswith(".ST"):
        base = {"symbol": ticker[:-3], "mic_code": "XSTO", "apikey": key}
    else:
        base = {"symbol": ticker, "apikey": key}

    stats, err = _td_call(base, backoff)
    if err and "not found" in err.lower() and ticker.endswith(".ST"):
        alt = {"symbol": ticker[:-3], "exchange": "Stockholm", "apikey": key}
        stats2, err2 = _td_call(alt, backoff)
        if not err2:
            stats, err = stats2, None
        else:
            return {}, f"{err}  (also tried exchange=Stockholm → {err2})"
    if err:
        return {}, err

    _, prof = _get_json("https://api.twelvedata.com/profile", base)
    return _parse_td(stats, prof), None


# ── Scoring / reporting ─────────────────────────────────────────────────────
def score(rows: dict, provider: str, tickers: list[str]) -> dict:
    out = {}
    for f in ALL_FIELDS:
        got = sum(1 for t in tickers if rows.get((t, provider)) and _present(f, rows[(t, provider)].get(f)))
        out[f] = got / len(tickers) if tickers else 0.0
    return out


def core_count(data: Optional[dict]) -> str:
    if not data:
        return "  ·"
    n = sum(1 for f in CORE_FIELDS if _present(f, data.get(f)))
    return f"{n:>2}/{len(CORE_FIELDS)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fundamentals coverage probe")
    ap.add_argument("--nordic", type=int, default=4, help="how many Nordic tickers (default 4)")
    ap.add_argument("--us", type=int, default=2, help="how many US tickers (default 2)")
    ap.add_argument("--tickers", type=str, default="", help="comma-separated override list")
    ap.add_argument("--sleep", type=float, default=8.0, help="seconds between Twelve Data tickers (trial ~8/min)")
    ap.add_argument("--env-file", type=str, default="", help="path to a .env with the API keys")
    args = ap.parse_args()

    loaded = _load_env(args.env_file or None)
    if loaded:
        print(f"• loaded env from: {', '.join(loaded)}", file=sys.stderr)

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nordic = [t for t in tickers if t.endswith(".ST")]
    else:
        nordic = DEFAULT_NORDIC[: args.nordic]
        tickers = nordic + DEFAULT_US[: args.us]

    td_key = os.environ.get("TWELVEDATA_API_KEY", "")
    eodhd_token = os.environ.get("EODHD_API_TOKEN", "")

    providers: list[tuple[str, Callable[[str], tuple[Optional[dict], Optional[str]]]]] = []
    try:
        import yfinance  # noqa: F401
        providers.append(("yfinance", fetch_yfinance))
    except Exception:
        print("• yfinance not importable — baseline column skipped", file=sys.stderr)
    if td_key:
        providers.append(("twelvedata", lambda t: fetch_twelvedata(t, td_key, args.sleep)))
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
    diag: list[tuple[str, str, str]] = []   # (provider, ticker, reason)
    print(f"\nProbing {len(tickers)} tickers × {len(names)} providers: {', '.join(names)}\n")

    hdr = f"{'ticker':<12} " + "  ".join(f"{n:>10}" for n in names)
    print(hdr)
    print("-" * len(hdr))
    for t in tickers:
        cells = []
        for name, fn in providers:
            data, note = fn(t)
            rows[(t, name)] = data
            if note:
                diag.append((name, t, note))
            cells.append(f"{core_count(data):>10}")
            if name == "twelvedata":
                time.sleep(args.sleep)
        print(f"{t:<12} " + "  ".join(cells))

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
        print("\n  * = statement-derived field (where yfinance .info is often blank)")

    if diag:
        print("\nDiagnostics — why cells are empty (dedup'd):\n")
        seen = set()
        for provider, ticker, reason in diag:
            key = (provider, reason)
            if key in seen:
                continue
            seen.add(key)
            example = "" if reason.count(ticker) else f"  [e.g. {ticker}]"
            print(f"  {provider:<11} {reason}{example}")
        print(
            "\n  → A 0% column with 'token lacks ...' or 'out of credits' is a PLAN/QUOTA"
            "\n    problem, not data quality. Only 'symbol not found' with a valid symbol"
            "\n    means genuine no-coverage."
        )

    print(
        "\nHow to read this: judge a paid feed only on Nordic '*' rows that fill where"
        "\nyfinance is blank — AND only once its diagnostics line is clean (real access)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
