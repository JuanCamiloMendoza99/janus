"""Logging setup.

Cost and token figures are only useful if they are queryable, so the default
format is single-line JSON — the cost middleware emits one record per request
that a log aggregator can aggregate on directly. `LOG_FORMAT=text` switches to
human-readable output for local tailing.
"""

from __future__ import annotations

import json
import logging

# Attributes present on every LogRecord. Anything a caller passes via `extra=`
# lands as an attribute *not* in this set, so this is how we recover the
# structured fields (provider, cost, tokens) for the JSON formatter.
_RESERVED_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Render each record as one line of JSON, folding in any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger for the process.

    Called once from the FastAPI lifespan handler, not at import time — import
    -time side effects make the module impossible to use from tests or scripts.
    Idempotent: existing handlers are replaced so repeated calls (reloads, tests)
    do not stack duplicate output.
    """
    handler = logging.StreamHandler()
    if fmt == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
