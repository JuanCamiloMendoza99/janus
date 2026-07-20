"""Per-request cost logging.

Installs a `UsageLedger` for each request and emits one structured log record
when the response is fully sent. Satisfies the project's headline requirement:
a queryable cost figure for every call.

The streaming caveat is the whole difficulty. For a normal response the ledger
is complete when the handler returns. For an SSE response the handler returns
immediately and the model call is still in flight — the tokens arrive as the
generator is consumed. So the flush must be attached to the *end of the
response body*, not to the end of the handler. Reading the ledger too early
yields a confident, well-formatted cost of $0.00, which is worse than no log at
all because nothing looks broken.

See ADR-004.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class CostLoggingMiddleware:
    """Pure-ASGI middleware that records token usage and cost per request.

    Written against the raw ASGI interface rather than
    `BaseHTTPMiddleware` on purpose: `BaseHTTPMiddleware` buffers responses in a
    way that interferes with server-sent events, and SSE is a first-class
    response type here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError("Phase 1")
