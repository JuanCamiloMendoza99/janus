"""`POST /v1/chat` — streaming chat with tool calling.

Router responsibilities stop at HTTP: validate input, resolve the provider,
translate normalized stream events into SSE frames, map failures to status
codes. The tool loop and prompt assembly live below, in the service layer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.api.errors import http_status_for
from app.api.schemas import ChatRequest
from app.core.config import Settings, get_settings
from app.core.pricing import compute_cost_usd
from app.providers.base import (
    Done,
    LLMProvider,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCallRequested,
    UsageReport,
)
from app.providers.registry import get_provider
from app.services.chat import stream_chat

router = APIRouter(prefix="/v1", tags=["chat"])

ProviderDep = Annotated[LLMProvider, Depends(get_provider)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _frame(event: StreamEvent) -> dict[str, str]:
    """Translate one normalized stream event into an SSE frame."""
    match event:
        case TextDelta():
            return {"event": "delta", "data": json.dumps({"text": event.text})}
        case ToolCallRequested():
            # Emitted the moment a call's arguments are complete, before the tool
            # runs, so a client can show what the assistant is doing instead of
            # freezing for the duration of a long tool turn.
            return {
                "event": "tool_call",
                "data": json.dumps(
                    {
                        "id": event.call.id,
                        "name": event.call.name,
                        "arguments": event.call.arguments,
                    }
                ),
            }
        case UsageReport():
            usage = event.usage
            cost = compute_cost_usd(
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )
            # All four token counts are reported, not just the two obvious ones,
            # so `cost_usd` is reconcilable from the frame itself. Writing the
            # cache is billed at a premium (1.25x input on Anthropic) and can
            # dominate the cost of the first call in a request: measured on a
            # real Sonnet 5 request, 84 input + 89 output tokens came to
            # $0.0095, of which $0.0079 was 2117 cache-write tokens. Omitting
            # that field leaves a client unable to explain its own bill.
            return {
                "event": "usage",
                "data": json.dumps(
                    {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_tokens": usage.cache_read_tokens,
                        "cache_write_tokens": usage.cache_write_tokens,
                        "cost_usd": cost,
                    }
                ),
            }
        case Done():
            return {
                "event": "done",
                "data": json.dumps({"stop_reason": event.stop_reason, "error": event.error}),
            }


async def _no_frames() -> AsyncIterator[dict[str, str]]:
    """An empty SSE body (a stream that produced no events)."""
    return
    yield  # pragma: no cover - marks this as an async generator


async def _sse_frames(
    stream: AsyncIterator[StreamEvent],
    first: StreamEvent,
) -> AsyncIterator[dict[str, str]]:
    """Yield the primed event, then the rest, turning a mid-stream error terminal."""
    try:
        yield _frame(first)
        async for event in stream:
            yield _frame(event)
    except ProviderError as exc:
        yield {
            "event": "done",
            "data": json.dumps({"stop_reason": "error", "error": str(exc)}),
        }


@router.post("/chat")
async def chat(
    request: ChatRequest,
    provider: ProviderDep,
    settings: SettingsDep,
) -> EventSourceResponse:
    """Stream an assistant response as server-sent events.

    Wire format — one SSE event type per normalized stream event, so a client
    can render tokens without knowing which vendor produced them:

        event: delta      data: {"text": "..."}
        event: tool_call  data: {"id": "...", "name": "...", "arguments": {...}}
        event: usage      data: {"input_tokens": N, "output_tokens": N,
                                 "cache_read_tokens": N, "cache_write_tokens": N,
                                 "cost_usd": N}
        event: done       data: {"stop_reason": "end_turn"}

    With `use_tools` (the default) the stream is a whole exchange, not a single
    call: `delta* -> tool_call+ -> usage -> delta* -> usage -> done`. Each model
    call in the tool loop reports its **own** `usage` frame, so the client can
    add them up and see what the tools actually cost. The cost ledger is flushed
    only after this generator is fully consumed — see ADR-004.
    """
    # Prime the stream so a failure that happens *before* any frame is sent
    # (auth, a malformed request) becomes an HTTP status rather than a 200 with
    # an error buried in the body. Once the first frame is out, the status line
    # is committed and later failures are surfaced as a terminal `done` frame.
    stream = stream_chat(
        provider=provider,
        messages=request.messages,
        use_tools=request.use_tools,
        max_iterations=settings.tool_loop_max_iterations,
    )
    try:
        first = await anext(stream)
    except ProviderError as exc:
        raise HTTPException(status_code=http_status_for(exc), detail=str(exc)) from exc
    except StopAsyncIteration:
        return EventSourceResponse(_no_frames())

    return EventSourceResponse(_sse_frames(stream, first))
