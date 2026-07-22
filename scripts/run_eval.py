"""Run the golden dataset through one or more configurations.

    python scripts/run_eval.py --configs haiku sonnet opus sonnet-thinking

This spends real money. `--max-spend` is enforced between batches and defaults
to a cap well above a normal sweep, so a runaway costs dollars rather than
hundreds.

Argument parsing and printing only — every decision worth testing lives in
`app/evals/`, which runs without a network.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.evals.dataset import DEFAULT_DATASET_PATH, load_tickets, select
from app.evals.results import dump_runs
from app.evals.runner import (
    CONFIGS_BY_NAME,
    DEFAULT_CONFIG_NAMES,
    BudgetExceeded,
    EvalConfig,
    RunResult,
    run_sweep,
)
from app.evals.scoring import score
from app.providers.registry import ProviderConfigurationError, build_provider

DEFAULT_OUT = Path("docs/evals")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--configs",
        nargs="+",
        default=list(DEFAULT_CONFIG_NAMES),
        choices=sorted(CONFIGS_BY_NAME),
        help="Configurations to sweep.",
    )
    parser.add_argument(
        "--split",
        choices=("train", "holdout", "all"),
        default="all",
        help="Which slice of the dataset to run. Default: all.",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--max-spend",
        type=float,
        default=5.0,
        help="Abort the sweep once this much has been spent, in USD.",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args(argv)


def _runnable(configs: list[EvalConfig]) -> list[EvalConfig]:
    """Drop configurations whose credentials are missing, and say so.

    Skipping loudly rather than failing the sweep: the OpenAI entries are in the
    list on purpose (see `app/evals/runner.py`), and a missing key for one vendor
    should not stop the comparison that *can* be made.
    """
    settings = get_settings()
    runnable: list[EvalConfig] = []
    for config in configs:
        try:
            build_provider(config.settings_for(settings), model=config.model)
        except ProviderConfigurationError as exc:
            print(f"  skipping {config.name}: {exc}", file=sys.stderr)
            continue
        runnable.append(config)
    return runnable


def _summarise(runs: list[RunResult]) -> None:
    for run in runs:
        metrics = score(run.outcomes)
        print(
            f"  {run.config.name:<16} "
            f"classification {metrics.classification_accuracy:>6.1%}  "
            f"failures {metrics.failures:>2}  "
            f"p50 {metrics.latency_p50_s:>5.1f}s  "
            f"${metrics.total_cost_usd:.4f}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    tickets = select(load_tickets(args.dataset), None if args.split == "all" else args.split)
    configs = _runnable([CONFIGS_BY_NAME[name] for name in args.configs])
    if not configs:
        print("Nothing to run: no configuration had usable credentials.", file=sys.stderr)
        return 1

    print(f"{len(tickets)} tickets ({args.split}) x {len(configs)} configurations")
    print(f"budget cap ${args.max_spend:.2f}, concurrency {args.concurrency}\n")

    try:
        runs = asyncio.run(
            run_sweep(
                configs,
                tickets,
                get_settings(),
                concurrency=args.concurrency,
                max_spend_usd=args.max_spend,
                split=None if args.split == "all" else args.split,
            )
        )
    except BudgetExceeded as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    destination = args.out / f"results-{datetime.now(UTC):%Y-%m-%d}.json"
    dump_runs(runs, destination)

    print()
    _summarise(runs)
    print(f"\ntotal ${sum(run.total_cost_usd for run in runs):.4f}  ->  {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
