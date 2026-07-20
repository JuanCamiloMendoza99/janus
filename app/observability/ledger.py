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
from dataclasses import dataclass, field

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
        raise NotImplementedError("Phase 1")

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    @property
    def call_count(self) -> int:
        return len(self.entries)

    def summary(self) -> dict[str, object]:
        """Flatten to the shape emitted in the per-request cost log line."""
        raise NotImplementedError("Phase 1")


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
