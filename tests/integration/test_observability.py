"""Metrics move when the things they describe happen. Prometheus counters are process-wide,
so every assertion compares a value before and after the action."""

import uuid

import httpx
import pytest
from prometheus_client import REGISTRY

from app.config import Settings
from tests.integration.conftest import running_app
from tests.integration.helpers import CARD_TO_AIRLINE, set_partner_mode, transfer

pytestmark = pytest.mark.usefixtures("seeded", "simulator", "ledger_stays_consistent")

ROUTE = "NOVA_REWARDS->SKYWARD_MILES"


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_completed_transfer_updates_transfer_and_partner_metrics(
    client: httpx.AsyncClient,
) -> None:
    completed = sample("transfers_total", status="COMPLETED", route=ROUTE)
    credits = sample(
        "partner_requests_total", partner="SKYWARD", operation="credit", outcome="SUCCESS"
    )
    durations = sample("transfer_duration_seconds_count", status="COMPLETED")

    await transfer(client, *CARD_TO_AIRLINE, 10_000)

    assert sample("transfers_total", status="COMPLETED", route=ROUTE) == completed + 1
    assert (
        sample("partner_requests_total", partner="SKYWARD", operation="credit", outcome="SUCCESS")
        == credits + 1
    )
    assert sample("transfer_duration_seconds_count", status="COMPLETED") == durations + 1


async def test_rejection_is_counted_as_reversed(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient
) -> None:
    await set_partner_mode(simulator, "SKYWARD", "reject")
    reversed_before = sample("transfers_total", status="REVERSED", route=ROUTE)

    await transfer(client, *CARD_TO_AIRLINE, 10_000)

    assert sample("transfers_total", status="REVERSED", route=ROUTE) == reversed_before + 1


async def test_idempotency_replays_and_conflicts_are_counted(client: httpx.AsyncClient) -> None:
    replays = sample("idempotent_replays_total")
    reused = sample("idempotency_conflicts_total", reason="key_reused")
    key = str(uuid.uuid4())

    await transfer(client, *CARD_TO_AIRLINE, 10_000, key=key)
    await transfer(client, *CARD_TO_AIRLINE, 10_000, key=key)
    await transfer(client, *CARD_TO_AIRLINE, 20_000, key=key)

    assert sample("idempotent_replays_total") == replays + 1
    assert sample("idempotency_conflicts_total", reason="key_reused") == reused + 1


async def test_rate_limit_rejections_are_counted(settings: Settings) -> None:
    rejections = sample("rate_limit_rejections_total")
    limited = settings.model_copy(update={"rate_limit_transfers_per_window": 1})

    async with running_app(limited) as (_, client):
        await transfer(client, points=1_000)
        response = await transfer(client, points=1_000)

    assert response.status_code == 429
    assert sample("rate_limit_rejections_total") == rejections + 1


async def test_open_circuit_is_visible_as_a_gauge(settings: Settings) -> None:
    async with running_app(settings) as (app, _):
        breaker = app.state.circuit_breakers.get("HARBOR")
        assert sample("circuit_breaker_state", partner="HARBOR") == 0
        for _ in range(settings.circuit_breaker_failure_threshold):
            breaker.record_failure()

    assert sample("circuit_breaker_state", partner="HARBOR") == 2


async def test_transfers_above_the_platform_limit_are_rejected(settings: Settings) -> None:
    capped = settings.model_copy(update={"max_transfer_points": 5_000})

    async with running_app(capped) as (_, client):
        response = await transfer(client, *CARD_TO_AIRLINE, 6_000)

    assert response.status_code == 422
    assert response.json()["code"] == "ABOVE_MAXIMUM"
    assert response.json()["max_source_points"] == 5_000
