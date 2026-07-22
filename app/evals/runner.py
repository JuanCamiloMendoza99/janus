"""Driving the golden dataset through one configuration at a time.

The runner never learns which vendor it is talking to. A configuration names a
provider and a model as strings; `build_provider()` turns that into an
`LLMProvider` and validates the credentials, and everything after that is the
same code path `/v1/triage` serves in production — `triage_ticket()`, the same
playbook, the same schema. Evaluating a different code path than the one that
ships would measure the harness.

Two behaviours are not obvious and both cost money when got wrong: the first
ticket of every configuration runs alone so the rest hit a warm cache instead of
all missing it at once, and the sweep carries a spending cap it actually
enforces between batches. Both are explained where they happen.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from app.api.schemas import TriageRequest
from app.core.config import ProviderName, Settings
from app.domain.prompts import DEFAULT_VARIANT, VARIANTS, load_playbook
from app.domain.triage import TriageResult
from app.evals.dataset import EvalTicket
from app.evals.scoring import TicketOutcome
from app.observability.ledger import new_ledger
from app.providers.base import LLMProvider, ProviderError
from app.providers.registry import build_provider
from app.services.triage import triage_ticket


class BudgetExceeded(RuntimeError):
    """The sweep was stopped because it reached its spending cap."""


@dataclass(frozen=True)
class EvalConfig:
    """One point in the sweep: a provider, a model, a prompt, and how it is configured."""

    name: str
    provider: ProviderName
    model: str
    adaptive_thinking: bool = False
    #: Overrides `MAX_OUTPUT_TOKENS` for this configuration only. Needed by the
    #: thinking-enabled run: reasoning tokens count against the same ceiling, so
    #: the default 4096 would truncate part of the set and register as failures
    #: that are really a misconfiguration.
    max_output_tokens: int | None = None
    #: The playbook variant this configuration sends (ADR-009). Phase 4 swept
    #: provider x model with this held fixed; Phase 5 sweeps this with the model
    #: held fixed. One axis at a time, or a difference has two explanations.
    prompt: str = DEFAULT_VARIANT

    def settings_for(self, base: Settings) -> Settings:
        """Derive the settings this configuration runs under.

        `Settings` is a Pydantic model, so a copy with a few fields replaced is
        the whole mechanism — no bespoke configuration object, and the sweep
        exercises exactly the settings a deployment would use.
        """
        update: dict[str, object] = {
            "llm_provider": self.provider,
            "anthropic_adaptive_thinking": self.adaptive_thinking,
            "triage_prompt": self.prompt,
        }
        if self.max_output_tokens is not None:
            update["max_output_tokens"] = self.max_output_tokens
        return base.model_copy(update=update)


@dataclass(frozen=True)
class RunResult:
    """Everything one configuration produced over the dataset."""

    config: EvalConfig
    started_at: datetime
    finished_at: datetime
    split: str | None
    outcomes: tuple[TicketOutcome, ...]

    @property
    def total_cost_usd(self) -> float:
        return sum(outcome.cost_usd for outcome in self.outcomes)


@dataclass
class SpendTracker:
    """Running total, shared across the batches of one sweep."""

    usd: float = 0.0
    cap_usd: float | None = None
    runs: list[str] = field(default_factory=list)

    def check(self) -> None:
        if self.cap_usd is not None and self.usd >= self.cap_usd:
            raise BudgetExceeded(
                f"Stopped after ${self.usd:.4f}, at or over the ${self.cap_usd:.2f} cap. "
                f"Completed: {', '.join(self.runs) or 'nothing'}."
            )


async def run_config(
    config: EvalConfig,
    tickets: Sequence[EvalTicket],
    base_settings: Settings,
    *,
    concurrency: int = 4,
    spend: SpendTracker | None = None,
    split: str | None = None,
) -> RunResult:
    """Triage every ticket under one configuration."""
    if not tickets:
        raise ValueError("Cannot run a configuration over an empty ticket set.")

    settings = config.settings_for(base_settings)
    provider = build_provider(settings, model=config.model)
    tracker = spend if spend is not None else SpendTracker()

    started_at = datetime.now(UTC)
    playbook = load_playbook(config.prompt)

    # The first ticket runs alone, and this is not politeness. Prompt caching is
    # a prefix match against an entry that only becomes readable once the first
    # response has begun streaming — so N concurrent requests with the same
    # playbook all miss, and the sweep pays the 1.25x write premium N times
    # instead of once. One request first, then fan out against a warm cache.
    outcomes: list[TicketOutcome] = [await _triage_one(provider, tickets[0], playbook)]
    tracker.usd += outcomes[0].cost_usd

    for batch in _batches(tickets[1:], concurrency):
        tracker.check()
        results = await asyncio.gather(
            *(_triage_one(provider, ticket, playbook) for ticket in batch)
        )
        outcomes.extend(results)
        tracker.usd += sum(result.cost_usd for result in results)

    tracker.runs.append(config.name)
    return RunResult(
        config=config,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        split=split,
        outcomes=tuple(outcomes),
    )


async def run_sweep(
    configs: Sequence[EvalConfig],
    tickets: Sequence[EvalTicket],
    base_settings: Settings,
    *,
    concurrency: int = 4,
    max_spend_usd: float | None = None,
    split: str | None = None,
) -> list[RunResult]:
    """Run every configuration in turn, stopping if the budget runs out.

    Sequential across configurations on purpose: caches are per model, so
    interleaving them would thrash every prefix and inflate the cost of the
    measurement it is trying to make.
    """
    tracker = SpendTracker(cap_usd=max_spend_usd)
    results: list[RunResult] = []
    for config in configs:
        tracker.check()
        results.append(
            await run_config(
                config,
                tickets,
                base_settings,
                concurrency=concurrency,
                spend=tracker,
                split=split,
            )
        )
    return results


async def _triage_one(provider: LLMProvider, ticket: EvalTicket, playbook: str) -> TicketOutcome:
    """Triage one ticket, recording a failure as data rather than raising.

    One bad ticket must not abandon a sweep that has already been paid for, and
    "this configuration fails 6% of the time" is a finding the report should
    carry rather than a crash.

    Usage is read back from a per-ticket ledger rather than from the returned
    completion, because the two disagree exactly where it matters: OpenAI's
    truncation path records usage on the way to raising, so the ledger sees a
    cost the caller never receives a `usage` for. Anthropic's validation-error
    path records nothing at all — a known gap (ADR-008) that shows up here as a
    failed ticket with a cost of zero, which is honest rather than accurate.
    """
    request = TriageRequest(ticket_id=ticket.id, subject=ticket.subject, body=ticket.body)
    ledger = new_ledger()
    predicted: TriageResult | None = None
    error: str | None = None

    started = time.perf_counter()
    try:
        predicted = (await triage_ticket(provider, request, playbook)).parsed
    except ProviderError as exc:
        error = str(exc)
    latency_s = time.perf_counter() - started

    entry = ledger.entries[0] if ledger.entries else None
    return TicketOutcome(
        ticket_id=ticket.id,
        expected=ticket.expected,
        predicted=predicted,
        error=error,
        latency_s=latency_s,
        usage=entry.usage if entry else None,
        cost_usd=ledger.total_cost_usd,
    )


def _batches(items: Sequence[EvalTicket], size: int) -> list[Sequence[EvalTicket]]:
    """Split into fixed-size batches so the budget can be checked between them."""
    if size < 1:
        raise ValueError("Concurrency must be at least 1.")
    return [items[start : start + size] for start in range(0, len(items), size)]


# --------------------------------------------------------------------------
# The configurations worth comparing
# --------------------------------------------------------------------------
#
# Each entry is a hypothesis, not a permutation. The three Anthropic tiers ask
# what quality costs; `sonnet-thinking` asks whether the reasoning ADR-008
# switched off was worth anything. The OpenAI entries are declared but cannot
# run here — there is no `OPENAI_API_KEY` on the machine this was built on — and
# they stay in the list rather than being deleted so the cross-vendor comparison
# is one credential away rather than one refactor away.
#
# The prompt is pinned to `v1-baseline` on every entry, not left to
# `DEFAULT_VARIANT`. Phase 4 measured the provider axis against the playbook that
# shipped then; when Phase 5 moved the default to the champion, that must not
# retroactively change what the provider comparison was run against. Phase 5
# sweeps the prompt axis separately (`prompt_variants_of`), and those runs set
# the variant explicitly.
_BASELINE_PROMPT = "v1-baseline"

CONFIGS: tuple[EvalConfig, ...] = (
    EvalConfig(
        name="haiku", provider="anthropic", model="claude-haiku-4-5", prompt=_BASELINE_PROMPT
    ),
    EvalConfig(
        name="sonnet", provider="anthropic", model="claude-sonnet-5", prompt=_BASELINE_PROMPT
    ),
    EvalConfig(name="opus", provider="anthropic", model="claude-opus-4-8", prompt=_BASELINE_PROMPT),
    EvalConfig(
        name="sonnet-thinking",
        provider="anthropic",
        model="claude-sonnet-5",
        adaptive_thinking=True,
        # Reasoning tokens are billed against the same ceiling as the answer, so
        # the default 4096 would truncate part of the set — and a truncated JSON
        # raises (ADR-008), which would show up as this configuration "failing"
        # when the real fault is the budget it was given.
        max_output_tokens=8192,
        prompt=_BASELINE_PROMPT,
    ),
    EvalConfig(name="gpt-luna", provider="openai", model="gpt-5.6-luna", prompt=_BASELINE_PROMPT),
    EvalConfig(name="gpt-terra", provider="openai", model="gpt-5.6-terra", prompt=_BASELINE_PROMPT),
    EvalConfig(name="gpt-sol", provider="openai", model="gpt-5.6-sol", prompt=_BASELINE_PROMPT),
)

CONFIGS_BY_NAME: dict[str, EvalConfig] = {config.name: config for config in CONFIGS}

#: What `scripts/run_eval.py` sweeps when told nothing more specific.
DEFAULT_CONFIG_NAMES: tuple[str, ...] = ("haiku", "sonnet", "opus", "sonnet-thinking")

#: The model the prompt sweep holds fixed. `sonnet` without thinking: the
#: cheapest of the configurations worth shipping, and the one with the most room
#: left on severity — a prompt effect measured on a model already near its
#: ceiling is a prompt effect you cannot see. Thinking stays off so the sweep
#: moves one axis.
PROMPT_SWEEP_CONFIG = "sonnet"


def prompt_variants_of(config: EvalConfig, names: Sequence[str]) -> list[EvalConfig]:
    """Expand one configuration into one per prompt variant.

    The names are validated against the registry here rather than at the API
    call: a typo would otherwise run the whole sweep against the default
    playbook and report it under a variant's name, which is worse than an error
    because it looks like a result.
    """
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        raise ValueError(f"Unknown prompt variant(s): {', '.join(unknown)}.")
    return [replace(config, name=f"{config.name}+{name}", prompt=name) for name in names]
