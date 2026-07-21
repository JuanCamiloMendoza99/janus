"""Provider selection.

The one module in the application that is allowed to know which vendor is in
play. Everything else receives an `LLMProvider` and cannot tell the difference —
that is what makes "change providers with an environment variable, without
touching business code" literally true rather than aspirational.

Credentials are validated here, at construction, rather than on the first API
call. A missing key should fail the request that selects the provider with a
clear message, not surface later as an opaque 401 from a vendor SDK.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.fake import FakeProvider
from app.providers.openai import OpenAIProvider


class ProviderConfigurationError(RuntimeError):
    """The selected provider cannot be constructed from the current settings."""


def build_provider(settings: Settings) -> LLMProvider:
    """Construct the provider named by `settings.llm_provider`."""
    match settings.llm_provider:
        case "anthropic":
            if not settings.anthropic_api_key:
                raise ProviderConfigurationError(
                    "LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set."
                )
            return AnthropicProvider(
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
                max_output_tokens=settings.max_output_tokens,
                prompt_caching_enabled=settings.prompt_caching_enabled,
            )
        case "openai":
            if not settings.openai_api_key:
                raise ProviderConfigurationError(
                    "LLM_PROVIDER=openai requires OPENAI_API_KEY to be set."
                )
            return OpenAIProvider(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                max_output_tokens=settings.max_output_tokens,
            )
        case "fake":
            return FakeProvider()

    # `ProviderName` is a Literal, so this is unreachable unless the type is
    # widened without updating this function.
    raise ProviderConfigurationError(f"Unknown provider: {settings.llm_provider!r}")


@lru_cache
def _cached_provider() -> LLMProvider:
    return build_provider(get_settings())


def get_provider() -> LLMProvider:
    """FastAPI dependency yielding the configured provider.

    Cached because provider instances hold an SDK client with a connection pool;
    rebuilding one per request would discard keep-alive connections. Tests
    substitute a provider with `app.dependency_overrides[get_provider]` rather
    than clearing the cache.
    """
    return _cached_provider()
