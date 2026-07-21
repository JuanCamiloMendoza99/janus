"""The tools themselves and `dispatch()`.

The contract under test is narrow and absolute: **`dispatch()` never raises for
a tool-level failure**. Every way a tool call can go wrong has to come back as a
`ToolResult` the model can read, because an exception here leaves the model's
tool call unpaired and both vendors reject the follow-up request outright — a
500 instead of a degraded answer.
"""

from __future__ import annotations

import json

import pytest

from app.tools import escalate_ticket as escalate_ticket_tool
from app.tools import registry as tool_registry
from app.tools import search_kb as search_kb_tool
from app.tools.registry import TOOL_SPECS, dispatch, get_tool_specs


@pytest.fixture(autouse=True)
def _isolate_escalation_log() -> None:
    """The escalation log is a process-wide singleton, like the usage store."""
    escalate_ticket_tool.escalation_log.reset()


# -- registry ---------------------------------------------------------------


def test_tool_specs_are_ordered_and_stable() -> None:
    """Tool definitions render ahead of the system block; order is cache state.

    Adding or reordering a tool invalidates the cached prefix for everything
    after it, so the order is part of the contract rather than an accident of
    iteration (ADR-003).
    """
    assert [spec.name for spec in get_tool_specs()] == ["search_kb", "escalate_ticket"]
    assert isinstance(TOOL_SPECS, tuple)


def test_specs_publish_the_schema_that_validates() -> None:
    """The published schema is derived from the model, so it cannot drift."""
    schema = search_kb_tool.SPEC.parameters

    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["limit"]["maximum"] == 10
    # Pydantic's `title` noise is stripped: it costs tokens in the cached prefix
    # and tells the model nothing its own field names do not.
    assert "title" not in schema
    assert all("title" not in prop for prop in schema["properties"].values())


# -- dispatch: the three failure paths ---------------------------------------


async def test_dispatch_reports_an_unknown_tool_without_raising() -> None:
    result = await dispatch("delete_production", {}, call_id="call-1")

    assert result.is_error is True
    assert result.call_id == "call-1"
    assert "search_kb" in result.content  # the model is told what does exist


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("search_kb", {}, "query"),
        ("search_kb", {"query": "refund", "limit": 99}, "limit"),
        ("search_kb", {"query": "refund", "colour": "blue"}, "colour"),
        ("escalate_ticket", {"ticket_id": "T-1", "reason": "x", "severity": "medium"}, "severity"),
    ],
)
async def test_dispatch_rejects_arguments_that_do_not_fit_the_schema(
    tool: str, arguments: dict, expected: str
) -> None:
    """The model does send arguments that do not fit. It must be told which one."""
    result = await dispatch(tool, arguments, call_id="call-1")

    assert result.is_error is True
    assert expected in result.content


async def test_dispatch_turns_a_raising_handler_into_an_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**_: object) -> dict:
        raise RuntimeError("knowledge base is on fire")

    monkeypatch.setitem(tool_registry._TOOLS, "search_kb", (boom, search_kb_tool.SearchKbArgs))

    result = await dispatch("search_kb", {"query": "refund"}, call_id="call-1")

    assert result.is_error is True
    assert "RuntimeError" in result.content
    assert "on fire" in result.content


async def test_dispatch_serializes_a_successful_result_as_json() -> None:
    result = await dispatch("search_kb", {"query": "duplicate charge"}, call_id="call-1")

    assert result.is_error is False
    assert json.loads(result.content)["articles"]


# -- search_kb --------------------------------------------------------------


async def test_search_kb_ranks_the_relevant_article_first() -> None:
    result = await search_kb_tool.search_kb("I was charged twice for one order")

    assert result["articles"][0]["id"] == "kb-101"


async def test_search_kb_is_deterministic() -> None:
    """Phase 4's evaluation set is worthless against an unstable ranking."""
    first = await search_kb_tool.search_kb("mfa recovery codes", limit=5)
    second = await search_kb_tool.search_kb("mfa recovery codes", limit=5)

    assert first == second


async def test_search_kb_respects_the_limit() -> None:
    result = await search_kb_tool.search_kb("billing refund payment plan", limit=2)

    assert len(result["articles"]) == 2


async def test_search_kb_says_so_when_nothing_matches() -> None:
    """An empty list reads to a model as permission to answer from memory."""
    result = await search_kb_tool.search_kb("xyzzy quantum unicorn")

    assert result["articles"] == []
    assert "inventing" in result["note"]


# -- escalate_ticket --------------------------------------------------------


async def test_escalate_ticket_records_and_confirms() -> None:
    result = await escalate_ticket_tool.escalate_ticket(
        ticket_id="T-4471", reason="charged twice", severity="critical"
    )

    assert result["escalation_id"] == "ESC-0001"
    assert result["already_escalated"] is False
    assert escalate_ticket_tool.escalation_log.entries[0].ticket_id == "T-4471"


async def test_escalating_the_same_ticket_twice_does_not_page_twice() -> None:
    """There is a pager on the other end of this tool in the fiction.

    A model that repeats itself — after a stream hiccup, or just because — must
    not summon a second human. The repeat returns the original confirmation.
    """
    first = await escalate_ticket_tool.escalate_ticket("T-4471", "charged twice", "critical")
    second = await escalate_ticket_tool.escalate_ticket("T-4471", "still charged twice", "high")

    assert second["escalation_id"] == first["escalation_id"]
    assert second["already_escalated"] is True
    assert len(escalate_ticket_tool.escalation_log.entries) == 1


async def test_different_tickets_get_different_escalations() -> None:
    await escalate_ticket_tool.escalate_ticket("T-1", "data exposed", "critical")
    await escalate_ticket_tool.escalate_ticket("T-2", "blocked customer", "high")

    assert [e.escalation_id for e in escalate_ticket_tool.escalation_log.entries] == [
        "ESC-0001",
        "ESC-0002",
    ]
