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

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from app.observability.ledger import current_ledger
from app.providers.base import (
    Completion,
    Done,
    Message,
    ParsedCompletion,
    Prompt,
    ProviderError,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallRequested,
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
        adaptive_thinking: bool = False,
    ) -> None:
        self.model = model
        self._max_output_tokens = max_output_tokens
        self._prompt_caching_enabled = prompt_caching_enabled
        self._adaptive_thinking = adaptive_thinking
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

    @staticmethod
    def _render_message(message: Message) -> dict[str, Any]:
        """Render one domain turn as an Anthropic message.

        Two translations happen here. An assistant turn that made tool calls
        becomes a text block (only if there *is* text — the API rejects an empty
        one) followed by a `tool_use` block per call. A `tool` turn becomes a
        **user** message of `tool_result` blocks: Anthropic has no tool role, and
        results are something the caller tells the model, so they arrive as user
        content. All of that turn's results stay in the one message (ADR-007).
        """
        if message.role == "tool":
            blocks: list[dict[str, Any]] = []
            for result in message.tool_results:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content,
                }
                if result.is_error:
                    block["is_error"] = True
                blocks.append(block)
            return {"role": "user", "content": blocks}

        if message.tool_calls:
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                for call in message.tool_calls
            )
            return {"role": message.role, "content": content}

        return {"role": message.role, "content": message.content}

    @staticmethod
    def _render_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        """Rewrap `ToolSpec`s into Anthropic's envelope.

        The JSON Schema itself passes through untouched — only the key name
        differs (`input_schema` here, `parameters` at OpenAI).
        """
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
            for tool in tools
        ]

    def _request_kwargs(self, prompt: Prompt, tools: Sequence[ToolSpec] = ()) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_output_tokens,
            "messages": [self._render_message(m) for m in prompt.messages],
        }
        system = self._system_blocks(prompt)
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._render_tools(tools)
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

    @staticmethod
    def _tool_call(call_id: str, name: str, raw_arguments: str) -> ToolCall:
        """Parse a fully-reassembled argument string into a `ToolCall`.

        Unparseable JSON becomes empty arguments rather than an exception. The
        call still has to reach the loop: dropping it leaves the model's tool_use
        block unpaired and the *next* request is rejected outright, whereas empty
        arguments fail validation in `dispatch()` and come back as an error the
        model can read and correct in the same turn.
        """
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError:
            arguments = {}
        return ToolCall(id=call_id, name=name, arguments=arguments)

    # -- protocol ---------------------------------------------------------

    async def stream(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[StreamEvent]:
        """Stream a turn, reassembling tool arguments before announcing them.

        Anthropic streams a tool call's arguments as `input_json_delta`
        fragments — `{"que`, `ry": "dou`, `ble charge"}` — which are not valid
        JSON individually. They are accumulated per content block index and
        parsed at `content_block_stop`, so `ToolCallRequested` always carries a
        complete argument dict and no consumer ever has to know that partial
        JSON exists.

        Iterating raw events rather than `stream.text_stream`, which yields text
        only and would silently drop every tool call.
        """
        # Content block index -> the tool call being assembled there.
        pending: dict[int, dict[str, str]] = {}
        try:
            async with self._client.messages.stream(
                **self._request_kwargs(prompt, tools)
            ) as stream:
                async for event in stream:
                    match event.type:
                        case "content_block_start" if event.content_block.type == "tool_use":
                            pending[event.index] = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "arguments": "",
                            }
                        case "content_block_delta" if event.delta.type == "text_delta":
                            yield TextDelta(text=event.delta.text)
                        case "content_block_delta" if event.delta.type == "input_json_delta":
                            if event.index in pending:
                                pending[event.index]["arguments"] += event.delta.partial_json
                        case "content_block_stop" if event.index in pending:
                            block = pending.pop(event.index)
                            yield ToolCallRequested(
                                call=self._tool_call(block["id"], block["name"], block["arguments"])
                            )
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
            message = await self._client.messages.create(**self._request_kwargs(prompt, tools))
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc

        text = "".join(block.text for block in message.content if block.type == "text")
        # Non-streamed, the SDK has already parsed each tool call's arguments —
        # `block.input` is a dict, not the partial JSON the streamed path sees.
        tool_calls = tuple(
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in message.content
            if block.type == "tool_use"
        )
        usage = self._usage_from(message.usage)
        self._record(usage)
        return Completion(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=self._stop_reason(message.stop_reason),
        )

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        """Run a turn constrained to `schema`, via `client.messages.parse()`.

        The SDK derives the JSON Schema from the Pydantic model and validates the
        response back into it. Constraints the API's schema dialect does not
        support — `confidence`'s `ge`/`le`, the `max_length` on the free-text
        fields — are moved into the field description by the SDK and enforced
        client-side on the way back, so `TriageResult` needs no vendor-shaped
        twin.

        Thinking is disabled by default. Sonnet 5 runs adaptive thinking
        whenever the parameter is omitted; classifying against a closed enum was
        judged not to need it, and letting it run spends part of `max_tokens` on
        reasoning that never reaches the caller. `ANTHROPIC_ADAPTIVE_THINKING`
        turns it back on — the setting exists because that judgement was a guess
        until Phase 4 measured it, and the eval harness sweeps this axis.

        Note the parameter is always sent explicitly, in either state. Omitting
        it does not mean "off": on Sonnet 5 an omitted `thinking` runs adaptive,
        so silence would make the behaviour depend on which model is configured.

        There are two ways this fails and they arrive by different routes. A
        refusal comes back as a normal response with nothing parsed in it. A
        response truncated mid-JSON makes the SDK raise `pydantic.ValidationError`
        while validating, *before* returning anything — so that one has to be
        caught, or a vendor-adjacent exception escapes past the seam (ADR-001).
        Verified against the live API on 2026-07-21 by capping `max_tokens` at 16.
        """
        try:
            message = await self._client.messages.parse(
                output_format=schema,
                thinking={"type": "adaptive"} if self._adaptive_thinking else {"type": "disabled"},
                **self._request_kwargs(prompt),
            )
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc
        except ValidationError as exc:
            # The one call this ledger cannot price: the SDK raises during
            # response validation, so the `Message` carrying `usage` never
            # reaches this frame. Left unrecorded on purpose — writing a zero
            # would read as a free request rather than an unmeasured one, and a
            # silently wrong number is worse than a visible gap.
            raise ProviderError(
                message=(
                    "The model returned content that does not satisfy the schema, most "
                    "often a response truncated before the JSON closed. Raise "
                    "MAX_OUTPUT_TOKENS or shorten the input."
                ),
                provider=self.name,
                retryable=False,
            ) from exc

        # Recorded before the result is inspected: the call was billed whether or
        # not it came back usable, and a ledger that silently omits the failures
        # is wrong in exactly the case worth auditing.
        usage = self._usage_from(message.usage)
        self._record(usage)

        parsed = message.parsed_output
        if parsed is None:
            raise ProviderError(
                message=self._parse_failure(message.stop_reason),
                provider=self.name,
                retryable=False,
            )
        return ParsedCompletion(parsed=parsed, usage=usage)

    @staticmethod
    def _parse_failure(stop_reason: str | None) -> str:
        """Explain an empty `parsed_output` in terms of what to do about it.

        There is no fallback path here on purpose (ADR-008): parsing free text
        with `json.loads` when the vendor could not honor the schema would turn
        the endpoint's guarantee into a coin flip. So the failure has to carry
        enough for the caller to act, and the three causes need three different
        responses.
        """
        match stop_reason:
            case "refusal":
                return "The model refused to produce a response for this input."
            case "max_tokens":
                return (
                    "The response was truncated before the schema was satisfied. "
                    "Raise MAX_OUTPUT_TOKENS or shorten the input."
                )
            case _:
                return f"The model returned no schema-valid content (stop_reason={stop_reason!r})."
