"""
Pure technical indicator functions. No I/O, no caching — just pandas math.
All inputs are pd.Series/DataFrame with a DatetimeIndex.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Wilder RSI using EWM (same as swing-trader). Returns latest value or None."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    series = 100 - (100 / (1 + rs))
    val = series.iloc[-1]
    return None if pd.isna(val) else round(float(val), 1)


def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Full RSI series (for screening passes that need the whole history)."""
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range using EWM (Wilder smoothing)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def vwap(bars: pd.DataFrame) -> float | None:
    """Cumulative VWAP from intraday bars. bars must have High, Low, Close, Volume columns."""
    if bars.empty or bars["Volume"].sum() == 0:
        return None
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    return float((typical * bars["Volume"]).sum() / bars["Volume"].sum())


def sma(closes: pd.Series, period: int) -> float | None:
    """Simple moving average of the last `period` bars."""
    if len(closes) < period:
        return None
    val = closes.rolling(period).mean().iloc[-1]
    return None if pd.isna(val) else float(val)


def ema(closes: pd.Series, period: int) -> float | None:
    """Exponential moving average, latest value."""
    if len(closes) < period:
        return None
    val = closes.ewm(span=period, adjust=False).mean().iloc[-1]
    return None if pd.isna(val) else float(val)


def ann_vol(closes: pd.Series, periods: int = 20) -> float | None:
    """Annualised historical volatility (log returns, 252 trading days)."""
    if len(closes) < periods + 1:
        return None
    log_ret = (closes / closes.shift(1)).apply(
        lambda x: math.log(x) if x > 0 else float("nan")
    )
    std = log_ret.iloc[-periods:].std()
    return None if pd.isna(std) else round(std * math.sqrt(252) * 100, 1)


def pct_return(closes: pd.Series, periods: int) -> float | None:
    """Percentage return over the last `periods` bars."""
    if len(closes) < periods + 1:
        return None
    val = (closes.iloc[-1] / closes.iloc[-1 - periods] - 1) * 100
    return round(val, 2)


def key_levels(daily: pd.DataFrame, last_price: float, lookback: int = 20) -> tuple[float | None, float | None]:
    """Nearest support (highest low below price) and resistance (lowest high above) in last `lookback` bars."""
    if len(daily) < 5:
        return None, None
    recent = daily.iloc[-lookback:]
    supports = sorted([l for l in recent["Low"].values if l < last_price], reverse=True)
    resistances = sorted([h for h in recent["High"].values if h > last_price])
    return (float(supports[0]) if supports else None,
            float(resistances[0]) if resistances else None)


def daily_momentum_score(
    close: pd.Series,
    volume: pd.Series,
    rsi_period: int = 14,
    ma_short: int = 20,
    ma_long: int = 50,
) -> float:
    """Quick composite score for batch screening (higher = more interesting)."""
    if len(close) < 10:
        return -99.0
    score = 0.0
    last = float(close.iloc[-1])

    ma20 = sma(close, ma_short)
    if ma20 is not None and last > ma20:
        score += 2.0
    ma50 = sma(close, ma_long)
    if ma50 is not None and last > ma50:
        score += 2.0

    rsi_val = rsi(close, rsi_period)
    if rsi_val is not None:
        if 40 <= rsi_val <= 60:
            score += 2.0
        elif rsi_val < 35:
            score += 1.5
        elif rsi_val > 70:
            score += 1.0

    if len(volume) >= 21:
        avg_vol = float(volume.iloc[-21:-1].mean())
        today_vol = float(volume.iloc[-1])
        if avg_vol > 0 and not math.isnan(avg_vol):
            ratio = today_vol / avg_vol
            if ratio > 1.5:
                score += 3.0
            elif ratio > 1.0:
                score += 1.5

    if len(close) >= 2:
        prev = float(close.iloc[-2])
        if prev > 0 and abs(last - prev) / prev < 0.005:
            score += 2.0

    return score
