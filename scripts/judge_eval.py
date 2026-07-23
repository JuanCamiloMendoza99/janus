"""Grade the free-text fields of a triage sweep with an LLM judge.

    python scripts/judge_eval.py --results docs/evals/results-prompts-holdout-2026-07-22.json
    python scripts/judge_eval.py --results <file> --calibrate

Reads a triage results file (the `summary` and `reasoning` are stored in it, so
this does not re-run triage), joins each prediction back to its ticket text, and
asks a strong, *separate* model to grade the two free-text fields against the
fixed rubric in `app/evals/prompts/judge.md`. Writes `docs/evals/judge-<date>.json`
and prints a summary.

`--calibrate` first runs the judge over the hand-scored examples in
`evals/judge_calibration.jsonl` and reports the agreement, so the reader can
decide whether to trust the judge before trusting its verdict on the variants.

This spends real money — a judgement is a model call. The judge is priced through
the same ledger as everything else and the total is reported. Argument parsing and
printing only; the grading lives in `app/evals/judge.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.evals.dataset import DEFAULT_DATASET_PATH, EvalTicket, load_tickets
from app.evals.judge import (
    AXES,
    CalibrationExample,
    JudgeAggregate,
    JudgedOutput,
    JudgeRun,
    JudgeVerdict,
    aggregate,
    agreement,
    dump_judge_run,
    judge_output,
    load_calibration,
)
from app.evals.results import load_runs
from app.evals.runner import RunResult
from app.providers.base import LLMProvider
from app.providers.registry import ProviderConfigurationError, build_provider

DEFAULT_DIR = Path("docs/evals")
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, default=None, help="Triage results file to grade.")
    parser.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE_MODEL,
        help=f"Model that grades. Default {DEFAULT_JUDGE_MODEL} — strong, and distinct from the "
        "configurations under test.",
    )
    parser.add_argument("--calibrate", action="store_true", help="Run the calibration check first.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--max-spend",
        type=float,
        default=3.0,
        help="Abort once this much has been spent grading, in USD.",
    )
    return parser.parse_args(argv)


def _latest_prompt_results(directory: Path) -> Path:
    candidates = sorted(directory.glob("results-prompts-*.json"))
    if not candidates:
        raise SystemExit(
            f"No results-prompts-*.json in {directory}. Run scripts/run_eval.py --prompts first."
        )
    return candidates[-1]


async def _judge_many(
    provider: LLMProvider,
    items: list[tuple[str, str, str, str]],
    *,
    concurrency: int,
    cap_usd: float,
) -> list[JudgedOutput]:
    """Grade a list of (subject, body, summary, reasoning) tuples, capped by spend."""
    judged: list[JudgedOutput] = []
    spent = 0.0
    for start in range(0, len(items), concurrency):
        if spent >= cap_usd:
            raise SystemExit(f"Stopped at ${spent:.4f}, over the ${cap_usd:.2f} cap.")
        batch = items[start : start + concurrency]
        results = await asyncio.gather(
            *(
                judge_output(provider, subject=s, body=b, summary=sm, reasoning=r)
                for s, b, sm, r in batch
            )
        )
        judged.extend(results)
        spent += sum(j.cost_usd for j in results)
    return judged


def _outputs_to_grade(
    run: RunResult, tickets: dict[str, EvalTicket]
) -> list[tuple[str, str, str, str]]:
    """Pair each successful prediction with its ticket text.

    Failed tickets carry no prediction, so there is nothing to grade — the judge
    scores the free text a run produced, and the objective metrics already count
    the failures. A ticket id that is in the results but not the dataset is a
    mismatch worth failing on, not skipping.
    """
    items: list[tuple[str, str, str, str]] = []
    for outcome in run.outcomes:
        if outcome.predicted is None:
            continue
        ticket = tickets.get(outcome.ticket_id)
        if ticket is None:
            raise SystemExit(
                f"Ticket {outcome.ticket_id!r} is in the results but not in the dataset "
                f"({DEFAULT_DATASET_PATH}). Grading against a different dataset than was run."
            )
        items.append(
            (ticket.subject, ticket.body, outcome.predicted.summary, outcome.predicted.reasoning)
        )
    return items


async def _calibrate(
    provider: LLMProvider, examples: tuple[CalibrationExample, ...], concurrency: int, cap: float
) -> tuple[list[JudgedOutput], object]:
    items = [(e.subject, e.body, e.summary, e.reasoning) for e in examples]
    judged = await _judge_many(provider, items, concurrency=concurrency, cap_usd=cap)
    verdicts: list[JudgeVerdict] = [j.verdict for j in judged]
    return judged, agreement(examples, verdicts)


def _print_agreement(report: object) -> None:
    print(f"\ncalibration ({report.n} hand-scored examples)")  # type: ignore[attr-defined]
    print(f"  {'axis':<24} exact  MAE")
    for axis in AXES:
        print(
            f"  {axis:<24} {report.exact[axis]:>4.0%}  {report.mae[axis]:>4.2f}"  # type: ignore[attr-defined]
        )
    print(
        f"  {'overall':<24} {report.exact_overall:>4.0%}  {report.mae_overall:>4.2f}"  # type: ignore[attr-defined]
    )


def _print_aggregates(aggregates: list[JudgeAggregate]) -> None:
    print(f"\n  {'configuration':<24} " + "  ".join(f"{a[:10]:>10}" for a in AXES) + "   mean")
    for agg in aggregates:
        cells = "  ".join(f"{agg.means[axis]:>10.2f}" for axis in AXES)
        print(f"  {agg.config_name:<24} {cells}   {agg.mean_overall:>4.2f}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    source = args.results or _latest_prompt_results(args.out)

    runs = load_runs(source)
    tickets = {ticket.id: ticket for ticket in load_tickets(args.dataset)}

    settings = get_settings().model_copy(update={"llm_provider": "anthropic"})
    try:
        provider = build_provider(settings, model=args.judge_model)
    except ProviderConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"judging {source.name} with {args.judge_model}")

    calibration_report = None
    if args.calibrate:
        examples = load_calibration()
        _, calibration_report = asyncio.run(
            _calibrate(provider, examples, args.concurrency, args.max_spend)
        )
        _print_agreement(calibration_report)

    aggregates: list[JudgeAggregate] = []
    for run in runs:
        items = _outputs_to_grade(run, tickets)
        judged = asyncio.run(
            _judge_many(provider, items, concurrency=args.concurrency, cap_usd=args.max_spend)
        )
        aggregates.append(aggregate(run.config.name, judged))

    judge_run = JudgeRun(
        judge_model=args.judge_model,
        judged_at=datetime.now(UTC),
        source=source.name,
        aggregates=tuple(aggregates),
        agreement=calibration_report,
    )
    destination = args.out / f"judge-{datetime.now(UTC):%Y-%m-%d}.json"
    dump_judge_run(judge_run, destination)

    _print_aggregates(aggregates)
    print(f"\ntotal ${judge_run.total_cost_usd:.4f}  ->  {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
