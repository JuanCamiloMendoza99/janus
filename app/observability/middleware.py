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

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger
from app.observability.ledger import new_ledger, usage_store

logger = get_logger("janus.cost")


class CostLoggingMiddleware:
    """Pure-ASGI middleware that records token usage and cost per request.

    Written against the raw ASGI interface rather than
    `BaseHTTPMiddleware` on purpose: `BaseHTTPMiddleware` buffers responses in a
    way that interferes with server-sent events, and SSE is a first-class
    response type here.

    The flush timing is the whole point (ADR-004). Awaiting `self.app(...)` does
    not return until the response *body* is fully sent — for an SSE stream that
    means the model call has completed and the trailing `UsageReport` has already
    landed in the ledger. Flushing at handler return (as `BaseHTTPMiddleware`
    forces) would read the ledger while the stream is still in flight and log a
    confident `$0.00`. The ledger is installed here so the request task's context
    is the one the streaming generator runs in.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        ledger = new_ledger()
        flushed = False

        def flush() -> None:
            nonlocal flushed
            if flushed:
                return
            flushed = True
            usage_store.record_request(ledger)
            logger.info(
                "request.cost",
                extra={
                    "path": scope.get("path"),
                    "method": scope.get("method"),
                    **ledger.summary(),
                },
            )

        async def send_wrapper(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                flush()

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Safety net for responses that finish without a terminal body
            # message (errors, empty responses). No-op if already flushed.
            flush()
