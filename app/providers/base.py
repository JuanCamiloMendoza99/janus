"""The provider seam: vendor-neutral types and the `LLMProvider` protocol.

This is the load-bearing module of Janus. Everything above it — routers, the
tool loop, the triage agent — speaks only the types defined here. Everything
below it (`anthropic.py`, `openai.py`, `fake.py`) translates these types to and
from a vendor SDK.

The rule that makes the gateway real: **no Anthropic or OpenAI SDK type ever
crosses this boundary.** If a vendor object leaks upward, swapping providers
stops being a config change and the project's premise collapses. See ADR-001.

The protocol is a `typing.Protocol` rather than an abstract base class (ADR-002),
matching the `EmbeddingsProvider` seam already proven in the sibling Veridex
project: implementations are structurally typed, so a test double needs no
inheritance and `app.dependency_overrides` can substitute one freely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "error"]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
#
# Declared before `Message` because a conversation turn can carry tool calls and
# tool results.


@dataclass(frozen=True)
class ToolSpec:
    """A tool exposed to the model, described in vendor-neutral terms.

    `parameters` is a JSON Schema object. Both vendors accept JSON Schema, so
    this needs no translation — only rewrapping into each vendor's envelope.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A model's request to invoke a tool.

    `id` is vendor-assigned and opaque. It must be echoed back on the matching
    result: both vendors reject a tool result whose id does not pair with a
    pending call.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The outcome of executing a `ToolCall`.

    A failed tool returns a result with `is_error=True` rather than raising past
    the loop. Dropping the result entirely leaves the call unpaired and the next
    request is rejected — and the model recovers gracefully from an error string
    it can read.
    """

    call_id: str
    content: str
    is_error: bool = False


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """A single conversation turn.

    Most turns are just `role` + `content`. The two extra fields exist for the
    tool loop, which has to replay what happened back to the model:

    * `tool_calls` belongs to an `assistant` turn — the calls the model made.
    * `tool_results` belongs to a `tool` turn — the outcomes of those calls.

    **Every result of a turn goes in ONE `tool` message.** That is Anthropic's
    native shape, and it is also the behaviour we want: splitting results across
    several messages teaches the model that its parallel calls were answered
    one at a time, and it stops making them. OpenAI requires one message per
    result, so its adapter fans this out — and that fan-out is the only place in
    the codebase that knows about the difference (ADR-007).
    """

    role: Role
    content: str = ""
    tool_calls: Sequence[ToolCall] = ()
    tool_results: Sequence[ToolResult] = ()


@dataclass(frozen=True)
class Prompt:
    """A request's full input, split by *stability* rather than by role.

    The split is the point. Prompt caching is a prefix match: the cacheable part
    must be byte-identical across requests, and anything volatile placed before
    it invalidates everything after. Modelling that as a distinct field makes
    the constraint impossible to violate by accident — you cannot interpolate a
    timestamp into `cacheable_prefix` without noticing what you are doing.

    Attributes:
        cacheable_prefix: Large, stable, reused verbatim across requests — the
            support playbook, taxonomy, tool documentation. This is what each
            adapter marks as cacheable in whatever way its vendor requires.
            Must be genuinely large to have any effect: Anthropic silently
            declines to cache prefixes below a per-model floor (~4096 tokens on
            Opus 4.8). See ADR-003.
        system: Volatile system-level instructions, if any. Rendered *after*
            the cacheable prefix precisely because it may change per request.
        messages: The conversation itself.
    """

    cacheable_prefix: str | None
    system: str | None
    messages: Sequence[Message]


# --------------------------------------------------------------------------
# Usage and results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Normalized token accounting for a single model call.

    `input_tokens` is the **uncached remainder only**, matching how both vendors
    report it. The true prompt size is
    `input_tokens + cache_read_tokens + cache_write_tokens`; summing them into
    one field would double-count the cached prefix and overstate cost.

    `cache_write_tokens` is always 0 on providers that cache automatically and
    charge no write premium — the field exists so the ledger stays uniform, not
    because every vendor populates it.
    """

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_prompt_tokens(self) -> int:
        """Total tokens in the prompt, cached and uncached."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_hit(self) -> bool:
        """Whether any part of the prompt was served from cache.

        This is the honest test that caching works. Code that compiles and a
        `cache_control` marker that was accepted both prove nothing — only a
        nonzero read on a *subsequent* request does.
        """
        return self.cache_read_tokens > 0


@dataclass(frozen=True)
class Completion:
    """A non-streaming model response."""

    text: str
    tool_calls: Sequence[ToolCall]
    usage: Usage
    stop_reason: StopReason


@dataclass(frozen=True)
class ParsedCompletion[T: BaseModel]:
    """A schema-constrained response, already validated into a Pydantic model."""

    parsed: T
    usage: Usage


# --------------------------------------------------------------------------
# Stream events
# --------------------------------------------------------------------------
#
# Vendors emit wildly different stream shapes. Rather than forwarding raw vendor
# events and asking routers to interpret them, adapters normalize down to this
# small closed set. The SSE layer then has exactly four cases to handle,
# regardless of who is on the other end.


@dataclass(frozen=True)
class TextDelta:
    """An incremental chunk of assistant text."""

    text: str


@dataclass(frozen=True)
class ToolCallRequested:
    """The model finished assembling a tool call.

    Emitted once the call's arguments are complete, not per-fragment: both
    vendors stream tool arguments as partial JSON, and exposing that upward
    would push JSON reassembly into every consumer.
    """

    call: ToolCall


@dataclass(frozen=True)
class UsageReport:
    """Final token accounting for the call.

    Arrives at the *end* of a stream — this is why the cost middleware cannot
    read tokens when the response starts, and why the ledger must be flushed
    after the stream closes. See ADR-004.
    """

    usage: Usage


@dataclass(frozen=True)
class Done:
    """Terminal event. Always emitted, including on the error path."""

    stop_reason: StopReason
    error: str | None = None


StreamEvent = TextDelta | ToolCallRequested | UsageReport | Done


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """What Janus requires of a model provider.

    Three capabilities, one per endpoint the gateway exposes: streaming chat,
    a blocking call for the tool loop, and schema-constrained parsing.

    Implementations are responsible for translating `Prompt` into their vendor's
    request shape — including applying whatever prompt-caching mechanism that
    vendor uses — and for reporting `Usage` with cached tokens broken out.
    """

    #: Stable identifier used in logs and the cost ledger ("anthropic", "openai", "fake").
    name: str

    #: The concrete model id sent on the wire. Needed by the pricing table.
    model: str

    def stream(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response as normalized events.

        Must emit exactly one `UsageReport` before `Done` whenever the vendor
        reports usage, and must emit `Done` even when the call fails — the
        ledger and the SSE consumer both rely on a terminal event to finalize.

        `ToolCallRequested` must carry fully-assembled arguments. Both vendors
        stream tool arguments as partial JSON; reassembly is the adapter's job.

        This is what the tool loop drives — every turn, not just the last one.
        A loop built on `complete()` would stop `/v1/chat` from streaming the
        final answer as soon as any tool was involved, which is most requests.
        """
        ...

    async def complete(
        self,
        prompt: Prompt,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        """Run a single non-streaming turn.

        The blocking sibling of `stream()`, for callers with no one watching
        tokens appear — scripts, evaluations, and the seam's contract tests.
        It returns the same `ToolCall`s the streamed path emits, so a future
        batch caller gets tool use without a second implementation.
        """
        ...

    async def parse[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
    ) -> ParsedCompletion[T]:
        """Run a turn constrained to `schema` and return the validated model.

        Both vendors support native schema-constrained decoding, so this is a
        real constraint on generation rather than a JSON-parsing-with-retries
        wrapper. Implementations must not fall back to prompt-and-hope: if the
        vendor cannot honor the schema, raise.
        """
        ...


@dataclass
class ProviderError(Exception):
    """A provider call failed in a way the caller must handle.

    Adapters translate vendor exceptions into this so routers can map failures
    to HTTP status codes without importing either SDK's exception hierarchy.

    Attributes:
        message: Human-readable cause.
        provider: Which provider raised it.
        retryable: True for rate limits, overloads and transport errors; False
            for malformed requests and auth failures.
        status_code: The vendor's HTTP status, when there was one.
    """

    message: str
    provider: str
    retryable: bool = False
    status_code: int | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.provider}] {self.message}"
