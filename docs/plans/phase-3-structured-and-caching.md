# Phase 3 — Structured outputs & prompt caching

**Goal.** `POST /v1/triage` returning a validated `TriageResult`, with the
support playbook cached and the caching **proven by measurement**.

## Scope

### 1. Expand the playbook

`app/domain/prompts/playbook.md` is currently a stub, and a stub is too small to
cache. It must grow past the per-model floor (~4096 tokens on Opus 4.8, ~2048 on
the Sonnet family) with content that earns its place:

- full category definitions with edge cases and worked examples,
- a severity rubric with concrete examples per level,
- PII patterns and what counts as the ticket body versus the envelope,
- escalation policy with examples of when *not* to escalate,
- 6–10 worked triage examples covering the hard boundaries.

Two rules while writing it: **never template anything into it** (one variable
byte invalidates the whole prefix), and confirm the token count with the
vendor's token-counting endpoint rather than estimating.

### 2. `parse()` in both adapters

- Anthropic: `client.messages.parse()` with the Pydantic model.
- OpenAI: the equivalent schema-constrained parse.
- **No fallback to prompt-and-hope.** If the vendor cannot honor the schema,
  raise. Silently degrading to free-text-plus-`json.loads` would make the
  endpoint's guarantee a lie.
- `FakeProvider.parse()` builds an instance from field defaults, and stays
  limited to fully-defaulted schemas on purpose — a fake that invented plausible
  values for required fields would let a broken schema pass its tests.

### 3. Caching in the Anthropic adapter

- Send `cacheable_prefix` as a `system` block carrying
  `cache_control={"type": "ephemeral"}`.
- Render order is `tools` → `system` → `messages`, so a marker on the last
  system block covers tool definitions too.

### 4. `POST /v1/triage`

- Assemble the `Prompt` with the playbook as `cacheable_prefix` and the ticket as
  the user turn, call `parse()`, return `TriageResponse` including `cost_usd`.

### 5. `GET /v1/usage`

- Add `cache_hit_rate` to the aggregate.

## Risks

**This is the phase with the silent failure.** Prompt caching below the token
floor is accepted by the API and does nothing — no error, no warning,
`cache_creation_input_tokens` just returns 0. Code that "works" proves nothing.
The only acceptable evidence is a nonzero `cache_read_input_tokens` on a second
identical request (ADR-003).

Second risk: any accidental variability in the prefix — a trailing newline that
differs, a dict serialized without sorted keys, a tool added mid-session —
invalidates the cache and the hit rate quietly drops to zero.

## Verification

```bash
LLM_PROVIDER=anthropic uvicorn app.main:app

TICKET='{"ticket_id":"T-1","subject":"Double charge","body":"I was billed twice for order 4471."}'
curl -s localhost:8000/v1/triage -H 'content-type: application/json' -d "$TICKET"
curl -s localhost:8000/v1/triage -H 'content-type: application/json' -d "$TICKET"
curl -s localhost:8000/v1/usage
```

Done when:

- [ ] `/v1/triage` returns a schema-valid `TriageResult` from both real providers.
- [ ] The playbook is measured above the caching floor with a real token count.
- [ ] **The second request reports `cache_read_tokens > 0`.** Without this the
      feature is decorative, regardless of what the code does.
- [ ] The second request is measurably cheaper than the first, and the README
      quotes the real measured numbers — not an estimate.
- [ ] `cache_hit_rate` on `/v1/usage` is non-zero after a burst of tickets.
- [ ] A test asserts that a malformed model response raises rather than being
      papered over.
