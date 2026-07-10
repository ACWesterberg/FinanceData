"""
Broker-tradable instrument universe, cached in the shared SQLite DB.

FinanceData keeps one complete, up-to-date universe of the countries covered by
the brokerage (Montrose). Downstream projects (DeepSwing, FundManager,
SwingTrader) don't re-fetch it — they call `get_universe(...)` for the full set,
or `get_universe_updates(since=...)` to pull only what changed since their last
sync (added / removed / modified listings).

Source: EODHD Exchanges API (needs an API token).

Environment:
    EODHD_API_TOKEN — EODHD API token (required to refresh from the source)

Typical usage:

    # On the Pi, on a schedule (cron / systemd timer):
    from financedata import refresh_universe
    refresh_universe()                       # fetch from EODHD, diff into cache

    # In a downstream project:
    from financedata import get_universe, get_universe_updates
    rows = get_universe(countries=["Sweden", "United States"])
    delta = get_universe_updates(since=my_last_sync_iso)
    #   → {"since": ..., "as_of": ..., "added": [...], "removed": [...], "modified": [...]}
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from typing import Any, Iterable

import httpx

from .cache import get_cache

logger = logging.getLogger(__name__)

API_BASE = "https://eodhd.com/api"

# The 18 countries visible in the Montrose market picker.
MONTROSE_COUNTRIES: set[str] = {
    "Sweden", "United States", "Denmark", "Finland", "Norway", "Canada",
    "United Kingdom", "Switzerland", "Poland", "Austria", "Germany", "France",
    "Ireland", "Spain", "Italy", "Netherlands", "Portugal", "Belgium",
}

COUNTRY_ALIASES = {
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "the netherlands": "Netherlands",
}

COMMON_EQUITY_TYPES = {
    "common stock", "ordinary shares", "ordinary share", "equity", "stock",
}
ETF_TYPES = {"etf", "exchange traded fund"}

# Fields compared to decide whether a stored listing "changed" on refresh.
# (`key`, `ticker`, `exchange_code` are part of the identity and never change.)
_MUTABLE_FIELDS = (
    "company_name", "exchange", "mic", "country",
    "isin", "security_type", "currency",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_country(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return COUNTRY_ALIASES.get(text.casefold(), text)


def _norm_col(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _is_allowed_security_type(value: Any, include_etfs: bool) -> bool:
    t = str(value or "").strip().casefold()
    if not t:
        # Some exchanges omit Type; keep the row rather than silently dropping it.
        return True
    if t in COMMON_EQUITY_TYPES or "common stock" in t or "ordinary" in t:
        return True
    if include_etfs and (t in ETF_TYPES or "etf" in t):
        return True
    return False


def _api_get(
    client: httpx.Client,
    endpoint: str,
    token: str,
    timeout: int,
    params: dict[str, Any] | None = None,
    retries: int = 4,
) -> Any:
    query = {"api_token": token, "fmt": "json"}
    if params:
        query.update(params)
    url = f"{API_BASE}/{endpoint.lstrip('/')}"

    for attempt in range(retries):
        try:
            response = client.get(url, params=query, timeout=timeout)
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPError(f"Temporary HTTP {response.status_code}")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            return payload
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Failed GET {url}: {exc}") from exc
            delay = 2 ** attempt
            logger.warning("Request failed (%s). Retrying in %ss…", exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _discover_exchanges(
    client: httpx.Client,
    token: str,
    timeout: int,
    countries: set[str],
    overrides: dict[str, list[str]] | None,
) -> list[dict]:
    payload = _api_get(client, "exchanges-list", token, timeout)
    if not payload:
        raise RuntimeError("The EODHD exchange directory was empty.")

    seen: set[tuple] = set()
    result: list[dict] = []
    for raw in payload:
        cols = {_norm_col(k): k for k in raw}
        code_key = cols.get("code") or cols.get("exchangecode")
        country_key = cols.get("country") or cols.get("countryname")
        name_key = cols.get("name") or cols.get("exchangename")
        mic_key = cols.get("operatingmic") or cols.get("mic")
        if not code_key or not country_key:
            raise RuntimeError(
                f"Unexpected exchanges-list schema. Keys: {list(raw.keys())}"
            )

        country = _normalize_country(raw.get(country_key))
        if country not in countries:
            continue
        code = str(raw.get(code_key) or "").strip()
        if not code:
            continue
        if overrides and code not in overrides.get(country, []):
            continue

        name = str(raw.get(name_key) or "").strip() if name_key else code
        mic = str(raw.get(mic_key) or "").strip() if mic_key else ""
        dedup = (country, code)
        if dedup in seen:
            continue
        seen.add(dedup)
        result.append(
            {"country": country, "code": code, "name": name or code, "mic": mic}
        )

    missing = countries - {e["country"] for e in result}
    if missing:
        logger.warning("No EODHD exchange found for: %s", ", ".join(sorted(missing)))
    result.sort(key=lambda e: (e["country"], e["code"]))
    return result


def _symbols_for_exchange(
    client: httpx.Client,
    token: str,
    timeout: int,
    exchange: dict,
    include_etfs: bool,
    include_delisted: bool,
) -> list[dict]:
    payload = _api_get(
        client,
        f"exchange-symbol-list/{exchange['code']}",
        token,
        timeout,
        params={"delisted": 1 if include_delisted else 0},
    )
    if not payload:
        return []

    rows: list[dict] = []
    for raw in payload:
        cols = {_norm_col(k): k for k in raw}

        def pick(*keys: str) -> str:
            for k in keys:
                if k in cols:
                    return str(raw.get(cols[k]) or "").strip()
            return ""

        sec_type = pick("type")
        if not _is_allowed_security_type(sec_type, include_etfs):
            continue

        ticker = pick("code", "symbol")
        company = pick("name")
        if not ticker or not company:
            continue

        rows.append(
            {
                "key": f"{exchange['code']}:{ticker}",
                "ticker": ticker,
                "company_name": company,
                "exchange": exchange["name"],
                "exchange_code": exchange["code"],
                "mic": exchange["mic"],
                "country": exchange["country"],
                "isin": pick("isin").upper(),
                "security_type": sec_type,
                "currency": pick("currency", "currencycode"),
            }
        )
    return rows


def _download_universe(
    token: str,
    countries: set[str],
    overrides: dict[str, list[str]] | None,
    include_etfs: bool,
    include_delisted: bool,
    timeout: int,
    sleep: float,
) -> list[dict]:
    headers = {"User-Agent": "financedata-universe/1.0"}
    with httpx.Client(headers=headers) as client:
        exchanges = _discover_exchanges(client, token, timeout, countries, overrides)
        logger.info("Selected %d exchange codes.", len(exchanges))

        rows: list[dict] = []
        for exch in exchanges:
            logger.info(
                "Downloading %s — %s (%s)",
                exch["country"], exch["name"], exch["code"],
            )
            try:
                rows.extend(
                    _symbols_for_exchange(
                        client, token, timeout, exch, include_etfs, include_delisted
                    )
                )
            except RuntimeError as exc:
                logger.error("Skipping %s: %s", exch["code"], exc)
            time.sleep(sleep)

    if not rows:
        raise RuntimeError("No symbol data was downloaded from EODHD.")
    return rows


# ── Public API ────────────────────────────────────────────────────────────────

def refresh_universe(
    api_token: str | None = None,
    *,
    countries: Iterable[str] | None = None,
    exchange_overrides: dict[str, list[str]] | None = None,
    include_etfs: bool = False,
    include_delisted: bool = False,
    timeout: int = 45,
    sleep: float = 0.25,
) -> dict:
    """
    Fetch the current universe from EODHD and reconcile it into the shared cache.

    New listings are inserted, changed listings are updated, and listings that
    disappeared from the source are flagged `status='removed'` (never deleted),
    so `get_universe_updates` can report them. Returns a summary dict:

        {"refreshed_at", "added", "removed", "modified", "unchanged", "total_active"}
    """
    import os

    token = api_token or os.environ.get("EODHD_API_TOKEN")
    if not token:
        raise ValueError(
            "Missing EODHD API token. Set EODHD_API_TOKEN or pass api_token=..."
        )

    wanted = {_normalize_country(c) for c in countries} if countries else set(MONTROSE_COUNTRIES)
    unknown = wanted - MONTROSE_COUNTRIES
    if unknown:
        raise ValueError(f"Unsupported countries: {', '.join(sorted(unknown))}")

    fetched = _download_universe(
        token, wanted, exchange_overrides, include_etfs, include_delisted, timeout, sleep
    )

    cache = get_cache()
    existing = cache.get_universe_map()
    now = datetime.utcnow().isoformat()

    seen: set[str] = set()
    to_write: list[dict] = []
    to_touch: list[str] = []
    added = modified = unchanged = 0

    for row in fetched:
        key = row["key"]
        if key in seen:
            continue
        seen.add(key)
        prev = existing.get(key)
        if prev is None:
            to_write.append(
                {**row, "status": "active", "first_seen": now, "last_seen": now, "updated_at": now}
            )
            added += 1
            continue
        changed = prev.get("status") != "active" or any(
            str(prev.get(f) or "") != str(row.get(f) or "") for f in _MUTABLE_FIELDS
        )
        if changed:
            to_write.append(
                {
                    **row,
                    "status": "active",
                    "first_seen": prev.get("first_seen") or now,
                    "last_seen": now,
                    "updated_at": now,
                }
            )
            modified += 1
        else:
            to_touch.append(key)
            unchanged += 1

    removed_keys = [
        k for k, v in existing.items()
        if k not in seen and v.get("status") == "active"
    ]

    cache.bulk_write_universe(to_write)
    cache.touch_universe(to_touch, now)
    cache.mark_universe_removed(removed_keys, now)

    summary = {
        "refreshed_at": now,
        "added": added,
        "removed": len(removed_keys),
        "modified": modified,
        "unchanged": unchanged,
        "total_active": added + modified + unchanged,
        "source": "eodhd",
    }
    cache.record_universe_refresh(summary)
    logger.info(
        "Universe refreshed: +%d added, -%d removed, ~%d modified, %d unchanged (%d active)",
        summary["added"], summary["removed"], summary["modified"],
        summary["unchanged"], summary["total_active"],
    )
    return summary


def get_universe(
    countries: Iterable[str] | None = None,
    *,
    active_only: bool = True,
    as_dataframe: bool = False,
):
    """
    Return the current cached universe (no network calls).

    countries    — optional filter (e.g. ["Sweden", "United States"])
    active_only  — exclude listings flagged removed (default True)
    as_dataframe — return a pandas DataFrame instead of list[dict]
    """
    country_list = [_normalize_country(c) for c in countries] if countries else None
    rows = get_cache().query_universe(country_list, active_only=active_only)
    if as_dataframe:
        import pandas as pd
        return pd.DataFrame(rows)
    return rows


def get_universe_symbols(
    countries: Iterable[str] | None = None,
    *,
    active_only: bool = True,
) -> list[str]:
    """Convenience: just the tickers for the (optionally filtered) universe."""
    return [r["ticker"] for r in get_universe(countries, active_only=active_only)]


def get_universe_updates(
    since: str,
    countries: Iterable[str] | None = None,
) -> dict:
    """
    Return listings that changed since `since` (an ISO timestamp), split by kind.

    A downstream project stores the `as_of` value it gets back and passes it as
    `since` on the next call, so it only ever processes deltas:

        {"since", "as_of", "added": [...], "removed": [...], "modified": [...]}
    """
    country_list = [_normalize_country(c) for c in countries] if countries else None
    rows = get_cache().query_universe(
        country_list, active_only=False, updated_since=since
    )
    added, removed, modified = [], [], []
    for r in rows:
        if r.get("status") == "removed":
            removed.append(r)
        elif (r.get("first_seen") or "") > since:
            added.append(r)
        else:
            modified.append(r)
    return {
        "since": since,
        "as_of": datetime.utcnow().isoformat(),
        "added": added,
        "removed": removed,
        "modified": modified,
    }


def last_refresh() -> dict | None:
    """Metadata about the most recent refresh, or None if never refreshed."""
    return get_cache().last_universe_refresh()


def export_universe(
    path: str,
    countries: Iterable[str] | None = None,
    *,
    active_only: bool = True,
) -> int:
    """
    Write the cached universe to CSV (or XLSX if `path` ends with .xlsx and
    xlsxwriter/openpyxl is installed). Returns the number of rows written.
    """
    import pandas as pd

    df = get_universe(countries, active_only=active_only, as_dataframe=True)
    if path.lower().endswith(".xlsx"):
        df.to_excel(path, index=False, sheet_name="Universe")
    else:
        df.to_csv(path, index=False, encoding="utf-8-sig")
    return len(df)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the FinanceData broker universe cache."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="Fetch from EODHD and update the cache.")
    p_refresh.add_argument("--api-token", default=None, help="EODHD token (else $EODHD_API_TOKEN).")
    p_refresh.add_argument("--include-etfs", action="store_true")
    p_refresh.add_argument("--include-delisted", action="store_true")
    p_refresh.add_argument(
        "--overrides",
        default=None,
        help='JSON file mapping country → exchange codes, e.g. {"Sweden": ["ST"]}.',
    )

    p_export = sub.add_parser("export", help="Write the cached universe to CSV/XLSX.")
    p_export.add_argument("path")
    p_export.add_argument("--country", action="append", dest="countries")
    p_export.add_argument("--include-removed", action="store_true")

    sub.add_parser("status", help="Show the last refresh summary.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "refresh":
        overrides = None
        if args.overrides:
            with open(args.overrides, "r", encoding="utf-8") as f:
                overrides = json.load(f)
        summary = refresh_universe(
            api_token=args.api_token,
            exchange_overrides=overrides,
            include_etfs=args.include_etfs,
            include_delisted=args.include_delisted,
        )
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "export":
        n = export_universe(
            args.path, args.countries, active_only=not args.include_removed
        )
        print(f"Wrote {n:,} rows to {args.path}")
        return 0

    if args.command == "status":
        info = last_refresh()
        print(json.dumps(info, indent=2) if info else "Universe has never been refreshed.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_main())
