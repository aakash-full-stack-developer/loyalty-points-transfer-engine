"""Idempotency end to end, through a small test route wrapped by `run_idempotent`.

The real POST /v1/transfers (step 7) uses the same wrapper; these tests pin down its
behaviour independently of the transfer logic.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUserId, IdempotencyServiceDep
from app.api.idempotency import HandlerResult, IdempotencyKeyHeader, run_idempotent
from app.config import Settings
from app.db.models import IdempotencyKey
from app.domain.errors import DomainError, ErrorCode
from app.main import create_app
from app.services.idempotency import IdempotencyService, compute_fingerprint
from scripts.cleanup_idempotency import delete_expired_keys

pytestmark = pytest.mark.usefixtures("seeded")

PATH = "/test/idempotent"


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: StrictInt
    note: str = ""


@dataclass
class Worker:
    """The protected side effect. `calls` proves how many times it really ran."""

    calls: int = 0
    delay: float = 0.0
    fail_with: BaseException | None = None  # raised once, on the next call

    async def __call__(self, body: Payload) -> HandlerResult:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        return HandlerResult(201, {"id": f"res_{self.calls}", "amount": body.amount})


def add_test_route(app: FastAPI) -> Worker:
    worker = Worker()

    @app.post(PATH)
    async def endpoint(
        request: Request,
        body: Payload,
        user_id: CurrentUserId,
        key: IdempotencyKeyHeader,
        idempotency: IdempotencyServiceDep,
    ) -> JSONResponse:
        return await run_idempotent(
            idempotency,
            request,
            user_id=user_id,
            key=key,
            fingerprint=compute_fingerprint("POST", PATH, body.model_dump(mode="json")),
            handler=lambda: worker(body),
        )

    return worker


@pytest.fixture
def worker(app: FastAPI) -> Worker:
    return add_test_route(app)


def headers(key: str = "key-1", user: str = "user_alice") -> dict[str, str]:
    return {"X-User-Id": user, "Idempotency-Key": key}


async def post(
    client: httpx.AsyncClient, amount: int = 100, key: str = "key-1", user: str = "user_alice"
) -> httpx.Response:
    return await client.post(PATH, json={"amount": amount}, headers=headers(key, user))


async def key_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(IdempotencyKey)) or 0


# ---------------------------------------------------------------- replay and mismatch


async def test_same_key_and_body_replays_the_stored_response(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    first = await post(client)
    second = await post(client)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert worker.calls == 1


async def test_json_formatting_and_key_order_do_not_matter(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    await client.post(PATH, content='{"amount":100,"note":"x"}', headers=headers())
    replay = await client.post(
        PATH, content='{ "note" : "x",\n  "amount" : 100 }', headers=headers()
    )

    assert replay.headers["idempotent-replayed"] == "true"
    assert worker.calls == 1


async def test_same_key_with_a_different_body_is_rejected(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    await post(client, amount=100)

    response = await post(client, amount=999)

    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert worker.calls == 1


async def test_keys_are_scoped_per_user(client: httpx.AsyncClient, worker: Worker) -> None:
    alice = await post(client, user="user_alice")
    bob = await post(client, user="user_bob")

    assert alice.status_code == bob.status_code == 201
    assert "idempotent-replayed" not in bob.headers
    assert worker.calls == 2


# ---------------------------------------------------------------- concurrency


async def test_concurrent_requests_with_the_same_key_run_once(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    worker.delay = 0.3

    responses = await asyncio.gather(*(post(client) for _ in range(5)))

    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409, 409, 409, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    assert worker.calls == 1
    # Once the first request finishes, a retry gets its response.
    assert (await post(client)).headers["idempotent-replayed"] == "true"


async def test_redis_outage_still_admits_exactly_one_request(
    seeded: object, settings: Settings
) -> None:
    app = create_app(settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"}))
    worker = add_test_route(app)
    worker.delay = 0.3

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        responses = await asyncio.gather(*(post(client) for _ in range(5)))
        replay = await post(client)

    assert sorted(response.status_code for response in responses) == [201, 409, 409, 409, 409]
    assert replay.headers["idempotent-replayed"] == "true"
    assert worker.calls == 1


# ---------------------------------------------------------------- failures


async def test_unexpected_error_releases_the_key_so_a_retry_can_proceed(
    client: httpx.AsyncClient,
    worker: Worker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker.fail_with = RuntimeError("database connection lost")

    failed = await post(client)

    assert failed.status_code == 500
    assert await key_rows(session_factory) == 0

    retried = await post(client)
    assert retried.status_code == 201
    assert "idempotent-replayed" not in retried.headers
    assert worker.calls == 2


async def test_business_error_is_stored_and_replayed(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    worker.fail_with = DomainError(ErrorCode.INSUFFICIENT_BALANCE, "not enough points")

    first = await post(client)
    second = await post(client)

    assert first.status_code == second.status_code == 422
    assert first.json() == second.json()
    assert second.json()["code"] == "INSUFFICIENT_BALANCE"
    assert second.headers["content-type"] == "application/problem+json"
    assert second.headers["idempotent-replayed"] == "true"
    assert worker.calls == 1


# ---------------------------------------------------------------- header validation


@pytest.mark.parametrize(
    ("key_header", "code"),
    [
        (None, "IDEMPOTENCY_KEY_REQUIRED"),
        ("x" * 256, "IDEMPOTENCY_KEY_INVALID"),
        ("tab\tkey", "IDEMPOTENCY_KEY_INVALID"),
    ],
)
async def test_missing_or_invalid_key_is_rejected_before_any_work(
    client: httpx.AsyncClient, worker: Worker, key_header: str | None, code: str
) -> None:
    request_headers = {"X-User-Id": "user_alice"}
    if key_header is not None:
        request_headers["Idempotency-Key"] = key_header

    response = await client.post(PATH, json={"amount": 100}, headers=request_headers)

    assert response.status_code == 400
    assert response.json()["code"] == code
    assert worker.calls == 0


# ---------------------------------------------------------------- expiry and recovery


async def test_expired_key_is_treated_as_new(
    client: httpx.AsyncClient,
    worker: Worker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await post(client, amount=100)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE idempotency_keys SET created_at = now() - interval '2 days', "
                "expires_at = now() - interval '1 day'"
            )
        )

    response = await post(client, amount=999)  # a different body is fine once expired

    assert response.status_code == 201
    assert worker.calls == 2


@pytest.mark.parametrize(
    ("age", "expected_status"),
    [(timedelta(hours=1), 201), (timedelta(seconds=1), 409)],
    ids=["abandoned_is_taken_over", "recent_is_in_progress"],
)
async def test_in_progress_record_without_a_lock(
    client: httpx.AsyncClient,
    worker: Worker,
    session_factory: async_sessionmaker[AsyncSession],
    age: timedelta,
    expected_status: int,
) -> None:
    """An IN_PROGRESS row with no Redis lock: either a crashed request (old) or a request
    running while Redis was down (recent). Only the old one may be taken over."""
    created = datetime.now(UTC) - age
    async with session_factory() as session, session.begin():
        session.add(
            IdempotencyKey(
                user_id="user_alice",
                key="key-1",
                request_fingerprint=compute_fingerprint("POST", PATH, {"amount": 100, "note": ""}),
                status="IN_PROGRESS",
                created_at=created,
                expires_at=created + timedelta(days=1),
            )
        )

    response = await post(client)

    assert response.status_code == expected_status


async def test_cleanup_deletes_only_expired_keys(
    client: httpx.AsyncClient,
    worker: Worker,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await post(client, key="old")
    await post(client, key="fresh")
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE idempotency_keys SET created_at = now() - interval '2 days', "
                "expires_at = now() - interval '1 day' WHERE key = 'old'"
            )
        )

    deleted = await delete_expired_keys(settings.database_url)

    assert deleted == 1
    assert await key_rows(session_factory) == 1


# ---------------------------------------------------------------- the Redis lock


async def test_a_request_can_only_release_its_own_lock(
    session_factory: async_sessionmaker[AsyncSession], clean_redis: Redis
) -> None:
    service = IdempotencyService(
        session_factory, clean_redis, key_ttl=timedelta(days=1), lock_ttl=timedelta(seconds=30)
    )
    await clean_redis.set("idempotency:lock:test", "owner-token")

    await service._release_lock("idempotency:lock:test", "someone-else")
    assert await clean_redis.get("idempotency:lock:test") == "owner-token"

    await service._release_lock("idempotency:lock:test", "owner-token")
    assert await clean_redis.get("idempotency:lock:test") is None


async def test_stored_response_survives_as_plain_json(
    client: httpx.AsyncClient,
    worker: Worker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await post(client)

    async with session_factory() as session:
        row = await session.scalar(select(IdempotencyKey))
    assert row is not None
    assert (row.status, row.response_status_code) == ("COMPLETED", 201)
    assert json.loads(json.dumps(row.response_body)) == {"id": "res_1", "amount": 100}
