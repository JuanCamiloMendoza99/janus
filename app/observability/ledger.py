"""Per-request usage accounting.

The problem this solves: an HTTP middleware cannot see token counts. Tokens live
in the provider's response, and on a streamed response they arrive *after* the
response has already started — in the trailing usage event. A middleware that
reads them when the response object is created reads zero.

The design (ADR-004):

1. A `UsageLedger` is created per request and stashed in a `ContextVar`.
2. Provider adapters call `record()` as calls complete. One HTTP request can
   produce several model calls — the tool loop makes at least two — so the
   ledger accumulates rather than holding a single value.
3. The cost middleware reads the ledger when the response finishes, which for
   SSE means *after the generator is exhausted*, not when it is returned.

A `ContextVar` rather than passing a ledger argument down through every layer:
adapters are several calls below the router, and threading an accounting object
through the domain signatures would put observability in the business types.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.pricing import compute_cost_usd
from app.providers.base import Usage

_ledger_var: contextvars.ContextVar[UsageLedger | None] = contextvars.ContextVar(
    "janus_usage_ledger", default=None
)


@dataclass
class LedgerEntry:
    """One model call and what it cost."""

    provider: str
    usage: Usage
    cost_usd: float


@dataclass
class UsageLedger:
    """Accumulates every model call made while serving one HTTP request."""

    entries: list[LedgerEntry] = field(default_factory=list)

    def record(self, provider: str, usage: Usage) -> None:
        """Append a completed model call, pricing it as it lands."""
        cost = compute_cost_usd(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        self.entries.append(LedgerEntry(provider=provider, usage=usage, cost_usd=cost))

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    @property
    def call_count(self) -> int:
        return len(self.entries)

    def summary(self) -> dict[str, object]:
        """Flatten to the shape emitted in the per-request cost log line."""
        return {
            "calls": self.call_count,
            "cost_usd": round(self.total_cost_usd, 6),
            "input_tokens": sum(e.usage.input_tokens for e in self.entries),
            "output_tokens": sum(e.usage.output_tokens for e in self.entries),
            "cache_read_tokens": sum(e.usage.cache_read_tokens for e in self.entries),
            "cache_write_tokens": sum(e.usage.cache_write_tokens for e in self.entries),
            "models": sorted({e.usage.model for e in self.entries}),
            "providers": sorted({e.provider for e in self.entries}),
        }


def new_ledger() -> UsageLedger:
    """Install a fresh ledger for the current context and return it."""
    ledger = UsageLedger()
    _ledger_var.set(ledger)
    return ledger


def current_ledger() -> UsageLedger | None:
    """Return the ledger for the current request, if one is installed.

    Returns `None` outside a request (scripts, tests calling a provider
    directly). Adapters must tolerate that instead of assuming a ledger exists —
    accounting is not allowed to break the call path it is measuring.
    """
    return _ledger_var.get()


# --------------------------------------------------------------------------
# Process-wide aggregate
# --------------------------------------------------------------------------
#
# The per-request ledger above answers "what did *this* request cost". `GET
# /v1/usage` needs "what has this process cost since it started", so the
# middleware folds each finished request's ledger into this in-memory store. It
# resets on restart — persisting spend is a database concern the project
# deliberately stays out of, and that limitation is stated in the README.


@dataclass(frozen=True)
class UsageSnapshot:
    """An immutable read of the process-wide totals."""

    since: datetime
    requests: int
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_prompt_tokens: int
    by_model: dict[str, float]

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from cache across all requests.

        The honest measure of whether prompt caching is doing anything: a marker
        that was accepted but never hit leaves this at 0.
        """
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.total_cache_read_tokens / self.total_prompt_tokens


class UsageStore:
    """Thread-safe accumulator of every request served since process start."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Zero every total. Used at construction and by tests for isolation."""
        with self._lock:
            self._started_at = datetime.now(UTC)
            self._requests = 0
            self._input_tokens = 0
            self._output_tokens = 0
            self._cache_read_tokens = 0
            self._prompt_tokens = 0
            self._cost_usd = 0.0
            self._by_model: dict[str, float] = {}

    def record_request(self, ledger: UsageLedger) -> None:
        """Fold one finished request's ledger into the running totals."""
        with self._lock:
            self._requests += 1
            for entry in ledger.entries:
                usage = entry.usage
                self._input_tokens += usage.input_tokens
                self._output_tokens += usage.output_tokens
                self._cache_read_tokens += usage.cache_read_tokens
                self._prompt_tokens += usage.total_prompt_tokens
                self._cost_usd += entry.cost_usd
                self._by_model[usage.model] = self._by_model.get(usage.model, 0.0) + entry.cost_usd

    def snapshot(self) -> UsageSnapshot:
        """Return a consistent read of the current totals."""
        with self._lock:
            return UsageSnapshot(
                since=self._started_at,
                requests=self._requests,
                total_cost_usd=self._cost_usd,
                total_input_tokens=self._input_tokens,
                total_output_tokens=self._output_tokens,
                total_cache_read_tokens=self._cache_read_tokens,
                total_prompt_tokens=self._prompt_tokens,
                by_model=dict(self._by_model),
            )


#: Process-wide singleton the cost middleware writes and `/v1/usage` reads.
usage_store = UsageStore()
