"""The acceptance test for prompt caching, against a real vendor.

Everything else in this suite runs on the fake and proves plumbing. None of it
can prove caching, because caching is the one feature where **working code and a
correct-looking request prove nothing**: Anthropic accepts a `cache_control`
marker on a prefix below the per-model token floor, caches nothing, and reports
no error. `cache_creation_input_tokens` simply comes back 0 (ADR-003).

So the criterion is empirical and there are only three assertions worth making:

1. the prefix measures above the floor, counted by the vendor rather than
   estimated;
2. a *second* identical request reports `cache_read_tokens > 0`;
3. that second request costs less than the first.

Run with `pytest -m live`. It costs real money — a few cents — and needs
`ANTHROPIC_API_KEY`. It is excluded from the default run and from CI, which is
why CI needs no secrets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.api.schemas import TriageRequest
from app.core.config import Settings, get_settings
from app.core.pricing import compute_cost_usd
from app.domain.triage import TriageResult
from app.providers.anthropic import AnthropicProvider
from app.providers.base import ProviderError
from app.services.triage import build_triage_prompt

pytestmark = pytest.mark.live

#: The highest per-model floor in Anthropic's table (the Opus 4.x family and
#: Haiku 4.5; the Sonnet family's is lower). The playbook is sized against the
#: high one so that re-pointing ANTHROPIC_MODEL cannot silently switch caching
#: off — and because Sonnet 5 is not listed in the published table at all, which
#: makes assuming the lower number a bet rather than a fact.
CACHING_FLOOR_TOKENS = 4096

TICKET = TriageRequest(
    ticket_id="T-1",
    subject="Double charge",
    body="I was billed twice for order 4471.",
)


@pytest.fixture
def settings() -> Settings:
    """Real settings, from the environment — not the CI stub in `conftest.py`."""
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
async def provider(settings: Settings) -> AsyncIterator[AnthropicProvider]:
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    built = AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        max_output_tokens=settings.max_output_tokens,
    )
    yield built
    # Closed explicitly: left to the garbage collector, the underlying httpx
    # client is finalized after the event loop has gone and the teardown fills
    # the report with `RuntimeError: Event loop is closed`.
    await built._client.close()


async def test_the_playbook_clears_the_caching_floor(provider: AnthropicProvider) -> None:
    """Counted by the vendor's tokenizer, not by a heuristic over characters.

    `/v1/triage` sends no tools, so the playbook is the *whole* prefix on this
    path — it does not get to borrow the tool schemas' tokens the way `/v1/chat`
    does. It has to clear the floor on its own merits.
    """
    kwargs = provider._request_kwargs(build_triage_prompt(TICKET))
    counted = await provider._client.messages.count_tokens(
        model=kwargs["model"],
        system=kwargs["system"],
        messages=kwargs["messages"],
    )

    assert counted.input_tokens > CACHING_FLOOR_TOKENS


async def test_a_repeated_ticket_is_served_from_cache(provider: AnthropicProvider) -> None:
    """The only honest proof that caching works: a nonzero read on request two.

    Note what is *not* asserted — that the first request writes the cache. It
    only does so on a cold prefix, and the entry survives five minutes, so
    re-running this file inside that window makes the first call a read too.
    Pinning the write would produce a test that passes or fails depending on how
    recently it last ran. What holds either way is that the prefix is cached
    *somehow* on the first call and read back on the second.
    """
    first = await provider.parse(build_triage_prompt(TICKET), TriageResult)
    second = await provider.parse(build_triage_prompt(TICKET), TriageResult)

    assert first.usage.cache_write_tokens + first.usage.cache_read_tokens > CACHING_FLOOR_TOKENS
    assert second.usage.cache_read_tokens > CACHING_FLOOR_TOKENS
    assert second.usage.cache_hit is True
    # The prefix really is a prefix: the uncached remainder is the ticket and
    # the reply, three orders of magnitude smaller than what was cached.
    assert second.usage.input_tokens < CACHING_FLOOR_TOKENS


async def test_a_cached_request_costs_a_fraction_of_an_uncached_one(
    provider: AnthropicProvider,
) -> None:
    """The economic argument, priced from a real request's own token counts.

    Compares what the cached call cost against what the identical prompt would
    have cost with the prefix billed at the full input rate — same tokens, one
    of them moved from `cache_read` to `input`. That comparison is deterministic
    and does not depend on whether the cache happened to be warm, which the
    naive "second request is cheaper than the first" version does.
    """
    await provider.parse(build_triage_prompt(TICKET), TriageResult)
    cached = (await provider.parse(build_triage_prompt(TICKET), TriageResult)).usage

    def price(input_tokens: int, cache_read_tokens: int) -> float:
        return compute_cost_usd(
            model=cached.model,
            input_tokens=input_tokens,
            output_tokens=cached.output_tokens,
            cache_read_tokens=cache_read_tokens,
        )

    actual = price(cached.input_tokens, cached.cache_read_tokens)
    if_uncached = price(cached.input_tokens + cached.cache_read_tokens, 0)

    assert actual < if_uncached
    # Cache reads bill at 0.1x the input rate, so the prompt half of the bill
    # drops by ~90%. Asserted loosely — the output tokens are not discounted and
    # their share varies with how much the model writes.
    assert actual < if_uncached * 0.8


async def test_the_verdict_is_schema_valid(provider: AnthropicProvider) -> None:
    """Structured output against the live API, not against a stubbed SDK.

    `parse()` returning at all is the assertion: it raises rather than degrading
    when the vendor cannot honor the schema, so a `TriageResult` in hand is
    proof the constraint held.
    """
    result = await provider.parse(build_triage_prompt(TICKET), TriageResult)

    verdict = result.parsed
    assert 0.0 <= verdict.confidence <= 1.0
    assert verdict.summary
    # Not asserted: the specific category. That is model behaviour, and pinning
    # it here would make this a flaky quality gate rather than a caching test.
    # Classification accuracy is Phase 4's job, on a golden set.
    assert isinstance(verdict, TriageResult)


async def test_an_impossible_schema_raises_rather_than_degrading(
    provider: AnthropicProvider,
) -> None:
    """A response that cannot satisfy the schema must fail, not be improvised.

    Forced by capping output at a handful of tokens: the model cannot finish the
    JSON, and the adapter has to say so instead of returning something half
    parsed.
    """
    starved = AnthropicProvider(
        api_key=provider._client.api_key,
        model=provider.model,
        max_output_tokens=16,
    )

    with pytest.raises(ProviderError, match="MAX_OUTPUT_TOKENS"):
        await starved.parse(build_triage_prompt(TICKET), TriageResult)
