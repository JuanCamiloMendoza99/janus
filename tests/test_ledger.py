"""The per-request ledger and the process-wide usage store."""

from __future__ import annotations

import pytest

from app.observability.ledger import UsageLedger, UsageStore
from app.providers.base import Usage


def test_record_prices_each_call_as_it_lands() -> None:
    ledger = UsageLedger()
    ledger.record(
        "anthropic", Usage(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)
    )

    assert ledger.call_count == 1
    assert ledger.total_cost_usd == pytest.approx(3.0)


def test_summary_flattens_every_call() -> None:
    ledger = UsageLedger()
    ledger.record("openai", Usage(model="gpt-5.6-terra", input_tokens=10, output_tokens=5))
    ledger.record("openai", Usage(model="gpt-5.6-terra", input_tokens=20, output_tokens=7))

    summary = ledger.summary()

    assert summary["calls"] == 2
    assert summary["input_tokens"] == 30
    assert summary["output_tokens"] == 12
    assert summary["providers"] == ["openai"]
    assert summary["models"] == ["gpt-5.6-terra"]


def test_store_aggregates_requests_and_cache_hit_rate() -> None:
    store = UsageStore()

    ledger = UsageLedger()
    ledger.record(
        "openai",
        Usage(
            model="gpt-5.6-terra",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=900,
        ),
    )
    store.record_request(ledger)

    snapshot = store.snapshot()

    assert snapshot.requests == 1
    assert snapshot.total_input_tokens == 100
    assert snapshot.total_output_tokens == 50
    assert snapshot.total_cache_read_tokens == 900
    assert snapshot.total_prompt_tokens == 1000
    assert snapshot.cache_hit_rate == pytest.approx(0.9)
    assert snapshot.by_model["gpt-5.6-terra"] > 0


def test_cache_hit_rate_is_zero_with_no_prompt_tokens() -> None:
    assert UsageStore().snapshot().cache_hit_rate == 0.0
