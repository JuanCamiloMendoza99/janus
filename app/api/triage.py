"""`POST /v1/triage` — schema-constrained ticket classification.

Deliberately not streamed. The consumer of a structured verdict is another
system, not a human watching tokens appear, and a partial JSON object is useless
to it. Streaming here would be a feature demo rather than a design choice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.schemas import TriageRequest, TriageResponse
from app.providers.base import LLMProvider
from app.providers.registry import get_provider

router = APIRouter(prefix="/v1", tags=["triage"])

ProviderDep = Annotated[LLMProvider, Depends(get_provider)]


@router.post("/triage", response_model=TriageResponse)
async def triage(request: TriageRequest, provider: ProviderDep) -> TriageResponse:
    """Classify a support ticket into a validated `TriageResult`.

    This is the endpoint where the two headline features meet: the support
    playbook is sent as the cacheable prefix, so a burst of tickets pays for it
    once, and the response is constrained to the `TriageResult` schema by the
    provider rather than parsed out of free text.
    """
    raise NotImplementedError("Phase 3")
