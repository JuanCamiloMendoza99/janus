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

**Phase 3 measurement (2026-07-21).** The playbook is no longer a stub and the
margin is no longer 3%. Two identical `POST /v1/triage` requests against
`claude-sonnet-5`, the second inside the five-minute TTL:

| | input | output | cache write | cache read | cost | latency |
|---|---:|---:|---:|---:|---:|---:|
| First (cold prefix) | 53 | 198 | 7,929 | 0 | **$0.032863** | 8.6s |
| Second (identical) | 53 | 212 | 0 | 7,929 | **$0.005718** | 5.0s |

**83% cheaper, and 42% faster**, for a verdict that was identical on both runs
(`billing` / `high` / `escalate`). Isolating the prompt half: $0.029889 →
$0.002538, a 91.5% drop, which is the 1.25x write premium giving way to the 0.1x
read rate. The residue is output tokens, which are never discounted — and the
second call happened to write 14 more of them, so the comparison is if anything
unkind to the cached run.

Three things this measurement pinned down that the Phase 2 one could not:

* **The playbook clears the floor on its own.** 6,737 tokens counted with
  `messages.count_tokens`, against the *highest* floor in the table (4,096 on the
  Opus 4.x family and Haiku 4.5). It is sized against the high one deliberately:
  Sonnet 5 is not in the published table at all, so assuming the lower 2,048 would
  be a bet rather than a fact, and re-pointing `ANTHROPIC_MODEL` must not silently
  switch caching off.
* **The response schema is inside the cached prefix.** The prefix on the wire is
  7,929 tokens, ~1,190 more than the playbook: `output_config.format` renders the
  `TriageResult` JSON schema ahead of the messages, so it caches alongside. Worth
  knowing before anyone "simplifies" the schema to save tokens — it would
  invalidate the prefix, not shrink it.
* **`/v1/chat` and `/v1/triage` do not share a cache entry.** Render order is
  `tools` → `system` → `messages`; chat sends tool definitions and triage does
  not, so the two prefixes diverge at the first block. The Phase 2 figure of 2,117
  tokens was tool schemas *plus* playbook, which is why the triage path could not
  have inherited it.

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

---

## ADR-008 — Structured output is a constraint, not a parse

**Status:** accepted (Phase 3)

**Context.** `POST /v1/triage` promises a `TriageResult`. There are two ways to
keep that promise. The common one is to ask the model for JSON in the prompt,
run `json.loads` on whatever comes back, and retry on failure. The other is to
use the vendors' schema-constrained decoding — `client.messages.parse()` at
Anthropic, `chat.completions.parse()` at OpenAI — where the schema restricts
generation rather than describing a hope.

The difference matters more than it looks. Prompt-and-parse fails at a rate that
depends on the model, the temperature and the length of the input, and it fails
*silently* into a retry loop that costs money. Worse, the tempting fallback —
"try the constrained call, and if the vendor cannot honour the schema, fall back
to parsing text" — makes the endpoint's guarantee conditional on something the
caller cannot observe.

**Decision.** Both adapters use native constrained decoding, and **there is no
fallback**. If the vendor cannot produce a schema-valid response, `parse()`
raises `ProviderError` and the router returns an HTTP error. A triage verdict
with invented fields routes a real ticket to the wrong queue; a 502 the caller
can retry is strictly better than a plausible wrong answer.

`TriageResult` needs no vendor-shaped twin. Constraints the API's schema dialect
does not support (`confidence`'s `ge`/`le`, the `max_length` on the free-text
fields) are relocated into the field description by the SDK and enforced
client-side on the way back, so the domain model stays the domain model.

**Consequence: the fake provider cannot serve this endpoint.**
`FakeProvider.parse()` builds an instance from field defaults and raises for any
schema with required fields — which `TriageResult` is, entirely. That is
deliberate. A fake that fabricated plausible values would satisfy *every*
schema, which means no schema change could ever fail a test, and the model whose
whole point is that nothing is optional would be the first thing it stopped
checking. So `/v1/triage` returns `501` on the default provider and needs real
credentials, stated in the README rather than hidden behind a convincing demo.

**The three failure paths do not arrive the same way.** This is the part worth
writing down, because two of the three are exceptions the seam would otherwise
leak (ADR-001):

| Failure | Anthropic | OpenAI |
|---|---|---|
| Model refused | Response returns, `parsed_output` is `None` | `message.refusal` is set |
| Output truncated mid-JSON | **Raises `pydantic.ValidationError`** from inside the SDK's response parser | **Raises `LengthFinishReasonError`** |
| Content filtered | — | **Raises `ContentFilterFinishReasonError`** |

Neither `LengthFinishReasonError` nor `ContentFilterFinishReasonError` descends
from `openai.APIError`, so the `except openai.APIError` used everywhere else in
that adapter does not catch them. Both were found by testing rather than by
reading: the truncation path was confirmed live on 2026-07-21 by capping
`max_tokens` at 16.

One accounting gap is left in, knowingly: when the Anthropic SDK raises during
validation, the `Message` carrying `usage` never reaches the adapter, so that
call's cost cannot be recorded. It is a real request that the ledger will not
see. Recording a zero instead would be worse — it would read as a free request
rather than an unmeasured one.

**Caching consequence.** `/v1/chat` and `/v1/triage` **do not share a cache
entry**, even though they send the same playbook. Anthropic renders
`tools` → `system` → `messages`, chat sends tool definitions and triage does
not, so the two prefixes differ from the first block onward. This is why the
playbook has to clear the token floor on its own merits: on the triage path it
cannot borrow the tool schemas' tokens the way the Phase 2 measurement did.

**Thinking is disabled on the triage call.** Sonnet 5 runs adaptive thinking
whenever the parameter is omitted. Classifying against a closed enum does not
need it, and leaving it on would spend part of `max_tokens` on reasoning that
never reaches the caller while making the per-request cost figures — the thing
this project exists to report — noisier for no gain.

## ADR-009 — The prompt is a versioned dependency behind a registry

**Status:** accepted (Phase 5)

**Context.** Through Phase 4 the triage playbook was a single file,
`app/domain/prompts/playbook.md`, loaded directly by `load_playbook()`. That made
"which prompt do we ship" unanswerable: there was no second prompt to compare the
first against, and no way to change the prompt without editing the file the whole
system depends on. Phase 4 could say which *model* to pay for; nothing could say
which *prompt* to ship, and "a better prompt" is worth measuring precisely because
prompts, unlike models, are free to change.

The project already had the right shape for this one layer up. The provider is a
swappable dependency chosen by `LLM_PROVIDER` and resolved in one registry
(ADR-006), and business code never branches on it. The prompt is the same kind of
thing: large, stable, chosen per deployment, and load-bearing. It deserved the
same discipline.

**Decision.** The playbook becomes named, versioned variants under
`app/domain/prompts/playbook/`, selected by a new `TRIAGE_PROMPT` setting and
resolved by a `PromptRegistry` that mirrors the provider registry. Selecting a
prompt is a configuration change, not a code change. Three properties are load-
bearing and each fails silently if broken, so each is enforced rather than trusted:

1. **Every variant is loaded as-is, never templated.** One interpolated byte
   invalidates the cache prefix (ADR-003). The registry reads a file and returns
   it; there is no substitution step to get wrong.
2. **Every variant clears the caching floor on its own.** `/v1/triage` sends no
   tools, so a variant is the whole cacheable prefix on that path (ADR-008) and
   cannot borrow tokens from tool schemas. A variant that drops below ~4096 tokens
   still runs and still classifies — it just silently stops caching, and then
   looks expensive for a reason that is not its quality. `tests/test_prompts_live.py`
   counts every variant with the vendor's tokenizer and fails below the floor.
3. **A variant's hypothesis and token count live in the registry, not in the
   prompt bytes.** Meta-commentary inside a cached prefix inflates that one
   variant's token count and makes the cost comparison unfair; variants have to
   compete on equal token footing. `measured_tokens` is recorded and dated in the
   registry (as prices are, ADR-005) and asserted against the live tokenizer.

The prompt text is injected into the services as an argument
(`build_triage_prompt(request, playbook)`), not fetched inside them. A service
that read `get_settings()` itself could not be swept over the prompt axis in one
process, which is exactly what the evaluation runner does — so the router owns the
lookup, the same way it owns the provider one. `/v1/chat` uses the same variant:
there is one playbook, and serving chat a prompt nobody measured would make it the
untested half of the system.

**Consequence.** `/health` reports the active variant alongside the provider and
model, so a swap can be confirmed without reading logs. An unknown `TRIAGE_PROMPT`
fails loudly at load time (`UnknownPromptError`) rather than falling back to the
default and reporting metrics under the wrong name. The evaluation harness gains a
second axis — Phase 4 sweeps provider × model with the prompt pinned, Phase 5
sweeps the prompt with the model pinned — and the two never move at once, because
a difference with two possible causes measures neither.

**How the default was chosen.** The three variants were A/B'd on the held-out
slice with an LLM-as-judge for the free-text fields; `v2-examples` won a
statistical tie on accuracy by having the lowest dropped-ticket rate and the best
free-text quality, at +21% prefix tokens (~+11% per cached ticket). The full
comparison, the judge's calibration against hand scores, and the trade-off the
champion accepts are in `docs/evals/prompts.md`.

## ADR-010 — The web console hand-rolls SSE-over-POST

**Status:** accepted (Phase 6)

**Context.** The console (Phase 6) has to consume the same `/v1/chat` stream that
`curl` does. The browser ships an API built for exactly this — `EventSource`,
which opens a Server-Sent Events connection and dispatches parsed events — and
reaching for it is the obvious move.

It does not work here, and the reason is not a detail: **`EventSource` can only
issue `GET` requests.** `/v1/chat` is a `POST` with a JSON body (the conversation
and `use_tools`), and there is no way to give `EventSource` a body or a method. The
discovery normally happens *after* a component has been written around it, which is
why it is worth recording as a decision rather than a footnote.

**Decision.** The client opens the stream with `fetch()`, takes
`response.body.getReader()`, and parses the SSE framing by hand
(`web/src/sse.ts`). Three things it has to get right, each of which produces a bug
that looks like a backend problem:

- **A read is not a frame.** The reader returns arbitrary byte boundaries, so one
  `event:`/`data:` pair can arrive split across two reads. The parser buffers and
  emits only frames terminated by a blank line.
- **UTF-8 characters split across reads.** Decoding each chunk in isolation renders
  a multi-byte character that lands on a boundary as `�`. The reader layer uses
  `TextDecoder().decode(chunk, { stream: true })`, which holds a trailing partial
  sequence until the next read completes it.
- **Keepalive comments.** `sse-starlette` emits `: ping` lines to hold the
  connection open; a parser that assumes every line is a field chokes on them, so
  comment lines are skipped.

**What it costs.** Roughly a hundred lines the platform would otherwise provide,
plus the reconnection and `Last-Event-ID` machinery `EventSource` gives for free
and this client simply does without — a chat exchange is short-lived and a dropped
stream is re-sent by the user, so automatic reconnection would be answering a
question nobody asked. The parser is a pure function (text in, frames out), which
is the payoff: it is the only part of the frontend with unit tests
(`web/src/sse.test.ts`), covering a frame split across reads, a `: ping`
keepalive, and a terminal `done` carrying `stop_reason: "error"`.

**The corollary the tests pin.** Because the request is a cross-origin `POST` in
development, the browser sends a CORS **preflight** `OPTIONS` before the stream
opens. If that is not answered the stream never starts, so `CORSMiddleware` is part
of this phase (not `*` — the gateway holds vendor credentials), and
`tests/test_web.py` asserts the preflight is answered for `POST /v1/chat`.
