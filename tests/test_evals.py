"""The golden dataset.

A golden set that silently drifts into being all easy tickets still reports 95%
accuracy for every model and tells you nothing, so the hard cases the phase plan
names are asserted to be present rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.triage import NextAction
from app.evals.dataset import (
    DatasetError,
    EvalTicket,
    load_tickets,
    select,
)

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
