"""The evaluation harness, tested without spending anything.

The metric arithmetic is the part of this phase most likely to be quietly wrong,
and a wrong metric is worse than no metric: it produces a confident
recommendation out of nothing, and nothing about the output looks suspicious.
So the numbers are checked against hand-computed answers on tiny sets where the
right value can be read off by eye.

The dataset gets its own assertions for the same reason. A golden set that
silently drifts into being all easy tickets still reports 95% accuracy for every
model and tells you nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.domain.triage import Category, NextAction, Sentiment, Severity, TriageResult
from app.evals.dataset import (
    DatasetError,
    EvalTicket,
    ExpectedLabels,
    load_tickets,
    select,
)
from app.evals.results import SCHEMA_VERSION, dump_runs, load_runs
from app.evals.runner import (
    CONFIGS_BY_NAME,
    BudgetExceeded,
    prompt_variants_of,
    run_config,
    run_sweep,
)
from app.evals.scoring import TicketOutcome, percentile, score
from app.observability.ledger import current_ledger
from app.providers.base import ParsedCompletion, Prompt, ProviderError, Usage

# -- the dataset ------------------------------------------------------------


@pytest.fixture(scope="module")
def tickets() -> tuple[EvalTicket, ...]:
    return load_tickets()


def test_the_dataset_loads_and_ids_are_unique(tickets: tuple[EvalTicket, ...]) -> None:
    assert len(tickets) >= 40
    assert len({ticket.id for ticket in tickets}) == len(tickets)


def test_both_splits_are_populated(tickets: tuple[EvalTicket, ...]) -> None:
    """The hold-out slice ships with the dataset, not with the results.

    Phase 5 tunes prompts on `train` and reports on `holdout`. A split created
    after seeing the numbers is not a hold-out, it is a selection.
    """
    train = select(tickets, "train")
    holdout = select(tickets, "holdout")

    assert len(train) + len(holdout) == len(tickets)
    assert len(holdout) >= 10
    # Not just the leftovers: the hard cases have to be represented on both
    # sides, or the hold-out is an easier exam than the one used for tuning.
    assert sum(1 for ticket in holdout if ticket.hard_case) >= 5


def test_no_ticket_expects_auto_reply(tickets: tuple[EvalTicket, ...]) -> None:
    """`/v1/triage` sends no tools, and the playbook forbids auto_reply without them.

    So a model choosing it is always wrong here. Pinned because it is an easy
    thing to break by adding a "simple question" ticket and labelling it the way
    it would be labelled on the chat path.
    """
    assert all(ticket.expected.next_action != NextAction.AUTO_REPLY for ticket in tickets)


@pytest.mark.parametrize(
    "hard_case",
    [
        "angry_but_trivial",
        "calm_but_critical",
        "feature_request_labelled_bug",
        "duplicate_charge_not_critical",
        "pii_buried_in_pasted_log",
        "sender_signature_not_pii",
        "cross_tenant_exposure",
        "unactionable",
        "escalation_requested_but_unwarranted",
        "customer_side_misconfiguration",
        "leaked_credential",
        "duplicate_of_live_incident",
        "multiple_problems_one_ticket",
        "legal_consequence_escalates",
    ],
)
def test_the_documented_hard_cases_are_present(
    tickets: tuple[EvalTicket, ...], hard_case: str
) -> None:
    """The phase plan names these boundaries; the dataset has to actually probe them."""
    assert any(ticket.hard_case == hard_case for ticket in tickets)


def test_every_ticket_cites_the_rule_that_decides_it(tickets: tuple[EvalTicket, ...]) -> None:
    """What makes a model-drafted dataset auditable rather than circular.

    The label is not "what the author thought"; it is what the playbook says.
    A human can check any row by reading `app/domain/prompts/playbook.md`.
    """
    assert all(len(ticket.rule) > 20 for ticket in tickets)


def test_a_malformed_dataset_raises_rather_than_skipping(tmp_path: Path) -> None:
    """A silently dropped ticket changes every denominator and nothing notices."""
    bad = tmp_path / "tickets.jsonl"
    bad.write_text('{"id": "T-1", "split": "train"}\n', encoding="utf-8")

    with pytest.raises(DatasetError, match="not a valid ticket"):
        load_tickets(bad)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    line = (
        '{"id": "T-1", "split": "train", "subject": "s", "body": "b", "rule": "r", '
        '"expected": {"category": "billing", "severity": "low", '
        '"next_action": "close", "contains_pii": false}}'
    )
    path = tmp_path / "tickets.jsonl"
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="Duplicate"):
        load_tickets(path)


# -- scoring ----------------------------------------------------------------


def _expected(
    category: Category = Category.BILLING,
    severity: Severity = Severity.HIGH,
    next_action: NextAction = NextAction.ROUTE_TO_HUMAN,
    contains_pii: bool = False,
) -> ExpectedLabels:
    return ExpectedLabels(
        category=category,
        severity=severity,
        next_action=next_action,
        contains_pii=contains_pii,
    )


def _predicted(
    category: Category = Category.BILLING,
    severity: Severity = Severity.HIGH,
    next_action: NextAction = NextAction.ROUTE_TO_HUMAN,
    contains_pii: bool = False,
    confidence: float = 0.9,
) -> TriageResult:
    return TriageResult(
        category=category,
        severity=severity,
        sentiment=Sentiment.NEUTRAL,
        next_action=next_action,
        summary="s",
        contains_pii=contains_pii,
        confidence=confidence,
        reasoning="r",
    )


def _outcome(
    expected: ExpectedLabels,
    predicted: TriageResult | None = None,
    *,
    latency_s: float = 1.0,
    cost_usd: float = 0.01,
    usage: Usage | None = None,
) -> TicketOutcome:
    return TicketOutcome(
        ticket_id="T-x",
        expected=expected,
        predicted=predicted,
        error=None if predicted else "boom",
        latency_s=latency_s,
        usage=usage,
        cost_usd=cost_usd,
    )


def test_a_failed_request_counts_as_wrong_not_as_absent() -> None:
    """Otherwise a model that refuses the hard tickets outranks one that tries.

    Three tickets, one correct prediction, one wrong, one failure: 1/3, not 1/2.
    """
    outcomes = [
        _outcome(_expected(), _predicted()),
        _outcome(_expected(), _predicted(category=Category.TECHNICAL)),
        _outcome(_expected(), None),
    ]

    metrics = score(outcomes)

    assert metrics.category_accuracy == pytest.approx(1 / 3)
    assert metrics.failures == 1
    assert metrics.tickets == 3


def test_classification_accuracy_needs_all_three_fields() -> None:
    outcomes = [
        _outcome(_expected(), _predicted()),
        # Category right, severity wrong: counts for category, not for the whole.
        _outcome(_expected(), _predicted(severity=Severity.LOW)),
    ]

    metrics = score(outcomes)

    assert metrics.category_accuracy == 1.0
    assert metrics.classification_accuracy == 0.5


def test_escalation_errors_are_reported_from_both_sides() -> None:
    """Reporting only false positives would flatter a model that never escalates."""
    outcomes = [
        # Should not escalate, did: a false positive.
        _outcome(
            _expected(next_action=NextAction.ROUTE_TO_HUMAN),
            _predicted(next_action=NextAction.ESCALATE),
        ),
        # Should not escalate, did not.
        _outcome(_expected(next_action=NextAction.ROUTE_TO_HUMAN), _predicted()),
        # Should escalate, did not: a false negative.
        _outcome(_expected(next_action=NextAction.ESCALATE), _predicted()),
        # Should escalate, did.
        _outcome(
            _expected(next_action=NextAction.ESCALATE), _predicted(next_action=NextAction.ESCALATE)
        ),
    ]

    metrics = score(outcomes)

    assert metrics.escalation_false_positive_rate == 0.5
    assert metrics.escalation_false_negative_rate == 0.5


def test_a_failure_counts_against_the_side_it_belonged_to() -> None:
    """No verdict is not the same as the right verdict."""
    outcomes = [_outcome(_expected(next_action=NextAction.ESCALATE), None)]

    assert score(outcomes).escalation_false_negative_rate == 1.0


def test_pii_recall_and_precision_are_reported_together() -> None:
    """Recall alone is maxed by flagging everything, which gets the redactor switched off."""
    outcomes = [
        _outcome(_expected(contains_pii=True), _predicted(contains_pii=True)),
        _outcome(_expected(contains_pii=True), _predicted(contains_pii=False)),
        _outcome(_expected(contains_pii=False), _predicted(contains_pii=True)),
        _outcome(_expected(contains_pii=False), _predicted(contains_pii=False)),
    ]

    metrics = score(outcomes)

    assert metrics.pii_recall == 0.5
    assert metrics.pii_precision == 0.5


def test_metrics_survive_a_set_with_no_pii_and_no_escalations() -> None:
    """The degenerate slice must not divide by zero."""
    outcomes = [
        _outcome(_expected(next_action=NextAction.CLOSE), _predicted(next_action=NextAction.CLOSE))
    ]

    metrics = score(outcomes)

    assert metrics.pii_recall == 0.0
    assert metrics.pii_precision == 0.0
    assert metrics.escalation_false_negative_rate == 0.0


def test_scoring_an_empty_run_is_an_error() -> None:
    with pytest.raises(ValueError, match="empty run"):
        score([])


def test_cost_is_projected_per_thousand_tickets() -> None:
    outcomes = [_outcome(_expected(), _predicted(), cost_usd=0.005) for _ in range(4)]

    metrics = score(outcomes)

    assert metrics.total_cost_usd == pytest.approx(0.02)
    assert metrics.cost_per_1000_tickets_usd == pytest.approx(5.0)


def test_cache_hit_rate_is_a_share_of_prompt_tokens() -> None:
    usage = Usage(model="m", input_tokens=100, output_tokens=10, cache_read_tokens=900)
    outcomes = [_outcome(_expected(), _predicted(), usage=usage)]

    assert score(outcomes).cache_hit_rate == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("rank", "expected"),
    [(50, 3), (95, 5), (100, 5), (0, 1)],
)
def test_percentiles_are_observed_values(rank: float, expected: float) -> None:
    """Nearest-rank: a p95 latency in a report should be a measurement that happened."""
    assert percentile([5, 1, 3, 2, 4], rank) == expected


def test_percentile_of_nothing_is_zero() -> None:
    assert percentile([], 95) == 0.0


def test_calibration_shows_whether_low_confidence_warns() -> None:
    outcomes = [
        _outcome(_expected(), _predicted(confidence=0.95)),
        _outcome(_expected(), _predicted(confidence=0.95)),
        _outcome(_expected(), _predicted(category=Category.OTHER, confidence=0.3)),
    ]

    metrics = score(outcomes)
    top = next(b for b in metrics.calibration if b.lower == 0.9)
    bottom = next(b for b in metrics.calibration if b.lower == 0.0)

    assert (top.count, top.accuracy) == (2, 1.0)
    assert (bottom.count, bottom.accuracy) == (1, 0.0)
    # Confident and right, unconfident and wrong: well calibrated on both rows,
    # so the weighted gap is small.
    assert metrics.expected_calibration_error == pytest.approx(2 / 3 * 0.05 + 1 / 3 * 0.3)


def test_a_confidently_wrong_model_scores_badly_on_calibration() -> None:
    outcomes = [_outcome(_expected(), _predicted(category=Category.OTHER, confidence=0.99))]

    assert score(outcomes).expected_calibration_error == pytest.approx(0.99)


def test_failures_are_excluded_from_calibration_only() -> None:
    """A request that raised has no confidence; inventing one would be fiction."""
    outcomes = [
        _outcome(_expected(), _predicted(confidence=0.95)),
        _outcome(_expected(), None),
    ]

    metrics = score(outcomes)

    assert sum(bucket.count for bucket in metrics.calibration) == 1
    assert metrics.category_accuracy == 0.5


# -- the runner -------------------------------------------------------------


class StubProvider:
    """Answers `parse()` from a script, and can be told to fail.

    Records to the request ledger exactly as the real adapters do. That is not
    decoration: the runner reads a ticket's cost back from the ledger rather than
    from the returned completion, so a stub that skipped it would report every
    sweep as free and quietly disable the budget guard under test.
    """

    name = "stub"
    model = "claude-sonnet-5"

    def __init__(self, *, fail_every: int | None = None) -> None:
        self.fail_every = fail_every
        self.calls = 0

    async def parse[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> ParsedCompletion[T]:
        self.calls += 1
        usage = Usage(model=self.model, input_tokens=50, output_tokens=200, cache_read_tokens=7929)
        ledger = current_ledger()
        if ledger is not None:
            ledger.record(self.name, usage)
        if self.fail_every and self.calls % self.fail_every == 0:
            raise ProviderError(message="stub failure", provider=self.name)
        return ParsedCompletion(parsed=_predicted(), usage=usage)


@pytest.fixture
def settings() -> Settings:
    return Settings(llm_provider="fake", environment="ci")


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubProvider:
    """Substitute the provider the runner builds.

    Patched rather than injected: `run_config` going through `build_provider()`
    is the point — it is what keeps the runner from knowing which vendor it is
    driving — and adding a factory parameter purely for tests would put a seam
    in production code that production never uses.
    """
    provider = StubProvider()
    monkeypatch.setattr("app.evals.runner.build_provider", lambda *a, **k: provider)
    return provider


def test_the_runner_triages_every_ticket(settings: Settings, stub: StubProvider) -> None:
    tickets = load_tickets()[:9]

    result = asyncio.run(run_config(CONFIGS_BY_NAME["sonnet"], tickets, settings, concurrency=4))

    assert len(result.outcomes) == len(tickets)
    assert stub.calls == len(tickets)
    assert [o.ticket_id for o in result.outcomes] == [t.id for t in tickets]


def test_a_failing_ticket_is_recorded_rather_than_abandoning_the_sweep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep already paid for must not be thrown away by one bad ticket."""
    provider = StubProvider(fail_every=2)
    monkeypatch.setattr("app.evals.runner.build_provider", lambda *a, **k: provider)
    tickets = load_tickets()[:6]

    result = asyncio.run(run_config(CONFIGS_BY_NAME["sonnet"], tickets, settings, concurrency=2))

    metrics = score(result.outcomes)
    assert metrics.failures == 3
    assert all(o.error == "[stub] stub failure" for o in result.outcomes if o.failed)


def test_the_budget_cap_stops_the_sweep(settings: Settings, stub: StubProvider) -> None:
    """A guard that is enforced between batches, not a warning in a docstring.

    The cap is below the cost of a single ticket, so the warm-up ticket runs and
    the next batch is refused. Some spending before the stop is inherent: the
    cap is checked against what has been spent, which can only be known after
    spending it.
    """
    with pytest.raises(BudgetExceeded, match=r"cap"):
        asyncio.run(
            run_sweep(
                [CONFIGS_BY_NAME["sonnet"]],
                load_tickets()[:8],
                settings,
                concurrency=2,
                max_spend_usd=0.0001,
            )
        )

    assert stub.calls < 8


def test_the_budget_is_shared_across_configurations(settings: Settings, stub: StubProvider) -> None:
    """A per-configuration cap would let a four-config sweep spend four times it."""
    with pytest.raises(BudgetExceeded):
        asyncio.run(
            run_sweep(
                [CONFIGS_BY_NAME["haiku"], CONFIGS_BY_NAME["sonnet"], CONFIGS_BY_NAME["opus"]],
                load_tickets()[:2],
                settings,
                max_spend_usd=0.005,
            )
        )


def test_a_configuration_derives_its_settings_from_the_base(settings: Settings) -> None:
    """No bespoke config object: the sweep runs the settings a deployment would."""
    derived = CONFIGS_BY_NAME["sonnet-thinking"].settings_for(settings)

    assert derived.llm_provider == "anthropic"
    assert derived.anthropic_adaptive_thinking is True
    # Reasoning tokens share the ceiling with the answer, so this run needs more.
    assert derived.max_output_tokens == 8192
    # Everything not named is inherited, not reset.
    assert derived.environment == settings.environment


def test_the_default_configuration_leaves_thinking_off(settings: Settings) -> None:
    assert CONFIGS_BY_NAME["haiku"].settings_for(settings).anthropic_adaptive_thinking is False


def test_a_configuration_carries_its_prompt_into_settings(settings: Settings) -> None:
    """The prompt variant is a swept axis, so it has to reach the settings the run uses."""
    config = CONFIGS_BY_NAME["sonnet"]
    assert config.settings_for(settings).triage_prompt == config.prompt


def test_prompt_variants_expand_one_config_into_one_run_each() -> None:
    """The Phase 5 sweep crosses a fixed model with the prompt axis."""
    expanded = prompt_variants_of(CONFIGS_BY_NAME["sonnet"], ["v1-baseline", "v3-terse"])

    assert [c.name for c in expanded] == ["sonnet+v1-baseline", "sonnet+v3-terse"]
    assert [c.prompt for c in expanded] == ["v1-baseline", "v3-terse"]
    # The model and everything else about the configuration is held fixed.
    assert {c.model for c in expanded} == {CONFIGS_BY_NAME["sonnet"].model}


def test_an_unknown_prompt_variant_is_rejected() -> None:
    """A typo must fail loudly, not run the default under a variant's name."""
    with pytest.raises(ValueError, match="nonexistent"):
        prompt_variants_of(CONFIGS_BY_NAME["sonnet"], ["nonexistent"])


def test_running_no_tickets_is_an_error(settings: Settings, stub: StubProvider) -> None:
    with pytest.raises(ValueError, match="empty ticket set"):
        asyncio.run(run_config(CONFIGS_BY_NAME["sonnet"], [], settings))


# -- persistence ------------------------------------------------------------


def test_results_survive_a_round_trip(
    settings: Settings, stub: StubProvider, tmp_path: Path
) -> None:
    """The raw outcomes are committed so the report can be recomputed, not believed."""
    result = asyncio.run(run_config(CONFIGS_BY_NAME["sonnet"], load_tickets()[:3], settings))
    path = tmp_path / "results.json"

    dump_runs([result], path)
    restored = load_runs(path)[0]

    assert restored.config == result.config
    assert restored.started_at == result.started_at
    assert [o.ticket_id for o in restored.outcomes] == [o.ticket_id for o in result.outcomes]
    assert restored.outcomes[0].predicted == result.outcomes[0].predicted
    assert restored.outcomes[0].usage == result.outcomes[0].usage
    assert score(restored.outcomes) == score(result.outcomes)


def test_a_results_file_from_another_schema_is_refused(tmp_path: Path) -> None:
    """Better than being silently misread into a plausible report."""
    path = tmp_path / "results.json"
    path.write_text(f'{{"schema_version": {SCHEMA_VERSION + 1}, "runs": []}}', encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_runs(path)


def test_every_declared_configuration_is_priced() -> None:
    """A model with no pricing entry would abort a paid sweep at the first response.

    `compute_cost_usd()` raises for an unknown model — correctly, since a silent
    0.0 would make the whole comparison wrong — but discovering that after the
    first API call has already been billed is an avoidable way to find out.
    """
    from app.core.pricing import get_pricing

    for config in CONFIGS_BY_NAME.values():
        assert get_pricing(config.model) is not None
