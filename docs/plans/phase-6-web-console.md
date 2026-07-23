# Phase 6 — Web console

**Goal.** A minimal React client that makes the gateway's work *visible*:
streaming as it arrives, tool calls as they happen, and what every request cost.

## Why this is not "a chat UI"

Everything this project is proud of is currently invisible without a terminal.
Streaming, the tool loop, per-request cost, the cache hit rate, and the claim
that the provider changes with one environment variable — all of it lives in
`curl` output and log lines.

So the console is an **instrument panel that happens to have a chat in it**, and
that framing decides every design question below. A beautiful chat interface with
no cost readout would demonstrate nothing this project cares about; a plain one
that shows a `search_kb` badge appearing mid-answer and a running total in
dollars demonstrates all of it.

It is also the honest test of the API's ergonomics. An SSE format that is
pleasant to `curl` and painful to consume from a browser is a badly designed
format, and this is where that would surface.

## Scope

### 1. Stack

- **Vite + React + TypeScript** in `web/`, in this repo. One clone, one link to
  send someone.
- **No Next.js.** There is nothing to server-render — the backend is FastAPI and
  the interesting content arrives over a stream. It would add a second server and
  a routing model for no gain.
- **No state library, no component library, no CSS framework.** Component state
  and one custom hook cover the whole app. Plain CSS.
- TypeScript types for the four SSE frames, mirroring `app/api/schemas.py`. They
  are hand-written and small; generating a client from OpenAPI would be a third
  toolchain to justify.

### 2. The SSE client — the one genuinely hard part

The browser's native `EventSource` **only issues GET requests**, and `/v1/chat`
is a POST with a JSON body. The obvious approach does not work at all, and this
is the first thing to know before writing any code.

The client therefore uses `fetch()` plus `response.body.getReader()` and parses
the SSE framing by hand. Three things it must get right, all of which produce
bugs that look like backend problems:

- **A read is not a frame.** The reader hands back arbitrary byte boundaries, so
  a single `event:`/`data:` pair can arrive split across two reads. Buffer across
  reads and only consume complete frames (terminated by a blank line).
- **UTF-8 characters split across reads.** Decode with
  `new TextDecoder().decode(chunk, { stream: true })`, or a multi-byte character
  landing on a boundary renders as a replacement character.
- **Keepalive comments.** `sse-starlette` emits `: ping` lines to hold the
  connection open. A parser that assumes every line is `event:` or `data:` will
  choke on them.

This parser is the only real logic in the app, so it is the only part that gets
unit tests (Vitest). Pure function in, frames out — no DOM, no network.

### 3. Rendering the four frame types

| Frame | Rendered as |
|---|---|
| `delta` | Appended text, so the answer visibly streams |
| `tool_call` | An inline badge with the tool name and its arguments, placed where it happened in the answer |
| `usage` | A per-call cost row — **several per request**, because a tool-using request makes several model calls |
| `done` | Finalizes the message. `stop_reason: "error"` must be *shown*, not swallowed |

That last row matters: the tool loop's iteration cap surfaces as a terminal
`done` with `stop_reason: "error"`, and a client that only listens for text would
render a request that silently stops mid-thought.

### 4. The instrument panel

- **Cost of the current exchange**, broken out into input / output / cache-read /
  cache-write. All four counts are on the wire precisely so this can add up.
- **Session totals** from `GET /v1/usage`, refreshed after each exchange rather
  than on a timer — the moment worth re-reading is when a request finishes.
- **Cache hit rate**, prominently. It is the metric that proves prompt caching is
  doing anything at all, and 0% is a meaningful reading rather than a missing one.
- **Active provider and model** from `GET /health`, as a badge. This is the
  project's central claim rendered as one line of UI: change `LLM_PROVIDER`,
  restart the backend, reload — the badge changes and no frontend code did.
- A **tools toggle** wired to `use_tools`, so the difference between a grounded
  answer and an ungrounded one is a checkbox away.

### 5. Backend changes

Small, and they belong to this phase rather than to the frontend:

- **`CORSMiddleware`**, with origins from a new `CORS_ALLOW_ORIGINS` setting
  defaulting to the Vite dev server. **Not `*`** — this is a gateway with vendor
  credentials behind it, and a wildcard is the kind of default that gets copied
  into production.
- **Serve the built assets.** FastAPI mounts `web/dist` at `/` so production is
  one process and one port. Mount it *last*, or a catch-all static route shadows
  `/v1/*` and every API call starts returning `index.html`.
- `node_modules/` in `.gitignore`. (`dist/` is already ignored.)

### 6. Explicit non-goals

Each of these is a day of work that demonstrates nothing about the gateway:
authentication, persisted conversations, a markdown renderer, a design system,
dark mode, mobile layouts, retry/regenerate, conversation branching.

If the console is not finished in a handful of sessions, the scope has drifted.

## Risks

- **`EventSource` cannot POST.** Worth stating twice, because the discovery
  normally happens after the component is already written around it.
- **Scope creep into a product.** The pull toward polishing the chat is strong
  and every hour spent there is an hour not spent on the instrumentation that
  makes the project distinctive.
- **A second toolchain.** npm, a lockfile, and a CI job now exist alongside the
  Python ones. Keep the CI addition to `npm ci && npm run build` plus the parser
  tests; do not import a JavaScript linting stack to sit beside ruff.
- **The frontend contradicting the backend's numbers.** The cost shown must come
  from the `usage` frames, never be recomputed in the browser from a price table
  copied out of `app/core/pricing.py`. Two pricing tables is one too many.
- **A demo that needs credentials.** It must run against `LLM_PROVIDER=fake`, or
  the first thing a visitor sees is an error.

## New ADR

The SSE-over-POST decision deserves a record: why the client hand-rolls stream
parsing instead of using the browser API built for exactly this, and what that
costs in code the platform would otherwise provide. Number assigned on
acceptance, following the ADRs from Phases 2 and 5.

## Verification

```bash
# terminal 1 — no credentials needed
LLM_PROVIDER=fake uvicorn app.main:app --reload

# terminal 2
cd web && npm install && npm run dev
```

Done when:

- [x] A reply streams token by token in the browser rather than appearing at once.
      (`delta` frames append to the trailing text segment; a blinking cursor marks
      the live turn.)
- [x] A duplicate-charge ticket shows a `search_kb` badge *before* the grounded
      answer continues. (`tool_call` frames render an inline `ToolBadge` in stream
      order; needs a real provider to choose the tool.)
- [x] A tool-using exchange displays more than one model call, and their costs sum
      to the session total shown in the panel. (`InstrumentPanel` lists each
      `usage` row and totals them from the frames — never recomputed in the browser.)
- [x] `cache_hit_rate` is visible and non-zero after a second identical request
      against a real provider. (Read from `GET /v1/usage`, refreshed after each
      exchange; shown prominently.)
- [x] Changing `LLM_PROVIDER`, restarting the backend and reloading changes the
      badge — **with no frontend change**. (`HealthBadge` reads `GET /health`;
      verified the payload reflects the active provider/model/prompt.)
- [x] The parser has tests for a frame split across reads, a `: ping` keepalive,
      and a terminal `done` carrying `stop_reason: "error"`. (`web/src/sse.test.ts`,
      12 tests green.)
- [x] `npm run build`, then the app works served by FastAPI alone with the Vite
      dev server stopped. (Verified: `/` serves `index.html`, assets resolve,
      `/v1/*` and `/health` are not shadowed.)
- [x] The console runs end to end on `LLM_PROVIDER=fake` with no API keys set.
- [x] The README shows a screenshot. `docs/images/console.png`, captured against
      `LLM_PROVIDER=anthropic`: an inline `search_kb` badge before a grounded answer
      citing `kb-102`, the per-call cost rows, the session total, and a 59% cache
      hit rate.
- [x] ADR-010 records the SSE-over-POST decision — why the client hand-rolls
      stream parsing instead of using `EventSource`, which cannot POST.
