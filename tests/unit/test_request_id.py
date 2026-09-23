import httpx
import pytest
from fastapi import FastAPI

from app.observability.middleware import REQUEST_ID_HEADER


async def _get_live(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health/live", headers=headers)


async def test_request_id_is_generated_when_missing(app: FastAPI) -> None:
    response = await _get_live(app)

    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == 32  # uuid4 hex


async def test_valid_incoming_request_id_is_propagated(app: FastAPI) -> None:
    response = await _get_live(app, headers={REQUEST_ID_HEADER: "client-req-123"})

    assert response.headers[REQUEST_ID_HEADER] == "client-req-123"


@pytest.mark.parametrize(
    "unsafe_id",
    ["x" * 129, "has spaces", "new\\nline", "<script>"],
)
async def test_unsafe_incoming_request_id_is_replaced(app: FastAPI, unsafe_id: str) -> None:
    response = await _get_live(app, headers={REQUEST_ID_HEADER: unsafe_id})

    assert response.headers[REQUEST_ID_HEADER] != unsafe_id
    assert len(response.headers[REQUEST_ID_HEADER]) == 32
