"""Small shared helpers: time, identifiers and Decimal arithmetic.

Money and quantities are handled with ``Decimal`` everywhere they feed into
an order or the ledger. Floats are only used for indicators and display.
"""
from __future__ import annotations

import secrets
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 34

ZERO = Decimal("0")
ONE = Decimal("1")


def now() -> float:
    return time.time()


def D(x) -> Decimal:
    """Convert to Decimal without float binary artefacts."""
    if isinstance(x, Decimal):
        return x
    if x is None:
        return ZERO
    if isinstance(x, float):  # includes numpy.float64, whose repr is 'np.float64(...)'
        return Decimal(repr(float(x)))
    return Decimal(str(x))


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` down to an exchange step size (LOT_SIZE / tickSize)."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step


def fmt(d: Decimal) -> str:
    """Plain decimal string suitable for Binance parameters (no exponent)."""
    s = format(d.normalize(), "f")
    return s if s not in ("-0", "") else "0"


def new_trade_id() -> str:
    # 'GB' + base-time + random: sortable, short enough for clientOrderId suffixes.
    t = int(time.time() * 1000)
    return f"GB{t:x}{secrets.token_hex(3)}".upper()


def client_order_id(trade_id: str, purpose: str, attempt: int = 0) -> str:
    """Deterministic client order id so that a restart can find an order that
    was already submitted instead of placing it a second time.

    Must satisfy Binance's ``^[\\.A-Z\\:/a-z0-9_-]{1,36}$``.
    """
    cid = f"{trade_id}-{purpose}" + (f"-{attempt}" if attempt else "")
    return cid[:36]
