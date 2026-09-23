"""Read side for transfers: one transfer with its timeline, and cursor-paginated listing.

Pagination is keyset-based: the cursor encodes (created_at, id) of the last item, and the
next page is everything strictly older. Unlike OFFSET it stays fast on deep pages and never
skips or repeats rows when new transfers arrive between requests. The cursor is opaque to
clients (base64url JSON) so its format can change freely.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Program, Transfer, TransferEvent
from app.domain.errors import DomainError, ErrorCode


@dataclass(frozen=True)
class TransferDetails:
    transfer: Transfer
    source_program: str
    destination_program: str
    events: list[TransferEvent]


@dataclass(frozen=True)
class TransferCursor:
    created_at: datetime
    transfer_id: str

    def encode(self) -> str:
        raw = json.dumps({"t": self.created_at.isoformat(), "id": self.transfer_id})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> "TransferCursor":
        try:
            padded = token + "=" * (-len(token) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()))
            return cls(datetime.fromisoformat(data["t"]), str(data["id"]))
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise DomainError(ErrorCode.INVALID_CURSOR, "The cursor is invalid.") from exc


def _with_program_codes() -> tuple[type[Program], type[Program]]:
    return aliased(Program), aliased(Program)


async def get_transfer_details(
    session: AsyncSession, transfer_id: str, user_id: str | None
) -> TransferDetails | None:
    """The transfer and its events. With `user_id`, only that user's transfer is returned."""
    source, destination = _with_program_codes()
    query = (
        select(Transfer, source.code, destination.code)
        .join(source, source.id == Transfer.source_program_id)
        .join(destination, destination.id == Transfer.destination_program_id)
        .where(Transfer.id == transfer_id)
    )
    if user_id is not None:
        query = query.where(Transfer.user_id == user_id)
    row = (await session.execute(query)).tuples().one_or_none()
    if row is None:
        return None
    transfer, source_code, destination_code = row
    events = await session.scalars(
        select(TransferEvent)
        .where(TransferEvent.transfer_id == transfer_id)
        .order_by(TransferEvent.id)
    )
    return TransferDetails(transfer, source_code, destination_code, list(events))


async def list_transfers(
    session: AsyncSession, user_id: str, limit: int, cursor: TransferCursor | None
) -> tuple[list[TransferDetails], str | None]:
    """Newest first. Returns the page and the cursor for the next page (None at the end)."""
    source, destination = _with_program_codes()
    query = (
        select(Transfer, source.code, destination.code)
        .join(source, source.id == Transfer.source_program_id)
        .join(destination, destination.id == Transfer.destination_program_id)
        .where(Transfer.user_id == user_id)
        .order_by(Transfer.created_at.desc(), Transfer.id.desc())
        .limit(limit + 1)  # one extra row tells us whether another page exists
    )
    if cursor is not None:
        query = query.where(
            tuple_(Transfer.created_at, Transfer.id)
            < tuple_(literal(cursor.created_at), literal(cursor.transfer_id))
        )
    rows = (await session.execute(query)).tuples().all()
    page = [TransferDetails(t, src, dst, []) for t, src, dst in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1].transfer
        next_cursor = TransferCursor(last.created_at, last.id).encode()
    return page, next_cursor
