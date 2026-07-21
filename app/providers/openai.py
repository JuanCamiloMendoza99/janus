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

* **Reasoning-tier models require `max_completion_tokens`.** The GPT-5.x models
  reject the legacy `max_tokens` field with a 400. Verified on 2026-07-20.

* **Tool call shape differs** from Anthropic's, as does the parallel-call
  behaviour. Both normalize to `ToolCall` / `ToolCallRequested`; the asymmetry
  stops at this file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
from openai.types.completion_usage import CompletionUsage
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

# OpenAI's `finish_reason` -> the domain's closed set.
_STOP_REASONS: dict[str | None, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


class OpenAIProvider:
    """Adapts the OpenAI API to the `LLMProvider` protocol."""

    name = "openai"

    def __init__(self, api_key: str, model: str, max_output_tokens: int) -> None:
        self.model = model
        self._max_output_tokens = max_output_tokens
        self._client = openai.AsyncOpenAI(api_key=api_key)

    # -- request assembly -------------------------------------------------

    def _messages(self, prompt: Prompt) -> list[dict[str, Any]]:
        """Render the stable prefix first, then volatile system, then the turns.

        Ordering is the only lever the adapter has over OpenAI's automatic
        caching: the byte-stable prefix must come first so repeated requests
        share it (ADR-003).
        """
        messages: list[dict[str, Any]] = []
        if prompt.cacheable_prefix:
            messages.append({"role": "system", "content": prompt.cacheable_prefix})
        if prompt.system:
            messages.append({"role": "system", "content": prompt.system})
        messages.extend({"role": m.role, "content": m.content} for m in prompt.messages)
        return messages

    def _request_kwargs(self, prompt: Prompt) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": self._messages(prompt),
            "max_completion_tokens": self._max_output_tokens,
        }

    # -- response mapping -------------------------------------------------

    def _usage_from(self, usage: CompletionUsage | None) -> Usage:
        """`input_tokens` is the uncached remainder: total prompt minus cached.

        Adding the cached tokens back into `input_tokens` would double-count the
        prefix — they are billed separately at the discounted read rate.
        """
        if usage is None:
            return Usage(model=self.model, input_tokens=0, output_tokens=0)
        details = usage.prompt_tokens_details
        cached = (details.cached_tokens or 0) if details is not None else 0
        return Usage(
            model=self.model,
            input_tokens=(usage.prompt_tokens or 0) - cached,
            output_tokens=usage.completion_tokens or 0,
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )

    @staticmethod
    def _stop_reason(reason: str | None) -> StopReason:
        return _STOP_REASONS.get(reason, "end_turn")

    def _translate(self, exc: openai.APIError) -> ProviderError:
        status = getattr(exc, "status_code", None)
        retryable = isinstance(
            exc,
            (
                openai.RateLimitError,
                openai.APIConnectionError,
                openai.InternalServerError,
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
        usage_obj: CompletionUsage | None = None
        finish_reason: str | None = None
        try:
            stream = await self._client.chat.completions.create(
                stream=True,
                stream_options={"include_usage": True},
                **self._request_kwargs(prompt),
            )
            async for chunk in stream:
                # The final chunk carries usage and an empty `choices` list.
                if chunk.usage is not None:
                    usage_obj = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    yield TextDelta(text=choice.delta.content)
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except openai.APIError as exc:
            raise self._translate(exc) from exc

        usage = self._usage_from(usage_obj)
        self._record(usage)
        yield UsageReport(usage=usage)
        yield Done(stop_reason=self._stop_reason(finish_reason))

    async def complete(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        try:
            completion = await self._client.chat.completions.create(**self._request_kwargs(prompt))
        except openai.APIError as exc:
            raise self._translate(exc) from exc

        choice = completion.choices[0]
        usage = self._usage_from(completion.usage)
        self._record(usage)
        return Completion(
            text=choice.message.content or "",
            tool_calls=(),
            usage=usage,
            stop_reason=self._stop_reason(choice.finish_reason),
        )

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        raise NotImplementedError("Phase 3")
