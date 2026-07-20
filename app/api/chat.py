"""`POST /v1/chat` — streaming chat with tool calling.

Router responsibilities stop at HTTP: validate input, resolve the provider,
translate normalized stream events into SSE frames, map failures to status
codes. The tool loop and prompt assembly live below, in the service layer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.api.schemas import ChatRequest
from app.providers.base import LLMProvider
from app.providers.registry import get_provider

router = APIRouter(prefix="/v1", tags=["chat"])

ProviderDep = Annotated[LLMProvider, Depends(get_provider)]


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
    """
    raise NotImplementedError("Phase 1")
