"""`GET /v1/usage` — aggregated cost and token accounting.

Phase 1 aggregates from the in-process ledger, which resets on restart. That
limitation is fine for a portfolio project and is stated in the README rather
than hidden: persisting spend is a database concern, and Janus is deliberately
not a database project.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import UsageWindow

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsageWindow)
async def usage() -> UsageWindow:
    """Return spend and token totals since process start.

    `cache_hit_rate` is the number to watch: it is the only thing that proves
    prompt caching is actually working, as opposed to configured.
    """
    raise NotImplementedError("Phase 1")
