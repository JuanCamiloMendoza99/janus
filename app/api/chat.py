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

from app.api.schemas import ChatRequest
from app.core.pricing import compute_cost_usd
from app.providers.base import (
    Done,
    LLMProvider,
    Message,
    Prompt,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCallRequested,
    UsageReport,
)
from app.providers.registry import get_provider

router = APIRouter(prefix="/v1", tags=["chat"])

ProviderDep = Annotated[LLMProvider, Depends(get_provider)]


def _frame(event: StreamEvent) -> dict[str, str]:
    """Translate one normalized stream event into an SSE frame."""
    match event:
        case TextDelta():
            return {"event": "delta", "data": json.dumps({"text": event.text})}
        case ToolCallRequested():
            # Dormant in Phase 1 — no tools are passed, so this never fires until
            # the Phase 2 tool loop lands. Mapped now so the wire format is fixed.
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
            return {
                "event": "usage",
                "data": json.dumps(
                    {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_tokens": usage.cache_read_tokens,
                        "cost_usd": cost,
                    }
                ),
            }
        case Done():
            return {
                "event": "done",
                "data": json.dumps({"stop_reason": event.stop_reason, "error": event.error}),
            }


def _http_status(exc: ProviderError) -> int:
    """Map a pre-stream provider failure to an HTTP status.

    Rate limits stay 429, malformed requests 400, everything else is an upstream
    failure (502). Once the SSE body has started this no longer applies — the
    status line is already sent, so mid-stream errors become a terminal frame.
    """
    if exc.status_code == 429:
        return 429
    if exc.status_code == 400:
        return 400
    return 502


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
async def chat(request: ChatRequest, provider: ProviderDep) -> EventSourceResponse:
    """Stream an assistant response as server-sent events.

    Wire format — one SSE event type per normalized stream event, so a client
    can render tokens without knowing which vendor produced them:

        event: delta      data: {"text": "..."}
        event: tool_call  data: {"name": "...", "arguments": {...}}
        event: usage      data: {"input_tokens": N, "output_tokens": N, "cost_usd": N}
        event: done       data: {"stop_reason": "end_turn"}

    The `usage` event is emitted near the end because that is when providers
    report it, and the cost ledger is flushed only after this generator is fully
    consumed — see ADR-004.

    Tool calling arrives in Phase 2; `use_tools` is accepted but inert for now,
    so no tools are passed and the stream is `delta* -> usage -> done`.
    """
    prompt = Prompt(
        cacheable_prefix=None,
        system=None,
        messages=[Message(role=m.role, content=m.content) for m in request.messages],
    )

    # Prime the stream so a failure that happens *before* any frame is sent
    # (auth, a malformed request) becomes an HTTP status rather than a 200 with
    # an error buried in the body. Once the first frame is out, the status line
    # is committed and later failures are surfaced as a terminal `done` frame.
    stream = provider.stream(prompt)
    try:
        first = await anext(stream)
    except ProviderError as exc:
        raise HTTPException(status_code=_http_status(exc), detail=str(exc)) from exc
    except StopAsyncIteration:
        return EventSourceResponse(_no_frames())

    return EventSourceResponse(_sse_frames(stream, first))
