from typing import Any

import httpx
import pytest

from app.api.errors import PROBLEM_MEDIA_TYPE
from app.config import Settings
from app.main import create_app
from scripts.seed import SeedReport

pytestmark = pytest.mark.usefixtures("seeded")


def quote_body(source: str, destination: str, points: Any) -> dict[str, Any]:
    return {"source_program": source, "destination_program": destination, "source_points": points}


async def test_programs_are_listed(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/programs")

    assert response.status_code == 200
    codes = {program["code"]: program["type"] for program in response.json()["data"]}
    assert codes == {
        "NOVA_REWARDS": "CARD",
        "ZENITH_POINTS": "CARD",
        "SKYWARD_MILES": "LOYALTY",
        "STAYWELL_POINTS": "LOYALTY",
        "HARBOR_CRUISE_POINTS": "LOYALTY",
    }


async def test_quote_applies_active_bonus_on_top_of_base(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/quotes", json=quote_body("NOVA_REWARDS", "SKYWARD_MILES", 10_000)
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["base_points"], body["bonus_points"], body["destination_points"]) == (
        10_000,
        2_500,
        12_500,
    )
    assert body["rate"]["version"] == 1
    assert body["rate"]["bonus_bps"] == 2_500


async def test_quote_rounds_down_on_uneven_ratio(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/quotes", json=quote_body("ZENITH_POINTS", "HARBOR_CRUISE_POINTS", 5_000)
    )

    assert response.json()["destination_points"] == 3_333


async def test_reverse_route_uses_its_own_rate(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/quotes", json=quote_body("SKYWARD_MILES", "NOVA_REWARDS", 9_000)
    )

    assert response.json()["destination_points"] == 3_000


async def test_unsupported_route_returns_problem_details(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/quotes", json=quote_body("SKYWARD_MILES", "ZENITH_POINTS", 10_000)
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "ROUTE_NOT_SUPPORTED"
    assert body["instance"] == "/v1/quotes"
    assert body["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize(
    ("source", "destination", "points", "code"),
    [
        ("NOVA_REWARDS", "SKYWARD_MILES", 500, "BELOW_MINIMUM"),
        ("NOVA_REWARDS", "SKYWARD_MILES", 600_000, "ABOVE_MAXIMUM"),
        ("NOVA_REWARDS", "SKYWARD_MILES", 1_500, "INVALID_INCREMENT"),
        ("NOVA_REWARDS", "NOVA_REWARDS", 1_000, "SAME_PROGRAM"),
        ("NOVA_REWARDS", "UNKNOWN_PROGRAM", 1_000, "PROGRAM_NOT_FOUND"),
    ],
)
async def test_business_rule_violations_return_stable_codes(
    client: httpx.AsyncClient, source: str, destination: str, points: int, code: str
) -> None:
    response = await client.post("/v1/quotes", json=quote_body(source, destination, points))

    assert response.status_code == 422
    assert response.json()["code"] == code


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(quote_body("NOVA_REWARDS", "SKYWARD_MILES", 1000.5), id="float_points"),
        pytest.param(quote_body("NOVA_REWARDS", "SKYWARD_MILES", "1000"), id="string_points"),
        pytest.param(quote_body("NOVA_REWARDS", "SKYWARD_MILES", -1000), id="negative_points"),
        pytest.param(quote_body("nova", "SKYWARD_MILES", 1000), id="bad_program_code"),
        pytest.param(
            {**quote_body("NOVA_REWARDS", "SKYWARD_MILES", 1000), "rate": 99}, id="unknown_field"
        ),
    ],
)
async def test_invalid_requests_are_rejected_before_pricing(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.post("/v1/quotes", json=body)

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "VALIDATION_ERROR"
    assert problem["errors"]


async def test_quotes_still_work_when_redis_is_down(seeded: SeedReport, settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"}))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/quotes", json=quote_body("NOVA_REWARDS", "SKYWARD_MILES", 10_000)
        )

    assert response.status_code == 200
    assert response.json()["destination_points"] == 12_500
