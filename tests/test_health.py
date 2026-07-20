"""Health endpoint and application wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_the_active_provider(client: TestClient) -> None:
    """The health check must expose which provider is wired in.

    This is the assertion behind the project's central claim: you can tell which
    vendor is active without reading code or logs.
    """
    body = client.get("/health").json()

    assert body["provider"] == "fake"
    assert body["model"] == "fake-1"


def test_openapi_exposes_the_three_endpoints(client: TestClient) -> None:
    """The public surface is fixed in Phase 0 even though the handlers are not.

    Locking the contract first is the point of this phase — later phases fill in
    behaviour without renegotiating the API.
    """
    paths = client.get("/openapi.json").json()["paths"]

    assert "/v1/chat" in paths
    assert "/v1/triage" in paths
    assert "/v1/usage" in paths
