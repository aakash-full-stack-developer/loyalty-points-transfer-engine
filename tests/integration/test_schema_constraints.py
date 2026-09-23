"""The database itself rejects invalid ledger and rate states, independent of application code.

Each test runs in a transaction that is rolled back. Deferred checks (journal balance) are
forced to run immediately with SET CONSTRAINTS ALL IMMEDIATE instead of waiting for COMMIT.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.ids import new_transfer_id
from scripts.seed import SeedReport


@pytest.fixture
async def conn(seeded: SeedReport, db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


async def _scalar(conn: AsyncConnection, sql: str, **params: Any) -> Any:
    return (await conn.execute(text(sql), params)).scalar_one()


async def _program_id(conn: AsyncConnection, code: str) -> int:
    return int(await _scalar(conn, "SELECT id FROM programs WHERE code = :code", code=code))


async def _user_account(conn: AsyncConnection, user_id: str, program_code: str) -> int:
    return int(
        await _scalar(
            conn,
            "SELECT a.id FROM accounts a JOIN programs p ON p.id = a.program_id "
            "WHERE a.user_id = :user_id AND p.code = :code",
            user_id=user_id,
            code=program_code,
        )
    )


async def _new_seed_journal(conn: AsyncConnection) -> int:
    sql = "INSERT INTO ledger_journals (journal_type) VALUES ('SEED') RETURNING id"
    return int(await _scalar(conn, sql))


async def _insert_entry(
    conn: AsyncConnection, journal_id: int, account_id: int, program_id: int, direction: str
) -> None:
    await conn.execute(
        text(
            "INSERT INTO ledger_entries (journal_id, account_id, program_id, direction, amount) "
            "VALUES (:journal_id, :account_id, :program_id, :direction, 100)"
        ),
        {
            "journal_id": journal_id,
            "account_id": account_id,
            "program_id": program_id,
            "direction": direction,
        },
    )


async def test_user_balance_cannot_go_negative(conn: AsyncConnection) -> None:
    account_id = await _user_account(conn, "user_alice", "NOVA_REWARDS")

    with pytest.raises(IntegrityError, match="ck_accounts_user_balance_non_negative"):
        await conn.execute(
            text("UPDATE accounts SET balance = -1 WHERE id = :id"), {"id": account_id}
        )


async def test_unbalanced_journal_is_rejected(conn: AsyncConnection) -> None:
    program_id = await _program_id(conn, "NOVA_REWARDS")
    account_id = await _user_account(conn, "user_alice", "NOVA_REWARDS")
    journal_id = await _new_seed_journal(conn)
    await _insert_entry(conn, journal_id, account_id, program_id, "CREDIT")

    with pytest.raises(IntegrityError, match="unbalanced"):
        await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_journal_must_balance_within_each_program_not_across_programs(
    conn: AsyncConnection,
) -> None:
    # 100 NOVA points credited and 100 SKYWARD miles debited: equal numbers, different units.
    nova_id = await _program_id(conn, "NOVA_REWARDS")
    skyward_id = await _program_id(conn, "SKYWARD_MILES")
    journal_id = await _new_seed_journal(conn)
    await _insert_entry(
        conn, journal_id, await _user_account(conn, "user_alice", "NOVA_REWARDS"), nova_id, "CREDIT"
    )
    await _insert_entry(
        conn,
        journal_id,
        await _user_account(conn, "user_alice", "SKYWARD_MILES"),
        skyward_id,
        "DEBIT",
    )

    with pytest.raises(IntegrityError, match="unbalanced"):
        await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_entry_program_must_match_its_account_program(conn: AsyncConnection) -> None:
    nova_account = await _user_account(conn, "user_alice", "NOVA_REWARDS")
    skyward_id = await _program_id(conn, "SKYWARD_MILES")
    journal_id = await _new_seed_journal(conn)

    with pytest.raises(IntegrityError, match="fk_ledger_entries_account_program"):
        await _insert_entry(conn, journal_id, nova_account, skyward_id, "CREDIT")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount = amount + 1",
        "DELETE FROM ledger_entries",
        "UPDATE ledger_journals SET journal_type = 'SEED'",
        "DELETE FROM ledger_journals",
    ],
)
async def test_ledger_is_append_only(conn: AsyncConnection, statement: str) -> None:
    with pytest.raises(IntegrityError, match="append-only"):
        await conn.execute(text(statement))


async def test_route_cannot_have_two_open_rate_versions(conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="uq_conversion_rates_route_open_version"):
        await conn.execute(
            text(
                "INSERT INTO conversion_rates (source_program_id, destination_program_id, "
                "numerator, denominator, min_source_points, source_increment, version) "
                "SELECT source_program_id, destination_program_id, 2, 1, 1000, 1000, 2 "
                "FROM conversion_rates WHERE version = 1 LIMIT 1"
            )
        )


async def test_bonuses_on_the_same_route_cannot_overlap(conn: AsyncConnection) -> None:
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError, match="ex_transfer_bonuses_no_overlap"):
        await conn.execute(
            text(
                "INSERT INTO transfer_bonuses (source_program_id, destination_program_id, "
                "bonus_bps, starts_at, ends_at) "
                "SELECT source_program_id, destination_program_id, 1000, :starts_at, :ends_at "
                "FROM transfer_bonuses LIMIT 1"
            ),
            {"starts_at": now + timedelta(days=1), "ends_at": now + timedelta(days=2)},
        )


async def test_transfer_can_be_settled_at_most_once(conn: AsyncConnection) -> None:
    transfer_id = new_transfer_id()
    await conn.execute(
        text(
            "INSERT INTO transfers (id, user_id, idempotency_key, source_program_id, "
            "destination_program_id, source_account_id, destination_account_id, source_points, "
            "base_points, bonus_points, destination_points, rate_id, rate_snapshot, status) "
            "SELECT :id, 'user_alice', 'key-1', r.source_program_id, r.destination_program_id, "
            "src.id, dst.id, 1000, 1000, 0, 1000, r.id, '{}'::jsonb, 'PENDING' "
            "FROM conversion_rates r "
            "JOIN accounts src ON src.program_id = r.source_program_id "
            "  AND src.user_id = 'user_alice' "
            "JOIN accounts dst ON dst.program_id = r.destination_program_id "
            "  AND dst.user_id = 'user_alice' "
            "LIMIT 1"
        ),
        {"id": transfer_id},
    )
    insert_settle = text(
        "INSERT INTO ledger_journals (journal_type, transfer_id) VALUES ('TRANSFER_SETTLE', :id)"
    )
    await conn.execute(insert_settle, {"id": transfer_id})

    with pytest.raises(IntegrityError, match="uq_ledger_journals_transfer_type"):
        await conn.execute(insert_settle, {"id": transfer_id})
