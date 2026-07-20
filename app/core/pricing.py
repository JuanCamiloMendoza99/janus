"""Model pricing table and cost computation.

A "cost per request" number is only as trustworthy as the table behind it, so
prices are data — versioned, dated and in one place — rather than constants
scattered across the provider adapters. See ADR-005.

Prices are USD per 1M tokens. `verified_on` is the date the figure was last
checked against the vendor's public pricing page; treat anything stale as
suspect before quoting a cost figure to anyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Anthropic bills cache operations at a multiple of the base input rate:
# writing a cache entry costs more than a plain input token, reading one costs
# far less. These multipliers are what make caching pay off after ~2 reads.
ANTHROPIC_CACHE_WRITE_MULTIPLIER = 1.25
ANTHROPIC_CACHE_READ_MULTIPLIER = 0.10

# OpenAI does not charge a write premium — caching is automatic and only the
# discounted read rate applies.
OPENAI_CACHE_WRITE_MULTIPLIER = 1.00
OPENAI_CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class ModelPricing:
    """Per-1M-token pricing for a single model."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_multiplier: float
    cache_read_multiplier: float
    verified_on: date


# Keyed by the exact model id sent on the wire.
PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-8": ModelPricing(
        input_usd_per_mtok=5.00,
        output_usd_per_mtok=25.00,
        cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
        cache_read_multiplier=ANTHROPIC_CACHE_READ_MULTIPLIER,
        verified_on=date(2026, 7, 20),
    ),
    "claude-sonnet-5": ModelPricing(
        input_usd_per_mtok=3.00,
        output_usd_per_mtok=15.00,
        cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
        cache_read_multiplier=ANTHROPIC_CACHE_READ_MULTIPLIER,
        verified_on=date(2026, 7, 20),
    ),
    "claude-haiku-4-5": ModelPricing(
        input_usd_per_mtok=1.00,
        output_usd_per_mtok=5.00,
        cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
        cache_read_multiplier=ANTHROPIC_CACHE_READ_MULTIPLIER,
        verified_on=date(2026, 7, 20),
    ),
    # OpenAI entries are placeholders until Phase 1 verifies both the current
    # model id and its published rates. Deliberately absent rather than guessed:
    # a wrong price is worse than a missing one, because it looks authoritative.
}

# The fake provider is free; giving it a real entry keeps the cost path exercised
# in tests instead of being special-cased away.
FAKE_MODEL_PRICING = ModelPricing(
    input_usd_per_mtok=0.0,
    output_usd_per_mtok=0.0,
    cache_write_multiplier=1.0,
    cache_read_multiplier=1.0,
    verified_on=date(2026, 7, 20),
)


class UnknownModelError(LookupError):
    """Raised when a cost is requested for a model with no pricing entry.

    Failing loudly is intentional: silently returning 0.0 would make the cost
    log quietly wrong, which is worse than an error.
    """


def get_pricing(model: str) -> ModelPricing:
    """Return the pricing entry for `model`, or raise `UnknownModelError`."""
    raise NotImplementedError("Phase 1")


def compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Compute the USD cost of a single model call.

    `input_tokens` must be the *uncached* remainder only. Both vendors report
    cached tokens separately from `input_tokens`, so adding them together
    double-counts the prefix and inflates the reported cost.
    """
    raise NotImplementedError("Phase 1")
