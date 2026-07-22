"""Mapping provider failures onto HTTP status codes.

One place, because two routers need the same answer and a gateway that reports
a vendor rate limit as `500` on one endpoint and `429` on another is worse than
one that is consistently wrong: a client cannot write a retry policy against it.

Routers import this instead of reaching into the provider layer's exception
details themselves — `ProviderError` already carries the vendor's status, and
translating it is an HTTP concern, not a business one.
"""

from __future__ import annotations

from app.providers.base import ProviderError

#: Vendor statuses that describe the *caller's* request accurately enough to
#: forward unchanged. Everything else becomes a 502: from the client's side, a
#: failure inside a provider we chose is an upstream failure, not their fault.
_PASSTHROUGH = frozenset({400, 429, 501})


def http_status_for(exc: ProviderError) -> int:
    """Return the HTTP status a `ProviderError` should surface as.

    Rate limits stay `429` so clients back off, malformed requests stay `400` so
    they stop retrying, and `501` marks a capability the selected provider does
    not have (the fake refusing to invent a structured result). Anything else —
    auth failures, overloads, transport errors — is a `502`.

    Note this applies only *before* a response body has started. Once an SSE
    stream is open the status line is already committed, and a mid-stream
    failure has to be reported as a terminal frame instead.
    """
    if exc.status_code in _PASSTHROUGH:
        return exc.status_code
    return 502
