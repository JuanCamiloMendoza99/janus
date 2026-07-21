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
from typing import Any

import anthropic
from pydantic import BaseModel

from app.observability.ledger import current_ledger
from app.providers.base import (
    Completion,
    Done,
    ParsedCompletion,
    Prompt,
    ProviderError,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolSpec,
    Usage,
    UsageReport,
)

# Anthropic's wire `stop_reason` -> the domain's smaller closed set. `stop_sequence`
# and `pause_turn` both mean "the turn ended for a benign reason we don't model
# separately", so they fold into `end_turn`.
_STOP_REASONS: dict[str | None, StopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "tool_use": "tool_use",
    "refusal": "refusal",
    "stop_sequence": "end_turn",
    "pause_turn": "end_turn",
}


class AnthropicProvider:
    """Adapts the Anthropic Messages API to the `LLMProvider` protocol."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        max_output_tokens: int,
        prompt_caching_enabled: bool = True,
    ) -> None:
        self.model = model
        self._max_output_tokens = max_output_tokens
        self._prompt_caching_enabled = prompt_caching_enabled
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    # -- request assembly -------------------------------------------------

    def _system_blocks(self, prompt: Prompt) -> list[dict[str, Any]]:
        """Render the cacheable prefix then the volatile system text as blocks.

        The `cache_control` marker goes on the stable prefix only — never on the
        volatile `system` text, which changes per request and would poison the
        prefix if cached (ADR-003).
        """
        blocks: list[dict[str, Any]] = []
        if prompt.cacheable_prefix:
            block: dict[str, Any] = {"type": "text", "text": prompt.cacheable_prefix}
            if self._prompt_caching_enabled:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        if prompt.system:
            blocks.append({"type": "text", "text": prompt.system})
        return blocks

    def _request_kwargs(self, prompt: Prompt) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_output_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in prompt.messages],
        }
        system = self._system_blocks(prompt)
        if system:
            kwargs["system"] = system
        return kwargs

    # -- response mapping -------------------------------------------------

    def _usage_from(self, usage: anthropic.types.Usage) -> Usage:
        return Usage(
            model=self.model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
        )

    @staticmethod
    def _stop_reason(reason: str | None) -> StopReason:
        return _STOP_REASONS.get(reason, "end_turn")

    def _translate(self, exc: anthropic.APIError) -> ProviderError:
        """Map an SDK exception to a `ProviderError` with `retryable` set right.

        Rate limits, transport failures and 5xx (including 529 overloads) are
        worth retrying; auth failures and malformed requests are not.
        """
        status = getattr(exc, "status_code", None)
        retryable = isinstance(
            exc,
            (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
            ),
        ) or (status is not None and status >= 500)
        return ProviderError(
            message=str(exc),
            provider=self.name,
            retryable=retryable,
            status_code=status,
        )

    def _record(self, usage: Usage) -> None:
        ledger = current_ledger()
        if ledger is not None:
            ledger.record(self.name, usage)

    # -- protocol ---------------------------------------------------------

    async def stream(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[StreamEvent]:
        try:
            async with self._client.messages.stream(**self._request_kwargs(prompt)) as stream:
                async for text in stream.text_stream:
                    yield TextDelta(text=text)
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc

        usage = self._usage_from(final.usage)
        self._record(usage)
        yield UsageReport(usage=usage)
        yield Done(stop_reason=self._stop_reason(final.stop_reason))

    async def complete(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        try:
            message = await self._client.messages.create(**self._request_kwargs(prompt))
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc

        text = "".join(block.text for block in message.content if block.type == "text")
        usage = self._usage_from(message.usage)
        self._record(usage)
        return Completion(
            text=text,
            tool_calls=(),
            usage=usage,
            stop_reason=self._stop_reason(message.stop_reason),
        )

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        raise NotImplementedError("Phase 3")
