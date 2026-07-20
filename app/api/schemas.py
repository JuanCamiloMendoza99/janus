"""HTTP request and response models.

Kept separate from the domain types in `app/domain/` and the provider types in
`app/providers/base.py`. Three layers, three vocabularies: what a client sends,
what the business logic reasons about, and what a vendor SDK expects. Collapsing
them saves a few lines now and couples the public API to a vendor's wire format
later.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.triage import TriageResult


class ChatMessage(BaseModel):
    """One turn of conversation as supplied by the client."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """Input to `POST /v1/chat`."""

    messages: list[ChatMessage] = Field(min_length=1)
    use_tools: bool = Field(
        default=True,
        description="Whether the model may call tools during this turn.",
    )


class TriageRequest(BaseModel):
    """Input to `POST /v1/triage`."""

    ticket_id: str = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)


class TriageResponse(BaseModel):
    """Output of `POST /v1/triage`.

    Carries the cost of producing the verdict alongside the verdict itself. A
    triage system whose per-decision cost is invisible cannot be reasoned about
    economically, and that economic question is the point of this project.
    """

    ticket_id: str
    result: TriageResult
    provider: str
    model: str
    cost_usd: float


class UsageWindow(BaseModel):
    """Aggregated spend over a time window, returned by `GET /v1/usage`."""

    since: str
    requests: int
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    cache_hit_rate: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of prompt tokens served from cache. The honest measure of "
            "whether prompt caching is doing anything — a marker that was accepted "
            "but never hit shows up here as 0."
        ),
    )
    by_model: dict[str, float] = Field(
        default_factory=dict,
        description="Cost in USD broken down by model id.",
    )


class HealthResponse(BaseModel):
    """Output of `GET /health`."""

    status: Literal["ok"]
    app: str
    environment: str
    provider: str
    model: str
