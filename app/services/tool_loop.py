"""The tool loop.

Call the model; if it asked for tools, run them, hand every result back, and ask
again. Repeat until it answers or the cap is reached. That is the whole of
"agentic" behaviour, and writing it out is the point of this project — a
framework would supply this loop and with it the four decisions below, none of
which are obvious and all of which are load-bearing.

**It streams every iteration.** The loop drives `provider.stream()`, not
`complete()`, so `tool_call` frames reach the client while the tools are running
and the final answer still arrives token by token. Tools are on by default on
`/v1/chat`; a loop built on `complete()` would quietly turn the flagship
streaming endpoint into a blocking one.

**All results go back in a single turn.** The model asked for three tools at
once; answering in three separate messages tells it the calls were serialized,
and it stops making parallel calls. One `tool` message, every result (ADR-007).

**Tool failures are data, not exceptions.** `dispatch()` never raises, so every
call the model made comes back paired with a result. An unpaired tool call is
rejected outright by both vendors on the next request — the failure mode is a
500, not a degraded answer.

**The cap is not optional.** A confused model can ask for the same tool forever,
and every iteration is a paid model call. When the cap is hit the client gets a
terminal error frame; it does not get an exception and it does not get silence.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from app.core.logging import get_logger
from app.providers.base import (
    Done,
    LLMProvider,
    Message,
    Prompt,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallRequested,
    ToolResult,
    ToolSpec,
)
from app.tools.registry import dispatch

logger = get_logger("janus.tool_loop")


async def _run_calls(calls: Sequence[ToolCall]) -> list[ToolResult]:
    """Execute every requested call concurrently, preserving request order.

    Concurrently because the model asked for them in one turn precisely so they
    could happen at once; in order because the results are rendered back into
    the conversation and a stable order keeps the cached prefix stable for the
    turns that follow (ADR-003).
    """
    return list(
        await asyncio.gather(*(dispatch(call.name, call.arguments, call.id) for call in calls))
    )


async def run_tool_loop(
    provider: LLMProvider,
    prompt: Prompt,
    tools: Sequence[ToolSpec] = (),
    max_iterations: int = 5,
) -> AsyncIterator[StreamEvent]:
    """Stream a full exchange, running tools until the model stops asking.

    Yields the same normalized events a single provider call would, so the SSE
    layer above is unchanged by the existence of the loop: text deltas pass
    through as they arrive, each tool call is announced as it is assembled, and
    every model call reports its own `UsageReport` — a tool-using request costs
    several calls and the client is shown all of them.

    Exactly one terminal `Done` is emitted, on every path.
    """
    messages = list(prompt.messages)

    for iteration in range(max_iterations):
        calls: list[ToolCall] = []
        text_parts: list[str] = []
        done: Done | None = None

        # `Prompt` is frozen, so each turn gets a fresh one carrying the history
        # accumulated so far. The cacheable prefix is passed through untouched —
        # that byte-stability is what makes the cache hit at all (ADR-003).
        turn = Prompt(
            cacheable_prefix=prompt.cacheable_prefix,
            system=prompt.system,
            messages=list(messages),
        )

        async for event in provider.stream(turn, tools):
            match event:
                case ToolCallRequested():
                    calls.append(event.call)
                    yield event
                case TextDelta():
                    text_parts.append(event.text)
                    yield event
                case Done():
                    # Not terminal if tools were requested — the turn continues
                    # after the results go back. Held rather than forwarded.
                    done = event
                case _:
                    yield event

        if not calls:
            yield done if done is not None else Done(stop_reason="end_turn")
            return

        results = await _run_calls(calls)
        logger.info(
            "tool_loop.iteration",
            extra={
                "iteration": iteration + 1,
                "tools": [call.name for call in calls],
                "errors": sum(1 for result in results if result.is_error),
            },
        )

        # The assistant turn that made the calls, then one turn carrying every
        # result. Both are required: a result whose call is missing from the
        # history is rejected by both vendors.
        messages.append(
            Message(role="assistant", content="".join(text_parts), tool_calls=tuple(calls))
        )
        messages.append(Message(role="tool", tool_results=tuple(results)))

    logger.warning("tool_loop.exhausted", extra={"max_iterations": max_iterations})
    yield Done(
        stop_reason="error",
        error=(
            f"Tool loop exceeded {max_iterations} iterations without a final answer. "
            "The request was stopped to avoid an unbounded number of model calls."
        ),
    )
