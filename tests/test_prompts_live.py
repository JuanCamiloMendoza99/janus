"""The acceptance test for every playbook variant, against a real vendor.

`tests/test_prompts.py` proves a variant loads and clears a *character* floor.
Only this file proves what actually decides whether caching works: the variant
clears the vendor's *token* floor, counted with the vendor's own tokenizer, on
the `/v1/triage` request it is really sent in.

It also pins each variant's `measured_tokens` to what the API reports now, so a
prompt edited without re-measuring fails a test rather than a bill — the same
discipline `tests/test_caching_live.py` applies to the caching feature itself.

Run with `pytest -m live`. Costs nothing but a few token-count calls (which are
free) and needs `ANTHROPIC_API_KEY`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.api.schemas import TriageRequest
from app.core.config import Settings, get_settings
from app.domain.prompts import VARIANTS, get_variant, load_playbook
from app.providers.anthropic import AnthropicProvider
from app.services.triage import build_triage_prompt

pytestmark = pytest.mark.live

#: The highest per-model floor in Anthropic's table (Opus 4.x and Haiku 4.5). A
#: variant sized against the high floor caches on every model — so re-pointing
#: ANTHROPIC_MODEL cannot silently switch a variant's caching off. See ADR-003.
CACHING_FLOOR_TOKENS = 4096

TICKET = TriageRequest(
    ticket_id="T-1",
    subject="Double charge",
    body="I was billed twice for order 4471.",
)


@pytest.fixture
def settings() -> Settings:
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
    await built._client.close()


async def _count(provider: AnthropicProvider, variant_name: str) -> int:
    kwargs = provider._request_kwargs(build_triage_prompt(TICKET, load_playbook(variant_name)))
    counted = await provider._client.messages.count_tokens(
        model=kwargs["model"],
        system=kwargs["system"],
        messages=kwargs["messages"],
    )
    return counted.input_tokens


@pytest.mark.parametrize("variant_name", sorted(VARIANTS))
async def test_every_variant_clears_the_caching_floor(
    provider: AnthropicProvider, variant_name: str
) -> None:
    """Counted by the vendor, not estimated from characters.

    `/v1/triage` sends no tools, so a variant is the whole prefix on this path
    and has to clear the floor on its own merits. A terser variant that drops
    below it still runs and still classifies — it just stops caching, silently,
    and then looks expensive for a reason that is not the prompt's quality. That
    is the failure this test exists to make loud.
    """
    assert await _count(provider, variant_name) > CACHING_FLOOR_TOKENS


@pytest.mark.parametrize("variant_name", sorted(VARIANTS))
async def test_recorded_token_count_matches_the_tokenizer(
    provider: AnthropicProvider, variant_name: str
) -> None:
    """The registry's `measured_tokens` is a claim; this is where it is checked.

    Pinned exactly rather than loosely: the cost comparison in
    `docs/evals/prompts.md` is only fair if the token counts it reports are the
    ones the variants really cost. A prompt edited without updating the registry
    should turn this red so the number gets re-measured, not quietly drift.
    """
    assert await _count(provider, variant_name) == get_variant(variant_name).measured_tokens
