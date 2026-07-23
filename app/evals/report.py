"""Rendering a sweep's numbers as the markdown tables committed to `docs/evals/`.

The scripts stay argparse-and-print; the table shapes live here, so Phase 4's
provider comparison and Phase 5's prompt comparison share one renderer and one
pair of generated-block markers. The prose around the block — the headline, the
recommendation, the trade-off it accepts — is written by a human, because a
script cannot decide that four points of severity accuracy is or is not worth the
saving. That judgement is the deliverable; the tables are evidence for it.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.evals.judge import AXES, JudgeRun
from app.evals.runner import RunResult
from app.evals.scoring import Metrics

BEGIN = "<!-- BEGIN GENERATED -->"
END = "<!-- END GENERATED -->"


def skeleton() -> str:
    """The initial file a report is spliced into, before any human prose."""
    placeholder = "_Write the headline finding and the recommendation here._"
    return f"# Evaluation\n\n{placeholder}\n\n{BEGIN}\n{END}\n"


def splice(existing: str, block: str) -> str:
    """Replace only the generated block, leaving the human prose untouched."""
    if BEGIN not in existing or END not in existing:
        raise SystemExit(
            f"Report is missing the {BEGIN} / {END} markers; refusing to overwrite it."
        )
    head, _, rest = existing.partition(BEGIN)
    _, _, tail = rest.partition(END)
    return f"{head}{block}{tail}"


# -- Phase 4: provider x model ---------------------------------------------


def comparison_table(rows: Sequence[tuple[RunResult, Metrics]]) -> str:
    """The provider comparison: one row per configuration, every objective metric."""
    header = (
        "| Configuration | Model | Thinking | Classification | Category | Severity | "
        "Next action | Escalate FP | Escalate FN | PII recall | PII prec. | "
        "p50 | p95 | $/1k tickets | Cache hit | ECE | Failures |\n"
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    body = "".join(
        f"| `{run.config.name}` | `{run.config.model}` "
        f"| {'adaptive' if run.config.adaptive_thinking else 'off'} "
        f"| **{m.classification_accuracy:.1%}** | {m.category_accuracy:.1%} "
        f"| {m.severity_accuracy:.1%} | {m.next_action_accuracy:.1%} "
        f"| {m.escalation_false_positive_rate:.1%} | {m.escalation_false_negative_rate:.1%} "
        f"| {m.pii_recall:.1%} | {m.pii_precision:.1%} "
        f"| {m.latency_p50_s:.1f}s | {m.latency_p95_s:.1f}s "
        f"| ${m.cost_per_1000_tickets_usd:.2f} | {m.cache_hit_rate:.1%} "
        f"| {m.expected_calibration_error:.3f} | {m.failures} |\n"
        for run, m in rows
    )
    return header + body


def calibration_tables(rows: Sequence[tuple[RunResult, Metrics]]) -> str:
    """One small confidence-calibration table per configuration."""
    parts: list[str] = []
    for run, metrics in rows:
        lines = [
            f"**`{run.config.name}`** — ECE {metrics.expected_calibration_error:.3f}\n",
            "| Confidence | Tickets | Mean confidence | Accuracy |",
            "|---|---:|---:|---:|",
        ]
        for bucket in metrics.calibration:
            if not bucket.count:
                continue
            lines.append(
                f"| {bucket.lower:.1f}–{bucket.upper:.1f} | {bucket.count} "
                f"| {bucket.mean_confidence:.2f} | {bucket.accuracy:.1%} |"
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# -- Phase 5: prompt variant, fixed model ----------------------------------


def prompt_comparison_table(
    rows: Sequence[tuple[RunResult, Metrics]],
    tokens: dict[str, int],
    judge: JudgeRun | None,
) -> str:
    """The prompt comparison: objective metrics plus prefix tokens, and judge means.

    Prefix tokens sit next to cost per ticket because they are the reason a
    better-scoring prompt might be the wrong one to ship: severity gained at a
    token cost is a trade, not a free lunch, and the trade has to be visible.
    """
    judge_by_name = {a.config_name: a for a in judge.aggregates} if judge else {}
    judge_header = "".join(f" {axis.replace('_', ' ')} |" for axis in AXES) if judge else ""
    judge_rule = "---:|" * len(AXES) if judge else ""

    header = (
        "| Variant | Prefix tokens | Classification | Category | Severity | "
        "Next action | Escalate FP | PII recall | p50 | $/1k tickets | ECE | "
        f"Failures |{judge_header}\n"
        f"|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|{judge_rule}\n"
    )
    body_rows: list[str] = []
    for run, m in rows:
        variant = run.config.prompt
        judge_cells = ""
        if judge:
            agg = judge_by_name.get(run.config.name)
            judge_cells = "".join(f" {agg.means[axis]:.2f} |" if agg else " – |" for axis in AXES)
        body_rows.append(
            f"| `{variant}` | {tokens.get(variant, 0):,} "
            f"| **{m.classification_accuracy:.1%}** | {m.category_accuracy:.1%} "
            f"| {m.severity_accuracy:.1%} | {m.next_action_accuracy:.1%} "
            f"| {m.escalation_false_positive_rate:.1%} | {m.pii_recall:.1%} "
            f"| {m.latency_p50_s:.1f}s | ${m.cost_per_1000_tickets_usd:.2f} "
            f"| {m.expected_calibration_error:.3f} | {m.failures} |{judge_cells}\n"
        )
    return header + "".join(body_rows)


def agreement_table(judge: JudgeRun) -> str:
    """The calibration check: how closely the judge matched the hand scores."""
    if judge.agreement is None:
        return "_No calibration was run for this judge pass._"
    report = judge.agreement
    lines = [
        f"Judge: `{judge.judge_model}`. Calibrated against {report.n} hand-scored examples "
        "(`evals/judge_calibration.jsonl`).\n",
        "| Axis | Exact agreement | Mean abs. error |",
        "|---|---:|---:|",
    ]
    for axis in AXES:
        lines.append(
            f"| {axis.replace('_', ' ')} | {report.exact[axis]:.0%} | {report.mae[axis]:.2f} |"
        )
    lines.append(f"| **overall** | {report.exact_overall:.0%} | {report.mae_overall:.2f} |")
    return "\n".join(lines)
