"""Tool: escalate a ticket.

The write-side counterpart to `search_kb`. Having one read tool and one write
tool is deliberate — they exercise different halves of the tool loop. A read
tool tolerates being called speculatively; a write tool does not, which is what
forces the loop to deal with idempotency and confirmation.
"""

from __future__ import annotations

from typing import Any

from app.providers.base import ToolSpec

SPEC = ToolSpec(
    name="escalate_ticket",
    description=(
        "Escalate a ticket to a human on-call responder. Call this when the "
        "situation is critical, involves data exposure or incorrect charges, or "
        "when an angry customer has a high-severity problem. Escalating is a "
        "real action with a real pager attached — do not call it speculatively."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticket_id": {
                "type": "string",
                "description": "Identifier of the ticket being escalated.",
            },
            "reason": {
                "type": "string",
                "description": "Why this needs a human now, in one or two sentences.",
            },
            "severity": {
                "type": "string",
                "enum": ["high", "critical"],
                "description": "Only high or critical tickets may be escalated.",
            },
        },
        "required": ["ticket_id", "reason", "severity"],
        "additionalProperties": False,
    },
)


async def escalate_ticket(ticket_id: str, reason: str, severity: str) -> dict[str, Any]:
    """Record an escalation and return its confirmation."""
    raise NotImplementedError("Phase 2")
