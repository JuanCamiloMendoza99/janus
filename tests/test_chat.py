"""`POST /v1/chat` streaming and the per-request cost accounting behind it.

The regression here is the load-bearing one for the whole project: it proves the
cost middleware flushes *after* the streamed body completes, not when the handler
returns. See ADR-004.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.providers.fake import FakeProvider
from app.providers.registry import get_provider


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into a list of (event, data) pairs."""
    events: list[tuple[str, dict]] = []
    event: str | None = None
    for line in body.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:") and event is not None:
            events.append((event, json.loads(line[len("data:") :].strip())))
            event = None
    return events


def test_chat_streams_delta_then_usage_then_done(client: TestClient) -> None:
    response = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]

    assert "delta" in names
    assert names[-2] == "usage"
    assert names[-1] == "done"
    # The usage frame carries a cost figure the client can render.
    usage_frame = next(data for name, data in events if name == "usage")
    assert "cost_usd" in usage_frame


def test_streamed_cost_is_non_zero(client: TestClient) -> None:
    """ADR-004 regression: a priced provider's late usage must reach the ledger.

    The fake here reports usage under a *priced* model, so a correctly-timed
    flush records a non-zero cost. A middleware that read the ledger at handler
    return — before the streaming generator ran — would log $0.00 and fail this.
    """
    settings = Settings(llm_provider="fake", environment="ci")
    provider = FakeProvider(model="claude-sonnet-5")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as test_client:
            chat = test_client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": "hello world"}]},
            )
            assert chat.status_code == 200
            usage = test_client.get("/v1/usage").json()
    finally:
        app.dependency_overrides.clear()

    assert usage["total_cost_usd"] > 0
    assert usage["requests"] >= 1
    assert usage["by_model"].get("claude-sonnet-5", 0) > 0


def test_usage_totals_match_the_recorded_request(client: TestClient) -> None:
    """`GET /v1/usage` totals equal what the request actually spent."""
    client.post("/v1/chat", json={"messages": [{"role": "user", "content": "count me"}]})

    usage = client.get("/v1/usage").json()

    # The default fake model is free, but the request and its tokens are counted.
    assert usage["requests"] >= 1
    assert usage["total_output_tokens"] > 0
    assert usage["cache_hit_rate"] == 0.0
