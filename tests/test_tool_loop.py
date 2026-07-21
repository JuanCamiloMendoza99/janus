"""The tool loop.

Everything here runs on `FakeProvider` with a scripted set of tool calls, so the
loop's control flow — parallel calls, error recovery, termination, accounting —
is tested without credentials and without spend. What the fake cannot tell you is
whether a real model *chooses* to call the right tool; that is the live
acceptance run, and no amount of fake testing substitutes for it.
"""

from __future__ import annotations

import json

import pytest

from app.observability.ledger import UsageLedger, new_ledger
from app.providers.base import (
    Done,
    Message,
    Prompt,
    TextDelta,
    ToolCall,
    ToolCallRequested,
    UsageReport,
)
from app.providers.fake import FakeProvider
from app.services.tool_loop import run_tool_loop
from app.tools import escalate_ticket as escalate_ticket_tool
from app.tools.registry import get_tool_specs


@pytest.fixture(autouse=True)
def _isolate_escalation_log() -> None:
    escalate_ticket_tool.escalation_log.reset()


@pytest.fixture
def prompt() -> Prompt:
    return Prompt(
        cacheable_prefix="stable playbook text",
        system=None,
        messages=[Message(role="user", content="I was charged twice, order 4471")],
    )


def _search_call(call_id: str = "call-1", query: str = "charged twice") -> ToolCall:
    return ToolCall(id=call_id, name="search_kb", arguments={"query": query})


def _escalate_call(call_id: str = "call-2") -> ToolCall:
    return ToolCall(
        id=call_id,
        name="escalate_ticket",
        arguments={"ticket_id": "T-4471", "reason": "charged twice", "severity": "critical"},
    )


async def _collect(provider: FakeProvider, prompt: Prompt, max_iterations: int = 5) -> list:
    return [
        event
        async for event in run_tool_loop(
            provider=provider,
            prompt=prompt,
            tools=get_tool_specs(),
            max_iterations=max_iterations,
        )
    ]


# -- the happy path ---------------------------------------------------------


async def test_loop_calls_a_tool_then_answers(prompt: Prompt) -> None:
    provider = FakeProvider(tool_script=[[_search_call()]])

    events = await _collect(provider, prompt)

    tool_calls = [e for e in events if isinstance(e, ToolCallRequested)]
    assert [call.call.name for call in tool_calls] == ["search_kb"]
    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason == "end_turn"
    # Two model calls, so two usage reports: the tool turn and the answer.
    assert sum(isinstance(e, UsageReport) for e in events) == 2


async def test_the_tool_result_reaches_the_next_turn(prompt: Prompt) -> None:
    """A loop that runs tools but never shows the model their output is a no-op."""
    provider = FakeProvider(tool_script=[[_search_call()]])

    events = await _collect(provider, prompt)

    answer = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "kb-101" in answer


async def test_disabling_tools_makes_it_a_single_pass_through(prompt: Prompt) -> None:
    """`use_tools=False` shares the code path; the model just cannot ask."""
    provider = FakeProvider()

    events = [
        event
        async for event in run_tool_loop(
            provider=provider, prompt=prompt, tools=(), max_iterations=5
        )
    ]

    assert not any(isinstance(e, ToolCallRequested) for e in events)
    assert sum(isinstance(e, UsageReport) for e in events) == 1
    assert isinstance(events[-1], Done)


# -- parallel calls ---------------------------------------------------------


async def test_parallel_calls_come_back_in_one_turn(prompt: Prompt) -> None:
    """Both tools in one turn, both results in **one** message.

    Splitting the results across two messages would tell the model its parallel
    calls were answered one at a time, and it would stop making them (ADR-007).
    """
    provider = FakeProvider(tool_script=[[_search_call(), _escalate_call()]])

    events = await _collect(provider, prompt)

    called = [e.call.name for e in events if isinstance(e, ToolCallRequested)]
    assert called == ["search_kb", "escalate_ticket"]
    assert isinstance(events[-1], Done)
    # The write tool really ran, exactly once.
    assert len(escalate_ticket_tool.escalation_log.entries) == 1

    answer = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "kb-101" in answer
    assert "ESC-0001" in answer


async def test_both_results_share_a_single_tool_message(prompt: Prompt) -> None:
    """Asserted on the prompt the provider actually received, not on the output."""
    seen: list[Prompt] = []
    provider = FakeProvider(tool_script=[[_search_call(), _escalate_call()]])
    original_stream = provider.stream

    def recording_stream(turn_prompt: Prompt, tools=()):  # type: ignore[no-untyped-def]
        seen.append(turn_prompt)
        return original_stream(turn_prompt, tools)

    provider.stream = recording_stream  # type: ignore[method-assign]

    await _collect(provider, prompt)

    final_turn = seen[-1].messages
    tool_messages = [m for m in final_turn if m.role == "tool"]
    assert len(tool_messages) == 1
    assert len(tool_messages[0].tool_results) == 2
    # And the assistant turn that made the calls is present — a result whose
    # call is missing from the history is rejected by both vendors.
    assistant = [m for m in final_turn if m.role == "assistant" and m.tool_calls]
    assert len(assistant[0].tool_calls) == 2


# -- failure recovery -------------------------------------------------------


async def test_a_failing_tool_does_not_break_the_request(prompt: Prompt) -> None:
    """The model sends bad arguments; the turn recovers instead of 500-ing."""
    bad = ToolCall(id="call-1", name="search_kb", arguments={"limit": 99})
    provider = FakeProvider(tool_script=[[bad]])

    events = await _collect(provider, prompt)

    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason == "end_turn"
    answer = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "tool!" in answer  # the fake marks an errored result
    assert "query" in answer  # and the model is told which argument was wrong


async def test_an_unknown_tool_is_answered_not_raised(prompt: Prompt) -> None:
    provider = FakeProvider(tool_script=[[ToolCall(id="call-1", name="rm_rf", arguments={})]])

    events = await _collect(provider, prompt)

    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason == "end_turn"


# -- the cap ----------------------------------------------------------------


async def test_a_model_that_never_stops_asking_is_stopped(prompt: Prompt) -> None:
    """Every iteration is a paid model call, so the cap is a budget control."""
    provider = FakeProvider(tool_script=[[_search_call(f"call-{i}")] for i in range(10)])

    events = await _collect(provider, prompt, max_iterations=3)

    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason == "error"
    assert "3 iterations" in (events[-1].error or "")
    # Exactly the cap: three model calls, not four, not ten.
    assert sum(isinstance(e, UsageReport) for e in events) == 3


# -- accounting -------------------------------------------------------------


async def test_the_ledger_records_every_call_in_the_loop(prompt: Prompt) -> None:
    """A tool-using request costs several model calls and must be billed for all.

    Logging only the last call is the plausible-looking bug: the number is
    well-formatted, it is just too small, and nothing looks broken.
    """
    ledger: UsageLedger = new_ledger()
    provider = FakeProvider(
        model="claude-sonnet-5",
        tool_script=[[_search_call("call-1")], [_escalate_call("call-2")]],
    )

    await _collect(provider, prompt)

    assert ledger.call_count == 3  # two tool turns plus the final answer
    assert ledger.total_cost_usd == pytest.approx(sum(e.cost_usd for e in ledger.entries))
    assert ledger.total_cost_usd > 0
    assert ledger.summary()["calls"] == 3


async def test_tool_results_are_json_the_model_can_parse(prompt: Prompt) -> None:
    """Handlers return dicts; the seam carries strings. The bridge is JSON."""
    seen: list[Prompt] = []
    provider = FakeProvider(tool_script=[[_search_call()]])
    original_stream = provider.stream

    def recording_stream(turn_prompt: Prompt, tools=()):  # type: ignore[no-untyped-def]
        seen.append(turn_prompt)
        return original_stream(turn_prompt, tools)

    provider.stream = recording_stream  # type: ignore[method-assign]

    await _collect(provider, prompt)

    result = [m for m in seen[-1].messages if m.role == "tool"][0].tool_results[0]
    assert result.call_id == "call-1"
    assert json.loads(result.content)["articles"][0]["id"] == "kb-101"
