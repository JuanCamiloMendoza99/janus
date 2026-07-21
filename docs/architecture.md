# Architecture Decision Records

Append-only. Each entry records a decision, the reasoning behind it, and what it
costs — because the cost is the part that gets forgotten and then rediscovered
painfully.

---

## ADR-001 — Vendor-neutral domain model, adapters at the edges

**Status:** accepted (Phase 0)

**Context.** Janus must let a provider be swapped by environment variable without
touching business code. The obvious shortcut is to pick one vendor's SDK types as
the internal representation and translate the other one into it. That shortcut
fails the moment the two vendors disagree — and they disagree constantly, on tool
call shape, on streaming events, on how usage is reported.

**Decision.** Define an independent domain model in `app/providers/base.py`
(`Message`, `Prompt`, `ToolSpec`, `ToolCall`, `ToolResult`, `Usage`, `Completion`,
and the `StreamEvent` union). Provider adapters translate domain → vendor SDK →
domain. **No Anthropic or OpenAI SDK type crosses that boundary in either
direction.**

**Consequences.** Every vendor feature must be either modelled explicitly or
consciously dropped; nothing is available "for free" by leaking a native object.
That friction is the mechanism — it forces each vendor difference to be a
deliberate design decision instead of an accident (see ADR-003 for the case where
this mattered most). The cost is real translation code in each adapter and a
domain model that must be extended whenever a genuinely new capability is added.

---

## ADR-002 — The seam is a `typing.Protocol`, not an abstract base class

**Status:** accepted (Phase 0)

**Context.** The provider interface needs implementations for two vendors plus a
test double.

**Decision.** `LLMProvider` is a `runtime_checkable` `Protocol`. Implementations
are structurally typed and inherit from nothing.

This reuses the pattern already proven in the sibling
[Veridex](https://github.com/JuanCamiloMendoza99/veridex) project, where
`EmbeddingsProvider` is a `Protocol` with `BedrockEmbeddings` and
`FakeEmbeddings` implementations selected by settings and swapped in tests via
`app.dependency_overrides`.

**Consequences.** `FakeProvider` is a first-class implementation rather than a
mock: the full request path — routing, SSE framing, the tool loop, the cost
ledger — is exercised with no credentials, no network and no spend. CI needs no
secrets, which means the build works on a fork. The trade-off is that a
structural mismatch is only caught by the type checker or by the shared contract
tests in `tests/test_provider_seam.py`, not by a failure to instantiate; those
tests exist specifically to cover that gap and every new adapter must pass them.

---

## ADR-003 — Prompt caching is modelled as a "cacheable prefix", not a flag

**Status:** accepted (Phase 0)

**Context.** The two vendors cache prompts in fundamentally different ways:

| | Anthropic | OpenAI |
|---|---|---|
| Opt-in | Explicit `cache_control: {"type": "ephemeral"}` on a content block | None — automatic |
| Reported at | `usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens` | `usage.prompt_tokens_details.cached_tokens` |
| `input_tokens` means | The uncached remainder | The *total*, cached included |
| Write premium | Yes (~1.25× base input rate) | No |

The tempting abstraction is a boolean like `use_cache: bool`. It cannot work:
there is nothing for that boolean to control on the OpenAI side, and on the
Anthropic side the real question is not *whether* to cache but *which bytes* are
stable enough to cache.

**Decision.** The domain expresses stability, not mechanism.
`Prompt.cacheable_prefix` holds the large, byte-stable text; `Prompt.system` and
`Prompt.messages` hold everything volatile, rendered after it. Each adapter does
whatever its vendor requires. `Usage` normalizes to `cache_read_tokens` and
`cache_write_tokens`, with the latter always 0 where there is no write premium.

**Consequences and the trap.** Caching is a *prefix match*: one changed byte
anywhere in the prefix invalidates everything after it. Modelling the stable text
as its own field makes that constraint hard to violate by accident — you cannot
interpolate a timestamp into `cacheable_prefix` without noticing.

The trap worth writing down: **Anthropic silently declines to cache prefixes
below a per-model floor** — roughly 4096 tokens on Opus 4.8, ~2048 on the Sonnet
family. Below it the API accepts the `cache_control` marker and caches nothing.
No error. No warning. `cache_creation_input_tokens` simply comes back 0.

So `app/domain/prompts/playbook.md` must be genuinely large or the entire feature
is decorative. **The acceptance test for caching is
`cache_read_input_tokens > 0` on a second identical request** — not that the code
runs, not that the marker was accepted. `GET /v1/usage` exposes `cache_hit_rate`
for exactly this reason.

**Phase 2 measurement (2026-07-21).** Caching is already engaging, earlier than
planned, and the margin is alarming. A live `claude-sonnet-5` request through the
tool loop reported `cache_write_tokens=2117` on its first model call and
`cache_read_tokens=2117` on its second — the prefix is paid for once per request
even before Phase 3 expands it.

The 2117 tokens are **the tool definitions plus the playbook**, not the playbook
alone: the `cache_control` marker sits on the system block and Anthropic renders
`tools` → `system` → `messages`, so the tool schemas fall inside the cached
prefix. That is the only reason a stub playbook clears the bar at all.

The Sonnet floor is ~2048. **The prefix clears it by 69 tokens.** Removing a
tool, shortening a description or trimming the playbook drops it below the floor
and caching stops silently — no error, `cache_creation_input_tokens` simply
returns 0. Phase 3 must put the playbook comfortably above the floor on its own
merits and assert it, rather than leaving the feature resting on a 3% margin
supplied by tool schemas that Phase 2 happened to add.

Two further consequences already visible:

* **Cache writes dominate the first call.** That same call was 84 input + 89
  output tokens and cost $0.0095, of which $0.0079 was the cache write (billed at
  1.25x input). A usage report omitting `cache_write_tokens` is therefore not
  reconcilable, which is why the `usage` SSE frame carries all four counts.
* **A tool-using request is where caching pays off fastest.** Every iteration of
  the loop re-sends the same prefix, so the write is amortized within a single
  request rather than across requests.

---

## ADR-004 — Cost accounting lives in a request-scoped ledger, not in the middleware

**Status:** accepted (Phase 0)

**Context.** The project's headline requirement is a cost figure per request. The
natural home is an HTTP middleware. But a middleware cannot see token counts:
they live in the provider's response, and on a streamed response they arrive
*after* the response has already begun — in the trailing usage event. A
middleware that reads them when the response object is created reads zero.

A second problem: one HTTP request can produce several model calls. The tool loop
makes at least two. A single usage value cannot represent that.

**Decision.** A three-part design:

1. A `UsageLedger` is created per request and stored in a `contextvars.ContextVar`.
2. Provider adapters call `ledger.record()` as each model call completes, so the
   ledger accumulates every call made while serving the request.
3. The middleware reads the ledger when the response body finishes — which for
   SSE means *after the generator is exhausted*, not when the handler returns.

A `ContextVar` rather than an explicit argument because adapters sit several
calls below the router, and threading an accounting object through every domain
signature would put observability into the business types.

**Consequences.** The flush point is subtle and is the most likely source of a
silent bug in this project. Reading the ledger too early produces a confident,
well-formatted `$0.00` — worse than no log at all, because nothing appears
broken. `FakeProvider` deliberately emits `UsageReport` late in its stream, and
`tests/test_provider_seam.py` asserts that ordering, so a middleware that flushes
early fails a test rather than shipping.

The middleware is written against the raw ASGI interface rather than
`BaseHTTPMiddleware`, which buffers responses in a way that breaks server-sent
events.

`current_ledger()` returns `None` outside a request. Adapters must tolerate that:
accounting is not permitted to break the call path it measures.

---

## ADR-005 — Pricing is versioned data, not scattered constants

**Status:** accepted (Phase 0)

**Context.** A "cost per request" figure is only as trustworthy as the numbers
behind it, and vendor prices change.

**Decision.** `app/core/pricing.py` holds a single table of USD-per-1M-token
rates keyed by exact wire model id, plus the cache write/read multipliers, and
every entry carries a `verified_on` date. A model with no entry raises
`UnknownModelError` rather than returning `0.0` — a missing price should be loud,
because a silently free model makes the whole cost log quietly wrong.

OpenAI entries are deliberately **absent** rather than guessed; Phase 1 fills
them in after verifying both the current model id and its published rates. A
wrong price is worse than a missing one because it looks authoritative.

**Consequences.** Prices go stale and the dates make that visible. The
`input_tokens`-is-the-uncached-remainder rule from ADR-003 must be respected by
every adapter, or `compute_cost_usd` double-counts the cached prefix.

**Phase 1 update (2026-07-20).** The OpenAI rows were verified against OpenAI's
published pricing page and added: `gpt-5.6-terra` ($2.50 / $15 per MTok, the
default mid tier) plus `gpt-5.6-luna` and `gpt-5.6-sol` as flagship/small
siblings. Cached input is exactly 0.10× the input rate on all of them, so the
shared `OPENAI_CACHE_READ_MULTIPLIER = 0.10` holds. `get_pricing` special-cases
the fake model to the all-zero `FAKE_MODEL_PRICING`; that lookup uses a
call-time import of `FAKE_MODEL` to avoid a `core -> providers -> observability`
cycle introduced once the fake began recording to the ledger.

---

## ADR-006 — Provider and model are separate settings

**Status:** accepted (Phase 0)

**Context.** "Which vendor" and "which tier of that vendor's models" are
different questions with different reasons to change. Cost tuning happens far
more often than vendor migration.

**Decision.** `LLM_PROVIDER` (`anthropic | openai | fake`) selects the adapter;
`ANTHROPIC_MODEL` / `OPENAI_MODEL` select the model within it. Credentials are
validated in `build_provider()` at construction time, not on first use, so a
missing key fails with a clear message instead of an opaque vendor 401 later.

**Consequences.** The default is `claude-opus-4-8`; `claude-sonnet-5` is roughly
40% of the per-token cost if paying out of pocket. The default provider is
`fake`, so a fresh clone runs with no credentials at all.

Note for Phase 1: **verify the current OpenAI model id against the published
list** rather than carrying one over from memory. The value in `.env.example` is
a placeholder.

**Phase 1 update (2026-07-20).** Done. The defaults are now each vendor's mid
tier — `ANTHROPIC_MODEL=claude-sonnet-5`, `OPENAI_MODEL=gpt-5.6-terra` — with the
OpenAI id verified against the published model list (the `gpt-5` placeholder is
gone). The Anthropic adapter also takes `prompt_caching_enabled` from settings at
construction, keeping the settings read in the registry.

---

## ADR-007 — A tool turn is one message in the domain, N at the OpenAI edge

**Status:** accepted (Phase 2)

**Context.** The tool loop has to replay what happened back to the model: the
assistant turn that requested the calls, and the results of those calls. The
domain `Message` was `role` + `content: str` and could express neither. Worse,
the two vendors disagree about the shape at every level:

| | Anthropic | OpenAI |
|---|---|---|
| A tool call | A `tool_use` content block on the assistant message | A `tool_calls` array, arguments as a JSON **string** |
| A tool result | A `tool_result` block inside a **user** message | A message with `role: "tool"` and a `tool_call_id` |
| Results per turn | All of them in one message | Exactly one per message |
| Marking a failure | A native `is_error` field | No such field |
| Streamed arguments | `input_json_delta` fragments, per content block index | `tool_calls` delta fragments, per call index, no per-call terminator |
| Argument schema | `input_schema` | `function.parameters` |

Following ADR-001, none of that may leak upward. The question was which of the
two shapes the domain should adopt.

**Decision.** `Message` gains `tool_calls` (on an `assistant` turn) and
`tool_results` (on a `tool` turn), and **every result of a turn lives in a single
`tool` message**. The OpenAI adapter fans that one message out into N; the
Anthropic adapter renders it as a user message of `tool_result` blocks. Both
adapters reassemble streamed argument fragments internally and emit
`ToolCallRequested` only once a call's arguments are complete and parsed.

Choosing the one-message shape is not just a coin flip toward Anthropic's native
form. **Splitting results across several messages teaches the model that its
parallel calls were answered one at a time, and it stops making them.** The
domain shape is the one that preserves the behaviour we want; the vendor that
disagrees pays for it inside its own adapter.

Two supporting rules:

* **`dispatch()` never raises for a tool-level failure.** An unknown tool,
  arguments that fail schema validation, and a handler that throws all return
  `ToolResult(is_error=True)` with a message the model can read. Letting the
  exception escape drops the result, which leaves the model's tool call unpaired
  — and both vendors reject the *next* request outright when a call has no
  matching result. The failure mode of "just raise" is a 500, not a worse answer.
* **The loop is capped** (`TOOL_LOOP_MAX_ITERATIONS`, default 5) and terminates
  with `Done(stop_reason="error")` rather than an exception. Every iteration is a
  paid model call; a model that keeps asking for the same tool would otherwise
  spend the budget one call at a time with nothing to stop it.

The loop drives `stream()`, not `complete()`, on every iteration — tools are on
by default, and a `complete()`-based loop would quietly turn the streaming
endpoint into a blocking one for most requests.

**Consequences.** `Message` is no longer a trivially simple record, and every
adapter now needs a `_render_message` that handles three cases instead of one.
That is the cost of ADR-001 being honoured rather than worked around: the
alternative was an `if provider == "openai"` in the loop.

Because OpenAI has no `is_error`, a failed result is marked by prefixing its
content with `ERROR: ` — the one place a vendor gap is papered over rather than
modelled, and it is a lossy translation. If a future vendor needs something
richer, it goes in `ToolResult`, not in the loop.

The trap worth writing down: **an unpaired tool call poisons the whole
conversation, not just the turn it came from.** It fails on the *following*
request, with a vendor error about a tool_use block having no tool_result — far
from the code that dropped it. That is why `dispatch()` returns errors as data
and why an argument string that will not parse still becomes a `ToolCall` with
empty arguments instead of being discarded.

Tool arguments are validated against a Pydantic model, and `ToolSpec.parameters`
is *derived* from that same model (`app/tools/schema.py`). A hand-written schema
next to a hand-written handler drifts, and the drift is invisible until a live
model finds it.
