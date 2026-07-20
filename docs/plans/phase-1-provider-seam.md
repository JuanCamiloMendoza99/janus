# Phase 1 — Provider seam & streaming

**Goal.** Both real adapters working behind the `LLMProvider` protocol, SSE
streaming on `POST /v1/chat`, and a cost figure logged for every request.

This is the phase that satisfies the original brief: *change providers with an
environment variable without touching business code, and have a per-request cost
log.* Everything after it is elaboration.

## Scope

### 1. `app/core/pricing.py`

- Implement `get_pricing()` and `compute_cost_usd()`.
- Add OpenAI entries — **verify the current model ids and published rates first**;
  do not carry them over from memory (ADR-005).
- Remember: `input_tokens` is the *uncached remainder*. Adding cached tokens back
  into it double-counts the prefix.

### 2. `app/core/logging.py`

- Implement `configure_logging()` with a JSON formatter and a text fallback.
- Call it from a FastAPI lifespan handler, never at import time.

### 3. `app/providers/anthropic.py`

- `complete()`, then `stream()`, then wire caching.
- Map usage: `input_tokens` → `input_tokens`, `cache_read_input_tokens` →
  `cache_read_tokens`, `cache_creation_input_tokens` → `cache_write_tokens`.
- Streamed usage arrives in the trailing `message_delta`; emit `UsageReport`
  only once it has landed.
- Translate SDK exceptions to `ProviderError` with `retryable` set correctly
  (rate limits and overloads are retryable; auth and malformed requests are not).

### 4. `app/providers/openai.py`

- Same three methods.
- Usage needs arithmetic, not a rename: `prompt_tokens` is the *total*, so
  `input_tokens = prompt_tokens - prompt_tokens_details.cached_tokens`.
  `cache_write_tokens` stays 0.
- Caching is automatic — the adapter's only lever is rendering
  `cacheable_prefix` first.

### 5. `app/observability/ledger.py`

- Implement `record()` (prices each call as it lands) and `summary()`.

### 6. `app/observability/middleware.py`

- Implement `CostLoggingMiddleware` as pure ASGI.
- **The hard part:** flush the ledger when the response *body* completes, not
  when the handler returns. For SSE the handler returns immediately and the model
  call is still in flight. Hook the `http.response.body` message with
  `more_body == False`.
- Wire it into `app/main.py`.

### 7. `app/api/chat.py` and `app/api/usage.py`

- Assemble the `Prompt`, consume the provider stream, map each `StreamEvent` to
  its SSE frame (`delta` / `tool_call` / `usage` / `done`).
- Map `ProviderError` to HTTP: 429 for rate limits, 502 for upstream failures,
  400 for malformed requests.
- Implement `GET /v1/usage` over the in-process ledger totals.

## Risks

- **Silent $0.00 cost.** The single most likely bug in the project. If the
  middleware flushes early, every log line is a well-formatted zero and nothing
  looks broken. The regression test below is not optional.
- **Divergent stream shapes.** Resist the urge to add per-vendor branches above
  the seam; if something cannot be normalized, extend the domain model instead.
- **Async generators and `contextvars`.** Verify the ledger set in the middleware
  is actually visible inside the streaming generator's context.

## Verification

```bash
# 1. Fake provider — no credentials, no spend.
LLM_PROVIDER=fake uvicorn app.main:app
curl -N localhost:8000/v1/chat -H 'content-type: application/json' \
     -d '{"messages":[{"role":"user","content":"hello"}]}'
# → SSE frames arrive incrementally, ending with `usage` then `done`.

# 2. Real providers — the actual acceptance test for the phase.
LLM_PROVIDER=anthropic uvicorn app.main:app   # same curl, real tokens
LLM_PROVIDER=openai    uvicorn app.main:app   # same curl, same client, no code change

# 3. Cost log
#    One structured line per request, with a NON-ZERO cost on the streaming path.
```

Done when:

- [ ] The same `curl` works unchanged against all three providers.
- [ ] `git diff` for the provider switch touches only `.env` — no application code.
- [ ] Every request emits one cost log line, **streaming included and non-zero**.
- [ ] A regression test asserts the streamed cost is greater than zero. This is
      what stops ADR-004's failure mode from silently returning.
- [ ] `GET /v1/usage` totals match the sum of the individual log lines.
