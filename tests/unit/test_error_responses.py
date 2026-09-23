import httpx
from fastapi import FastAPI

from app.api.errors import PROBLEM_MEDIA_TYPE
from app.observability.middleware import REQUEST_ID_HEADER


async def _request(app: FastAPI, method: str, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


async def test_unhandled_exception_returns_generic_problem_without_internals(app: FastAPI) -> None:
    async def explode() -> None:
        raise RuntimeError("database password is hunter2")

    app.add_api_route("/boom", explode)

    response = await _request(app, "GET", "/boom")

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in response.text
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_unknown_path_returns_not_found_problem(app: FastAPI) -> None:
    response = await _request(app, "GET", "/v1/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["code"] == "NOT_FOUND"


async def test_wrong_method_returns_method_not_allowed_problem(app: FastAPI) -> None:
    response = await _request(app, "DELETE", "/health/live")

    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert "GET" in response.headers["allow"]
