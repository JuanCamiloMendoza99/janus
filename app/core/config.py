"""Application settings.

Every tunable value in Janus lives here and is read from the environment. Nothing
is hardcoded at a call site: swapping providers, models or budgets is an ops
change, not a code change. That is the whole premise of the gateway.

New settings must be added in three places: this class, `.env.example` (with a
comment explaining *why* it exists), and the README when a user would ever set it.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["anthropic", "openai", "fake"]


class Settings(BaseSettings):
    """Environment-driven configuration.

    Field order mirrors the roadmap: transport, provider selection, models,
    tools, caching, budgets, observability.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Service -----------------------------------------------------------
    app_name: str = "janus"
    environment: Literal["local", "ci", "production"] = "local"

    # --- Provider selection ------------------------------------------------
    # The single switch the whole project is built around. Business code never
    # branches on this value; only `app.providers.registry` reads it.
    llm_provider: ProviderName = "fake"

    # --- Credentials -------------------------------------------------------
    # Optional so the app boots (and CI runs) with no credentials at all when
    # LLM_PROVIDER=fake. The registry validates that the selected provider has
    # its key at startup rather than failing on the first request.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # --- Models ------------------------------------------------------------
    # Kept separate from `llm_provider` so cost/quality tier can be changed
    # without touching the provider seam. Defaults are the mid tier of each
    # vendor (Sonnet 5 / gpt-5.6-terra), verified against the pricing table.
    anthropic_model: str = "claude-sonnet-5"
    openai_model: str = "gpt-5.6-terra"

    # --- Generation --------------------------------------------------------
    max_output_tokens: int = 4096
    request_timeout_seconds: float = 120.0

    # --- Tools -------------------------------------------------------------
    # How many model calls one tool-using request may make before the loop gives
    # up. A model that keeps asking for the same tool would otherwise spend the
    # budget one paid call at a time, with nothing to stop it.
    tool_loop_max_iterations: int = 5

    # --- Prompt caching ----------------------------------------------------
    # Anthropic requires an explicit opt-in and a prefix above a per-model token
    # floor; OpenAI caches automatically with no opt-in. See ADR-003.
    prompt_caching_enabled: bool = True

    # --- Cost controls -----------------------------------------------------
    # A hard ceiling per request. Estimated before the call and enforced by the
    # ledger; a request projected above this is rejected rather than truncated.
    # 0 disables the check.
    max_cost_usd_per_request: float = 0.50

    # --- Observability -----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Structured JSON logs are the right default for anything shipping to a log
    # aggregator; plain text is friendlier when tailing locally.
    log_format: Literal["json", "text"] = "json"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the `.env` file is parsed once. Tests that need different values
    call `get_settings.cache_clear()` or override the FastAPI dependency.
    """
    return Settings()
