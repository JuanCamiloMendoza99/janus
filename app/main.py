"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import chat, triage, usage
from app.api.schemas import HealthResponse
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.domain.prompts import get_variant
from app.observability.middleware import CostLoggingMiddleware
from app.providers.base import LLMProvider
from app.providers.registry import get_provider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging at startup — never at import time (ADR: see logging.py)."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    yield


app = FastAPI(
    title="Janus",
    description=(
        "A provider-agnostic LLM gateway. Two faces, one door: Anthropic and "
        "OpenAI behind a single API, with streaming, tool calling, structured "
        "outputs, prompt caching and per-request cost accounting."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Records token usage and a cost figure for every request, flushing after the
# response body completes so streamed usage is captured (ADR-004).
app.add_middleware(CostLoggingMiddleware)

# Lets the web console (Phase 6) call the API from Vite's dev server, which is a
# different origin than the API in development. Origins come from a setting and
# default to localhost:5173 — never `*`, because this gateway has vendor
# credentials behind it. Only the two methods and the header the console actually
# uses are allowed; the browser's preflight is answered here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(chat.router)
app.include_router(triage.router)
app.include_router(usage.router)

SettingsDep = Annotated[Settings, Depends(get_settings)]
ProviderDep = Annotated[LLMProvider, Depends(get_provider)]


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health(settings: SettingsDep, provider: ProviderDep) -> HealthResponse:
    """Liveness check that also reports which provider, model and prompt are wired in.

    Reporting all three is the point: the project's core claim is that they
    change by environment variable alone, and this endpoint is how you confirm a
    swap took effect without reading the logs. `prompt` is validated by
    `get_variant()`, so an unknown `TRIAGE_PROMPT` surfaces here rather than on
    the first triage request.
    """
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        environment=settings.environment,
        provider=provider.name,
        model=provider.model,
        prompt=get_variant(settings.triage_prompt).name,
    )


# Serve the built web console (Phase 6) as static files, so production is one
# process and one port. Mounted LAST and at "/" so the API routes above win: a
# catch-all static mount registered earlier would shadow `/v1/*` and `/health`
# and every API call would start returning `index.html`. `html=True` serves
# `index.html` for "/".
#
# Guarded on existence: `web/dist` only appears after `npm run build`, and the
# app has to boot without it — that is how the test suite, CI, and a
# backend-only dev loop run. When it is missing, the console simply is not
# served; the API is unaffected.
_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web-console")
