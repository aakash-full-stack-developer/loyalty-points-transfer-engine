"""ORM models: the complete PostgreSQL schema.

Design notes
- Points are BIGINT everywhere. Rates are integer numerator/denominator pairs.
- Enumerated values are VARCHAR plus a named CHECK constraint generated from the enums in
  `app/domain/enums.py` (easier to evolve than native PostgreSQL ENUM types).
- Ledger sign convention: for every account, balance = sum(CREDIT) - sum(DEBIT).
  A DEBIT decreases a balance, a CREDIT increases it.
- Integrity is enforced by the database, not only by application code:
  * user balances can never go negative (CHECK);
  * a ledger entry's program must match its account's program (composite foreign key);
  * ledger_journals, ledger_entries and transfer_events are append-only (triggers);
  * every journal must balance per program at commit time (deferred constraint trigger);
  * a transfer can have at most one journal of each type (unique), so it can never be
    debited, settled or reversed twice;
  * a route has at most one open rate version (partial unique index) and no overlapping
    bonuses (exclusion constraint).
  The triggers and the exclusion constraint's extension live in the migration.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    column,
    func,
    text,
    true,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import (
    RECONCILABLE_STATUSES,
    AccountType,
    EntryDirection,
    IdempotencyStatus,
    JournalType,
    OwnerType,
    ProgramType,
    TransferStatus,
)


def _enum(enum_cls: type[StrEnum]) -> SAEnum:
    """VARCHAR column mapped to a Python enum. The CHECK constraint is declared explicitly."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


def _values_in(column_name: str, values: type[StrEnum] | tuple[StrEnum, ...]) -> str:
    quoted = ", ".join(f"'{member.value}'" for member in values)
    return f"{column_name} IN ({quoted})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (CheckConstraint("id ~ '^[A-Za-z0-9_.-]{1,64}$'", name="id_format"),)


class Program(Base):
    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[ProgramType] = mapped_column(_enum(ProgramType))
    partner_code: Mapped[str] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_values_in("type", ProgramType), name="type_valid"),
        CheckConstraint("code ~ '^[A-Z][A-Z0-9_]{1,49}$'", name="code_format"),
    )


class ConversionRate(Base):
    """One version of the rate for a directed route. Never updated in place: a new version
    closes the previous one by setting its effective_to."""

    __tablename__ = "conversion_rates"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    destination_program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    # destination points per source point = numerator / denominator
    numerator: Mapped[int] = mapped_column(BigInteger)
    denominator: Mapped[int] = mapped_column(BigInteger)
    min_source_points: Mapped[int] = mapped_column(BigInteger)
    source_increment: Mapped[int] = mapped_column(BigInteger)
    max_source_points: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(server_default=func.now())
    effective_to: Mapped[datetime | None]
    active: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("source_program_id <> destination_program_id", name="distinct_programs"),
        CheckConstraint("numerator > 0", name="numerator_positive"),
        CheckConstraint("denominator > 0", name="denominator_positive"),
        CheckConstraint("min_source_points >= 1", name="min_source_points_positive"),
        CheckConstraint("source_increment >= 1", name="source_increment_positive"),
        CheckConstraint(
            "max_source_points IS NULL OR max_source_points >= min_source_points",
            name="max_not_below_min",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="effective_window_valid"
        ),
        UniqueConstraint(
            "source_program_id",
            "destination_program_id",
            "version",
            name="uq_conversion_rates_route_version",
        ),
        # At most one open (current) version per route.
        Index(
            "uq_conversion_rates_route_open_version",
            "source_program_id",
            "destination_program_id",
            unique=True,
            postgresql_where=text("effective_to IS NULL AND active"),
        ),
        # Active-rate lookup: route + effective_from window.
        Index(
            "ix_conversion_rates_route_effective",
            "source_program_id",
            "destination_program_id",
            "effective_from",
        ),
    )


class TransferBonus(Base):
    """Time-bound bonus on a route, in basis points of the base amount (2500 = +25%)."""

    __tablename__ = "transfer_bonuses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    destination_program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    bonus_bps: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("source_program_id <> destination_program_id", name="distinct_programs"),
        CheckConstraint("bonus_bps > 0 AND bonus_bps <= 10000", name="bonus_bps_range"),
        CheckConstraint("ends_at > starts_at", name="window_valid"),
        # Two bonuses on the same route may not overlap in time, so the applicable bonus is
        # always unambiguous. Needs the btree_gist extension (created in the migration).
        # ExcludeConstraint has no type annotations in SQLAlchemy.
        ExcludeConstraint(  # type: ignore[no-untyped-call]
            (column("source_program_id"), "="),
            (column("destination_program_id"), "="),
            (func.tstzrange(column("starts_at"), column("ends_at")), "&&"),
            name="ex_transfer_bonuses_no_overlap",
            using="gist",
        ),
    )


class Account(Base):
    """A balance in one program: a user's balance, or a system account
    (TRANSFER_CLEARING / PARTNER_SETTLEMENT) of which each program has exactly one each."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    owner_type: Mapped[OwnerType] = mapped_column(_enum(OwnerType))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    account_type: Mapped[AccountType] = mapped_column(_enum(AccountType))
    external_member_id: Mapped[str | None] = mapped_column(String(100))
    balance: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(_values_in("owner_type", OwnerType), name="owner_type_valid"),
        CheckConstraint(_values_in("account_type", AccountType), name="account_type_valid"),
        CheckConstraint(
            "(owner_type = 'USER' AND user_id IS NOT NULL AND account_type = 'USER_BALANCE'"
            " AND external_member_id IS NOT NULL)"
            " OR (owner_type = 'SYSTEM' AND user_id IS NULL AND account_type <> 'USER_BALANCE'"
            " AND external_member_id IS NULL)",
            name="owner_consistent",
        ),
        CheckConstraint("owner_type <> 'USER' OR balance >= 0", name="user_balance_non_negative"),
        UniqueConstraint("user_id", "program_id", name="uq_accounts_user_program"),
        UniqueConstraint(
            "program_id", "external_member_id", name="uq_accounts_program_external_member"
        ),
        # Target of composite foreign keys that tie an account to its program.
        UniqueConstraint("id", "program_id", name="uq_accounts_id_program"),
        Index(
            "uq_accounts_system_program_type",
            "program_id",
            "account_type",
            unique=True,
            postgresql_where=text("owner_type = 'SYSTEM'"),
        ),
    )


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    source_program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    destination_program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    source_account_id: Mapped[int] = mapped_column(BigInteger)
    destination_account_id: Mapped[int] = mapped_column(BigInteger)
    source_points: Mapped[int] = mapped_column(BigInteger)
    base_points: Mapped[int] = mapped_column(BigInteger)
    bonus_points: Mapped[int] = mapped_column(BigInteger)
    destination_points: Mapped[int] = mapped_column(BigInteger)
    rate_id: Mapped[int] = mapped_column(ForeignKey("conversion_rates.id"))
    rate_snapshot: Mapped[dict[str, Any]]
    status: Mapped[TransferStatus] = mapped_column(_enum(TransferStatus))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    partner_reference: Mapped[str | None] = mapped_column(String(64), unique=True)
    partner_confirmation_id: Mapped[str | None] = mapped_column(String(128))
    verification_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_verification_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None]

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_account_id", "source_program_id"],
            ["accounts.id", "accounts.program_id"],
            name="fk_transfers_source_account_program",
        ),
        ForeignKeyConstraint(
            ["destination_account_id", "destination_program_id"],
            ["accounts.id", "accounts.program_id"],
            name="fk_transfers_destination_account_program",
        ),
        CheckConstraint("id ~ '^tr_[0-9A-HJKMNP-TV-Z]{26}$'", name="id_format"),
        CheckConstraint(_values_in("status", TransferStatus), name="status_valid"),
        CheckConstraint("source_program_id <> destination_program_id", name="distinct_programs"),
        CheckConstraint("source_points > 0", name="source_points_positive"),
        CheckConstraint("base_points >= 0", name="base_points_non_negative"),
        CheckConstraint("bonus_points >= 0", name="bonus_points_non_negative"),
        CheckConstraint("destination_points > 0", name="destination_points_positive"),
        CheckConstraint(
            "destination_points = base_points + bonus_points", name="destination_points_sum"
        ),
        CheckConstraint("verification_attempts >= 0", name="verification_attempts_non_negative"),
        CheckConstraint(
            "status <> 'COMPLETED' OR partner_confirmation_id IS NOT NULL",
            name="completed_has_confirmation",
        ),
        CheckConstraint(
            "status <> 'REVERSED' OR failure_code IS NOT NULL", name="reversed_has_failure_code"
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'REVERSED')) = (completed_at IS NOT NULL)",
            name="final_has_completed_at",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_transfers_user_idempotency_key"),
        # The reconciler's work queue: only non-final transfers are indexed.
        Index(
            "ix_transfers_reconcilable",
            "status",
            "next_verification_at",
            postgresql_where=text(_values_in("status", RECONCILABLE_STATUSES)),
        ),
    )


# Newest-first listing per user with an (created_at, id) cursor.
Index(
    "ix_transfers_user_created",
    Transfer.user_id,
    Transfer.created_at.desc(),
    Transfer.id.desc(),
)


class TransferEvent(Base):
    """Append-only audit trail of every status transition."""

    __tablename__ = "transfer_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id"))
    from_status: Mapped[TransferStatus | None] = mapped_column(_enum(TransferStatus))
    to_status: Mapped[TransferStatus] = mapped_column(_enum(TransferStatus))
    reason: Mapped[str] = mapped_column(String(200))
    # "metadata" is reserved on declarative classes, so the attribute has a different name.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR {_values_in('from_status', TransferStatus)}",
            name="from_status_valid",
        ),
        CheckConstraint(_values_in("to_status", TransferStatus), name="to_status_valid"),
        Index("ix_transfer_events_transfer_id_id", "transfer_id", "id"),
    )


class LedgerJournal(Base):
    """A group of ledger entries that is balanced per program."""

    __tablename__ = "ledger_journals"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    journal_type: Mapped[JournalType] = mapped_column(_enum(JournalType))
    transfer_id: Mapped[str | None] = mapped_column(ForeignKey("transfers.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_values_in("journal_type", JournalType), name="journal_type_valid"),
        CheckConstraint(
            "(journal_type = 'SEED') = (transfer_id IS NULL)", name="transfer_link_consistent"
        ),
        UniqueConstraint("transfer_id", "journal_type", name="uq_ledger_journals_transfer_type"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    journal_id: Mapped[int] = mapped_column(ForeignKey("ledger_journals.id"), index=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    program_id: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[EntryDirection] = mapped_column(_enum(EntryDirection))
    amount: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "program_id"],
            ["accounts.id", "accounts.program_id"],
            name="fk_ledger_entries_account_program",
        ),
        CheckConstraint(_values_in("direction", EntryDirection), name="direction_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )


class IdempotencyKey(Base):
    """Durable idempotency record for POST /v1/transfers, scoped to (user_id, key)."""

    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(255))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[IdempotencyStatus] = mapped_column(_enum(IdempotencyStatus))
    response_status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None]
    transfer_id: Mapped[str | None] = mapped_column(ForeignKey("transfers.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(index=True)

    __table_args__ = (
        CheckConstraint(_values_in("status", IdempotencyStatus), name="status_valid"),
        CheckConstraint(
            "status <> 'COMPLETED' OR response_status_code IS NOT NULL",
            name="completed_has_response",
        ),
        CheckConstraint("request_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
    )
