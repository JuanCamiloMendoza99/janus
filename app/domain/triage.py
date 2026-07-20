"""The triage domain model.

This is the schema the model is constrained to produce. It is not a
post-hoc parse of free text: both providers support native schema-constrained
decoding, so this class *is* the output contract.

Design rule: every field is something a downstream system would act on. A
field nothing consumes is a field the model spends tokens guessing at.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    """Ticket taxonomy.

    Closed set on purpose. A free-text category cannot be routed, counted or
    alerted on, and the model will happily invent a new one every time.
    """

    BILLING = "billing"
    TECHNICAL = "technical"
    ACCOUNT_ACCESS = "account_access"
    FEATURE_REQUEST = "feature_request"
    COMPLAINT = "complaint"
    OTHER = "other"


class Severity(StrEnum):
    """Operational urgency, not customer mood — those are graded separately."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Sentiment(StrEnum):
    """Customer mood, kept independent of `Severity`.

    A calm message can describe an outage and a furious one can be a typo
    report. Collapsing the two loses the distinction that makes routing useful.
    """

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    ANGRY = "angry"


class NextAction(StrEnum):
    """What the system should do next."""

    AUTO_REPLY = "auto_reply"
    ROUTE_TO_HUMAN = "route_to_human"
    ESCALATE = "escalate"
    REQUEST_MORE_INFO = "request_more_info"
    CLOSE = "close"


class TriageResult(BaseModel):
    """Structured verdict for one support ticket."""

    category: Category
    severity: Severity
    sentiment: Sentiment
    next_action: NextAction

    summary: str = Field(
        description="One sentence a human can triage from without opening the ticket.",
        max_length=280,
    )

    contains_pii: bool = Field(
        description=(
            "Whether the ticket body contains personal data (emails, phone numbers, "
            "card numbers, national ids). Drives redaction before the ticket is "
            "forwarded or logged."
        ),
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "The model's own confidence in this classification. Used to gate "
            "automation: low-confidence results go to a human regardless of "
            "next_action."
        ),
    )

    reasoning: str = Field(
        description="Why this classification — shown to the agent who picks the ticket up.",
        max_length=1000,
    )
