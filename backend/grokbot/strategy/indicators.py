"""Technical indicators. All functions are causal: value[i] only uses data
up to and including bar i, so they are safe for backtesting without
look-ahead bias. Leading values without enough history are NaN.
"""
from __future__ import annotations

import numpy as np

NaN = np.nan


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), NaN)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), NaN)
    if len(x) < n:
        return out
    a = 2 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), NaN)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(close), NaN)
    if len(close) <= n:
        return out
    d = np.diff(close)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = _wilder(up, n), _wilder(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        r = 100 - 100 / (1 + rs)
    r = np.where(ad == 0, 100.0, r)
    out[1:] = r
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = np.full(len(close), NaN)
    valid = ~np.isnan(line)
    if valid.sum() >= signal:
        idx = np.argmax(valid)
        sig[idx:] = ema(line[idx:], signal)
    return line, sig, line - sig


def true_range(high, low, close) -> np.ndarray:
    prev = np.concatenate([[close[0]], close[:-1]])
    return np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return _wilder(true_range(high, low, close), n)


def adx(high, low, close, n: int = 14) -> np.ndarray:
    up = np.diff(high, prepend=high[0])
    dn = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = _wilder(true_range(high, low, close), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * _wilder(plus_dm, n) / tr
        mdi = 100 * _wilder(minus_dm, n) / tr
        dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    out = np.full(len(close), NaN)
    valid = ~np.isnan(dx)
    if valid.sum() >= n:
        i0 = np.argmax(valid)
        out[i0:] = _wilder(np.nan_to_num(dx[i0:]), n)
    return out


def vwap_session(open_time_ms: np.ndarray, high, low, close, volume) -> np.ndarray:
    """VWAP anchored at each UTC day start."""
    tp = (high + low + close) / 3
    day = (open_time_ms // 86_400_000).astype(np.int64)
    out = np.empty(len(close))
    pv = vol = 0.0
    for i in range(len(close)):
        if i == 0 or day[i] != day[i - 1]:
            pv = vol = 0.0
        pv += tp[i] * volume[i]
        vol += volume[i]
        out[i] = pv / vol if vol > 0 else close[i]
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), NaN)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].max()
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), NaN)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].min()
    return out


def bb_width(close: np.ndarray, n: int = 20, k: float = 2.0) -> np.ndarray:
    m = sma(close, n)
    out = np.full(len(close), NaN)
    for i in range(n - 1, len(close)):
        out[i] = 2 * k * close[i - n + 1:i + 1].std() / m[i] if m[i] else NaN
    return out


def zscore(x: np.ndarray, n: int = 50) -> float:
    """z-score of the last value against the previous n values."""
    if len(x) < n + 1:
        return 0.0
    w = x[-n - 1:-1]
    sd = w.std()
    return float((x[-1] - w.mean()) / sd) if sd > 0 else 0.0


def swing_levels(high: np.ndarray, low: np.ndarray, lookback: int = 100, k: int = 3) -> tuple[list[float], list[float]]:
    """Recent pivot highs (resistance) and pivot lows (support). A pivot at i
    needs k bars on each side, so the most recent k bars cannot be pivots."""
    h, l = high[-lookback:], low[-lookback:]
    res, sup = [], []
    for i in range(k, len(h) - k):
        if h[i] == h[i - k:i + k + 1].max():
            res.append(float(h[i]))
        if l[i] == l[i - k:i + k + 1].min():
            sup.append(float(l[i]))
    return res, sup


def trend_direction(close: np.ndarray) -> int:
    """+1 up, -1 down, 0 neutral, from EMA50/EMA200 alignment and slope."""
    if len(close) < 60:
        return 0
    e50 = ema(close, 50)
    e200 = ema(close, min(200, len(close) - 1))
    slope = e50[-1] - e50[-6]
    if close[-1] > e50[-1] > e200[-1] and slope > 0:
        return 1
    if close[-1] < e50[-1] < e200[-1] and slope < 0:
        return -1
    return 0
