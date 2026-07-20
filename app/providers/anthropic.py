"""Anthropic adapter.

Implementation notes for Phase 1, recorded now so they are not rediscovered the
hard way:

* **Caching is explicit.** `Prompt.cacheable_prefix` is sent as a `system` block
  carrying `cache_control={"type": "ephemeral"}`. Rendering order is
  `tools` -> `system` -> `messages`, so a marker on the last system block covers
  the tool definitions too. Below the per-model floor (~4096 tokens on Opus 4.8)
  the API accepts the marker and caches nothing — no error, no warning. Verify
  with `usage.cache_read_input_tokens` on a *second* request. See ADR-003.

* **Usage maps directly.** `input_tokens` -> `Usage.input_tokens`,
  `cache_read_input_tokens` -> `cache_read_tokens`,
  `cache_creation_input_tokens` -> `cache_write_tokens`.

* **Streamed usage arrives late**, in the trailing `message_delta` event. The
  adapter must emit `UsageReport` only after it has that event.

* **Structured output** uses `client.messages.parse()` with a Pydantic model,
  which constrains decoding rather than parsing-and-praying.

* **Tool arguments stream as partial JSON.** Reassemble before emitting
  `ToolCallRequested`; never forward fragments upward.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from pydantic import BaseModel

from app.providers.base import (
    Completion,
    ParsedCompletion,
    Prompt,
    StreamEvent,
    ToolSpec,
)


class AnthropicProvider:
    """Adapts the Anthropic Messages API to the `LLMProvider` protocol."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_output_tokens: int) -> None:
        self.model = model
        self._api_key = api_key
        self._max_output_tokens = max_output_tokens

    async def stream(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("Phase 1")

    async def complete(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        raise NotImplementedError("Phase 1")

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        raise NotImplementedError("Phase 3")
