"""Money helpers — integer micros everywhere (roadmap DB convention).

USD x 1_000_000, stored as ``int``. Floats are never accumulated; external
USD figures enter only through :func:`usd_to_micros` (Decimal, HALF_UP).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICROS_PER_USD = 1_000_000


def usd_to_micros(value: Decimal | str | int | float) -> int:
    """Convert a USD amount to integer micros, rounding half-up."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * MICROS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> Decimal:
    """Convert integer micros back to an exact Decimal USD amount."""
    return (Decimal(int(micros)) / MICROS_PER_USD).quantize(Decimal("0.000001"))
