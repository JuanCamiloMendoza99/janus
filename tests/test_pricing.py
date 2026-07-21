"""The pricing table and cost arithmetic.

A cost figure is only as trustworthy as the table behind it, so these lock down
the two failure modes that would silently corrupt every reported cost: a missing
price returning 0, and cached tokens being double-counted against the prefix.
"""

from __future__ import annotations

import pytest

from app.core.pricing import UnknownModelError, compute_cost_usd, get_pricing
from app.providers.fake import FAKE_MODEL


def test_get_pricing_returns_the_entry_for_a_known_model() -> None:
    pricing = get_pricing("claude-sonnet-5")

    assert pricing.input_usd_per_mtok == 3.00
    assert pricing.output_usd_per_mtok == 15.00


def test_get_pricing_raises_for_an_unknown_model() -> None:
    """Missing prices fail loudly; a silent 0 would make the cost log quietly wrong."""
    with pytest.raises(UnknownModelError):
        get_pricing("gpt-does-not-exist")


def test_the_fake_model_is_free() -> None:
    assert compute_cost_usd(FAKE_MODEL, input_tokens=10_000, output_tokens=10_000) == 0.0


def test_cost_sums_input_and_output_at_their_rates() -> None:
    # claude-sonnet-5: $3 / MTok in, $15 / MTok out.
    cost = compute_cost_usd("claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)

    assert cost == pytest.approx(3.0 + 15.0)


def test_cache_reads_are_discounted_not_double_counted() -> None:
    """`input_tokens` is the uncached remainder; cached reads bill at 0.1x input.

    A cost function that folded the cached tokens back into input would report
    the prefix twice.
    """
    cost = compute_cost_usd(
        "claude-sonnet-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )

    # 0.10 (read multiplier) * $3 input rate = $0.30 for 1M cached tokens.
    assert cost == pytest.approx(0.30)


def test_openai_default_model_is_priced() -> None:
    cost = compute_cost_usd("gpt-5.6-terra", input_tokens=1_000_000, output_tokens=0)

    assert cost == pytest.approx(2.50)
