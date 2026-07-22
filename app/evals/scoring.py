"""Turning a run's outcomes into the numbers a decision gets made on.

Two conventions decide what every figure in the report means, and both are
choices rather than facts:

**A failed request counts as wrong.** Failures are in the denominator of every
accuracy figure, not excluded from it. A triage system that raises gives the
caller nothing, and scoring only the requests that succeeded would let a model
that refuses the hard tickets outrank one that attempts them. `failures` is also
reported on its own, because *why* the accuracy is low matters.

**Confidence is graded against the whole classification.** `TriageResult.
confidence` is documented as the model's confidence in *this classification*, so
calibration treats a prediction as correct only when category, severity and
next_action are all right. Grading it against category alone would flatter every
model, since category is the easiest of the three.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.domain.triage import NextAction, TriageResult
from app.evals.dataset import ExpectedLabels
from app.providers.base import Usage

#: Fixed bucket edges for the calibration table. Fixed rather than derived from
#: the data so two runs are comparable: quantile buckets would move between
#: models and the rows would stop lining up.
CONFIDENCE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 0.5),
    (0.5, 0.7),
    (0.7, 0.9),
    (0.9, 1.0),
)


@dataclass(frozen=True)
class TicketOutcome:
    """What happened when one ticket was triaged by one configuration."""

    ticket_id: str
    expected: ExpectedLabels
    predicted: TriageResult | None
    error: str | None
    latency_s: float
    usage: Usage | None
    cost_usd: float

    @property
    def failed(self) -> bool:
        return self.predicted is None

    @property
    def classification_correct(self) -> bool:
        """All three routing decisions right. The bar calibration is graded at."""
        if self.predicted is None:
            return False
        return (
            self.predicted.category == self.expected.category
            and self.predicted.severity == self.expected.severity
            and self.predicted.next_action == self.expected.next_action
        )


@dataclass(frozen=True)
class CalibrationBucket:
    """One row of the calibration table."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float


@dataclass(frozen=True)
class Metrics:
    """Everything the report compares configurations on."""

    tickets: int
    failures: int

    category_accuracy: float
    severity_accuracy: float
    next_action_accuracy: float
    #: All three enum fields right at once — the honest "got the ticket right".
    classification_accuracy: float

    escalation_false_positive_rate: float
    escalation_false_negative_rate: float
    pii_recall: float
    pii_precision: float

    latency_p50_s: float
    latency_p95_s: float

    total_cost_usd: float
    cost_per_1000_tickets_usd: float
    cache_hit_rate: float

    calibration: tuple[CalibrationBucket, ...]
    expected_calibration_error: float


def score(outcomes: Sequence[TicketOutcome]) -> Metrics:
    """Compute every reported metric from one configuration's outcomes."""
    if not outcomes:
        raise ValueError("Cannot score an empty run.")

    total = len(outcomes)
    failures = sum(1 for outcome in outcomes if outcome.failed)

    return Metrics(
        tickets=total,
        failures=failures,
        category_accuracy=_accuracy(outcomes, _category_correct),
        severity_accuracy=_accuracy(outcomes, _severity_correct),
        next_action_accuracy=_accuracy(outcomes, _next_action_correct),
        classification_accuracy=_accuracy(outcomes, lambda o: o.classification_correct),
        escalation_false_positive_rate=_escalation_rate(outcomes, expected_escalation=False),
        escalation_false_negative_rate=_escalation_rate(outcomes, expected_escalation=True),
        pii_recall=_pii_recall(outcomes),
        pii_precision=_pii_precision(outcomes),
        latency_p50_s=percentile([o.latency_s for o in outcomes], 50),
        latency_p95_s=percentile([o.latency_s for o in outcomes], 95),
        total_cost_usd=sum(o.cost_usd for o in outcomes),
        cost_per_1000_tickets_usd=sum(o.cost_usd for o in outcomes) / total * 1000,
        cache_hit_rate=_cache_hit_rate(outcomes),
        calibration=_calibration(outcomes),
        expected_calibration_error=_expected_calibration_error(outcomes),
    )


# -- accuracy ---------------------------------------------------------------


def _category_correct(outcome: TicketOutcome) -> bool:
    return outcome.predicted is not None and outcome.predicted.category == outcome.expected.category


def _severity_correct(outcome: TicketOutcome) -> bool:
    return outcome.predicted is not None and outcome.predicted.severity == outcome.expected.severity


def _next_action_correct(outcome: TicketOutcome) -> bool:
    return (
        outcome.predicted is not None
        and outcome.predicted.next_action == outcome.expected.next_action
    )


def _accuracy(
    outcomes: Sequence[TicketOutcome],
    correct: Callable[[TicketOutcome], bool],
) -> float:
    """Fraction correct over *every* ticket, failures included as wrong."""
    return sum(1 for outcome in outcomes if correct(outcome)) / len(outcomes)


# -- escalation -------------------------------------------------------------


def _escalation_rate(outcomes: Sequence[TicketOutcome], *, expected_escalation: bool) -> float:
    """Rate of getting the escalate/do-not-escalate call wrong, one side at a time.

    A false positive pages a human who was not needed; a false negative leaves a
    critical ticket in a queue. The phase plan asks only for the first — wrongly
    paging is the expensive error — but reporting it alone would let a model that
    never escalates look excellent, so both sides are here.

    A failed request counts against whichever side it belonged to: no verdict is
    not the same as the right verdict.
    """
    relevant = [
        outcome
        for outcome in outcomes
        if (outcome.expected.next_action == NextAction.ESCALATE) is expected_escalation
    ]
    if not relevant:
        return 0.0

    wrong = 0
    for outcome in relevant:
        if outcome.predicted is None:
            wrong += 1
            continue
        predicted_escalation = outcome.predicted.next_action == NextAction.ESCALATE
        if predicted_escalation is not expected_escalation:
            wrong += 1
    return wrong / len(relevant)


# -- PII --------------------------------------------------------------------


def _pii_recall(outcomes: Sequence[TicketOutcome]) -> float:
    """Of the tickets that do contain PII, how many were flagged.

    The metric that matters most of the two: a missed flag means personal data
    is forwarded or written to a log unredacted, which is a compliance incident
    rather than a quality problem.
    """
    with_pii = [o for o in outcomes if o.expected.contains_pii]
    if not with_pii:
        return 0.0
    return sum(1 for o in with_pii if o.predicted is not None and o.predicted.contains_pii) / len(
        with_pii
    )


def _pii_precision(outcomes: Sequence[TicketOutcome]) -> float:
    """Of the tickets flagged, how many really contain PII.

    Reported because recall alone is trivially maxed by flagging everything —
    and a redactor that fires on every ticket gets switched off within a week.
    """
    flagged = [o for o in outcomes if o.predicted is not None and o.predicted.contains_pii]
    if not flagged:
        return 0.0
    return sum(1 for o in flagged if o.expected.contains_pii) / len(flagged)


# -- latency, cost, cache ---------------------------------------------------


def percentile(values: Sequence[float], rank: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than an interpolating definition because the result is
    always an observed measurement. At n=55 an interpolated p95 is a number that
    never happened, which is a strange thing to put in a latency report.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = math.ceil(rank / 100 * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def _cache_hit_rate(outcomes: Sequence[TicketOutcome]) -> float:
    """Fraction of prompt tokens served from cache across the whole run."""
    prompt_tokens = sum(o.usage.total_prompt_tokens for o in outcomes if o.usage)
    if not prompt_tokens:
        return 0.0
    return sum(o.usage.cache_read_tokens for o in outcomes if o.usage) / prompt_tokens


# -- calibration ------------------------------------------------------------


def _scored_predictions(
    outcomes: Sequence[TicketOutcome],
) -> list[tuple[TicketOutcome, TriageResult]]:
    """Outcomes paired with the prediction they carry.

    Failures are excluded here and only here: a request that raised has no
    confidence to be calibrated against. Counting it as an overconfident wrong
    answer would invent a number the model never produced.
    """
    return [(o, o.predicted) for o in outcomes if o.predicted is not None]


def _calibration(outcomes: Sequence[TicketOutcome]) -> tuple[CalibrationBucket, ...]:
    """Accuracy per confidence band — is low confidence actually a warning?"""
    scored = _scored_predictions(outcomes)
    buckets: list[CalibrationBucket] = []
    for lower, upper in CONFIDENCE_BUCKETS:
        members = [pair for pair in scored if _in_bucket(pair[1].confidence, lower, upper)]
        if not members:
            buckets.append(CalibrationBucket(lower, upper, 0, 0.0, 0.0))
            continue
        buckets.append(
            CalibrationBucket(
                lower=lower,
                upper=upper,
                count=len(members),
                mean_confidence=sum(p.confidence for _, p in members) / len(members),
                accuracy=sum(1 for o, _ in members if o.classification_correct) / len(members),
            )
        )
    return tuple(buckets)


def _in_bucket(confidence: float, lower: float, upper: float) -> bool:
    """Half-open bands, except the last one, which has to include 1.0."""
    if upper >= 1.0:
        return lower <= confidence <= upper
    return lower <= confidence < upper


def _expected_calibration_error(outcomes: Sequence[TicketOutcome]) -> float:
    """Weighted average gap between stated confidence and observed accuracy.

    0 is perfect calibration. A model at 0.95 confidence that is right 60% of the
    time scores badly here even if its accuracy is respectable — which is the
    point: downstream automation gates on this number, so a model that does not
    know when it is guessing is dangerous in a way accuracy alone does not show.
    """
    scored = _scored_predictions(outcomes)
    if not scored:
        return 0.0
    return sum(
        bucket.count / len(scored) * abs(bucket.accuracy - bucket.mean_confidence)
        for bucket in _calibration(outcomes)
        if bucket.count
    )
