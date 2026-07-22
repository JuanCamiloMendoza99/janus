"""The golden dataset: loading and validating hand-labelled tickets.

`evals/tickets.jsonl` holds one ticket per line with the labels a triage system
can be graded against objectively. Two fields do not appear in it, deliberately:

* **`sentiment`** — too subjective to label defensibly. Two reasonable people
  disagree on where `negative` ends and `angry` begins, and a metric built on a
  coin flip measures the coin.
* **`summary` and `reasoning`** — free text, not exact-matchable. Grading those
  needs a judge, which is Phase 5's problem.

`auto_reply` never appears as an expected `next_action`. The playbook only
permits it when `search_kb` returned a matching article, and `/v1/triage` sends
no tools — so a model choosing it is always wrong on this dataset, and that is
itself a thing worth measuring.

**Provenance, stated plainly.** These tickets and their labels were drafted by
an LLM, which the phase plan warned against: a dataset a model produced measures
agreement with that model rather than correctness. Two things blunt that, and
neither removes it:

1. The labels are derived from the written rules in
   `app/domain/prompts/playbook.md`, and each ticket records the governing
   clause in `rule`. The metric is therefore "does the model apply the
   documented policy", which a human can audit by reading the playbook rather
   than by trusting the author.
2. Roughly two thirds of the tickets are adversarial by construction — the
   correct label contradicts the obvious reading. Those are tagged in
   `hard_case`.

The residual bias is real and is restated in the report: tickets written by a
Claude-family model may suit Claude-family models. Read any cross-vendor
comparison with that in mind.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.triage import Category, NextAction, Severity

#: Repo-root-relative, because the dataset is development tooling rather than
#: something the packaged application serves. `pyproject.toml` ships `app*`
#: only, so this path resolves in a source checkout and not from a wheel — which
#: is correct: nothing in the running gateway reads it.
DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[2] / "evals" / "tickets.jsonl"

Split = Literal["train", "holdout"]


class ExpectedLabels(BaseModel):
    """What a correct triage of this ticket looks like."""

    category: Category
    severity: Severity
    next_action: NextAction
    contains_pii: bool


class EvalTicket(BaseModel):
    """One labelled ticket."""

    id: str = Field(min_length=1)
    split: Split
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    expected: ExpectedLabels
    #: Names the boundary this ticket probes, or `None` for a straightforward
    #: one. Tests assert that the documented hard cases are actually present, so
    #: the dataset cannot quietly drift into being all easy tickets.
    hard_case: str | None = None
    #: The playbook clause that decides the labels. This is what makes the
    #: ground truth auditable by a human instead of taken on faith.
    rule: str = Field(min_length=1)


class DatasetError(ValueError):
    """The dataset on disk is not usable."""


def load_tickets(path: Path | None = None) -> tuple[EvalTicket, ...]:
    """Read and validate the golden dataset.

    Raises rather than skipping bad lines. A silently dropped ticket changes
    every denominator in the report, and nothing downstream would notice.
    """
    source = path or DEFAULT_DATASET_PATH
    if not source.exists():
        raise DatasetError(f"No dataset at {source}.")

    tickets = tuple(_parse(source))
    if not tickets:
        raise DatasetError(f"{source} contains no tickets.")

    seen: set[str] = set()
    for ticket in tickets:
        if ticket.id in seen:
            raise DatasetError(f"Duplicate ticket id {ticket.id!r}.")
        seen.add(ticket.id)
    return tickets


def _parse(source: Path) -> Iterator[EvalTicket]:
    # UTF-8 explicitly: the tickets contain typographic punctuation and accented
    # names, and on Windows the platform default is not UTF-8.
    with source.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield EvalTicket.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                raise DatasetError(f"{source}:{number} is not a valid ticket: {exc}") from exc


def select(tickets: tuple[EvalTicket, ...], split: Split | None) -> tuple[EvalTicket, ...]:
    """Filter to one split, or return everything when `split` is `None`.

    The split exists for Phase 5: prompt variants are tuned on `train` and
    reported on `holdout`, which is never read while writing prompts. Creating
    the split after seeing results would make it worthless, so it ships with the
    dataset.
    """
    if split is None:
        return tickets
    return tuple(ticket for ticket in tickets if ticket.split == split)
