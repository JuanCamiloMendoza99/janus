"""`POST /v1/triage` — the endpoint, and the cache invariant underneath it.

`FakeProvider` cannot serve this endpoint by design (it refuses to fabricate a
verdict for a schema whose fields are all required), so these tests supply their
own double. That double returns a canned `TriageResult` and records every
`Prompt` it was handed — which is the only way to assert the thing that actually
matters here.

That thing is prompt stability. Caching is a prefix match: interpolate the
ticket id into the playbook "for context" and every request writes a fresh cache
entry instead of reading the last one. Nothing fails. No error is raised, the
verdicts stay correct, and the bill quietly multiplies. So the prefix is
asserted byte-for-byte across two different tickets, and that assertion is the
real subject of this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.domain.prompts import load_playbook
from app.domain.triage import Category, NextAction, Sentiment, Severity, TriageResult
from app.main import app
from app.providers.base import ParsedCompletion, Prompt, ProviderError, Usage
from app.providers.fake import FakeProvider
from app.providers.registry import get_provider

TICKET = {
    "ticket_id": "T-1",
    "subject": "Double charge",
    "body": "I was billed twice for order 4471.",
}

OTHER_TICKET = {
    "ticket_id": "T-2",
    "subject": "Cannot log in",
    "body": "SSO fails with 'certificate expired' for our whole team.",
}

VERDICT = TriageResult(
    category=Category.BILLING,
    severity=Severity.HIGH,
    sentiment=Sentiment.NEUTRAL,
    next_action=NextAction.ESCALATE,
    summary="Customer billed twice for order 4471.",
    contains_pii=False,
    confidence=0.92,
    reasoning="Money moved incorrectly, so the escalation rule fires.",
)


class StubProvider:
    """A provider that answers `parse()` from a script and remembers the prompt.

    Structurally typed against `LLMProvider` (ADR-002), so it needs no base
    class — only the members `/v1/triage` actually reaches for. `model` is a real
    id from the pricing table, so the cost path is exercised rather than
    special-cased into returning zero.
    """

    name = "stub"
    model = "claude-sonnet-5"

    def __init__(self, error: ProviderError | None = None) -> None:
        self.error = error
        self.prompts: list[Prompt] = []
        self.schemas: list[type[BaseModel]] = []

    async def parse[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> ParsedCompletion[T]:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.error is not None:
            raise self.error
        return ParsedCompletion(
            parsed=VERDICT,  # type: ignore[arg-type]
            usage=Usage(
                model=self.model,
                input_tokens=120,
                output_tokens=90,
                cache_read_tokens=6594,
            ),
        )


@pytest.fixture
def stub_provider() -> StubProvider:
    return StubProvider()


@contextmanager
def client_for(provider: object, settings: Settings) -> Iterator[TestClient]:
    """A `TestClient` wired to a specific provider.

    Overriding the dependency rather than mutating the module-level caches keeps
    these tests independent of import order and of whatever `.env` happens to be
    on the developer's machine.
    """
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def triage_client(stub_provider: StubProvider, settings: Settings) -> Iterator[TestClient]:
    with client_for(stub_provider, settings) as test_client:
        yield test_client


# -- the endpoint -----------------------------------------------------------


def test_triage_returns_a_validated_verdict_with_its_cost(triage_client: TestClient) -> None:
    response = triage_client.post("/v1/triage", json=TICKET)

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == "T-1"
    assert body["result"]["category"] == "billing"
    assert body["result"]["next_action"] == "escalate"
    assert body["provider"] == "stub"
    assert body["model"] == "claude-sonnet-5"
    # Priced from the call's own usage, cached reads included — not a placeholder.
    assert body["cost_usd"] > 0


def test_the_response_is_constrained_to_the_triage_schema(
    triage_client: TestClient, stub_provider: StubProvider
) -> None:
    """The provider is asked for `TriageResult` itself, not for text to parse."""
    triage_client.post("/v1/triage", json=TICKET)

    assert stub_provider.schemas == [TriageResult]


# -- the cache invariant ----------------------------------------------------


def test_the_playbook_is_the_prefix_and_the_ticket_is_not(
    triage_client: TestClient, stub_provider: StubProvider
) -> None:
    triage_client.post("/v1/triage", json=TICKET)
    prompt = stub_provider.prompts[0]

    assert prompt.cacheable_prefix == load_playbook()
    assert prompt.system is None

    # The volatile half. Every one of these in the prefix would be a cache miss
    # per request, reported by nothing.
    for value in TICKET.values():
        assert value not in (prompt.cacheable_prefix or "")

    turn = prompt.messages[0]
    assert turn.role == "user"
    assert all(value in turn.content for value in TICKET.values())


def test_two_different_tickets_share_a_byte_identical_prefix(
    triage_client: TestClient, stub_provider: StubProvider
) -> None:
    """The regression test for silent cache invalidation.

    `==` is not enough on its own to catch every way this breaks, but it is
    exactly what the vendor compares: one differing byte anywhere in the prefix
    and the second request pays full price with no warning.
    """
    triage_client.post("/v1/triage", json=TICKET)
    triage_client.post("/v1/triage", json=OTHER_TICKET)

    first, second = stub_provider.prompts
    assert first.cacheable_prefix == second.cacheable_prefix
    assert first.messages[0].content != second.messages[0].content


def test_the_playbook_clears_the_caching_floor_by_character_count() -> None:
    """A crude guard against the prefix being trimmed below the token floor.

    Characters, not tokens: a real count needs the vendor's endpoint and costs a
    network round trip, so that assertion lives in `tests/test_caching_live.py`.
    This one runs in CI and catches the obvious version of the mistake — someone
    shortening the playbook and not noticing that caching stops.

    16,000 characters is well under the measured 6,594 tokens; it is a floor, not
    an estimate.
    """
    assert len(load_playbook()) > 16_000


# -- failures ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (501, 501),  # a capability the selected provider does not have
        (429, 429),  # a rate limit the client should back off from
        (400, 400),  # a malformed request the client should not retry
        (None, 502),  # anything else is an upstream failure
        (401, 502),
    ],
)
def test_a_provider_failure_becomes_a_meaningful_status(
    settings: Settings, status_code: int | None, expected: int
) -> None:
    provider = StubProvider(
        error=ProviderError(message="nope", provider="stub", status_code=status_code)
    )
    with client_for(provider, settings) as client:
        response = client.post("/v1/triage", json=TICKET)

    assert response.status_code == expected
    assert "nope" in response.json()["detail"]


def test_the_fake_provider_refuses_to_invent_a_verdict(settings: Settings) -> None:
    """A fresh clone gets an honest 501, not a fabricated classification.

    This is the visible cost of the choice in ADR-008, and it is the right way
    round: a fake that filled `TriageResult` with plausible values would demo
    beautifully and would stop any broken schema from ever failing a test.
    """
    with client_for(FakeProvider(), settings) as client:
        response = client.post("/v1/triage", json=TICKET)

    assert response.status_code == 501
    assert "TriageResult" in response.json()["detail"]
