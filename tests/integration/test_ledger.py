import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, LedgerEntry, LedgerJournal
from app.db.session import unit_of_work
from app.domain.enums import AccountType, JournalType
from app.domain.errors import DomainError, ErrorCode
from app.services.ledger import (
    JournalAlreadyPostedError,
    LedgerError,
    Posting,
    lock_account_for_update,
    lock_accounts_for_update,
    post_journal,
)
from app.services.ledger_invariants import find_violations
from tests.integration.helpers import insert_transfer_row, system_account, user_account

pytestmark = pytest.mark.usefixtures("seeded")

Factory = async_sessionmaker[AsyncSession]


async def balance_of(factory: Factory, account_id: int) -> int:
    async with factory() as session:
        balance = await session.scalar(select(Account.balance).where(Account.id == account_id))
        assert balance is not None
        return balance


async def row_counts(factory: Factory) -> tuple[int, int]:
    async with factory() as session:
        journals = await session.scalar(select(func.count()).select_from(LedgerJournal))
        entries = await session.scalar(select(func.count()).select_from(LedgerEntry))
        return journals or 0, entries or 0


async def debit_to_clearing(
    factory: Factory, user_id: str, amount: int, *, check_under_lock: bool = True
) -> None:
    """The shape of a transfer debit: user source account -> source TRANSFER_CLEARING.
    Lock first, then insert the transfer that references the account (as step 7 will)."""
    async with unit_of_work(factory) as session:
        source = await user_account(session, user_id, "NOVA_REWARDS")
        clearing = await system_account(session, "NOVA_REWARDS", AccountType.TRANSFER_CLEARING)
        if check_under_lock:
            locked = await lock_account_for_update(session, source.id)
            if locked.balance < amount:
                raise DomainError(ErrorCode.INSUFFICIENT_BALANCE, "not enough points")
        transfer_id = await insert_transfer_row(session, user_id, points=amount)
        await post_journal(
            session,
            JournalType.TRANSFER_DEBIT,
            [Posting.debit(source, amount), Posting.credit(clearing, amount)],
            transfer_id=transfer_id,
        )


async def test_posting_a_journal_moves_balances_and_records_entries(
    session_factory: Factory,
) -> None:
    async with session_factory() as session:
        source = await user_account(session, "user_alice", "NOVA_REWARDS")
        clearing = await system_account(session, "NOVA_REWARDS", AccountType.TRANSFER_CLEARING)
    journals_before, entries_before = await row_counts(session_factory)

    await debit_to_clearing(session_factory, "user_alice", 10_000)

    assert await balance_of(session_factory, source.id) == source.balance - 10_000
    assert await balance_of(session_factory, clearing.id) == clearing.balance + 10_000
    assert await row_counts(session_factory) == (journals_before + 1, entries_before + 2)


async def test_insufficient_balance_is_reported_and_nothing_is_written(
    session_factory: Factory,
) -> None:
    async with session_factory() as session:
        source = await user_account(session, "user_bob", "NOVA_REWARDS")
    before = await row_counts(session_factory)

    # Skip the application-level check: the database CHECK constraint must still refuse.
    with pytest.raises(DomainError) as raised:
        await debit_to_clearing(
            session_factory, "user_bob", source.balance + 1_000, check_under_lock=False
        )

    assert raised.value.code == ErrorCode.INSUFFICIENT_BALANCE
    assert await balance_of(session_factory, source.id) == source.balance
    assert await row_counts(session_factory) == before
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM transfers")) == 0


async def test_a_transfer_cannot_be_debited_twice(session_factory: Factory) -> None:
    async with unit_of_work(session_factory) as session:
        source = await user_account(session, "user_alice", "NOVA_REWARDS")
        clearing = await system_account(session, "NOVA_REWARDS", AccountType.TRANSFER_CLEARING)
        transfer_id = await insert_transfer_row(session)
        postings = [Posting.debit(source, 1_000), Posting.credit(clearing, 1_000)]
        await post_journal(session, JournalType.TRANSFER_DEBIT, postings, transfer_id)

    with pytest.raises(JournalAlreadyPostedError):
        async with unit_of_work(session_factory) as session:
            await post_journal(session, JournalType.TRANSFER_DEBIT, postings, transfer_id)


async def test_unbalanced_journal_is_rejected_before_touching_the_database(
    session_factory: Factory,
) -> None:
    before = await row_counts(session_factory)
    async with unit_of_work(session_factory) as session:
        source = await user_account(session, "user_alice", "NOVA_REWARDS")
        clearing = await system_account(session, "NOVA_REWARDS", AccountType.TRANSFER_CLEARING)
        with pytest.raises(LedgerError):
            await post_journal(
                session,
                JournalType.SEED,
                [Posting.debit(source, 1_000), Posting.credit(clearing, 999)],
            )

    assert await row_counts(session_factory) == before


@pytest.mark.parametrize("check_under_lock", [True, False], ids=["row_lock", "check_constraint"])
async def test_concurrent_debits_never_overdraw(
    session_factory: Factory, check_under_lock: bool
) -> None:
    # Alice has 250,000 NOVA points: at most two of ten concurrent 100,000 debits can succeed.
    results = await asyncio.gather(
        *(
            debit_to_clearing(
                session_factory, "user_alice", 100_000, check_under_lock=check_under_lock
            )
            for _ in range(10)
        ),
        return_exceptions=True,
    )

    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 8
    assert all(
        isinstance(failure, DomainError) and failure.code == ErrorCode.INSUFFICIENT_BALANCE
        for failure in failures
    )
    async with session_factory() as session:
        source = await user_account(session, "user_alice", "NOVA_REWARDS")
        assert source.balance == 50_000
        assert await find_violations(session) == []


async def test_opposite_direction_journals_do_not_deadlock(session_factory: Factory) -> None:
    """Alice -> Bob and Bob -> Alice at the same time touch the same two rows in opposite
    orders. Updating rows in ascending id order means neither can wait on the other."""

    async def move(sender: str, receiver: str) -> None:
        async with unit_of_work(session_factory) as session:
            source = await user_account(session, sender, "NOVA_REWARDS")
            target = await user_account(session, receiver, "NOVA_REWARDS")
            await lock_accounts_for_update(session, [target.id, source.id])
            transfer_id = await insert_transfer_row(session, sender, points=100)
            await asyncio.sleep(0.01)  # widen the window in which both hold locks
            await post_journal(
                session,
                JournalType.TRANSFER_DEBIT,
                [Posting.debit(source, 100), Posting.credit(target, 100)],
                transfer_id,
            )

    pairs = [("user_alice", "user_bob"), ("user_bob", "user_alice")] * 10
    await asyncio.wait_for(asyncio.gather(*(move(s, r) for s, r in pairs)), timeout=20)

    async with session_factory() as session:
        assert await find_violations(session) == []


async def test_invariants_hold_after_a_full_debit_settle_reversal_cycle(
    session_factory: Factory,
) -> None:
    async with unit_of_work(session_factory) as session:
        alice_nova = await user_account(session, "user_alice", "NOVA_REWARDS")
        alice_sky = await user_account(session, "user_alice", "SKYWARD_MILES")
        nova_clearing = await system_account(session, "NOVA_REWARDS", AccountType.TRANSFER_CLEARING)
        nova_settlement = await system_account(
            session, "NOVA_REWARDS", AccountType.PARTNER_SETTLEMENT
        )
        sky_settlement = await system_account(
            session, "SKYWARD_MILES", AccountType.PARTNER_SETTLEMENT
        )
        # Plain ints: the ORM objects themselves are updated in place by post_journal.
        nova_before, sky_before = alice_nova.balance, alice_sky.balance

        completed = await insert_transfer_row(session, points=10_000)
        await post_journal(
            session,
            JournalType.TRANSFER_DEBIT,
            [Posting.debit(alice_nova, 10_000), Posting.credit(nova_clearing, 10_000)],
            completed,
        )
        await post_journal(
            session,
            JournalType.TRANSFER_SETTLE,
            [
                Posting.debit(nova_clearing, 10_000),
                Posting.credit(nova_settlement, 10_000),
                Posting.debit(sky_settlement, 12_500),
                Posting.credit(alice_sky, 12_500),
            ],
            completed,
        )

        reversed_ = await insert_transfer_row(session, points=5_000)
        await post_journal(
            session,
            JournalType.TRANSFER_DEBIT,
            [Posting.debit(alice_nova, 5_000), Posting.credit(nova_clearing, 5_000)],
            reversed_,
        )
        await post_journal(
            session,
            JournalType.TRANSFER_REVERSAL,
            [Posting.debit(nova_clearing, 5_000), Posting.credit(alice_nova, 5_000)],
            reversed_,
        )

    async with session_factory() as session:
        assert await find_violations(session) == []
        assert await balance_of(session_factory, alice_nova.id) == nova_before - 10_000
        assert await balance_of(session_factory, alice_sky.id) == sky_before + 12_500
        assert await balance_of(session_factory, nova_clearing.id) == 0
        # Loaded objects were kept in sync with the database by the ORM-enabled UPDATE.
        assert alice_nova.balance == nova_before - 10_000


async def test_invariant_check_detects_a_tampered_balance(session_factory: Factory) -> None:
    async with unit_of_work(session_factory) as session:
        account = await user_account(session, "user_alice", "NOVA_REWARDS")
        await session.execute(
            text("UPDATE accounts SET balance = balance + 1 WHERE id = :id"), {"id": account.id}
        )

    async with session_factory() as session:
        violated = {violation.invariant for violation in await find_violations(session)}

    assert violated == {"program_balances_sum_to_zero", "account_balance_matches_entries"}
