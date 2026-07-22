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

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
from openai.types.completion_usage import CompletionUsage
from pydantic import BaseModel

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
        for message in prompt.messages:
            messages.extend(self._render_message(message))
        return messages

    @staticmethod
    def _render_message(message: Message) -> list[dict[str, Any]]:
        """Render one domain turn as one *or more* OpenAI messages.

        This is where the vendors' shapes stop agreeing, and it is why this
        returns a list. The domain keeps every result of a turn in a single
        `tool` message (ADR-007); OpenAI requires one message per result, keyed
        by `tool_call_id`. So a turn with three results fans out into three
        messages here, and nothing above this line has to know.

        The second asymmetry: OpenAI has no `is_error` on a tool message. A
        failed tool is marked in the text itself, because the model has to be
        able to tell "the tool answered" from "the tool broke" and the prefix is
        the only channel available.
        """
        if message.role == "tool":
            return [
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": f"ERROR: {result.content}" if result.is_error else result.content,
                }
                for result in message.tool_results
            ]

        if message.tool_calls:
            return [
                {
                    "role": message.role,
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            ]

        return [{"role": message.role, "content": message.content}]

    @staticmethod
    def _render_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        """Rewrap `ToolSpec`s into OpenAI's function envelope.

        `strict` is deliberately left off. Strict mode requires every property to
        be required, and `search_kb.limit` is optional with a default — turning
        it on would mean either a 400 or quietly deleting a useful default.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def _request_kwargs(self, prompt: Prompt, tools: Sequence[ToolSpec] = ()) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(prompt),
            "max_completion_tokens": self._max_output_tokens,
        }
        if tools:
            kwargs["tools"] = self._render_tools(tools)
        return kwargs

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

    @staticmethod
    def _tool_call(call_id: str, name: str, raw_arguments: str) -> ToolCall:
        """Parse a fully-reassembled argument string into a `ToolCall`.

        Unparseable JSON becomes empty arguments rather than an exception — the
        call must still reach the loop, or its `tool_calls` entry goes unanswered
        and the next request is rejected. Empty arguments fail validation in
        `dispatch()`, which the model can read and correct.
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

        Like Anthropic, OpenAI streams arguments as fragments — but there the
        similarity ends. Calls are identified by an `index` on the delta rather
        than by a content block, the `id` and function name arrive once on the
        first fragment, and there is no per-call terminator: the only signal a
        call is complete is `finish_reason == "tool_calls"` at the end of the
        turn. So calls are accumulated by index and emitted together at the end.
        """
        usage_obj: CompletionUsage | None = None
        finish_reason: str | None = None
        # Delta index -> the call being assembled there. Ordered by first
        # appearance, which is the order the model asked for them in.
        pending: dict[int, dict[str, str]] = {}
        try:
            stream = await self._client.chat.completions.create(
                stream=True,
                stream_options={"include_usage": True},
                **self._request_kwargs(prompt, tools),
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
                for fragment in (choice.delta.tool_calls or ()) if choice.delta else ():
                    call = pending.setdefault(
                        fragment.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if fragment.id:
                        call["id"] = fragment.id
                    if fragment.function is None:
                        continue
                    if fragment.function.name:
                        call["name"] = fragment.function.name
                    if fragment.function.arguments:
                        call["arguments"] += fragment.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except openai.APIError as exc:
            raise self._translate(exc) from exc

        for call in pending.values():
            yield ToolCallRequested(
                call=self._tool_call(call["id"], call["name"], call["arguments"])
            )

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
            completion = await self._client.chat.completions.create(
                **self._request_kwargs(prompt, tools)
            )
        except openai.APIError as exc:
            raise self._translate(exc) from exc

        choice = completion.choices[0]
        # Even non-streamed, arguments come back as a JSON *string* — OpenAI
        # never parses them for you, unlike Anthropic's `input` dict.
        tool_calls = tuple(
            self._tool_call(call.id, call.function.name, call.function.arguments)
            for call in (choice.message.tool_calls or ())
        )
        usage = self._usage_from(completion.usage)
        self._record(usage)
        return Completion(
            text=choice.message.content or "",
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=self._stop_reason(choice.finish_reason),
        )

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        """Run a turn constrained to `schema`, via `chat.completions.parse()`.

        Where the failure modes diverge from Anthropic's: this SDK **raises**
        rather than returning a response with nothing parsed in it, and the two
        exceptions it raises for that (`LengthFinishReasonError`,
        `ContentFilterFinishReasonError`) descend from `OpenAIError` rather than
        `APIError` — so the `except openai.APIError` used everywhere else in
        this file does not catch them. Left uncaught they would escape as vendor
        types, which is the one thing the seam exists to prevent (ADR-001).
        Both are translated to `ProviderError` here.
        """
        try:
            completion = await self._client.chat.completions.parse(
                response_format=schema,
                **self._request_kwargs(prompt),
            )
        except openai.APIError as exc:
            raise self._translate(exc) from exc
        except openai.LengthFinishReasonError as exc:
            # The truncated response was billed for every token it did produce,
            # and the SDK hands the partial completion back on the exception —
            # so the ledger can still be told the truth about what it cost.
            self._record(self._usage_from(exc.completion.usage))
            raise ProviderError(
                message=(
                    "The response was truncated before the schema was satisfied. "
                    "Raise MAX_OUTPUT_TOKENS or shorten the input."
                ),
                provider=self.name,
                retryable=False,
            ) from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise ProviderError(
                message="The response was rejected by the content filter.",
                provider=self.name,
                retryable=False,
            ) from exc

        # Recorded before the result is inspected: the call was billed whether or
        # not it came back usable, and a ledger that silently omits the failures
        # is wrong in exactly the case worth auditing.
        usage = self._usage_from(completion.usage)
        self._record(usage)

        message = completion.choices[0].message
        if message.refusal:
            raise ProviderError(
                message=f"The model refused to produce a response: {message.refusal}",
                provider=self.name,
                retryable=False,
            )
        if message.parsed is None:
            raise ProviderError(
                message="The model returned no schema-valid content.",
                provider=self.name,
                retryable=False,
            )
        return ParsedCompletion(parsed=message.parsed, usage=usage)
