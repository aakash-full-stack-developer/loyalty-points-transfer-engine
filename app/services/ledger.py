"""Double-entry ledger. Every balance change in the system goes through `post_journal`.

Sign convention (the same for every account):
    balance = sum(CREDIT amounts) - sum(DEBIT amounts)
    A CREDIT increases an account's balance; a DEBIT decreases it.
So debiting a user's account takes points from them, and crediting it gives them points.

Rules enforced here, and again by the database (see app/db/models.py):
- A journal must balance per program: within each program, total debits == total credits.
  Points of different programs are different units and are never netted against each other.
- Entries are append-only. Mistakes are corrected with a new, reversing journal.
- A user balance can never go negative. The CHECK constraint on accounts is the final
  guard: even if a caller skipped its own balance check, the database rejects the write,
  and it is reported as INSUFFICIENT_BALANCE.

Transactions: the ledger never commits or rolls back. The caller owns the transaction
boundary (see app/db/session.py `unit_of_work`), so a journal is atomic together with
whatever else the caller writes, such as a transfer status change.

Locking and deadlocks: balances are updated with `balance = balance + delta` in ascending
account id order, and `lock_accounts_for_update` also locks in ascending id order. When
every transaction acquires row locks in the same global order, two transactions can never
wait on each other in a cycle, so they cannot deadlock. Locks are FOR NO KEY UPDATE (see
`lock_account_for_update`), and callers lock accounts before inserting rows that reference
them.

Balance updates are ORM-enabled UPDATEs, so Account objects already loaded in the session
reflect the new balance after `post_journal` returns.

Known trade-off: each program's TRANSFER_CLEARING and PARTNER_SETTLEMENT rows are updated
by every transfer in that program, so they serialize writes per program. That is fine at
this scale; at high volume, system balances could be sharded or derived from entries.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.errors import violated_constraint
from app.db.models import Account, LedgerEntry, LedgerJournal
from app.domain.enums import AccountType, EntryDirection, JournalType, OwnerType
from app.domain.errors import DomainError, ErrorCode


class LedgerError(Exception):
    """A programming error: the caller built an invalid journal. Never a client error."""


class JournalAlreadyPostedError(LedgerError):
    """The transfer already has a journal of this type (settled or reversed twice)."""


@dataclass(frozen=True)
class Posting:
    """One line of a journal: move `amount` on `account_id` in `direction`."""

    account_id: int
    program_id: int
    direction: EntryDirection
    amount: int

    @classmethod
    def debit(cls, account: Account, amount: int) -> "Posting":
        return cls(account.id, account.program_id, EntryDirection.DEBIT, amount)

    @classmethod
    def credit(cls, account: Account, amount: int) -> "Posting":
        return cls(account.id, account.program_id, EntryDirection.CREDIT, amount)

    @property
    def signed_amount(self) -> int:
        return self.amount if self.direction == EntryDirection.CREDIT else -self.amount


def validate_journal(postings: Sequence[Posting]) -> None:
    """Raise LedgerError unless the postings form a valid journal. Pure."""
    if len(postings) < 2:
        raise LedgerError("a journal needs at least two postings")
    for posting in postings:
        if not isinstance(posting.amount, int) or isinstance(posting.amount, bool):
            raise LedgerError(f"amount must be an int, got {type(posting.amount).__name__}")
        if posting.amount <= 0:
            raise LedgerError(f"amount must be positive, got {posting.amount}")

    net_by_program: dict[int, int] = defaultdict(int)
    for posting in postings:
        net_by_program[posting.program_id] += posting.signed_amount
    unbalanced = {program: net for program, net in net_by_program.items() if net != 0}
    if unbalanced:
        raise LedgerError(f"journal is unbalanced per program (program_id -> net): {unbalanced}")


def net_balance_changes(postings: Iterable[Posting]) -> dict[int, int]:
    """account_id -> net change in balance. Pure."""
    changes: dict[int, int] = defaultdict(int)
    for posting in postings:
        changes[posting.account_id] += posting.signed_amount
    return dict(changes)


async def post_journal(
    session: AsyncSession,
    journal_type: JournalType,
    postings: Sequence[Posting],
    transfer_id: str | None = None,
) -> LedgerJournal:
    """Write a balanced journal and apply it to account balances, in the caller's transaction.

    Raises:
        LedgerError: the postings are invalid (a bug in the caller).
        JournalAlreadyPostedError: this transfer already has a journal of this type.
        DomainError(INSUFFICIENT_BALANCE): a user balance would become negative.
    After an exception, the transaction must be rolled back (unit_of_work does this).
    """
    validate_journal(postings)

    journal = LedgerJournal(journal_type=journal_type, transfer_id=transfer_id)
    session.add(journal)
    try:
        await session.flush()
    except IntegrityError as exc:
        if violated_constraint(exc) == "uq_ledger_journals_transfer_type":
            raise JournalAlreadyPostedError(
                f"transfer {transfer_id} already has a {journal_type} journal"
            ) from exc
        raise

    session.add_all(
        LedgerEntry(
            journal_id=journal.id,
            account_id=posting.account_id,
            program_id=posting.program_id,
            direction=posting.direction,
            amount=posting.amount,
        )
        for posting in postings
    )

    # Ascending id order: see "Locking and deadlocks" in the module docstring.
    for account_id, delta in sorted(net_balance_changes(postings).items()):
        if delta == 0:
            continue
        try:
            await session.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(balance=Account.balance + delta, updated_at=func.now())
            )
        except IntegrityError as exc:
            if violated_constraint(exc) == "ck_accounts_user_balance_non_negative":
                raise DomainError(
                    ErrorCode.INSUFFICIENT_BALANCE,
                    "The account does not have enough points for this transfer.",
                ) from exc
            raise

    await session.flush()
    return journal


async def lock_account_for_update(session: AsyncSession, account_id: int) -> Account:
    """SELECT ... FOR NO KEY UPDATE: other writers of this row wait until we finish.

    FOR NO KEY UPDATE rather than FOR UPDATE: we change the balance, never the key. Inserting
    a row that references an account (a transfer, a ledger entry) takes FOR KEY SHARE on it,
    which conflicts with FOR UPDATE but not with FOR NO KEY UPDATE. With FOR UPDATE, two
    transactions that each referenced the account and then locked it would deadlock. It is
    also the lock PostgreSQL itself takes for an UPDATE that does not touch key columns.

    populate_existing: if the session already holds this Account, SQLAlchemy would otherwise
    keep its old attribute values, and a balance check "under the lock" would read a stale
    balance. This forces the freshly locked row's values into the object.
    """
    account = await session.scalar(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise LedgerError(f"account {account_id} does not exist")
    return account


async def lock_accounts_for_update(
    session: AsyncSession, account_ids: Iterable[int]
) -> dict[int, Account]:
    """Lock several accounts in ascending id order (deadlock-free, see module docstring)."""
    ids = sorted(set(account_ids))
    rows = await session.scalars(
        select(Account)
        .where(Account.id.in_(ids))
        .order_by(Account.id)
        .with_for_update(key_share=True)  # FOR NO KEY UPDATE, see lock_account_for_update
        .execution_options(populate_existing=True)
    )
    accounts = {account.id: account for account in rows}
    missing = set(ids) - accounts.keys()
    if missing:
        raise LedgerError(f"accounts do not exist: {sorted(missing)}")
    return accounts


async def get_user_account(session: AsyncSession, user_id: str, program_id: int) -> Account | None:
    account: Account | None = await session.scalar(
        select(Account).where(
            Account.owner_type == OwnerType.USER,
            Account.user_id == user_id,
            Account.program_id == program_id,
        )
    )
    return account


async def get_system_account(
    session: AsyncSession, program_id: int, account_type: AccountType
) -> Account:
    account = await session.scalar(
        select(Account).where(
            Account.owner_type == OwnerType.SYSTEM,
            Account.program_id == program_id,
            Account.account_type == account_type,
        )
    )
    if account is None:
        raise LedgerError(f"program {program_id} has no {account_type} system account")
    return account
