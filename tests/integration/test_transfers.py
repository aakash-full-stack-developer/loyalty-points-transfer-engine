"""The transfer saga end to end: real PostgreSQL, Redis, and the partner simulator over HTTP.

After every test the ledger invariants are checked, so no scenario may create or destroy
points, whatever else it asserts.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import LedgerJournal, Transfer, User
from app.domain.enums import AccountType
from app.services.ledger_invariants import find_violations
from tests.integration.conftest import running_app
from tests.integration.helpers import system_account

pytestmark = pytest.mark.usefixtures("seeded", "simulator")

Factory = async_sessionmaker[AsyncSession]
CARD_TO_AIRLINE = ("NOVA_REWARDS", "SKYWARD_MILES")


@pytest.fixture(autouse=True)
async def ledger_stays_consistent(session_factory: Factory) -> AsyncIterator[None]:
    yield
    async with session_factory() as session:
        assert await find_violations(session) == []


async def transfer(
    client: httpx.AsyncClient,
    source: str = "NOVA_REWARDS",
    destination: str = "SKYWARD_MILES",
    points: int = 10_000,
    *,
    user: str = "user_alice",
    key: str | None = None,
) -> httpx.Response:
    body = {"source_program": source, "destination_program": destination, "source_points": points}
    return await client.post(
        "/v1/transfers",
        json=body,
        headers={"X-User-Id": user, "Idempotency-Key": key or str(uuid.uuid4())},
    )


async def balances(client: httpx.AsyncClient, user: str = "user_alice") -> dict[str, int]:
    response = await client.get("/v1/accounts", headers={"X-User-Id": user})
    return {account["program"]: account["balance"] for account in response.json()["data"]}


async def set_partner_mode(
    simulator: httpx.AsyncClient, partner: str, mode: str, **extra: Any
) -> None:
    response = await simulator.post(
        "/simulator/config", json={"partner_code": partner, "mode": mode, **extra}
    )
    assert response.status_code == 200


async def partner_credits(simulator: httpx.AsyncClient) -> list[dict[str, Any]]:
    return list((await simulator.get("/simulator/credits")).json())


async def count(factory: Factory, model: type[Transfer] | type[LedgerJournal]) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def clearing_balance(factory: Factory, program: str) -> int:
    async with factory() as session:
        return (await system_account(session, program, AccountType.TRANSFER_CLEARING)).balance


def statuses(body: dict[str, Any]) -> list[str]:
    return [event["to_status"] for event in body["events"]]


# ---------------------------------------------------------------- happy paths


async def test_card_to_loyalty_transfer_completes_and_moves_both_balances(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient, session_factory: Factory
) -> None:
    before = await balances(client)

    response = await transfer(client, *CARD_TO_AIRLINE, 10_000)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["destination"] == {
        "program": "SKYWARD_MILES",
        "points": 12_500,
        "base_points": 10_000,
        "bonus_points": 2_500,
    }
    assert statuses(body) == ["PENDING", "SOURCE_DEBITED", "PARTNER_SUBMITTED", "COMPLETED"]
    assert body["partner_confirmation_id"]
    after = await balances(client)
    assert after["NOVA_REWARDS"] == before["NOVA_REWARDS"] - 10_000
    assert after["SKYWARD_MILES"] == before["SKYWARD_MILES"] + 12_500
    assert await clearing_balance(session_factory, "NOVA_REWARDS") == 0
    [credit] = await partner_credits(simulator)
    assert (credit["reference"], credit["points"], credit["member_id"]) == (
        body["id"],
        12_500,
        "SKY100200301",
    )


async def test_loyalty_to_card_transfer_uses_the_reverse_rate(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient
) -> None:
    before = await balances(client)

    response = await transfer(client, "SKYWARD_MILES", "NOVA_REWARDS", 9_000)

    assert response.status_code == 201
    body = response.json()
    assert (body["status"], body["destination"]["points"]) == ("COMPLETED", 3_000)
    assert (body["rate"]["numerator"], body["rate"]["denominator"]) == (1, 3)
    after = await balances(client)
    assert after["SKYWARD_MILES"] == before["SKYWARD_MILES"] - 9_000
    assert after["NOVA_REWARDS"] == before["NOVA_REWARDS"] + 3_000
    [credit] = await partner_credits(simulator)
    assert (credit["member_id"], credit["points"]) == ("NOVA-4410-7730-0001", 3_000)


# ---------------------------------------------------------------- automatic rollback


async def test_partner_rejection_reverses_the_debit_and_restores_the_balance(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient, session_factory: Factory
) -> None:
    await set_partner_mode(simulator, "SKYWARD", "reject")
    before = await balances(client)

    response = await transfer(client, *CARD_TO_AIRLINE, 10_000)

    assert response.status_code == 201  # final state; `status` says what happened
    body = response.json()
    assert body["status"] == "REVERSED"
    assert body["failure"]["code"] == "PARTNER_REJECTED"
    assert statuses(body) == ["PENDING", "SOURCE_DEBITED", "PARTNER_SUBMITTED", "REVERSED"]
    assert await balances(client) == before
    assert await clearing_balance(session_factory, "NOVA_REWARDS") == 0
    assert await partner_credits(simulator) == []


async def test_unreachable_partner_reverses_with_partner_unavailable(
    seeded: object, settings: Settings
) -> None:
    unreachable = settings.model_copy(update={"partner_base_url": "http://127.0.0.1:1"})
    async with running_app(unreachable) as (_, client):
        before = await balances(client)
        response = await transfer(client, *CARD_TO_AIRLINE, 10_000)
        after = await balances(client)

    body = response.json()
    assert (response.status_code, body["status"]) == (201, "REVERSED")
    assert body["failure"]["code"] == "PARTNER_UNAVAILABLE"
    assert after == before


async def test_open_circuit_fails_fast_and_reverses_without_calling_the_partner(
    app: FastAPI,
    client: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    settings: Settings,
) -> None:
    breaker = app.state.circuit_breakers.get("SKYWARD")
    for _ in range(settings.circuit_breaker_failure_threshold):
        breaker.record_failure()
    before = await balances(client)

    response = await transfer(client, *CARD_TO_AIRLINE, 10_000)

    body = response.json()
    assert (body["status"], body["failure"]["code"]) == ("REVERSED", "PARTNER_UNAVAILABLE")
    assert body["events"][-1]["metadata"]["partner_reason"] == "circuit_open"
    assert await balances(client) == before
    assert await partner_credits(simulator) == []


# ---------------------------------------------------------------- unknown outcomes


async def assert_pending_with_points_in_clearing(
    client: httpx.AsyncClient,
    session_factory: Factory,
    response: httpx.Response,
    before: dict[str, int],
) -> None:
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING_VERIFICATION"
    assert body["failure"] is None
    after = await balances(client)
    # Debited, not yet credited in our ledger: the points wait in clearing, not lost.
    assert after["NOVA_REWARDS"] == before["NOVA_REWARDS"] - 10_000
    assert after["SKYWARD_MILES"] == before["SKYWARD_MILES"]
    assert await clearing_balance(session_factory, "NOVA_REWARDS") == 10_000
    async with session_factory() as session:
        stored = await session.get(Transfer, body["id"])
    assert stored is not None
    assert stored.next_verification_at is not None


async def test_partner_timeout_leaves_points_in_clearing_pending_verification(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient, session_factory: Factory
) -> None:
    await set_partner_mode(simulator, "SKYWARD", "timeout", delay_ms=2_500)
    before = await balances(client)

    response = await transfer(client, *CARD_TO_AIRLINE, 10_000)

    await assert_pending_with_points_in_clearing(client, session_factory, response, before)
    assert await partner_credits(simulator) == []  # the reconciler will reverse it (step 8)


async def test_timeout_after_commit_without_retries_is_pending_although_partner_credited(
    seeded: object, settings: Settings, simulator: httpx.AsyncClient, session_factory: Factory
) -> None:
    """The dangerous case: we saw a timeout, but the partner applied the credit. Reversing
    now would pay the user twice; the transfer must wait for verification instead."""
    await set_partner_mode(simulator, "SKYWARD", "timeout_after_commit", delay_ms=1_500)
    no_retries = settings.model_copy(update={"partner_retry_max_attempts": 1})
    async with running_app(no_retries) as (_, client):
        before = await balances(client)
        response = await transfer(client, *CARD_TO_AIRLINE, 10_000)
        await assert_pending_with_points_in_clearing(client, session_factory, response, before)

    await asyncio.sleep(0.6)  # the partner finishes its delayed response
    assert len(await partner_credits(simulator)) == 1  # the reconciler will complete it


async def test_retry_after_timeout_after_commit_finds_the_credit_via_idempotent_replay(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient
) -> None:
    """Attempt 1 is applied but times out; attempt 2 reuses the reference and the partner
    answers with the original credit. The unknown outcome resolves itself, safely."""
    await set_partner_mode(simulator, "SKYWARD", "timeout_after_commit", delay_ms=2_500)

    response = await transfer(client, *CARD_TO_AIRLINE, 10_000)

    body = response.json()
    assert (response.status_code, body["status"]) == (201, "COMPLETED")
    assert body["events"][-1]["metadata"]["attempts"] == 2
    assert len(await partner_credits(simulator)) == 1


# ---------------------------------------------------------------- business errors


async def test_insufficient_balance_creates_no_transfer_and_no_ledger_rows(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    journals_before = await count(session_factory, LedgerJournal)

    response = await transfer(client, *CARD_TO_AIRLINE, 100_000, user="user_bob")

    assert response.status_code == 422
    assert response.json()["code"] == "INSUFFICIENT_BALANCE"
    assert await count(session_factory, Transfer) == 0
    assert await count(session_factory, LedgerJournal) == journals_before


@pytest.mark.parametrize(
    ("source", "destination", "points", "user", "code"),
    [
        ("SKYWARD_MILES", "ZENITH_POINTS", 10_000, "user_alice", "ROUTE_NOT_SUPPORTED"),
        ("NOVA_REWARDS", "SKYWARD_MILES", 1_500, "user_alice", "INVALID_INCREMENT"),
        ("NOVA_REWARDS", "SKYWARD_MILES", 1_000, "user_carol", "ACCOUNT_NOT_FOUND"),
    ],
)
async def test_rejected_requests_create_nothing(
    client: httpx.AsyncClient,
    session_factory: Factory,
    source: str,
    destination: str,
    points: int,
    user: str,
    code: str,
) -> None:
    async with session_factory() as session, session.begin():
        session.add(User(id="user_carol"))  # an existing user with no linked accounts

    response = await transfer(client, source, destination, points, user=user)

    assert response.status_code == 422
    assert response.json()["code"] == code
    assert await count(session_factory, Transfer) == 0


# ---------------------------------------------------------------- rate snapshots


async def test_rate_change_after_a_transfer_does_not_change_its_snapshot(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    old = (await transfer(client, *CARD_TO_AIRLINE, 10_000)).json()
    new_rate = await client.post(
        "/v1/admin/rates",
        json={
            "source_program": "NOVA_REWARDS",
            "destination_program": "SKYWARD_MILES",
            "numerator": 2,
            "denominator": 1,
            "min_source_points": 1_000,
            "source_increment": 1_000,
        },
        headers={"X-Admin-Key": settings.admin_api_key.get_secret_value()},
    )
    assert new_rate.status_code == 201

    reread = await client.get(f"/v1/transfers/{old['id']}", headers={"X-User-Id": "user_alice"})
    new = (await transfer(client, *CARD_TO_AIRLINE, 10_000)).json()

    assert reread.json()["rate"] == old["rate"]
    assert reread.json()["rate"]["version"] == 1
    assert reread.json()["destination"]["points"] == 12_500
    assert (new["rate"]["version"], new["destination"]["points"]) == (2, 25_000)


# ---------------------------------------------------------------- idempotency and concurrency


async def test_same_idempotency_key_replays_without_a_second_debit(
    client: httpx.AsyncClient, simulator: httpx.AsyncClient, session_factory: Factory
) -> None:
    before = await balances(client)

    first = await transfer(client, *CARD_TO_AIRLINE, 10_000, key="order-42")
    second = await transfer(client, *CARD_TO_AIRLINE, 10_000, key="order-42")

    assert first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert await count(session_factory, Transfer) == 1
    assert (await balances(client))["NOVA_REWARDS"] == before["NOVA_REWARDS"] - 10_000
    assert len(await partner_credits(simulator)) == 1


async def test_reusing_a_key_for_a_different_transfer_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    await transfer(client, *CARD_TO_AIRLINE, 10_000, key="order-43")

    response = await transfer(client, *CARD_TO_AIRLINE, 20_000, key="order-43")

    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_concurrent_transfers_never_overdraw(
    client: httpx.AsyncClient,
) -> None:
    # Bob has 50,000 NOVA points: only two of five 20,000-point transfers can succeed.
    responses = await asyncio.gather(
        *(transfer(client, *CARD_TO_AIRLINE, 20_000, user="user_bob") for _ in range(5))
    )

    outcomes = sorted(
        # Error bodies carry a `code`; transfer bodies a string `status`.
        response.json().get("code") or response.json()["status"]
        for response in responses
    )
    assert outcomes == ["COMPLETED"] * 2 + ["INSUFFICIENT_BALANCE"] * 3
    assert (await balances(client, "user_bob"))["NOVA_REWARDS"] == 10_000


async def test_opposite_direction_transfers_for_one_user_all_complete(
    client: httpx.AsyncClient,
) -> None:
    requests = [transfer(client, "NOVA_REWARDS", "SKYWARD_MILES", 3_000) for _ in range(5)]
    requests += [transfer(client, "SKYWARD_MILES", "NOVA_REWARDS", 3_000) for _ in range(5)]

    responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=30)

    assert {response.json()["status"] for response in responses} == {"COMPLETED"}


# ---------------------------------------------------------------- reading transfers


async def test_transfer_detail_is_only_visible_to_its_owner(client: httpx.AsyncClient) -> None:
    transfer_id = (await transfer(client)).json()["id"]

    own = await client.get(f"/v1/transfers/{transfer_id}", headers={"X-User-Id": "user_alice"})
    other = await client.get(f"/v1/transfers/{transfer_id}", headers={"X-User-Id": "user_bob"})
    bogus = await client.get("/v1/transfers/not-an-id", headers={"X-User-Id": "user_alice"})

    assert own.status_code == 200
    assert statuses(own.json())[-1] == "COMPLETED"
    assert (other.status_code, other.json()["code"]) == (404, "TRANSFER_NOT_FOUND")
    assert bogus.status_code == 404


async def test_listing_is_newest_first_with_cursor_pagination(client: httpx.AsyncClient) -> None:
    created = [(await transfer(client, points=1_000 * (n + 1))).json()["id"] for n in range(3)]
    alice = {"X-User-Id": "user_alice"}

    first_page = (await client.get("/v1/transfers", params={"limit": 2}, headers=alice)).json()
    second_page = (
        await client.get(
            "/v1/transfers",
            params={"limit": 2, "cursor": first_page["next_cursor"]},
            headers=alice,
        )
    ).json()

    listed = [item["id"] for item in first_page["data"] + second_page["data"]]
    assert listed == list(reversed(created))
    assert second_page["next_cursor"] is None
    bob = (await client.get("/v1/transfers", headers={"X-User-Id": "user_bob"})).json()
    assert bob == {"data": [], "next_cursor": None}


async def test_invalid_cursor_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/v1/transfers", params={"cursor": "garbage!"}, headers={"X-User-Id": "user_alice"}
    )

    assert (response.status_code, response.json()["code"]) == (400, "INVALID_CURSOR")


# ---------------------------------------------------------------- request guards


async def test_idempotency_key_is_required(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/transfers",
        json={
            "source_program": "NOVA_REWARDS",
            "destination_program": "SKYWARD_MILES",
            "source_points": 1_000,
        },
        headers={"X-User-Id": "user_alice"},
    )

    assert (response.status_code, response.json()["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")


async def test_transfers_are_rate_limited_per_user(seeded: object, settings: Settings) -> None:
    limited = settings.model_copy(update={"rate_limit_transfers_per_window": 2})
    async with running_app(limited) as (_, client):
        responses = [await transfer(client, points=1_000) for _ in range(3)]
        other_user = await transfer(client, points=1_000, user="user_bob")

    assert [response.status_code for response in responses[:2]] == [201, 201]
    assert responses[2].status_code == 429
    assert responses[2].json()["code"] == "RATE_LIMITED"
    assert int(responses[2].headers["retry-after"]) > 0
    assert other_user.status_code == 201
