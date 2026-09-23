import httpx
from fastapi import FastAPI


async def test_liveness_returns_ok_without_touching_dependencies(app: FastAPI) -> None:
    # No lifespan: no database or Redis clients exist, and liveness must still succeed.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
