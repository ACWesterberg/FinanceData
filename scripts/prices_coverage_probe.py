#!/usr/bin/env python3
"""
Prices coverage probe — the comparison that actually decides the $29 price feed.

Fundamentals turned out to be a non-issue (yfinance .info is 17/18 on Nordic, and no
sub-$50 feed sells Nordic fundamentals). What's genuinely broken is the PRICE layer:
Alpha Vantage's 25 req/day for Nordic, yfinance's flaky/stale quotes. Twelve Data Grow
($29) and EODHD All World Extended ($29.99) are the SAME price, so the real question is
which one gives better daily bars + live quotes than yfinance — especially on Stockholm.

For each ticker × provider this reports, on daily bars over the last ~90 days:
    bars      — how many daily bars came back (coverage)
    last      — date of the most recent bar (freshness; lag vs today flagged)
    close     — most recent close, native currency (cross-provider sanity check)
    live      — a live/intraday quote if the provider/plan offers one

Providers:
    yfinance     — today's source (baseline), needs yfinance importable
    Twelve Data  — /time_series + /quote        (needs TWELVEDATA_API_KEY; ON the Grow plan)
    EODHD        — /api/eod + /api/real-time     (needs a token whose plan includes EOD)

Like the fundamentals probe, it surfaces WHY any cell is empty (plan/quota vs no-coverage)
so a Grow-plan 403 or a stale token is never mistaken for "this feed can't do Stockholm".

Run:
    python scripts/prices_coverage_probe.py
    python scripts/prices_coverage_probe.py --nordic 6 --us 3 --sleep 10
    python scripts/prices_coverage_probe.py --tickers ERIC-B.ST,AAPL

Read-only; never writes the shared cache.
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
from datetime import date, datetime
from typing import Any, Callable, Optional


def _get_json(url: str, params: dict, timeout: float = 30) -> tuple[int, Any]:
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


DEFAULT_NORDIC = [
    "ERIC-B.ST", "VOLV-B.ST", "SAND.ST", "SEB-A.ST", "INVE-B.ST",
    "ATCO-A.ST", "HM-B.ST", "NIBE-B.ST", "EVO.ST", "ASSA-B.ST",
]
DEFAULT_US = ["AAPL", "MSFT", "NVDA", "JPM", "XOM"]


class Result:
    __slots__ = ("bars", "last_date", "last_close", "live", "currency", "note")

    def __init__(self):
        self.bars: int = 0
        self.last_date: Optional[str] = None
        self.last_close: Optional[float] = None
        self.live: Optional[float] = None
        self.currency: Optional[str] = None
        self.note: Optional[str] = None


def _lag_days(d: Optional[str]) -> Optional[int]:
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return (date.today() - datetime.strptime(d[:19], fmt).date()).days
        except ValueError:
            continue
    return None


def _td_error(body: Any) -> Optional[str]:
    if isinstance(body, dict):
        if "_transport_error" in body:
            return f"transport: {body['_transport_error'][:50]}"
        if body.get("status") == "error":
            return f"{body.get('code', '?')}: {str(body.get('message', ''))[:70]}"
    return None


# ── Providers ────────────────────────────────────────────────────────────────
def probe_yfinance(ticker: str) -> Result:
    r = Result()
    try:
        import yfinance as yf
    except Exception:
        r.note = "yfinance not importable"
        return r
    try:
        hist = yf.Ticker(ticker).history(period="3mo", auto_adjust=False)
    except Exception as exc:
        r.note = f"error: {str(exc)[:60]}"
        return r
    if hist is None or hist.empty:
        r.note = "no history returned"
        return r
    r.bars = len(hist)
    r.last_date = hist.index[-1].strftime("%Y-%m-%d")
    try:
        r.last_close = float(hist["Close"].iloc[-1])
    except Exception:
        pass
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        r.live = float(fi["last_price"])
        r.currency = getattr(fi, "currency", None) or (fi["currency"] if "currency" in fi else None)
    except Exception:
        r.live = r.last_close
    return r


def _eodhd_symbol(ticker: str) -> str:
    return ticker if "." in ticker else f"{ticker}.US"


def probe_eodhd(ticker: str, token: str) -> Result:
    r = Result()
    sym = _eodhd_symbol(ticker)
    frm = (date.today().replace(day=1)).replace(month=max(1, date.today().month - 3)).isoformat()
    status, d = _get_json(
        f"https://eodhd.com/api/eod/{sym}",
        {"api_token": token, "fmt": "json", "from": frm, "period": "d"},
    )
    if status in (401, 402, 403):
        r.note = f"{status}: token's plan lacks EOD access"
        return r
    if status == 404:
        r.note = "404: symbol not found on EODHD"
        return r
    if status != 200 or not isinstance(d, list) or not d:
        r.note = f"{status}: empty/unexpected EOD response"
        return r
    r.bars = len(d)
    last = d[-1]
    r.last_date = last.get("date")
    try:
        r.last_close = float(last.get("close"))
    except (TypeError, ValueError):
        pass
    # live real-time (delayed) quote
    _, rt = _get_json(f"https://eodhd.com/api/real-time/{sym}", {"api_token": token, "fmt": "json"})
    if isinstance(rt, dict):
        try:
            r.live = float(rt.get("close"))
        except (TypeError, ValueError):
            pass
    return r


def _td_params(ticker: str, key: str, extra: dict) -> dict:
    if ticker.endswith(".ST"):
        p = {"symbol": ticker[:-3], "mic_code": "XSTO", "apikey": key}
    else:
        p = {"symbol": ticker, "apikey": key}
    p.update(extra)
    return p


def probe_twelvedata(ticker: str, key: str, backoff: float) -> Result:
    r = Result()

    def _ts(params):
        _, body = _get_json("https://api.twelvedata.com/time_series", params)
        err = _td_error(body)
        if err and err.startswith("429"):
            print(f"    …rate-limited, waiting {backoff:.0f}s and retrying once", file=sys.stderr)
            time.sleep(backoff)
            _, body = _get_json("https://api.twelvedata.com/time_series", params)
            err = _td_error(body)
        return body, err

    params = _td_params(ticker, key, {"interval": "1day", "outputsize": "90"})
    body, err = _ts(params)
    if err and "not found" in err.lower() and ticker.endswith(".ST"):
        alt = _td_params(ticker, key, {"interval": "1day", "outputsize": "90"})
        alt.pop("mic_code", None)
        alt["exchange"] = "Stockholm"
        body2, err2 = _ts(alt)
        if not err2:
            body, err, params = body2, None, alt
        else:
            r.note = f"{err}  (also tried exchange=Stockholm → {err2})"
            return r
    if err:
        r.note = err
        return r

    values = _dig_list(body, "values")
    r.currency = _dig(body, "meta", "currency")
    if values:
        r.bars = len(values)
        r.last_date = values[0].get("datetime")   # TD returns newest-first
        try:
            r.last_close = float(values[0].get("close"))
        except (TypeError, ValueError):
            pass
    else:
        r.note = "no values in time_series response"

    # live quote (cheap, 1 credit)
    qp = _td_params(ticker, key, {})
    if "exchange" in params:
        qp.pop("mic_code", None)
        qp["exchange"] = "Stockholm"
    _, q = _get_json("https://api.twelvedata.com/quote", qp)
    if isinstance(q, dict) and not _td_error(q):
        try:
            r.live = float(q.get("close"))
        except (TypeError, ValueError):
            pass
    return r


def _dig(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _dig_list(d: Any, key: str) -> list:
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, list) else []


# ── Reporting ────────────────────────────────────────────────────────────────
def _cov_cell(r: Result) -> str:
    if r.note and r.bars == 0:
        return "        ·"
    lag = _lag_days(r.last_date)
    flag = "" if (lag is None or lag <= 4) else "!"   # >4 calendar days ≈ stale (past a weekend)
    md = r.last_date[5:] if r.last_date else "  -  "
    return f"{r.bars:>3}b {md}{flag}"


def _close_cell(r: Result) -> str:
    if r.last_close is None:
        return "       ·"
    return f"{r.last_close:>8.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Prices coverage probe")
    ap.add_argument("--nordic", type=int, default=4)
    ap.add_argument("--us", type=int, default=2)
    ap.add_argument("--tickers", type=str, default="")
    ap.add_argument("--sleep", type=float, default=8.0, help="seconds between Twelve Data tickers")
    args = ap.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = DEFAULT_NORDIC[: args.nordic] + DEFAULT_US[: args.us]

    td_key = os.environ.get("TWELVEDATA_API_KEY", "")
    eodhd_token = os.environ.get("EODHD_API_TOKEN", "")

    providers: list[tuple[str, Callable[[str], Result]]] = []
    try:
        import yfinance  # noqa: F401
        providers.append(("yfinance", probe_yfinance))
    except Exception:
        print("• yfinance not importable — baseline column skipped", file=sys.stderr)
    if td_key:
        providers.append(("twelvedata", lambda t: probe_twelvedata(t, td_key, args.sleep)))
    else:
        print("• TWELVEDATA_API_KEY unset — Twelve Data column skipped", file=sys.stderr)
    if eodhd_token:
        providers.append(("eodhd", lambda t: probe_eodhd(t, eodhd_token)))
    else:
        print("• EODHD_API_TOKEN unset — EODHD column skipped", file=sys.stderr)

    if not providers:
        print("No providers available. Set at least one key or install yfinance.", file=sys.stderr)
        return 1

    names = [n for n, _ in providers]
    results: dict = {}
    diag: list[tuple[str, str, str]] = []
    print(f"\nProbing prices for {len(tickers)} tickers × {len(names)} providers: {', '.join(names)}")
    print("(daily bars over ~90d; 'b'=bar count, date=last bar MM-DD, ! = >4 calendar days stale)\n")

    hdr = f"{'ticker':<12} " + "  ".join(f"{n:>12}" for n in names)
    print(hdr)
    print("-" * len(hdr))
    for t in tickers:
        cells = []
        for name, fn in providers:
            r = fn(t)
            results[(t, name)] = r
            if r.note:
                diag.append((name, t, r.note))
            cells.append(f"{_cov_cell(r):>12}")
            if name == "twelvedata":
                time.sleep(args.sleep)
        print(f"{t:<12} " + "  ".join(cells))

    # last-close cross-check: same instrument & currency across providers?
    print("\nLast close (native currency) — cross-provider sanity check:\n")
    chdr = f"{'ticker':<12} " + "  ".join(f"{n:>10}" for n in names)
    print(chdr)
    print("-" * len(chdr))
    for t in tickers:
        cells = "  ".join(f"{_close_cell(results[(t, n)]):>10}" for n in names)
        print(f"{t:<12} " + cells)
        closes = [results[(t, n)].last_close for n in names if results[(t, n)].last_close]
        if len(closes) >= 2 and min(closes) > 0:
            spread = (max(closes) - min(closes)) / min(closes)
            if spread > 0.02:
                print(f"{'':<12}   ⚠ closes disagree by {spread * 100:.0f}% — check symbol/currency/adjustment")

    if diag:
        print("\nDiagnostics — why cells are empty (dedup'd):\n")
        seen = set()
        for provider, ticker, reason in diag:
            if (provider, reason) in seen:
                continue
            seen.add((provider, reason))
            print(f"  {provider:<11} {reason}  [e.g. {ticker}]")
        print(
            "\n  → 'plan lacks ...' / 'out of credits' = PLAN/QUOTA, not coverage."
            "\n    'symbol not found' on a valid .ST symbol (after the Stockholm fallback)"
            "\n    is the one that means the feed genuinely can't do Nordic."
        )

    print(
        "\nWhat to look for: does each paid feed return Stockholm daily bars AND a fresh"
        "\nlast date (no '!'), with a live quote? That — not fundamentals — is the $29 call."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
