"""LLM-as-judge for the two free-text fields the labels cannot grade.

`category`, `severity` and `next_action` are scored against `evals/tickets.jsonl`.
`summary` and `reasoning` are free text — there is no exact answer to match — so
they are graded by a separate model call against a fixed rubric. This module is
that call, the schema it is constrained to, and the aggregation and calibration
that turn a pile of verdicts into a number the report can carry.

The known ways an LLM judge goes wrong, and what is done about each here:

* **It prefers the longer, more confident answer.** The rubric grades one output
  at a time against absolute anchors, never two variants side by side, and says
  in as many words not to reward length. The judge is not told which variant
  produced the text, or that variants exist.
* **It grades correctness instead of faithfulness.** The rubric is explicit that
  classification correctness is graded elsewhere and is not the judge's job — and
  the gold labels are deliberately withheld from the judge prompt, so it *cannot*
  grade against them even if it drifted.
* **Its verdict is trusted blind.** `--calibrate` runs the judge over a handful of
  hand-scored examples first and reports the agreement, so the reader can decide
  whether to trust it before reading its verdict on the variants.
* **It spends real money unaccounted for.** Every judgement runs inside a ledger
  and its cost is returned, summed and reported.

Objective metrics outrank the judge wherever they meet: it informs the free-text
question the labels cannot answer, it does not overrule them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.observability.ledger import new_ledger
from app.providers.base import LLMProvider, Message, Prompt

_RUBRIC_PATH = Path(__file__).parent / "prompts" / "judge.md"

#: Repo-root-relative, like the golden dataset: hand-scored examples are dev
#: tooling, not something the packaged application serves.
DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "evals" / "judge_calibration.jsonl"

#: The 1–5 scale, stated once. Used in the field descriptions the SDK relocates
#: into the schema, because the API treats numeric bounds as advice rather than a
#: constraint (see `AnthropicProvider._validation_failure`): the model is asked to
#: stay in range, and Pydantic enforces it on the way back.
_SCALE_MIN, _SCALE_MAX = 1, 5

#: The four graded axes, in a fixed order so every table and every average lines up.
AXES: tuple[str, ...] = (
    "summary_faithful",
    "summary_actionable",
    "reasoning_grounded",
    "reasoning_names_signal",
)


class AxisScores(BaseModel):
    """The four bounded-integer axes, shared by a judge verdict and a hand score."""

    summary_faithful: int = Field(
        ge=_SCALE_MIN,
        le=_SCALE_MAX,
        description="1-5. Is the summary true to the ticket, inventing nothing?",
    )
    summary_actionable: int = Field(
        ge=_SCALE_MIN,
        le=_SCALE_MAX,
        description="1-5. Could a human triage from the summary without opening the ticket?",
    )
    reasoning_grounded: int = Field(
        ge=_SCALE_MIN,
        le=_SCALE_MAX,
        description="1-5. Is the reasoning grounded in the ticket text, with no invented facts?",
    )
    reasoning_names_signal: int = Field(
        ge=_SCALE_MIN,
        le=_SCALE_MAX,
        description="1-5. Does the reasoning name the signal that drove the classification?",
    )

    def axis(self, name: str) -> int:
        return int(getattr(self, name))

    @property
    def mean_score(self) -> float:
        """The four axes averaged — a single number for ranking, not for nuance."""
        return sum(self.axis(name) for name in AXES) / len(AXES)


class JudgeVerdict(AxisScores):
    """A judge's scores for one triage output, plus a one-line note.

    Two axes per field rather than one "quality" number so a variant that writes
    faithful-but-vague summaries can be told apart from one that writes
    vivid-but-invented ones — they fail differently and the report should say
    which.
    """

    notes: str = Field(
        max_length=280,
        description="One short sentence naming the biggest weakness, or 'none'.",
    )


class CalibrationExample(BaseModel):
    """One hand-scored triage output, used to check the judge before trusting it."""

    id: str = Field(min_length=1)
    #: What this example probes — a strong output, a vague one, an invented one.
    note: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    #: The scores a human gave it. The judge is graded against these.
    expected: AxisScores


@dataclass(frozen=True)
class JudgedOutput:
    """One judgement and what it cost."""

    verdict: JudgeVerdict
    cost_usd: float


@dataclass(frozen=True)
class JudgeAggregate:
    """Mean scores over all outputs judged for one configuration."""

    config_name: str
    judged: int
    means: dict[str, float]
    cost_usd: float

    @property
    def mean_overall(self) -> float:
        return sum(self.means.values()) / len(self.means) if self.means else 0.0


@dataclass(frozen=True)
class AgreementReport:
    """How closely the judge matched the hand scores, per axis and overall.

    `exact` is the fraction where judge and human gave the identical integer;
    `mae` is the mean absolute gap. Both are reported because they answer
    different questions: exact agreement is strict and small samples make it
    jumpy, while MAE says whether the disagreements are near-misses or wild.
    """

    n: int
    exact: dict[str, float]
    mae: dict[str, float]

    @property
    def exact_overall(self) -> float:
        return sum(self.exact.values()) / len(self.exact) if self.exact else 0.0

    @property
    def mae_overall(self) -> float:
        return sum(self.mae.values()) / len(self.mae) if self.mae else 0.0


# -- the call ---------------------------------------------------------------


@lru_cache(maxsize=1)
def load_rubric() -> str:
    """The grading rubric — read once, UTF-8, for the same reasons as the playbook."""
    return _RUBRIC_PATH.read_text(encoding="utf-8")


def render_output_to_grade(subject: str, body: str, summary: str, reasoning: str) -> str:
    """Render the one thing the judge grades: a ticket and a system's two fields.

    Deliberately carries no variant name, no model name and no gold label. The
    judge must grade faithfulness and usefulness blind — telling it which prompt
    wrote this, or what the 'right' classification was, is exactly how a judge
    starts grading the wrong question.
    """
    return (
        "<ticket>\n"
        f"<subject>{subject}</subject>\n"
        f"<body>\n{body}\n</body>\n"
        "</ticket>\n"
        "<triage_output>\n"
        f"<summary>{summary}</summary>\n"
        f"<reasoning>{reasoning}</reasoning>\n"
        "</triage_output>"
    )


def build_judge_prompt(subject: str, body: str, summary: str, reasoning: str) -> Prompt:
    """Assemble the judge call: the rubric as prefix, the output to grade as the turn.

    The rubric goes in `cacheable_prefix` for consistency with the rest of the
    codebase, but it is ~1.5k tokens — below every caching floor — so it does not
    actually cache. That is stated in the report rather than hidden: the same
    ADR-003 trap seen from the other side, and the reason the judge's per-call
    cost does not fall on repeated runs the way triage's does.
    """
    return Prompt(
        cacheable_prefix=load_rubric(),
        system=None,
        messages=[
            Message(role="user", content=render_output_to_grade(subject, body, summary, reasoning))
        ],
    )


async def judge_output(
    provider: LLMProvider,
    *,
    subject: str,
    body: str,
    summary: str,
    reasoning: str,
) -> JudgedOutput:
    """Grade one triage output's free-text fields, returning the verdict and its cost.

    The cost is read back from a per-call ledger rather than from the completion,
    matching how the eval runner prices triage: it is the one number that makes
    "the judge spends real money" auditable instead of asserted.
    """
    ledger = new_ledger()
    completion = await provider.parse(
        build_judge_prompt(subject, body, summary, reasoning), JudgeVerdict
    )
    return JudgedOutput(verdict=completion.parsed, cost_usd=ledger.total_cost_usd)


# -- aggregation and calibration -------------------------------------------


def aggregate(config_name: str, judged: Sequence[JudgedOutput]) -> JudgeAggregate:
    """Average a configuration's verdicts into one row of the report."""
    n = len(judged)
    means = {axis: (sum(j.verdict.axis(axis) for j in judged) / n if n else 0.0) for axis in AXES}
    return JudgeAggregate(
        config_name=config_name,
        judged=n,
        means=means,
        cost_usd=sum(j.cost_usd for j in judged),
    )


def agreement(
    examples: Sequence[CalibrationExample], verdicts: Sequence[JudgeVerdict]
) -> AgreementReport:
    """Compare the judge's scores against the hand scores, axis by axis.

    Pairs are positional: `verdicts[i]` is the judge's grade of `examples[i]`, so
    the caller must judge the examples in order. Mismatched lengths are a bug in
    the caller, not something to paper over, so this raises.
    """
    if len(examples) != len(verdicts):
        raise ValueError(f"{len(examples)} examples but {len(verdicts)} verdicts.")
    n = len(examples)
    exact: dict[str, float] = {}
    mae: dict[str, float] = {}
    for axis in AXES:
        gaps = [
            abs(v.axis(axis) - e.expected.axis(axis))
            for e, v in zip(examples, verdicts, strict=True)
        ]
        exact[axis] = sum(1 for gap in gaps if gap == 0) / n if n else 0.0
        mae[axis] = sum(gaps) / n if n else 0.0
    return AgreementReport(n=n, exact=exact, mae=mae)


def load_calibration(path: Path | None = None) -> tuple[CalibrationExample, ...]:
    """Read and validate the hand-scored calibration set."""
    source = path or DEFAULT_CALIBRATION_PATH
    if not source.exists():
        raise FileNotFoundError(f"No calibration set at {source}.")
    examples: list[CalibrationExample] = []
    with source.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                examples.append(CalibrationExample.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{source}:{number} is not a valid example: {exc}") from exc
    if not examples:
        raise ValueError(f"{source} contains no examples.")
    return tuple(examples)


# -- persistence ------------------------------------------------------------
#
# A judge run is committed next to the triage results it graded, so the report is
# reproducible and the spend is on the record. Kept separate from the triage
# results file (`app/evals/results.py`) because the judge is a distinct, optional
# pass: you can re-judge without re-running triage, which is the whole reason the
# triage results carry the full `summary` and `reasoning`.

JUDGE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class JudgeRun:
    """Everything one judge pass produced over one triage results file."""

    judge_model: str
    judged_at: datetime
    source: str
    aggregates: tuple[JudgeAggregate, ...]
    agreement: AgreementReport | None

    @property
    def total_cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.aggregates)


def dump_judge_run(run: JudgeRun, path: Path) -> None:
    """Write a judge run to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_run_to_dict(run), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_judge_run(path: Path) -> JudgeRun:
    """Read a judge run back, refusing a file written by a different shape."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version")
    if version != JUDGE_SCHEMA_VERSION:
        raise ValueError(
            f"{path} was written with schema_version {version!r}, "
            f"but this code reads {JUDGE_SCHEMA_VERSION}."
        )
    return _run_from_dict(payload)


def _run_to_dict(run: JudgeRun) -> dict[str, Any]:
    return {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "judge_model": run.judge_model,
        "judged_at": run.judged_at.isoformat(),
        "source": run.source,
        "total_cost_usd": run.total_cost_usd,
        "aggregates": [
            {
                "config_name": a.config_name,
                "judged": a.judged,
                "means": a.means,
                "cost_usd": a.cost_usd,
            }
            for a in run.aggregates
        ],
        "agreement": (
            None
            if run.agreement is None
            else {"n": run.agreement.n, "exact": run.agreement.exact, "mae": run.agreement.mae}
        ),
    }


def _run_from_dict(data: dict[str, Any]) -> JudgeRun:
    agreement_data = data.get("agreement")
    return JudgeRun(
        judge_model=data["judge_model"],
        judged_at=datetime.fromisoformat(data["judged_at"]),
        source=data["source"],
        aggregates=tuple(
            JudgeAggregate(
                config_name=a["config_name"],
                judged=a["judged"],
                means=dict(a["means"]),
                cost_usd=a["cost_usd"],
            )
            for a in data["aggregates"]
        ),
        agreement=(
            None
            if agreement_data is None
            else AgreementReport(
                n=agreement_data["n"],
                exact=dict(agreement_data["exact"]),
                mae=dict(agreement_data["mae"]),
            )
        ),
    )
