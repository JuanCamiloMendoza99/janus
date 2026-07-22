"""The LLM-as-judge, everything that can be checked without a real model.

The single call is thin — assemble a prompt, ask the provider to `parse()` a
`JudgeVerdict` — so the tests here are about what surrounds it: that the judge is
handed the text and *only* the text (no gold label, no variant name), that the
aggregation and calibration arithmetic is right, that a run round-trips through
disk, and that the cost is read back rather than assumed. A stub provider stands
in for the model; the real judge lives behind the `live` marker in the sweep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evals.judge import (
    AXES,
    AxisScores,
    CalibrationExample,
    JudgedOutput,
    JudgeRun,
    JudgeVerdict,
    aggregate,
    agreement,
    build_judge_prompt,
    dump_judge_run,
    judge_output,
    load_calibration,
    load_judge_run,
    render_output_to_grade,
)
from app.observability.ledger import current_ledger
from app.providers.base import ParsedCompletion, Prompt, Usage

# -- a stub judge -----------------------------------------------------------


class StubJudge:
    """Answers `parse()` with a scripted verdict and records a cost to the ledger."""

    name = "stub"
    model = "claude-opus-4-8"

    def __init__(self, verdict: JudgeVerdict) -> None:
        self.verdict = verdict
        self.prompts: list[Prompt] = []

    async def parse[T](self, prompt: Prompt, schema: type[T]) -> ParsedCompletion[T]:
        self.prompts.append(prompt)
        ledger = current_ledger()
        usage = Usage(model=self.model, input_tokens=1500, output_tokens=40)
        if ledger is not None:
            ledger.record(self.name, usage)
        return ParsedCompletion(parsed=self.verdict, usage=usage)  # type: ignore[arg-type]


def _verdict(**overrides: int | str) -> JudgeVerdict:
    base: dict[str, int | str] = {
        "summary_faithful": 5,
        "summary_actionable": 4,
        "reasoning_grounded": 5,
        "reasoning_names_signal": 3,
        "notes": "none",
    }
    base.update(overrides)
    return JudgeVerdict(**base)  # type: ignore[arg-type]


# -- the prompt is blind ----------------------------------------------------


def test_the_judge_is_shown_only_the_ticket_and_the_output() -> None:
    """No gold label, no variant name, no model name reaches the judge.

    The regression guard for the judge's worst failure mode: grading against the
    labels instead of for faithfulness. The rendered prompt must contain the text
    to grade and nothing that would let the judge cheat.
    """
    rendered = render_output_to_grade(
        subject="Billed twice",
        body="Charged twice for order 4471.",
        summary="Customer double-charged for order 4471.",
        reasoning="Money moved incorrectly, so escalate.",
    )
    assert "order 4471" in rendered
    assert "Money moved incorrectly" in rendered
    # Nothing that would tell the judge the 'right' answer or which prompt wrote this.
    for leak in ("billing", "v1-baseline", "v2-examples", "expected", "gold", "claude"):
        assert leak not in rendered


def test_the_rubric_is_the_prefix_and_the_output_is_the_turn() -> None:
    prompt = build_judge_prompt("s", "b", "sm", "r")
    assert isinstance(prompt, Prompt)
    assert prompt.cacheable_prefix and "rubric" in prompt.cacheable_prefix.lower()
    assert "sm" in prompt.messages[0].content


# -- the call, cost, aggregation --------------------------------------------


async def test_a_judgement_reads_its_cost_from_the_ledger() -> None:
    provider = StubJudge(_verdict())
    result = await judge_output(provider, subject="s", body="b", summary="sm", reasoning="r")

    assert isinstance(result, JudgedOutput)
    # Priced from the ledger the call installed, not assumed to be zero.
    assert result.cost_usd > 0


def test_aggregate_means_each_axis() -> None:
    judged = [
        JudgedOutput(verdict=_verdict(summary_faithful=5), cost_usd=0.01),
        JudgedOutput(verdict=_verdict(summary_faithful=1), cost_usd=0.02),
    ]
    agg = aggregate("sonnet+v1-baseline", judged)

    assert agg.judged == 2
    assert agg.means["summary_faithful"] == pytest.approx(3.0)
    assert agg.cost_usd == pytest.approx(0.03)


def test_the_mean_score_averages_the_four_axes() -> None:
    verdict = _verdict(
        summary_faithful=4, summary_actionable=4, reasoning_grounded=2, reasoning_names_signal=2
    )
    assert verdict.mean_score == pytest.approx(3.0)


# -- calibration ------------------------------------------------------------


def _example(idx: int, **scores: int) -> CalibrationExample:
    return CalibrationExample(
        id=f"C-{idx}",
        note="test",
        subject="s",
        body="b",
        summary="sm",
        reasoning="r",
        expected=AxisScores(**scores),  # type: ignore[arg-type]
    )


def test_agreement_reports_exact_and_mae_per_axis() -> None:
    examples = [
        _example(
            1,
            summary_faithful=5,
            summary_actionable=5,
            reasoning_grounded=5,
            reasoning_names_signal=5,
        ),
        _example(
            2,
            summary_faithful=1,
            summary_actionable=3,
            reasoning_grounded=2,
            reasoning_names_signal=4,
        ),
    ]
    verdicts = [
        _verdict(
            summary_faithful=5,
            summary_actionable=5,
            reasoning_grounded=5,
            reasoning_names_signal=5,
        ),
        _verdict(
            summary_faithful=2,  # off by 1
            summary_actionable=3,  # exact
            reasoning_grounded=2,  # exact
            reasoning_names_signal=4,  # exact
        ),
    ]
    report = agreement(examples, verdicts)

    assert report.n == 2
    assert report.exact["summary_actionable"] == pytest.approx(1.0)  # both exact
    assert report.exact["summary_faithful"] == pytest.approx(0.5)  # one off
    assert report.mae["summary_faithful"] == pytest.approx(0.5)  # gaps 0 and 1
    assert report.exact_overall == pytest.approx((0.5 + 1.0 + 1.0 + 1.0) / 4)


def test_agreement_refuses_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="examples"):
        agreement(
            [
                _example(
                    1,
                    summary_faithful=5,
                    summary_actionable=5,
                    reasoning_grounded=5,
                    reasoning_names_signal=5,
                )
            ],
            [],
        )


def test_the_shipped_calibration_set_loads_and_is_hand_scored() -> None:
    """The committed calibration file is valid and spans the quality range.

    A calibration set that is all 5s measures nothing: the judge could agree by
    always saying 5. So it must contain deliberately weak and unfaithful examples
    too, which is what makes disagreement informative.
    """
    examples = load_calibration()
    assert len(examples) >= 8
    faithfulness = [e.expected.summary_faithful for e in examples]
    # At least one example a faithful judge should score low, and one high.
    assert min(faithfulness) <= 2
    assert max(faithfulness) == 5


# -- persistence ------------------------------------------------------------


def _run() -> JudgeRun:
    from datetime import UTC, datetime

    return JudgeRun(
        judge_model="claude-opus-4-8",
        judged_at=datetime(2026, 7, 22, tzinfo=UTC),
        source="results-prompts-holdout-2026-07-22.json",
        aggregates=(
            aggregate("sonnet+v1-baseline", [JudgedOutput(verdict=_verdict(), cost_usd=0.01)]),
        ),
        agreement=agreement(
            [
                _example(
                    1,
                    summary_faithful=5,
                    summary_actionable=4,
                    reasoning_grounded=5,
                    reasoning_names_signal=3,
                )
            ],
            [_verdict()],
        ),
    )


def test_a_judge_run_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "judge.json"
    original = _run()
    dump_judge_run(original, path)
    restored = load_judge_run(path)

    assert restored.judge_model == original.judge_model
    assert restored.source == original.source
    assert restored.aggregates[0].means == original.aggregates[0].means
    assert restored.agreement is not None
    assert restored.agreement.n == 1
    assert restored.total_cost_usd == pytest.approx(original.total_cost_usd)


def test_a_run_without_calibration_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "judge.json"
    run = JudgeRun(
        judge_model="claude-opus-4-8",
        judged_at=_run().judged_at,
        source="x.json",
        aggregates=(aggregate("c", [JudgedOutput(verdict=_verdict(), cost_usd=0.0)]),),
        agreement=None,
    )
    dump_judge_run(run, path)
    assert load_judge_run(path).agreement is None


def test_a_stale_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "judge.json"
    path.write_text('{"schema_version": 0, "aggregates": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_judge_run(path)


def test_the_scale_bounds_are_enforced_on_the_way_back() -> None:
    """A 6 on a 1-5 axis is rejected, so an out-of-range judge verdict fails loudly."""
    with pytest.raises(ValidationError):
        _verdict(summary_faithful=6)


def test_notes_have_a_length_limit() -> None:
    with pytest.raises(ValidationError):
        _verdict(notes="x" * 500)


def test_the_axes_constant_matches_the_schema() -> None:
    """AXES drives every table and average; it must match the model's fields."""
    model_fields = set(AxisScores.model_fields)
    assert set(AXES) == model_fields
