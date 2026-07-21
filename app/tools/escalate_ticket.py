"""Tool: escalate a ticket.

The write-side counterpart to `search_kb`. Having one read tool and one write
tool is deliberate — they exercise different halves of the tool loop. A read
tool tolerates being called speculatively; a write tool does not, which is what
forces the loop to deal with idempotency and confirmation.

Idempotency is not decoration here. A model that loses a stream mid-turn, or
simply repeats itself, will call this twice for the same ticket; in the fiction
there is a pager on the other end. Escalations are therefore keyed by ticket id,
and a repeat returns the original confirmation instead of paging again.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import ToolSpec
from app.tools.schema import json_schema_for

Severity = Literal["high", "critical"]


class EscalateTicketArgs(BaseModel):
    """Arguments accepted by `escalate_ticket`."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(
        min_length=1,
        description="Identifier of the ticket being escalated.",
    )
    reason: str = Field(
        min_length=1,
        description="Why this needs a human now, in one or two sentences.",
    )
    severity: Severity = Field(
        description="Only high or critical tickets may be escalated.",
    )


SPEC = ToolSpec(
    name="escalate_ticket",
    description=(
        "Escalate a ticket to a human on-call responder. Call this when the "
        "situation is critical, involves data exposure or incorrect charges, or "
        "when an angry customer has a high-severity problem. Escalating is a "
        "real action with a real pager attached — do not call it speculatively."
    ),
    parameters=json_schema_for(EscalateTicketArgs),
)


@dataclass(frozen=True)
class Escalation:
    """One recorded escalation."""

    escalation_id: str
    ticket_id: str
    reason: str
    severity: str
    created_at: datetime


class EscalationLog:
    """In-process record of everything this tool has escalated.

    In-process because persistence is a database concern the project stays out
    of; the log resets on restart and that limitation is stated in the README.
    Locked because Uvicorn serves requests concurrently and two tickets
    escalating at once must not collide on the counter.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Drop every escalation. Used at construction and by tests."""
        with self._lock:
            self._entries: list[Escalation] = []
            self._by_ticket: dict[str, Escalation] = {}

    @property
    def entries(self) -> tuple[Escalation, ...]:
        with self._lock:
            return tuple(self._entries)

    def record(self, ticket_id: str, reason: str, severity: str) -> tuple[Escalation, bool]:
        """Escalate `ticket_id`, or return the existing escalation for it.

        Returns `(escalation, is_new)`. The caller reports `is_new` back to the
        model so a duplicate call reads as "already handled" rather than as a
        second page.
        """
        with self._lock:
            existing = self._by_ticket.get(ticket_id)
            if existing is not None:
                return existing, False
            escalation = Escalation(
                escalation_id=f"ESC-{len(self._entries) + 1:04d}",
                ticket_id=ticket_id,
                reason=reason,
                severity=severity,
                created_at=datetime.now(UTC),
            )
            self._entries.append(escalation)
            self._by_ticket[ticket_id] = escalation
            return escalation, True


#: Process-wide log the tool writes to.
escalation_log = EscalationLog()


async def escalate_ticket(ticket_id: str, reason: str, severity: str) -> dict[str, Any]:
    """Record an escalation and return its confirmation."""
    escalation, is_new = escalation_log.record(ticket_id, reason, severity)
    return {
        "escalation_id": escalation.escalation_id,
        "ticket_id": escalation.ticket_id,
        "severity": escalation.severity,
        "already_escalated": not is_new,
        "status": "paged on-call" if is_new else "already escalated, on-call not paged again",
    }
