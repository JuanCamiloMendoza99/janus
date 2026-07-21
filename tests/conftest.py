"""Shared test fixtures.

The whole suite runs against `FakeProvider`, so tests need no API keys, make no
network calls and cost nothing. That is a deliberate consequence of the provider
seam (ADR-002), not a convenience: it is what lets CI run on a fork with no
secrets configured.

Anything that requires a real provider belongs behind the `live` marker and is
excluded from the default run.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.observability.ledger import usage_store
from app.providers.fake import FakeProvider
from app.providers.registry import get_provider


@pytest.fixture(autouse=True)
def _isolate_usage_store() -> Iterator[None]:
    """Zero the process-wide usage store around every test.

    It is a module-level singleton, so without this a request in one test would
    leak its totals into the next.
    """
    usage_store.reset()
    yield
    usage_store.reset()


@pytest.fixture
def settings() -> Settings:
    """Settings with the fake provider selected, ignoring any local `.env`."""
    return Settings(llm_provider="fake", environment="ci")


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(settings: Settings, fake_provider: FakeProvider) -> TestClient:
    """A `TestClient` with settings and provider overridden.

    Overriding the dependencies rather than mutating the module-level caches
    keeps tests independent of import order and of whatever `.env` happens to be
    on the developer's machine.
    """
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_provider] = lambda: fake_provider
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
