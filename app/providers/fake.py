"""A deterministic in-process provider.

Not a mock bolted onto the tests — a first-class implementation of the seam.
It exists so the entire gateway (routing, SSE framing, the tool loop, the cost
ledger) can be exercised with no credentials, no network and no spend. CI runs
against it, which is why CI needs no secrets.

It is the same trick the sibling Veridex project uses for embeddings: keep the
expensive dependency behind an interface with a free implementation, and the
rest of the system stays testable.

What it does *not* do is produce meaningful text. Its output is derived from the
input, so it is useful for asserting plumbing and worthless for asserting answer
quality. Anything about model behaviour has to be verified against a real
provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from pydantic import BaseModel, ValidationError

from app.observability.ledger import current_ledger
from app.providers.base import (
    Completion,
    Done,
    ParsedCompletion,
    Prompt,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallRequested,
    ToolSpec,
    Usage,
    UsageReport,
)

FAKE_MODEL = "fake-1"

# Token counts are approximated by whitespace-splitting. Deliberately crude: a
# real tokenizer here would imply the numbers mean something, and they do not.
_TOKENS_PER_WORD = 1


class FakeProvider:
    """Echoes a deterministic response derived from the prompt.

    Optionally scripted: `tool_script` is one sequence of `ToolCall`s per turn.
    Turn *i* requests exactly what `tool_script[i]` says and stops with
    `tool_use`; once the script runs out the provider answers with its usual
    echo. That is enough to drive the whole tool loop — parallel calls, failing
    calls, and (with a script longer than the cap) a model that never stops
    asking — with no credentials and no network.

    Scripting the *calls* rather than the outcomes is the point: the tools
    themselves, `dispatch()`, the result rendering and the loop's termination
    are all the real implementations under test.
    """

    name = "fake"

    def __init__(
        self,
        model: str = FAKE_MODEL,
        tool_script: Sequence[Sequence[ToolCall]] = (),
    ) -> None:
        self.model = model
        self._tool_script = tuple(tuple(turn) for turn in tool_script)
        #: Turns taken so far. Instance state, so a test gets a fresh script by
        #: constructing a fresh provider.
        self._turn = 0

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str | None) -> int:
        return len(text.split()) * _TOKENS_PER_WORD if text else 0

    def _next_tool_calls(self) -> tuple[ToolCall, ...]:
        """Return the calls scripted for this turn and advance the counter."""
        turn = self._turn
        self._turn += 1
        return self._tool_script[turn] if turn < len(self._tool_script) else ()

    def _reply(self, prompt: Prompt) -> str:
        last_user = next(
            (m.content for m in reversed(prompt.messages) if m.role == "user"),
            "",
        )
        # Echoing the tool results back makes it visible, in the response body,
        # that results actually reached the model — including the errored ones,
        # which is the recovery path worth being able to see.
        results = [
            result
            for message in prompt.messages
            if message.role == "tool"
            for result in message.tool_results
        ]
        suffix = "".join(
            f" | tool{'!' if result.is_error else ''}: {result.content}" for result in results
        )
        return f"[fake:{self.model}] {last_user}{suffix}"

    def _usage(self, prompt: Prompt, reply: str) -> Usage:
        prompt_tokens = (
            self._estimate_tokens(prompt.cacheable_prefix)
            + self._estimate_tokens(prompt.system)
            + sum(self._estimate_tokens(m.content) for m in prompt.messages)
            + sum(
                self._estimate_tokens(result.content)
                for m in prompt.messages
                for result in m.tool_results
            )
        )
        return Usage(
            model=self.model,
            input_tokens=prompt_tokens,
            output_tokens=self._estimate_tokens(reply),
        )

    def _record(self, usage: Usage) -> None:
        """Record to the request ledger if one is installed.

        The fake is a first-class implementation of the seam, so it accounts for
        its (free) usage exactly like the real adapters — this is what keeps the
        whole cost path exercised in CI. `current_ledger()` is `None` when a test
        calls the provider directly, and that must be tolerated (ADR-004).
        """
        ledger = current_ledger()
        if ledger is not None:
            ledger.record(self.name, usage)

    # -- protocol ---------------------------------------------------------

    async def stream(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[StreamEvent]:
        """Emit the reply word by word, then usage, then `Done`.

        The event order mirrors what real providers do — usage last — so tests
        written against the fake catch the flush-after-close bug described in
        ADR-004 instead of hiding it. Usage is recorded to the ledger at the same
        late point a real adapter would, so a middleware that flushed early would
        see an empty ledger.

        A scripted turn emits its tool calls instead of text and stops with
        `tool_use`. Usage still comes last: an intermediate turn of the tool loop
        costs money too, and the ledger has to see it.
        """
        calls = self._next_tool_calls()
        if calls:
            for call in calls:
                yield ToolCallRequested(call=call)
            usage = self._usage(prompt, "")
            self._record(usage)
            yield UsageReport(usage=usage)
            yield Done(stop_reason="tool_use")
            return

        reply = self._reply(prompt)
        for word in reply.split():
            yield TextDelta(text=word + " ")
        usage = self._usage(prompt, reply)
        self._record(usage)
        yield UsageReport(usage=usage)
        yield Done(stop_reason="end_turn")

    async def complete(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        calls = self._next_tool_calls()
        reply = "" if calls else self._reply(prompt)
        usage = self._usage(prompt, reply)
        self._record(usage)
        return Completion(
            text=reply,
            tool_calls=calls,
            usage=usage,
            stop_reason="tool_use" if calls else "end_turn",
        )

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        """Return a schema-valid instance built from field defaults.

        Only works for schemas whose fields are all defaulted or optional. That
        is a real limitation and it is left in on purpose: a fake that
        fabricates plausible values for required fields would satisfy *any*
        schema, which means no schema change could ever fail a test — and
        `TriageResult`, whose whole point is that every field is required, would
        be the first thing it stopped checking.

        So `POST /v1/triage` does not work on the fake provider. It raises here,
        loudly and with the reason, rather than returning a fabricated verdict
        that looks real in a demo.
        """
        try:
            parsed = schema()
        except ValidationError as exc:
            raise ProviderError(
                message=(
                    f"FakeProvider cannot produce a {schema.__name__}: it builds instances "
                    "from field defaults only, and this schema has required fields. Set "
                    "LLM_PROVIDER to a real vendor, or override the provider dependency "
                    "with a test double that returns a canned result."
                ),
                provider=self.name,
                retryable=False,
                status_code=501,
            ) from exc

        usage = self._usage(prompt, "")
        self._record(usage)
        return ParsedCompletion(parsed=parsed, usage=usage)
