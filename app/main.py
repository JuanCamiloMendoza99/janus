"""FastAPI application entrypoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI

from app.api import chat, triage, usage
from app.api.schemas import HealthResponse
from app.core.config import Settings, get_settings
from app.providers.base import LLMProvider
from app.providers.registry import get_provider

app = FastAPI(
    title="Janus",
    description=(
        "A provider-agnostic LLM gateway. Two faces, one door: Anthropic and "
        "OpenAI behind a single API, with streaming, tool calling, structured "
        "outputs, prompt caching and per-request cost accounting."
    ),
    version="0.1.0",
)

# Phase 1 installs CostLoggingMiddleware here. It is intentionally not wired up
# yet — an unimplemented middleware would break every request, including the
# health check that proves the scaffold works.

app.include_router(chat.router)
app.include_router(triage.router)
app.include_router(usage.router)

SettingsDep = Annotated[Settings, Depends(get_settings)]
ProviderDep = Annotated[LLMProvider, Depends(get_provider)]


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health(settings: SettingsDep, provider: ProviderDep) -> HealthResponse:
    """Liveness check that also reports which provider is wired in.

    Reporting the active provider and model is the point: the project's core
    claim is that these change by environment variable alone, and this endpoint
    is how you confirm the swap took effect without reading the logs.
    """
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        environment=settings.environment,
        provider=provider.name,
        model=provider.model,
    )
