import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.config import Settings

pytestmark = pytest.mark.usefixtures("seeded")


@pytest.fixture
def admin_headers(settings: Settings) -> dict[str, str]:
    return {"X-Admin-Key": settings.admin_api_key.get_secret_value()}


def rate_body(source: str, destination: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_program": source,
        "destination_program": destination,
        "numerator": 2,
        "denominator": 1,
        "min_source_points": 1_000,
        "source_increment": 1_000,
        "max_source_points": 500_000,
    }
    return {**body, **overrides}


async def quote(client: httpx.AsyncClient, source: str, destination: str, points: int) -> Any:
    body = {"source_program": source, "destination_program": destination, "source_points": points}
    response = await client.post("/v1/quotes", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Key": "wrong-key"}], ids=["missing", "wrong"])
async def test_admin_endpoints_require_the_admin_key(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/v1/admin/rates", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "ADMIN_AUTH_REQUIRED"


async def test_new_rate_version_closes_previous_and_is_used_by_next_quote(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    # Warm the cache with version 1 so the test also proves invalidation.
    before = await quote(client, "NOVA_REWARDS", "SKYWARD_MILES", 10_000)
    assert before["rate"]["version"] == 1

    created = await client.post(
        "/v1/admin/rates", json=rate_body("NOVA_REWARDS", "SKYWARD_MILES"), headers=admin_headers
    )

    assert created.status_code == 201
    new_version = created.json()
    assert (new_version["version"], new_version["is_current"]) == (2, True)

    listing = await client.get(
        "/v1/admin/rates",
        params={"source_program": "NOVA_REWARDS", "destination_program": "SKYWARD_MILES"},
        headers=admin_headers,
    )
    versions = {rate["version"]: rate for rate in listing.json()["data"]}
    assert set(versions) == {1, 2}
    old = versions[1]
    # Version 1 is closed exactly when version 2 starts, and its terms are unchanged, so a
    # transfer that references rate_id 1 still describes what it was priced with.
    assert old["is_current"] is False
    assert old["effective_to"] == new_version["effective_from"]
    assert (old["id"], old["numerator"], old["denominator"]) == (before["rate"]["rate_id"], 1, 1)

    after = await quote(client, "NOVA_REWARDS", "SKYWARD_MILES", 10_000)
    assert after["rate"]["version"] == 2
    assert (after["base_points"], after["bonus_points"]) == (20_000, 5_000)


async def test_concurrent_rate_changes_get_consecutive_versions(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/admin/rates",
                json=rate_body("NOVA_REWARDS", "STAYWELL_POINTS", numerator=n),
                headers=admin_headers,
            )
            for n in range(2, 7)
        )
    )

    assert all(response.status_code == 201 for response in responses)
    listing = await client.get(
        "/v1/admin/rates",
        params={"source_program": "NOVA_REWARDS", "destination_program": "STAYWELL_POINTS"},
        headers=admin_headers,
    )
    rates = listing.json()["data"]
    assert sorted(rate["version"] for rate in rates) == [1, 2, 3, 4, 5, 6]
    assert sum(rate["is_current"] for rate in rates) == 1


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"max_source_points": 500}, "INVALID_RATE"),
        ({"min_source_points": 1_500}, "INVALID_RATE"),
        ({"destination_program": "NOVA_REWARDS"}, "SAME_PROGRAM"),
        ({"destination_program": "NOT_A_PROGRAM"}, "PROGRAM_NOT_FOUND"),
        ({"numerator": 0}, "VALIDATION_ERROR"),
        ({"denominator": 1.5}, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_rates_are_rejected(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    overrides: dict[str, Any],
    code: str,
) -> None:
    response = await client.post(
        "/v1/admin/rates",
        json=rate_body("NOVA_REWARDS", "SKYWARD_MILES", **overrides),
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == code


def bonus_body(source: str, destination: str, starts_at: datetime, ends_at: datetime) -> Any:
    return {
        "source_program": source,
        "destination_program": destination,
        "bonus_bps": 1_000,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
    }


async def test_new_bonus_applies_to_the_next_quote(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    before = await quote(client, "NOVA_REWARDS", "STAYWELL_POINTS", 1_000)
    assert before["bonus_points"] == 0
    now = datetime.now(UTC)

    created = await client.post(
        "/v1/admin/bonuses",
        json=bonus_body(
            "NOVA_REWARDS", "STAYWELL_POINTS", now - timedelta(minutes=1), now + timedelta(days=7)
        ),
        headers=admin_headers,
    )

    assert created.status_code == 201
    after = await quote(client, "NOVA_REWARDS", "STAYWELL_POINTS", 1_000)
    assert (after["base_points"], after["bonus_points"]) == (2_000, 200)


async def test_overlapping_bonus_is_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    now = datetime.now(UTC)
    response = await client.post(
        "/v1/admin/bonuses",
        json=bonus_body(
            "NOVA_REWARDS", "SKYWARD_MILES", now + timedelta(days=1), now + timedelta(days=2)
        ),
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "BONUS_OVERLAP"


@pytest.mark.parametrize(
    ("source", "destination", "start_offset", "end_offset", "code"),
    [
        ("SKYWARD_MILES", "ZENITH_POINTS", 0, 1, "ROUTE_NOT_SUPPORTED"),
        ("NOVA_REWARDS", "STAYWELL_POINTS", 2, 1, "INVALID_BONUS"),
        ("NOVA_REWARDS", "STAYWELL_POINTS", -3, -2, "INVALID_BONUS"),
    ],
)
async def test_invalid_bonuses_are_rejected(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    source: str,
    destination: str,
    start_offset: int,
    end_offset: int,
    code: str,
) -> None:
    now = datetime.now(UTC)
    response = await client.post(
        "/v1/admin/bonuses",
        json=bonus_body(
            source,
            destination,
            now + timedelta(days=start_offset),
            now + timedelta(days=end_offset),
        ),
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == code


async def test_bonus_times_must_include_a_timezone(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    body = bonus_body("NOVA_REWARDS", "STAYWELL_POINTS", datetime(2030, 1, 1), datetime(2030, 1, 2))

    response = await client.post("/v1/admin/bonuses", json=body, headers=admin_headers)

    assert response.json()["code"] == "VALIDATION_ERROR"
