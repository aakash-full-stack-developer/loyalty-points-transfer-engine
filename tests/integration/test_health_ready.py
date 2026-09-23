import httpx

from app.config import Settings
from app.main import create_app


async def test_readiness_is_ok_when_database_and_redis_are_reachable(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_readiness_is_503_when_redis_is_unreachable(settings: Settings) -> None:
    broken = settings.model_copy(
        update={"redis_url": "redis://127.0.0.1:1/0", "health_check_timeout_seconds": 1.0}
    )
    app = create_app(broken)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "redis": "unavailable"},
    }
