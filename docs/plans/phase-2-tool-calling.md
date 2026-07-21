# Phase 2 — Tool calling

**Goal.** A working tool loop with two real tools, normalized across both
vendors.

## Scope

### 1. Tool implementations

- `search_kb` — search a small in-repo corpus of support articles. Deliberately
  not a vector index: the sibling Veridex project already demonstrates retrieval
  properly, and a second, worse RAG system here would add nothing.
- `escalate_ticket` — append to an in-process escalation log and return a
  confirmation. The write-side counterpart, which is the point: a read tool
  tolerates speculative calls, a write tool does not.

### 2. `app/tools/registry.py`

- Implement `dispatch()`. It must **never raise** for a tool-level failure —
  return `ToolResult(is_error=True)` with a readable message instead. A raised
  exception leaves the call unpaired, and both vendors reject a follow-up
  request whose tool call has no matching result.
- Validate arguments against the tool's JSON Schema before invoking the handler.
  The model will occasionally send something that does not fit.

### 3. The loop (`app/services/tool_loop.py`, new)

- Iterate: call the provider → if `stop_reason == "tool_use"`, execute every
  requested call → append all results in a single turn → repeat.
- **Return all results in one turn.** Splitting them across several messages
  trains the model to stop making parallel calls.
- Cap iterations (5 is reasonable) so a confused model cannot loop forever.
- Every iteration records to the ledger — a tool-using request legitimately
  costs several model calls, and the log must reflect that.

### 4. Adapter work

- Anthropic: tool arguments stream as partial JSON. Reassemble fully before
  emitting `ToolCallRequested`; never forward fragments upward.
- OpenAI: different call shape and different parallel-call behaviour. Both
  normalize to `ToolCall`. The asymmetry stops inside the adapter.

### 5. `POST /v1/chat`

- Emit `tool_call` SSE frames as calls are made, so a client can show what the
  assistant is doing rather than freezing during a long tool turn.

## Risks

- **Tool definitions are rendered first in the prompt**, ahead of the system
  block. Adding, removing or reordering a tool invalidates the cached prefix for
  everything after it. `TOOL_SPECS` is a tuple for this reason — keep the order
  stable (ADR-003).
- Over-eager escalation. `escalate_ticket` has a real pager attached in the
  fiction; the playbook must make the bar explicit and the evals in Phase 4
  should measure how often it fires wrongly.

## Verification

```bash
LLM_PROVIDER=anthropic uvicorn app.main:app
curl -N localhost:8000/v1/chat -H 'content-type: application/json' \
     -d '{"messages":[{"role":"user","content":"I was charged twice, order 4471"}]}'
# → a tool_call frame for search_kb, then a grounded answer.
```

Done when:

- [ ] Both tools are invoked correctly by both real providers. *(Request
      rendering and streamed-argument reassembly are covered offline in
      `tests/test_adapters.py`; the live run is still outstanding.)*
- [x] A deliberately failing tool returns `is_error=True` and the model recovers
      in the same turn instead of the request 500-ing.
- [x] A parallel tool call (both tools in one turn) returns both results in a
      single message and completes.
- [x] The iteration cap is covered by a test.
- [x] A multi-call request logs the **sum** of all its model calls, not just the last.

## Implementation notes (2026-07-21)

Three decisions taken while implementing, recorded here because the plan above
did not settle them:

1. **The domain model had to grow.** `Message` was `role` + `content` and could
   not express a turn that made tool calls or one that carried results. It now
   has `tool_calls` and `tool_results`, with all of a turn's results in a single
   `tool` message — see the new ADR-007, which also covers the OpenAI fan-out.
2. **The loop drives `stream()`, not `complete()`,** on every iteration. Tools
   are on by default, so a `complete()`-based loop would have turned `/v1/chat`
   into a blocking endpoint for most requests.
3. **Tool arguments are validated by a Pydantic model, and the published JSON
   Schema is derived from it** (`app/tools/schema.py`). Two hand-written
   artefacts would drift, and the drift only shows up when a live model finds it.

The playbook is now wired in as `Prompt.cacheable_prefix` on `/v1/chat`: without
it the escalation bar is unstated and `escalate_ticket` fires on anything. Phase
3 still owns expanding it past the caching floor and proving `cache_read > 0`.
