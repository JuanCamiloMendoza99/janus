# Images

## `console.png`

The screenshot the top-level README embeds: the web console mid-exchange against
a real provider (`LLM_PROVIDER=anthropic`, `claude-sonnet-5`, prompt `v2-examples`).
It shows the whole point of Phase 6 in one frame — an inline `search_kb` badge
before a grounded answer that cites KB article `kb-102`, the per-model-call cost
rows in the panel, the session total, and a non-zero cache hit rate.

To refresh it — for a new UI, a different model, or a different example — build the
console and drive it against a real provider (the fake would show fabricated costs
and a 0% cache hit rate):

```bash
# 1. Build the console
cd web && npm run build && cd ..

# 2. Serve it and the API from one process, against a real vendor
LLM_PROVIDER=anthropic uvicorn app.main:app        # http://localhost:8000

# 3. In the browser, with "tools" on, ask something the knowledge base answers,
#    e.g. "How long do refunds usually take to show up on my card?" — the model
#    calls search_kb, grounds the reply in kb-102, and the panel fills in. Send a
#    second request so the cache hit rate climbs above zero.

# 4. Screenshot the window and save it as docs/images/console.png
```

Aim to capture: the streaming answer with the inline `search_kb` badge, the
instrument panel showing the per-call cost rows, the session total, and the cache
hit rate — plus the `provider · model · prompt` badge in the header.
