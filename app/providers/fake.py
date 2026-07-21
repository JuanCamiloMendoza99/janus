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

from pydantic import BaseModel

from app.observability.ledger import current_ledger
from app.providers.base import (
    Completion,
    Done,
    ParsedCompletion,
    Prompt,
    StreamEvent,
    TextDelta,
    ToolSpec,
    Usage,
    UsageReport,
)

FAKE_MODEL = "fake-1"

# Token counts are approximated by whitespace-splitting. Deliberately crude: a
# real tokenizer here would imply the numbers mean something, and they do not.
_TOKENS_PER_WORD = 1


class FakeProvider:
    """Echoes a deterministic response derived from the prompt."""

    name = "fake"

    def __init__(self, model: str = FAKE_MODEL) -> None:
        self.model = model

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str | None) -> int:
        return len(text.split()) * _TOKENS_PER_WORD if text else 0

    def _reply(self, prompt: Prompt) -> str:
        last_user = next(
            (m.content for m in reversed(prompt.messages) if m.role == "user"),
            "",
        )
        return f"[fake:{self.model}] {last_user}"

    def _usage(self, prompt: Prompt, reply: str) -> Usage:
        prompt_tokens = (
            self._estimate_tokens(prompt.cacheable_prefix)
            + self._estimate_tokens(prompt.system)
            + sum(self._estimate_tokens(m.content) for m in prompt.messages)
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
        """
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
        reply = self._reply(prompt)
        usage = self._usage(prompt, reply)
        self._record(usage)
        return Completion(
            text=reply,
            tool_calls=(),
            usage=usage,
            stop_reason="end_turn",
        )

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        """Return a schema-valid instance built from field defaults.

        Only works for schemas whose fields are all defaulted or optional. That
        is a real limitation and it is left in on purpose: a fake that
        fabricates plausible values for required fields would let a broken
        schema pass its tests.
        """
        raise NotImplementedError("Phase 3")
