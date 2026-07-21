"""`POST /v1/triage` — schema-constrained ticket classification.

Deliberately not streamed. The consumer of a structured verdict is another
system, not a human watching tokens appear, and a partial JSON object is useless
to it. Streaming here would be a feature demo rather than a design choice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.errors import http_status_for
from app.api.schemas import TriageRequest, TriageResponse
from app.core.pricing import compute_cost_usd
from app.providers.base import LLMProvider, ProviderError
from app.providers.registry import get_provider
from app.services.triage import triage_ticket

router = APIRouter(prefix="/v1", tags=["triage"])

ProviderDep = Annotated[LLMProvider, Depends(get_provider)]


@router.post("/triage", response_model=TriageResponse)
async def triage(request: TriageRequest, provider: ProviderDep) -> TriageResponse:
    """Classify a support ticket into a validated `TriageResult`.

    This is the endpoint where the two headline features meet: the support
    playbook is sent as the cacheable prefix, so a burst of tickets pays for it
    once, and the response is constrained to the `TriageResult` schema by the
    provider rather than parsed out of free text.

    A failure is an HTTP error, never a degraded result. If the provider could
    not honor the schema there is nothing partially useful to return — a
    `TriageResult` with invented fields would route a real ticket to the wrong
    queue, which is worse than a 502 the caller can retry (ADR-008).

    Note the default provider cannot serve this endpoint: `FakeProvider` refuses
    to fabricate a verdict and returns `501`. `/v1/triage` needs real
    credentials, which is stated in the README rather than papered over.
    """
    try:
        completion = await triage_ticket(provider, request)
    except ProviderError as exc:
        raise HTTPException(status_code=http_status_for(exc), detail=str(exc)) from exc

    # Priced from this call's own usage rather than read back from the request
    # ledger. Triage is exactly one model call, so the two agree — but the
    # ledger is observability, and an endpoint whose response body depends on
    # the observability layer being installed is one refactor away from
    # reporting $0.00 to its caller.
    usage = completion.usage
    cost = compute_cost_usd(
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return TriageResponse(
        ticket_id=request.ticket_id,
        result=completion.parsed,
        provider=provider.name,
        model=provider.model,
        cost_usd=cost,
    )
