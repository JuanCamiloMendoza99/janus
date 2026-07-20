# CLAUDE.md — Agent Guide for Janus

Context file for AI agents working on this repo. Read this first, then the phase
plan you are implementing.

## What this project is

**Janus** (*two faces, one door*) is a provider-agnostic LLM gateway: Claude and
GPT behind a single FastAPI service, with SSE streaming, tool calling,
schema-constrained outputs, prompt caching and per-request cost accounting.
Stack: **FastAPI + Anthropic SDK + OpenAI SDK**, deliberately **without** an
orchestration framework — the point is to demonstrate the mechanics a framework
would hide.

It is a **personal portfolio project** for Juan Camilo Mendoza (GitHub:
`JuanCamiloMendoza99`) whose audience is **technical recruiters**. It is the
companion to [Veridex](https://github.com/JuanCamiloMendoza99/veridex): Veridex
covers retrieval, Janus covers the production layer. Quality bar: code and docs
must read like a professional production project — clean, tested, documented, no
dead code, no placeholder hacks left behind.

## Golden rules (non-negotiable)

1. **Commits**: authored ONLY as `Juan Camilo Mendoza <juan.camilo.mendoza99@gmail.com>`
   (already set in local git config). **Never add `Co-Authored-By:` trailers or
   any AI attribution.** Recruiters inspect the git history.
2. **Language**: all repo artifacts (code, comments, docs, commit messages) in
   **English**. Conversation with the user is in **Spanish**.
3. **Follow the phase plans**: implementation work is pre-designed in
   `docs/plans/phase-N-*.md`. Implement the current phase's plan; don't redesign
   unless you find a real blocker — if so, explain it to the user and update the
   plan doc with the decision.
4. **One phase = one coherent unit of work.** After completing a phase: run its
   Verification section, tick the roadmap checkbox in `README.md`, add any new
   ADRs to `docs/architecture.md`, and update "Current status" below and in the
   README.
5. **Never commit secrets.** `.env` is git-ignored. The default provider is
   `fake`, so a fresh clone runs with no credentials at all.
6. **Nothing above the provider seam may import a vendor SDK.** This is the one
   architectural rule the whole project rests on. If you find yourself wanting an
   `if provider == "anthropic"` outside `app/providers/`, the domain model needs
   extending instead.

## Current status

- ✅ **Phase 0 — Infrastructure & contracts** (done 2026-07-20): repo scaffold,
  the `LLMProvider` seam and vendor-neutral domain model, working `FakeProvider`,
  `/health`, ADR-001..006, phase plans, CI. 11 tests green, ruff clean.
- ⬜ **Phase 1 — Provider seam & streaming** ← next up:
  `docs/plans/phase-1-provider-seam.md`. Delivers both real adapters, SSE and the
  cost ledger. **Completing it satisfies the original brief** (swap provider by
  env var; cost logged per request).
- ⬜ Phase 2 — Tool calling: `docs/plans/phase-2-tool-calling.md`
- ⬜ Phase 3 — Structured outputs & caching: `docs/plans/phase-3-structured-and-caching.md`
- ⬜ Phase 4 — Evaluation: `docs/plans/phase-4-evals.md`
- ⚠️ **No real LLM call has been made yet.** Everything to date runs on
  `FakeProvider`. Treat any claim about provider behaviour as unverified until
  Phase 1 exercises it against a live API.
- ⚠️ **The OpenAI model id in `.env.example` is a placeholder.** Verify the
  current published id before implementing the OpenAI adapter; do not carry one
  over from memory.

*(Update this section whenever a phase or pending item changes.)*

## Architecture in one paragraph

`app/providers/base.py` defines a vendor-neutral domain model (`Prompt`,
`Message`, `ToolSpec`, `ToolCall`, `Usage`, and a four-member `StreamEvent`
union) plus the `LLMProvider` `Protocol`. `app/providers/registry.py` is the only
module that knows which vendor is selected; it reads `LLM_PROVIDER` and builds
`AnthropicProvider`, `OpenAIProvider` or `FakeProvider`, validating credentials
at construction. Routers in `app/api/` are thin: they assemble a `Prompt`,
consume normalized stream events, and map errors to status codes. Token usage
flows into a request-scoped `UsageLedger` (`ContextVar`) that the cost middleware
flushes when the response body completes. Design decisions are recorded as ADRs
in `docs/architecture.md`.

## The two subtle things

Both are documented as ADRs; both are the kind of bug that ships silently.

1. **Streamed usage arrives last** (ADR-004). Reading the ledger when the handler
   returns yields a well-formatted `$0.00` for every streamed request. The flush
   must hang off the end of the response *body*. `FakeProvider` emits its
   `UsageReport` late on purpose, and `tests/test_provider_seam.py` asserts that
   ordering so an early flush fails a test.
2. **Prompt caching below the token floor is a no-op** (ADR-003). ~4096 tokens on
   Opus 4.8, ~2048 on the Sonnet family. The API accepts the marker and caches
   nothing — no error. The only acceptance criterion is
   `cache_read_tokens > 0` on a second identical request.

## Repo layout

```
app/main.py            FastAPI entrypoint + /health
app/core/config.py     Settings (env-driven); add new settings here, never hardcode
app/core/pricing.py    Dated $/MTok table — the basis of every cost figure
app/api/               Routers, one module per endpoint + schemas.py
app/providers/base.py  THE SEAM: domain model + LLMProvider Protocol
app/providers/         Adapters: anthropic.py, openai.py, fake.py, registry.py
app/domain/            TriageResult + prompts/playbook.md (the cacheable prefix)
app/tools/             Tool specs, handlers, dispatch
app/observability/     ledger.py (per-request usage), middleware.py (cost log)
tests/                 Pytest — runs entirely on the fake provider
docs/architecture.md   ADRs (append-only record of decisions)
docs/plans/            Per-phase implementation plans (source of truth for what to build)
```

## Commands

```bash
pip install -e ".[dev]"                 # local dev install (Python 3.12)
uvicorn app.main:app --reload           # API on :8000, docs on /docs
curl localhost:8000/health              # reports the active provider and model

pytest                                  # full suite, no credentials needed
pytest -m "not live"                    # explicit: skip anything that spends money
ruff check . && ruff format --check .
```

## Conventions

- **Python 3.12**, ruff (line length 100). Type hints everywhere; Pydantic models
  for all API request/response schemas.
- **Three vocabularies, kept separate**: HTTP schemas (`app/api/schemas.py`),
  domain types (`app/domain/`), provider types (`app/providers/base.py`).
  Collapsing them couples the public API to a vendor's wire format.
- Routers thin, services do the work. No business logic in `app/api/`.
  Dependencies via `Annotated` aliases at the top of the module.
- New env vars: add to `Settings`, to `.env.example` (with a comment explaining
  *why*), and to the README if user-facing.
- Tools that hit a real vendor go behind the `live` pytest marker.
- Commit messages: imperative summary line, body explains the why. One logical
  change per commit; a phase is typically 3–6 commits.
- Windows host: the user runs PowerShell; prefer cross-platform instructions in docs.
