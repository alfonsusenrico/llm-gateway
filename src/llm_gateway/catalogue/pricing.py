"""Exact call cost in micro-USD from usage and a dated price entry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from llm_gateway.catalogue.model import PriceEntry


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int  # includes reasoning tokens, as the provider bills them
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0


def cost_micro_usd(usage: TokenUsage, price: PriceEntry | None) -> int | None:
    """The cost rounded up to whole micro-USD, or None when no price applies.

    A rate in USD per million tokens equals micro-USD per token, so token counts
    multiply the rates directly.
    """
    if price is None:
        return None
    rates = price.usd_per_million
    input_multiplier = output_multiplier = Decimal(1)
    long_context = price.long_context
    if long_context is not None and usage.input_tokens > long_context.threshold_input_tokens:
        # The long-context rate applies to the whole request, not just the excess.
        input_multiplier = long_context.input_multiplier
        output_multiplier = long_context.output_multiplier
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens)
    total = (
        uncached * rates.input * input_multiplier
        + usage.cached_input_tokens * rates.cached_input * input_multiplier
        + usage.cache_write_tokens * rates.cache_write * input_multiplier
        + usage.output_tokens * rates.output * output_multiplier
    )
    return math.ceil(total)
