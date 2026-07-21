"""Vendor translation, tested without a vendor.

Two things in the adapters are worth testing offline, because both fail
*silently* and neither is visible from the domain side:

1. **Request rendering.** One turn of tool results is one message to Anthropic
   and N messages to OpenAI. The fan-out is the whole of ADR-007 and it is pure
   function — no reason to spend money to find out it is wrong.
2. **Tool argument reassembly.** Both vendors stream arguments as fragments that
   are not valid JSON on their own. Assembling them wrongly produces empty or
   truncated arguments, which look like a confused *model* rather than a broken
   adapter.

The SDK clients are replaced with stubs that emit the wire shapes the vendors
document. That is the honest limit of this file: it proves the adapter handles
the shape it was told to expect, not that the shape is right. Only the live
acceptance run proves that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.providers.anthropic import AnthropicProvider
from app.providers.base import (
    Done,
    Message,
    Prompt,
    TextDelta,
    ToolCall,
    ToolCallRequested,
    ToolResult,
)
from app.providers.openai import OpenAIProvider
from app.tools.registry import get_tool_specs


@pytest.fixture
def anthropic_provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="sk-test", model="claude-sonnet-5", max_output_tokens=1024)


@pytest.fixture
def openai_provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="sk-test", model="gpt-5.6-terra", max_output_tokens=1024)


@pytest.fixture
def tool_history() -> list[Message]:
    """A conversation mid-loop: the model called two tools and both answered."""
    return [
        Message(role="user", content="I was charged twice"),
        Message(
            role="assistant",
            content="Let me check.",
            tool_calls=(
                ToolCall(id="call-1", name="search_kb", arguments={"query": "double charge"}),
                ToolCall(id="call-2", name="escalate_ticket", arguments={"ticket_id": "T-1"}),
            ),
        ),
        Message(
            role="tool",
            tool_results=(
                ToolResult(call_id="call-1", content='{"articles": []}'),
                ToolResult(call_id="call-2", content="tool exploded", is_error=True),
            ),
        ),
    ]


# -- request rendering ------------------------------------------------------


def test_anthropic_keeps_every_result_in_one_user_message(
    anthropic_provider: AnthropicProvider, tool_history: list[Message]
) -> None:
    """Anthropic has no tool role: results are user content, one message."""
    prompt = Prompt(cacheable_prefix=None, system=None, messages=tool_history)

    messages = anthropic_provider._request_kwargs(prompt)["messages"]

    assistant = messages[1]
    assert [block["type"] for block in assistant["content"]] == ["text", "tool_use", "tool_use"]
    assert assistant["content"][1]["input"] == {"query": "double charge"}

    results = messages[2]
    assert results["role"] == "user"
    assert len(results["content"]) == 2
    assert results["content"][0]["tool_use_id"] == "call-1"
    assert results["content"][1]["is_error"] is True


def test_anthropic_omits_an_empty_text_block(anthropic_provider: AnthropicProvider) -> None:
    """The API rejects an empty text block, and a tool-only turn has no text."""
    prompt = Prompt(
        cacheable_prefix=None,
        system=None,
        messages=[
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                tool_calls=(ToolCall(id="c1", name="search_kb", arguments={"query": "x"}),),
            ),
        ],
    )

    blocks = anthropic_provider._request_kwargs(prompt)["messages"][1]["content"]

    assert [block["type"] for block in blocks] == ["tool_use"]


def test_openai_fans_the_results_out_into_one_message_each(
    openai_provider: OpenAIProvider, tool_history: list[Message]
) -> None:
    """The asymmetry that ADR-007 confines to this adapter."""
    prompt = Prompt(cacheable_prefix=None, system=None, messages=tool_history)

    messages = openai_provider._request_kwargs(prompt)["messages"]

    assistant = messages[1]
    assert [call["id"] for call in assistant["tool_calls"]] == ["call-1", "call-2"]
    # Arguments go back over the wire as a JSON *string*, not an object.
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"query": "double charge"}'

    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call-1", "call-2"]
    # No `is_error` field exists here, so the failure has to be in the text.
    assert tool_messages[1]["content"].startswith("ERROR: ")


def test_tool_specs_are_rewrapped_per_vendor(
    anthropic_provider: AnthropicProvider, openai_provider: OpenAIProvider
) -> None:
    """Same JSON Schema, two envelopes. Nothing above the seam sees either."""
    specs = get_tool_specs()
    prompt = Prompt(
        cacheable_prefix=None, system=None, messages=[Message(role="user", content="x")]
    )

    anthropic_tools = anthropic_provider._request_kwargs(prompt, specs)["tools"]
    openai_tools = openai_provider._request_kwargs(prompt, specs)["tools"]

    assert anthropic_tools[0]["input_schema"] == specs[0].parameters
    assert openai_tools[0]["function"]["parameters"] == specs[0].parameters
    # Order is preserved, because tool definitions sit inside the cached prefix.
    assert [t["name"] for t in anthropic_tools] == [s.name for s in specs]


def test_no_tools_key_is_sent_when_there_are_no_tools(
    anthropic_provider: AnthropicProvider, openai_provider: OpenAIProvider
) -> None:
    prompt = Prompt(
        cacheable_prefix=None, system=None, messages=[Message(role="user", content="x")]
    )

    assert "tools" not in anthropic_provider._request_kwargs(prompt)
    assert "tools" not in openai_provider._request_kwargs(prompt)


# -- streamed tool call reassembly ------------------------------------------


def _anthropic_stub(events: list[Any]) -> Any:
    """A stand-in for `client.messages.stream(...)`."""

    class Stream:
        async def __aenter__(self) -> Stream:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def __aiter__(self) -> AsyncIterator[Any]:
            for event in events:
                yield event

        async def get_final_message(self) -> Any:
            return SimpleNamespace(
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

    return SimpleNamespace(messages=SimpleNamespace(stream=lambda **_: Stream()))


async def test_anthropic_reassembles_partial_json_before_announcing_a_call(
    anthropic_provider: AnthropicProvider,
) -> None:
    """The fragments are not valid JSON individually; only the whole is."""
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="Checking. "),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="tool_use", id="call-1", name="search_kb"),
        ),
        # Split mid-key and mid-value, the way the API really splits them.
        *[
            SimpleNamespace(
                type="content_block_delta",
                index=1,
                delta=SimpleNamespace(type="input_json_delta", partial_json=fragment),
            )
            for fragment in ('{"que', 'ry": "dou', 'ble charge"', "}")
        ],
        SimpleNamespace(type="content_block_stop", index=1),
    ]
    anthropic_provider._client = _anthropic_stub(events)

    emitted = [
        event
        async for event in anthropic_provider.stream(
            Prompt(
                cacheable_prefix=None, system=None, messages=[Message(role="user", content="hi")]
            ),
            get_tool_specs(),
        )
    ]

    calls = [e.call for e in emitted if isinstance(e, ToolCallRequested)]
    assert calls == [ToolCall(id="call-1", name="search_kb", arguments={"query": "double charge"})]
    # Text came through once — not once per raw event and once per synthesized one.
    assert "".join(e.text for e in emitted if isinstance(e, TextDelta)) == "Checking. "
    assert isinstance(emitted[-1], Done)
    assert emitted[-1].stop_reason == "tool_use"


def _openai_stub(chunks: list[Any]) -> Any:
    """A stand-in for `client.chat.completions.create(stream=True, ...)`."""

    async def create(**_: object) -> AsyncIterator[Any]:
        async def iterator() -> AsyncIterator[Any]:
            for chunk in chunks:
                yield chunk

        return iterator()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _openai_chunk(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
) -> Any:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
    )


def _openai_fragment(index: int, call_id: str | None, name: str | None, arguments: str) -> Any:
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


async def test_openai_assembles_parallel_calls_by_index(
    openai_provider: OpenAIProvider,
) -> None:
    """Two calls interleaved across chunks, identified only by `index`.

    OpenAI sends no per-call terminator, so the adapter cannot emit anything
    until the turn finishes — and it must not mix the two calls' fragments up.
    """
    chunks = [
        _openai_chunk(content="Checking. "),
        _openai_chunk(tool_calls=[_openai_fragment(0, "call-1", "search_kb", '{"query"')]),
        _openai_chunk(
            tool_calls=[_openai_fragment(1, "call-2", "escalate_ticket", '{"ticket_id"')]
        ),
        _openai_chunk(tool_calls=[_openai_fragment(0, None, None, ': "refund"}')]),
        _openai_chunk(tool_calls=[_openai_fragment(1, None, None, ': "T-1"}')]),
        _openai_chunk(finish_reason="tool_calls"),
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                prompt_tokens_details=None,
            ),
            choices=[],
        ),
    ]
    openai_provider._client = _openai_stub(chunks)

    emitted = [
        event
        async for event in openai_provider.stream(
            Prompt(
                cacheable_prefix=None, system=None, messages=[Message(role="user", content="hi")]
            ),
            get_tool_specs(),
        )
    ]

    calls = [e.call for e in emitted if isinstance(e, ToolCallRequested)]
    assert calls == [
        ToolCall(id="call-1", name="search_kb", arguments={"query": "refund"}),
        ToolCall(id="call-2", name="escalate_ticket", arguments={"ticket_id": "T-1"}),
    ]
    assert emitted[-1].stop_reason == "tool_use"


async def test_unparseable_arguments_still_reach_the_loop(
    openai_provider: OpenAIProvider,
) -> None:
    """A dropped call is fatal; a call with bad arguments is recoverable.

    Dropping it leaves the model's `tool_calls` entry unanswered and the vendor
    rejects the next request outright. Passing it on with empty arguments lets
    `dispatch()` return a validation error the model can read and correct.
    """
    chunks = [
        _openai_chunk(tool_calls=[_openai_fragment(0, "call-1", "search_kb", "{not json")]),
        _openai_chunk(finish_reason="tool_calls"),
    ]
    openai_provider._client = _openai_stub(chunks)

    emitted = [
        event
        async for event in openai_provider.stream(
            Prompt(
                cacheable_prefix=None, system=None, messages=[Message(role="user", content="hi")]
            ),
            get_tool_specs(),
        )
    ]

    calls = [e.call for e in emitted if isinstance(e, ToolCallRequested)]
    assert calls == [ToolCall(id="call-1", name="search_kb", arguments={})]
