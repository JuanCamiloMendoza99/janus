"""Logging setup.

Cost and token figures are only useful if they are queryable, so the default
format is single-line JSON — the cost middleware emits one record per request
that a log aggregator can aggregate on directly. `LOG_FORMAT=text` switches to
human-readable output for local tailing.
"""

import logging


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger for the process.

    Called once from the FastAPI lifespan handler, not at import time — import
    -time side effects make the module impossible to use from tests or scripts.
    """
    raise NotImplementedError("Phase 1")


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
