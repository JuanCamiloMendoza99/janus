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
companion to Veridex (private repo): Veridex
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
7. **The playbook is chosen by config, never hardcoded.** It is versioned variants
   behind `app/domain/prompts/registry.py`, selected by `TRIAGE_PROMPT` (ADR-009).
   Every variant is loaded byte-for-byte as-is (never templated) and must clear the
   caching floor; its hypothesis and token count live in the registry, not in the
   prompt bytes. `load_playbook(name)` returns the text; services take it as an
   argument so the eval harness can sweep the prompt axis.

## Current status

- ✅ **Phase 0 — Infrastructure & contracts** (done 2026-07-20): repo scaffold,
  the `LLMProvider` seam and vendor-neutral domain model, working `FakeProvider`,
  `/health`, ADR-001..006, phase plans, CI. 11 tests green, ruff clean.
- ✅ **Phase 1 — Provider seam & streaming** (done 2026-07-21): both real
  adapters, SSE on `/v1/chat`, the per-request cost ledger and `/v1/usage`,
  verified live against both vendors. Defaults are the mid tier of each vendor —
  `claude-sonnet-5` and `gpt-5.6-terra` — ids/rates verified 2026-07-20.
- ✅ **Phase 2 — Tool calling** (done 2026-07-21): `docs/plans/phase-2-tool-calling.md`.
  The tool loop (`app/services/tool_loop.py`), both tools, tool calling in both
  adapters and `tool_call` SSE frames. Adds ADR-007 (tool-turn shape) and
  `TOOL_LOOP_MAX_ITERATIONS`. Live acceptance run passed: a real model picks
  `search_kb` and answers from what it returned.
- ✅ **Phase 3 — Structured outputs & caching** (done 2026-07-21):
  `docs/plans/phase-3-structured-and-caching.md`. `POST /v1/triage` with
  schema-constrained decoding in both adapters, the playbook expanded from a stub
  into a real 6,737-token prefix, and caching **proven by measurement** against
  the live API (a repeated ticket costs 83% less) — see ADR-003's Phase 3
  addendum. Adds ADR-008, `app/api/errors.py`, `app/services/triage.py`,
  `tests/test_triage.py` and `tests/test_caching_live.py`. 85 tests green on the
  fake, 5 more behind `-m live`, ruff clean.
- ✅ **Phase 4 — Evaluation** (done 2026-07-22): `docs/plans/phase-4-evals.md`.
  The golden set (`evals/tickets.jsonl`, 55 tickets, 38 train / 17 holdout),
  `app/evals/` (dataset, runner, scoring, results) and `scripts/run_eval.py` /
  `scripts/report_eval.py`. Adds `ANTHROPIC_ADAPTIVE_THINKING`. Measured
  2026-07-22 for $1.62: haiku 60.0% classification, sonnet 70.9%, opus 74.5%,
  sonnet+thinking 81.8% (the recommendation — best accuracy and calibration, and
  cheaper than opus). Recommendation and caveats in `docs/evals/README.md`.
- ✅ **Phase 6 — Web console** (done 2026-07-23): `docs/plans/phase-6-web-console.md`.
  A Vite + React + TS client in `web/` (no framework, no state/CSS library): an
  instrument panel with a chat in it. The SSE-over-POST client is hand-rolled
  (`web/src/sse.ts`, the only unit-tested part — `EventSource` cannot POST, ADR-010),
  the four frames render (inline tool badges, per-call cost rows, `done` errors
  shown), and the panel reads `/v1/usage` + `/health`. Backend gains
  `CORSMiddleware` (`CORS_ALLOW_ORIGINS`, never `*`) and serves `web/dist` at `/`
  (guarded on existence, mounted last). Adds ADR-010, `tests/test_web.py`, a Node CI
  job. Runs end to end on `LLM_PROVIDER=fake`.
- ✅ **Phase 5 — Prompt engineering & optimization** (done 2026-07-22):
  `docs/plans/phase-5-prompt-optimization.md`. The playbook is now versioned
  variants under `app/domain/prompts/playbook/` behind a `PromptRegistry`, selected
  by `TRIAGE_PROMPT`; `/health` reports the active one. Three variants A/B'd on the
  holdout with an LLM-as-judge (`app/evals/judge.py`, `claude-opus-4-8`) for the
  free-text fields, calibrated against `evals/judge_calibration.jsonl` (78% exact
  agreement). Champion `v2-examples`; report in `docs/evals/prompts.md`. Adds
  ADR-009, `app/evals/report.py`, `scripts/judge_eval.py` /
  `scripts/report_prompt_eval.py`. Sweep + judge cost $0.51. Results schema bumped
  to 2 (config now carries `prompt`).
- ✅ **OpenAI model id resolved.** `OPENAI_MODEL` now defaults to `gpt-5.6-terra`
  (mid tier), with the id and rates verified against OpenAI's pricing page on
  2026-07-20 rather than carried from memory. The `.env.example` placeholder is
  gone; the adapter itself is still unverified against the live API (see below).
- ⚠️ **No `OPENAI_API_KEY` is configured on this machine.** Phase 3's live
  acceptance ran against `anthropic` only. `OpenAIProvider.parse()` is covered by
  stubbed-SDK tests — including the two `OpenAIError` subclasses that
  `except openai.APIError` does not catch — but has never made a real call.
  Treat the OpenAI half of "works on both providers" as unverified.

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

## The three subtle things

All are documented as ADRs; all are the kind of bug that ships silently.

1. **Streamed usage arrives last** (ADR-004). Reading the ledger when the handler
   returns yields a well-formatted `$0.00` for every streamed request. The flush
   must hang off the end of the response *body*. `FakeProvider` emits its
   `UsageReport` late on purpose, and `tests/test_provider_seam.py` asserts that
   ordering so an early flush fails a test.
2. **Prompt caching below the token floor is a no-op** (ADR-003). ~4096 tokens on
   the Opus 4.x family and Haiku 4.5, ~2048 on Sonnet 4.6 and Fable 5 — and
   Sonnet 5, the default, is not in the published table at all, so assuming the
   lower number is a bet. The API accepts the marker and caches nothing — no
   error. The only acceptance criterion is `cache_read_tokens > 0` on a second
   identical request, which is what `tests/test_caching_live.py` asserts.
   Phase 3 sized the playbook to clear the *high* floor on its own: measured
   2026-07-21 at **6,737 tokens**, cached and read back live. The prefix on the
   wire is **7,929** — the extra ~1,190 is the `TriageResult` JSON schema, which
   `output_config.format` renders ahead of the messages and which therefore
   caches too. Note the two endpoints do **not** share a cache entry: `tools`
   renders before `system` and `/v1/triage` sends none, so the playbook cannot
   borrow the tool schemas' tokens the way `/v1/chat` does.
3. **An unpaired tool call poisons the next request, not the current one**
   (ADR-007). Drop a `ToolResult` — by letting a handler's exception escape, or
   by discarding a call whose arguments would not parse — and the turn looks
   fine; the *following* request fails with a vendor error about a tool_use block
   with no tool_result. Hence `dispatch()` never raises, and a broken argument
   string still becomes a `ToolCall` with empty arguments.

## Repo layout

```
app/main.py            FastAPI entrypoint + /health
app/core/config.py     Settings (env-driven); add new settings here, never hardcode
app/core/pricing.py    Dated $/MTok table — the basis of every cost figure
app/api/               Routers, one module per endpoint + schemas.py + errors.py
app/providers/base.py  THE SEAM: domain model + LLMProvider Protocol
app/providers/         Adapters: anthropic.py, openai.py, fake.py, registry.py
app/domain/            TriageResult + prompts/ (registry + playbook/*.md variants)
app/services/          Prompt assembly + the tool loop (what the routers delegate to)
                       chat.py (the loop) and triage.py (the constrained call)
app/tools/             Tool specs, handlers, dispatch, the KB corpus
app/observability/     ledger.py (per-request usage), middleware.py (cost log)
app/evals/             The eval harness: dataset, runner, scoring, results, judge
web/                   The console: Vite + React + TS; src/sse.ts is the tested part
evals/                 The golden dataset (dev tooling — not shipped in the wheel)
scripts/               CLI entrypoints for the harness (argparse + print only)
tests/                 Pytest — runs entirely on the fake provider
docs/architecture.md   ADRs (append-only record of decisions)
docs/plans/            Per-phase implementation plans (source of truth for what to build)
```

## Commands

```bash
pip install -e ".[dev]"                 # local dev install (Python 3.12)
uvicorn app.main:app --reload           # API on :8000, docs on /docs
curl localhost:8000/health              # reports the active provider and model

pytest                                  # default suite, no credentials needed
pytest -m live                          # acceptance against a real vendor — spends money
ruff check . && ruff format --check .

python scripts/run_eval.py --configs sonnet   # eval sweep — spends money, capped by --max-spend
python scripts/report_eval.py                 # regenerate the tables in docs/evals/
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
