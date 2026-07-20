"""The provider seam itself.

These tests are about the contract, not about any vendor. They are what tells
you the abstraction holds before either real adapter exists — and they are the
tests every future adapter must also pass.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.providers.base import (
    Done,
    LLMProvider,
    Message,
    Prompt,
    TextDelta,
    Usage,
    UsageReport,
)
from app.providers.fake import FakeProvider
from app.providers.registry import ProviderConfigurationError, build_provider


@pytest.fixture
def prompt() -> Prompt:
    return Prompt(
        cacheable_prefix="stable playbook text",
        system=None,
        messages=[Message(role="user", content="my invoice is wrong")],
    )


def test_fake_provider_satisfies_the_protocol(fake_provider: FakeProvider) -> None:
    """Structural typing means no inheritance — but the shape must still match."""
    assert isinstance(fake_provider, LLMProvider)


async def test_stream_ends_with_usage_then_done(
    fake_provider: FakeProvider, prompt: Prompt
) -> None:
    """Event order is part of the contract, not an implementation detail.

    The cost ledger cannot be flushed until `UsageReport` has arrived, and
    `UsageReport` arrives near the end of the stream. A provider that emitted
    usage first would let a broken middleware pass its tests. See ADR-004.
    """
    events = [event async for event in fake_provider.stream(prompt)]

    assert isinstance(events[-1], Done)
    assert isinstance(events[-2], UsageReport)
    assert any(isinstance(e, TextDelta) for e in events)


async def test_completion_reports_usage(fake_provider: FakeProvider, prompt: Prompt) -> None:
    completion = await fake_provider.complete(prompt)

    assert completion.stop_reason == "end_turn"
    assert completion.usage.input_tokens > 0
    assert completion.usage.output_tokens > 0


def test_usage_keeps_cached_tokens_separate_from_input() -> None:
    """`input_tokens` is the uncached remainder, matching both vendors.

    Folding cached tokens into `input_tokens` would double-count the prefix and
    silently overstate every cost figure the project produces.
    """
    usage = Usage(
        model="m",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=900,
        cache_write_tokens=0,
    )

    assert usage.total_prompt_tokens == 1000
    assert usage.cache_hit is True


def test_usage_without_cache_reads_is_not_a_hit() -> None:
    usage = Usage(model="m", input_tokens=1000, output_tokens=50)

    assert usage.cache_hit is False


def test_registry_selects_the_fake_provider() -> None:
    provider = build_provider(Settings(llm_provider="fake"))

    assert provider.name == "fake"


@pytest.mark.parametrize("provider_name", ["anthropic", "openai"])
def test_registry_rejects_a_provider_without_credentials(provider_name: str) -> None:
    """Fail at selection time with a clear message, not later with a vendor 401."""
    settings = Settings(llm_provider=provider_name, anthropic_api_key=None, openai_api_key=None)

    with pytest.raises(ProviderConfigurationError, match="API_KEY"):
        build_provider(settings)
