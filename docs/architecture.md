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
