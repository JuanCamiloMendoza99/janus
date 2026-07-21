"""`GET /v1/usage` — aggregated cost and token accounting.

Phase 1 aggregates from the in-process ledger, which resets on restart. That
limitation is fine for a portfolio project and is stated in the README rather
than hidden: persisting spend is a database concern, and Janus is deliberately
not a database project.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import UsageWindow
from app.observability.ledger import usage_store

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsageWindow)
async def usage() -> UsageWindow:
    """Return spend and token totals since process start.

    `cache_hit_rate` is the number to watch: it is the only thing that proves
    prompt caching is actually working, as opposed to configured. In Phase 1 it
    reads 0 — nothing sends a cacheable prefix yet (that arrives with `/v1/triage`
    in Phase 3).
    """
    snapshot = usage_store.snapshot()
    return UsageWindow(
        since=snapshot.since.isoformat(),
        requests=snapshot.requests,
        total_cost_usd=snapshot.total_cost_usd,
        total_input_tokens=snapshot.total_input_tokens,
        total_output_tokens=snapshot.total_output_tokens,
        total_cache_read_tokens=snapshot.total_cache_read_tokens,
        cache_hit_rate=snapshot.cache_hit_rate,
        by_model=snapshot.by_model,
    )
