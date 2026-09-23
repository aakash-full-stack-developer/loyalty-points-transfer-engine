import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.config import Settings
from app.domain.errors import ERROR_CATALOGUE, ErrorCode
from app.main import create_app


async def _get(app: FastAPI, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, **kwargs)  # type: ignore[arg-type]


def test_every_error_code_has_a_status_and_title() -> None:
    assert set(ERROR_CATALOGUE) == set(ErrorCode)


async def test_metrics_endpoint_exposes_the_documented_metrics(app: FastAPI) -> None:
    response = await _get(app, "/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in (
        "transfers_total",
        "transfer_duration_seconds",
        "partner_requests_total",
        "partner_request_duration_seconds",
        "circuit_breaker_state",
        "idempotent_replays_total",
        "idempotency_conflicts_total",
        "reconciliation_resolved_total",
        "transfers_pending_verification",
        "transfers_manual_review",
        "rate_limit_rejections_total",
    ):
        assert f"# HELP {name.removesuffix('_total')}" in response.text, name


async def test_every_response_carries_security_and_request_id_headers(app: FastAPI) -> None:
    response = await _get(app, "/health/live")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


async def test_cors_is_disabled_by_default(app: FastAPI) -> None:
    response = await _get(app, "/health/live", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in response.headers


async def test_cors_allows_only_configured_origins() -> None:
    app = create_app(Settings(cors_allow_origins=["https://app.example"]))

    allowed = await _get(app, "/health/live", headers={"Origin": "https://app.example"})
    other = await _get(app, "/health/live", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "https://app.example"
    assert "access-control-allow-origin" not in other.headers


def test_production_refuses_the_default_admin_key() -> None:
    with pytest.raises(ValidationError, match="ADMIN_API_KEY"):
        Settings(environment="production")

    Settings(environment="production", admin_api_key="a-real-secret")  # type: ignore[arg-type]
