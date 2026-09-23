"""Helpers shared by integration tests: ledger access and API/simulator shortcuts."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, Program
from app.domain.enums import AccountType, OwnerType
from app.domain.ids import new_transfer_id

CARD_TO_AIRLINE = ("NOVA_REWARDS", "SKYWARD_MILES")


class Clock:
    """A controllable clock for services that take one: 'an hour later' takes no time."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


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


async def clearing_balance(factory: async_sessionmaker[AsyncSession], program: str) -> int:
    async with factory() as session:
        return (await system_account(session, program, AccountType.TRANSFER_CLEARING)).balance


async def user_account(session: AsyncSession, user_id: str, program_code: str) -> Account:
    account = await session.scalar(
        select(Account)
        .join(Program, Program.id == Account.program_id)
        .where(Account.user_id == user_id, Program.code == program_code)
    )
    assert account is not None, f"no {program_code} account for {user_id}"
    return account


async def system_account(
    session: AsyncSession, program_code: str, account_type: AccountType
) -> Account:
    account = await session.scalar(
        select(Account)
        .join(Program, Program.id == Account.program_id)
        .where(
            Account.owner_type == OwnerType.SYSTEM,
            Account.account_type == account_type,
            Program.code == program_code,
        )
    )
    assert account is not None
    return account


async def insert_transfer_row(
    session: AsyncSession,
    user_id: str = "user_alice",
    source: str = "NOVA_REWARDS",
    destination: str = "SKYWARD_MILES",
    points: int = 1_000,
) -> str:
    """Insert a minimal PENDING transfer so transfer journals have something to reference.
    The real transfer flow arrives in step 7."""
    transfer_id = new_transfer_id()
    await session.execute(
        text(
            "INSERT INTO transfers (id, user_id, idempotency_key, source_program_id, "
            "destination_program_id, source_account_id, destination_account_id, source_points, "
            "base_points, bonus_points, destination_points, rate_id, rate_snapshot, status) "
            "SELECT :id, :user_id, :id, s.id, d.id, src.id, dst.id, :points, :points, 0, "
            ":points, r.id, '{}'::jsonb, 'PENDING' "
            "FROM programs s "
            "JOIN programs d ON d.code = :destination "
            "JOIN conversion_rates r "
            "  ON r.source_program_id = s.id AND r.destination_program_id = d.id "
            "JOIN accounts src ON src.program_id = s.id AND src.user_id = :user_id "
            "JOIN accounts dst ON dst.program_id = d.id AND dst.user_id = :user_id "
            "WHERE s.code = :source "
            "LIMIT 1"
        ),
        {
            "id": transfer_id,
            "user_id": user_id,
            "source": source,
            "destination": destination,
            "points": points,
        },
    )
    return transfer_id
