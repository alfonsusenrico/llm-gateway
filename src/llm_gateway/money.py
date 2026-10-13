"""USD amounts as integer micro-USD (1 USD = 1,000,000) so sums never round."""

from __future__ import annotations

from decimal import Decimal

MICRO_PER_USD = 1_000_000


def to_micro_usd(amount: Decimal) -> int:
    return int(amount * MICRO_PER_USD)


def format_usd(micro_usd: int) -> str:
    """A decimal string with at least two decimals, for example "1.50" or "0.000125"."""
    text = f"{Decimal(micro_usd) / MICRO_PER_USD:.6f}".rstrip("0")
    whole, _, fraction = text.partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"
