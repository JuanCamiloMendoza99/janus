"""Persisting a sweep so its conclusions can be re-derived rather than believed.

The report in `docs/evals/` is a summary, and a summary is a claim. The raw
per-ticket outcomes are committed alongside it so anyone can recompute every
figure — or notice that the recommendation does not follow from the data. That
is also what lets Phase 5 diff a prompt variant against this baseline without
paying for the baseline again.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.triage import TriageResult
from app.evals.dataset import ExpectedLabels
from app.evals.runner import EvalConfig, RunResult
from app.evals.scoring import TicketOutcome
from app.providers.base import Usage

#: Bumped when the on-disk shape changes incompatibly, so a stale results file
#: fails loudly instead of being silently misread into a plausible report.
SCHEMA_VERSION = 1


def dump_runs(runs: Sequence[RunResult], path: Path) -> None:
    """Write a sweep to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "runs": [_run_to_dict(run) for run in runs],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_runs(path: Path) -> list[RunResult]:
    """Read a sweep back, refusing a file written by a different shape."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path} was written with schema_version {version!r}, "
            f"but this code reads {SCHEMA_VERSION}."
        )
    return [_run_from_dict(run) for run in payload["runs"]]


def _run_to_dict(run: RunResult) -> dict[str, Any]:
    return {
        "config": {
            "name": run.config.name,
            "provider": run.config.provider,
            "model": run.config.model,
            "adaptive_thinking": run.config.adaptive_thinking,
            "max_output_tokens": run.config.max_output_tokens,
        },
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "split": run.split,
        "outcomes": [_outcome_to_dict(outcome) for outcome in run.outcomes],
    }


def _run_from_dict(data: dict[str, Any]) -> RunResult:
    return RunResult(
        config=EvalConfig(**data["config"]),
        started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["finished_at"]),
        split=data["split"],
        outcomes=tuple(_outcome_from_dict(outcome) for outcome in data["outcomes"]),
    )


def _outcome_to_dict(outcome: TicketOutcome) -> dict[str, Any]:
    return {
        "ticket_id": outcome.ticket_id,
        "expected": outcome.expected.model_dump(mode="json"),
        # The full prediction, not just the fields that are scored: the free-text
        # summary and reasoning are what Phase 5's judge grades, and re-running
        # the sweep to get them back would cost what this file exists to avoid.
        "predicted": outcome.predicted.model_dump(mode="json") if outcome.predicted else None,
        "error": outcome.error,
        "latency_s": outcome.latency_s,
        "usage": _usage_to_dict(outcome.usage),
        "cost_usd": outcome.cost_usd,
    }


def _outcome_from_dict(data: dict[str, Any]) -> TicketOutcome:
    return TicketOutcome(
        ticket_id=data["ticket_id"],
        expected=ExpectedLabels.model_validate(data["expected"]),
        predicted=(
            TriageResult.model_validate(data["predicted"])
            if data["predicted"] is not None
            else None
        ),
        error=data["error"],
        latency_s=data["latency_s"],
        usage=_usage_from_dict(data["usage"]),
        cost_usd=data["cost_usd"],
    )


def _usage_to_dict(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _usage_from_dict(data: dict[str, Any] | None) -> Usage | None:
    return Usage(**data) if data is not None else None
