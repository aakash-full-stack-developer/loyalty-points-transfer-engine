"""Shared fixtures. Tests under tests/unit are marked `unit`, tests under tests/integration
are marked `integration`, based on their directory, so markers never drift from layout."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app

_TESTS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        relative = Path(item.path).relative_to(_TESTS_DIR)
        if relative.parts[0] == "unit":
            item.add_marker(pytest.mark.unit)
        elif relative.parts[0] == "integration":
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client that runs the app in-process with its lifespan (real DB/Redis clients)."""
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http_client,
    ):
        yield http_client
