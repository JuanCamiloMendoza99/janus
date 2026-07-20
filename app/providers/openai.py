"""OpenAI adapter.

Implementation notes for Phase 1:

* **Caching is automatic.** There is no opt-in and no marker to set: OpenAI
  caches long prompt prefixes on its own. `Prompt.cacheable_prefix` is simply
  rendered first, which is all the adapter can do to help. The consequence for
  the domain model is in ADR-003 — caching had to be modelled as "this text is
  stable", not as a boolean flag, precisely because this vendor has no flag.

* **Usage maps less directly.** Cached tokens are reported nested, under
  `usage.prompt_tokens_details.cached_tokens`, and `prompt_tokens` is the
  *total* rather than the uncached remainder. The adapter must subtract to fill
  `Usage.input_tokens`, or the cost calculation double-counts the prefix.
  `cache_write_tokens` stays 0: there is no write premium to account for.

* **`OPENAI_MODEL` must be verified before use.** Do not carry over a model id
  from memory — check the current published list when implementing this.

* **Tool call shape differs** from Anthropic's, as does the parallel-call
  behaviour. Both normalize to `ToolCall` / `ToolCallRequested`; the asymmetry
  stops at this file.
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


class OpenAIProvider:
    """Adapts the OpenAI API to the `LLMProvider` protocol."""

    name = "openai"

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
