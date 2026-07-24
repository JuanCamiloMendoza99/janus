"""The backend half of the web console (Phase 6): CORS and static serving.

The console itself is tested in `web/` with Vitest. What belongs here is the
small amount of FastAPI wiring the frontend depends on and that a Python change
could break: the CORS policy that lets the browser call the API cross-origin, and
the static mount that must never shadow the API routes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# The default origins in `Settings.cors_allow_origins`; the CORS middleware is
# configured from these at import time.
ALLOWED_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example"


def test_an_allowed_origin_gets_cors_headers(client: TestClient) -> None:
    """The console's origin is echoed back, so the browser lets the response through."""
    response = client.get("/health", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_a_foreign_origin_gets_no_cors_grant(client: TestClient) -> None:
    """A wildcard would hand every site access to a gateway holding vendor keys.

    The policy is an allow-list, so an origin that is not on it gets no
    `access-control-allow-origin` header and the browser blocks the read.
    """
    response = client.get("/health", headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 200  # the request still runs server-side
    assert response.headers.get("access-control-allow-origin") != FOREIGN_ORIGIN


def test_the_chat_preflight_is_answered(client: TestClient) -> None:
    """`/v1/chat` is a POST, so the browser preflights it before `fetch` streams.

    This is the crux of Phase 6: the native `EventSource` cannot POST, the client
    uses `fetch`, and `fetch` to another origin sends an OPTIONS preflight first.
    If that is not answered the stream never starts.
    """
    response = client.options(
        "/v1/chat",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "POST" in response.headers.get("access-control-allow-methods", "")


def test_the_api_is_served_without_a_built_frontend(client: TestClient) -> None:
    """The app must boot and serve the API whether or not `web/dist` exists.

    The static mount is guarded on existence precisely so the test suite, CI, and
    a backend-only dev loop run with no `npm run build`. If a stray catch-all
    mount ever shadowed the routers, these would come back as `index.html`.
    """
    assert client.get("/health").status_code == 200
    assert client.get("/v1/usage").status_code == 200
