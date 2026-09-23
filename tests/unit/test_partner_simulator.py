from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from partner_simulator.main import create_app
from partner_simulator.state import SimulatorState


@pytest.fixture
async def sim() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(SimulatorState())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        yield client


async def configure(sim: httpx.AsyncClient, mode: str, **extra: Any) -> None:
    response = await sim.post(
        "/simulator/config", json={"partner_code": "SKYWARD", "mode": mode, **extra}
    )
    assert response.status_code == 200, response.text


async def credit(
    sim: httpx.AsyncClient, reference: str = "tr_1", points: int = 1_000
) -> httpx.Response:
    return await sim.post(
        "/partners/SKYWARD/credits",
        json={"reference": reference, "member_id": "SKY100200301", "points": points},
    )


async def credit_count(sim: httpx.AsyncClient) -> int:
    return len((await sim.get("/simulator/credits")).json())


async def test_same_reference_twice_credits_once(sim: httpx.AsyncClient) -> None:
    first = await credit(sim)
    second = await credit(sim)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["idempotent-replayed"] == "true"
    assert first.json()["confirmation_id"] == second.json()["confirmation_id"]
    assert await credit_count(sim) == 1


async def test_reusing_a_reference_for_a_different_credit_is_a_conflict(
    sim: httpx.AsyncClient,
) -> None:
    await credit(sim, points=1_000)

    response = await credit(sim, points=2_000)

    assert response.status_code == 409
    assert await credit_count(sim) == 1


async def test_status_is_404_until_the_credit_exists(sim: httpx.AsyncClient) -> None:
    assert (await sim.get("/partners/SKYWARD/credits/tr_1")).status_code == 404

    created = await credit(sim)
    found = await sim.get("/partners/SKYWARD/credits/tr_1")

    assert found.status_code == 200
    assert found.json()["confirmation_id"] == created.json()["confirmation_id"]


@pytest.mark.parametrize(
    ("mode", "status_code"),
    [("reject", 422), ("error", 500), ("unavailable", 503)],
)
async def test_failure_modes_apply_nothing(
    sim: httpx.AsyncClient, mode: str, status_code: int
) -> None:
    await configure(sim, mode)

    response = await credit(sim)

    assert response.status_code == status_code
    assert await credit_count(sim) == 0


async def test_timeout_mode_applies_nothing(sim: httpx.AsyncClient) -> None:
    await configure(sim, "timeout", delay_ms=10)

    response = await credit(sim)

    assert response.status_code == 504
    assert await credit_count(sim) == 0


async def test_timeout_after_commit_applies_the_credit(sim: httpx.AsyncClient) -> None:
    await configure(sim, "timeout_after_commit", delay_ms=10)

    response = await credit(sim)

    assert response.status_code == 201
    assert await credit_count(sim) == 1


async def test_slow_mode_still_succeeds(sim: httpx.AsyncClient) -> None:
    await configure(sim, "slow", delay_ms=10)

    assert (await credit(sim)).status_code == 201


@pytest.mark.parametrize(("failure_rate", "status_code"), [(1.0, 500), (0.0, 201)])
async def test_flaky_mode_fails_at_the_configured_rate(
    sim: httpx.AsyncClient, failure_rate: float, status_code: int
) -> None:
    await configure(sim, "flaky", failure_rate=failure_rate)

    assert (await credit(sim)).status_code == status_code


async def test_outage_modes_also_fail_status_checks(sim: httpx.AsyncClient) -> None:
    await credit(sim)
    await configure(sim, "unavailable")

    assert (await sim.get("/partners/SKYWARD/credits/tr_1")).status_code == 503


async def test_modes_are_per_partner(sim: httpx.AsyncClient) -> None:
    await configure(sim, "error")

    response = await sim.post(
        "/partners/STAYWELL/credits",
        json={"reference": "tr_1", "member_id": "SW-8812-0001", "points": 1_000},
    )

    assert response.status_code == 201


async def test_config_uses_mode_default_delays_and_can_be_read_back(
    sim: httpx.AsyncClient,
) -> None:
    await configure(sim, "timeout")

    configs = (await sim.get("/simulator/config")).json()

    assert configs == [
        {"partner_code": "SKYWARD", "mode": "timeout", "delay_ms": 10_000, "failure_rate": 0.5}
    ]


async def test_reset_clears_credits_and_modes(sim: httpx.AsyncClient) -> None:
    await credit(sim)
    await configure(sim, "error")

    assert (await sim.post("/simulator/reset")).status_code == 204

    assert await credit_count(sim) == 0
    assert (await sim.get("/simulator/config")).json() == []
