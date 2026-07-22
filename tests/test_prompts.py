"""The prompt registry: selection, isolation, and the invariants every variant
has to hold whatever else changes about it.

These run on no network. The one thing they cannot check — that a variant clears
the vendor's *token* floor rather than a character proxy for it — lives in
`tests/test_prompts_live.py` behind the `live` marker.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.prompts import (
    DEFAULT_VARIANT,
    VARIANTS,
    UnknownPromptError,
    get_variant,
    load_playbook,
)

#: A crude offline smoke test, not the authoritative floor. The real constraint is
#: 4096 *tokens*, and only the vendor's tokenizer knows the exact count — that
#: assertion lives in `tests/test_prompts_live.py`. This one runs in CI with no
#: network and catches the gross version of the mistake: a variant trimmed well
#: below the shipped set. Set under the shortest current variant (`v3-terse`, ~13k
#: chars / 4,738 tokens) so a real trim trips it here before it trips a bill.
CHARACTER_FLOOR = 12_000


@pytest.fixture(params=sorted(VARIANTS))
def variant_name(request: pytest.FixtureRequest) -> str:
    """Every registered variant, one test per variant."""
    return request.param


def test_every_variant_loads(variant_name: str) -> None:
    text = load_playbook(variant_name)
    assert text.strip(), f"{variant_name} loaded empty"


def test_every_variant_clears_the_character_floor(variant_name: str) -> None:
    """The crude, offline half of the caching-floor guard.

    A real token count needs the vendor and costs a round trip, so the
    authoritative check lives in `test_prompts_live.py`. This one runs in CI and
    catches the obvious version: a variant shortened well past the point where
    caching keeps working.
    """
    assert len(load_playbook(variant_name)) > CHARACTER_FLOOR


def test_no_variant_carries_meta_commentary(variant_name: str) -> None:
    """A variant's hypothesis and token count live in the registry, not the bytes.

    An HTML comment in the prompt is the tell that a note leaked into the cached
    prefix — which inflates that one variant's token count and makes the cost
    comparison unfair. Variants compete on equal token footing or they compete on
    nothing (see the registry module docstring, and Phase 5's risks).
    """
    assert "<!--" not in load_playbook(variant_name)


def test_a_measured_token_count_is_recorded_and_dated(variant_name: str) -> None:
    """Every variant carries a dated token count, so cost is auditable, not asserted.

    The number itself is checked against the live tokenizer elsewhere; here we
    only insist it exists and is above the floor, so a new variant cannot be
    added without someone having measured it.
    """
    variant = get_variant(variant_name)
    assert variant.measured_tokens > 4096
    assert variant.measured_on is not None


def test_the_default_is_a_real_variant() -> None:
    assert DEFAULT_VARIANT in VARIANTS
    assert get_variant(None).name == DEFAULT_VARIANT


def test_an_unknown_variant_is_rejected_not_defaulted() -> None:
    """A typo in TRIAGE_PROMPT must fail loudly.

    Falling back to the default would serve a different prompt than the operator
    asked for and report the metrics under the wrong name — a silently wrong
    result, which is worse than an error.
    """
    with pytest.raises(UnknownPromptError, match="nonexistent"):
        get_variant("nonexistent")


def test_the_setting_defaults_to_the_champion() -> None:
    """`TRIAGE_PROMPT` unset means the champion, wherever the registry moves it."""
    assert Settings(llm_provider="fake").triage_prompt == DEFAULT_VARIANT


def test_each_variant_is_byte_stable_across_loads(variant_name: str) -> None:
    """The same bytes every time, or prompt caching silently stops working.

    The registry caches per name; this is the regression guard that a second load
    returns an identical string rather than re-reading and re-encoding into
    something subtly different.
    """
    assert load_playbook(variant_name) == load_playbook(variant_name)


def test_variants_are_distinct() -> None:
    """Three hypotheses, three prompts — not the same file registered thrice."""
    texts = {name: load_playbook(name) for name in VARIANTS}
    assert len(set(texts.values())) == len(texts)
