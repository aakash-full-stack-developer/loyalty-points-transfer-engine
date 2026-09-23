"""Idempotency for client requests (POST /v1/transfers): a retry never moves points twice.

A request is identified by (user_id, Idempotency-Key). Its fingerprint (SHA-256 of method,
path and canonical JSON body) detects a key reused for a *different* request.

Lifecycle:
    begin()    -> NEW                   proceed; an IN_PROGRESS record now exists
               -> REPLAY                return the stored status and body
               -> CONFLICT_IN_PROGRESS  another request with this key is running (409)
               -> CONFLICT_MISMATCH     the key was used with a different body (422)
    complete() -> store the final response (2xx and 4xx business errors)
    release()  -> 5xx/crash path: forget the key so the client can retry

Two layers, deliberately:
- Redis `SET NX PX` lock: rejects concurrent duplicates cheaply, before any DB write.
  Released with a compare-and-delete script, so a request can only release its own lock.
- PostgreSQL `idempotency_keys` row, UNIQUE (user_id, key): the durable record and the real
  guarantee. If Redis is down, the unique constraint alone still admits exactly one request.
Correctness never depends on Redis. A third safety net sits on transfers:
UNIQUE (user_id, idempotency_key), so even a bypass of this layer cannot create two transfers.

Stale records: if a process dies mid-request, its IN_PROGRESS row would block the key.
A row older than the lock TTL is treated as abandoned and taken over. The lock TTL must
exceed the longest possible request (the partner deadline plus database time); if one ever
did run longer, the transfers unique constraint still prevents a second transfer.
Expired rows (older than IDEMPOTENCY_TTL_SECONDS) are treated as new; `make
cleanup-idempotency` deletes them.
"""

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import IdempotencyKey
from app.db.session import unit_of_work
from app.domain.enums import IdempotencyStatus
from app.domain.errors import DomainError, ErrorCode

logger = structlog.get_logger(__name__)

MAX_KEY_LENGTH = 255

# Deletes the lock only if it still holds our token: never release someone else's lock.
_RELEASE_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


def compute_fingerprint(method: str, path: str, body: Any) -> str:
    """SHA-256 over method, path and canonical JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{method.upper()}\n{path}\n{canonical}".encode()).hexdigest()


def validate_idempotency_key(key: str | None) -> str:
    if key is None or key == "":
        raise DomainError(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            "Send a unique Idempotency-Key header (for example a UUID) with this request.",
        )
    if len(key) > MAX_KEY_LENGTH or not key.isascii() or not key.isprintable():
        raise DomainError(
            ErrorCode.IDEMPOTENCY_KEY_INVALID,
            f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} printable ASCII characters.",
        )
    return key


class BeginOutcome(StrEnum):
    NEW = "NEW"
    REPLAY = "REPLAY"
    CONFLICT_IN_PROGRESS = "CONFLICT_IN_PROGRESS"
    CONFLICT_MISMATCH = "CONFLICT_MISMATCH"


@dataclass(frozen=True)
class IdempotencyRecord:
    """A claimed key. Pass it to complete() or release()."""

    id: int
    user_id: str
    key: str
    lock_key: str
    lock_token: str | None  # None when Redis was unavailable


@dataclass(frozen=True)
class BeginResult:
    outcome: BeginOutcome
    record: IdempotencyRecord | None = None
    status_code: int | None = None
    body: dict[str, Any] | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


class IdempotencyService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        key_ttl: timedelta,
        lock_ttl: timedelta,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._key_ttl = key_ttl
        self._lock_ttl = lock_ttl
        self._clock = clock

    async def begin(self, user_id: str, key: str, fingerprint: str) -> BeginResult:
        lock_key = _lock_key(user_id, key)
        token = secrets.token_hex(16)
        lock = await self._acquire_lock(lock_key, token)

        if lock is False:
            # Another request holds the lock. A finished record still wins (replay or
            # mismatch); otherwise that request is in progress.
            async with self._session_factory() as session:
                existing = await _load(session, user_id, key)
            if existing is not None and existing.request_fingerprint != fingerprint:
                return BeginResult(BeginOutcome.CONFLICT_MISMATCH)
            if existing is not None and existing.status == IdempotencyStatus.COMPLETED:
                return _replay(existing)
            return BeginResult(BeginOutcome.CONFLICT_IN_PROGRESS)

        held_token = token if lock else None
        try:
            result = await self._claim(user_id, key, fingerprint, lock_key, held_token)
        except BaseException:
            await self._release_lock(lock_key, held_token)
            raise
        if result.outcome != BeginOutcome.NEW:
            await self._release_lock(lock_key, held_token)
        return result

    async def complete(
        self,
        record: IdempotencyRecord,
        status_code: int,
        body: dict[str, Any],
        transfer_id: str | None = None,
    ) -> None:
        async with unit_of_work(self._session_factory) as session:
            updated = await session.execute(
                update(IdempotencyKey)
                .where(
                    IdempotencyKey.id == record.id,
                    IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS,
                )
                .values(
                    status=IdempotencyStatus.COMPLETED,
                    response_status_code=status_code,
                    response_body=body,
                    transfer_id=transfer_id,
                )
            )
        if updated.rowcount == 0:
            logger.warning(
                "idempotency_record_missing_on_complete", user_id=record.user_id, key=record.key
            )
        await self._release_lock(record.lock_key, record.lock_token)

    async def release(self, record: IdempotencyRecord) -> None:
        """Forget an unfinished key (5xx or crash path) so the client can retry it."""
        async with unit_of_work(self._session_factory) as session:
            await session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.id == record.id,
                    IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS,
                )
            )
        await self._release_lock(record.lock_key, record.lock_token)

    async def _claim(
        self,
        user_id: str,
        key: str,
        fingerprint: str,
        lock_key: str,
        lock_token: str | None,
    ) -> BeginResult:
        now = self._clock()
        async with unit_of_work(self._session_factory) as session:
            existing = await _load(session, user_id, key)
            if existing is not None:
                if self._is_reusable(existing, now):
                    await session.execute(
                        delete(IdempotencyKey).where(IdempotencyKey.id == existing.id)
                    )
                else:
                    return _classify(existing, fingerprint)

            new_id = await session.scalar(
                insert(IdempotencyKey)
                .values(
                    user_id=user_id,
                    key=key,
                    request_fingerprint=fingerprint,
                    status=IdempotencyStatus.IN_PROGRESS,
                    created_at=now,
                    expires_at=now + self._key_ttl,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "key"])
                .returning(IdempotencyKey.id)
            )
            if new_id is None:
                # Lost a race (possible when Redis is down): the other request's row wins.
                winner = await _load(session, user_id, key)
                if winner is None:
                    return BeginResult(BeginOutcome.CONFLICT_IN_PROGRESS)
                return _classify(winner, fingerprint)

        return BeginResult(
            BeginOutcome.NEW,
            record=IdempotencyRecord(new_id, user_id, key, lock_key, lock_token),
        )

    def _is_reusable(self, existing: IdempotencyKey, now: datetime) -> bool:
        expired = existing.expires_at <= now
        abandoned = (
            existing.status == IdempotencyStatus.IN_PROGRESS
            and existing.created_at <= now - self._lock_ttl
        )
        if abandoned and not expired:
            logger.warning(
                "idempotency_abandoned_request_taken_over",
                user_id=existing.user_id,
                key=existing.key,
            )
        return expired or abandoned

    async def _acquire_lock(self, lock_key: str, token: str) -> bool | None:
        """True: acquired. False: held by another request. None: Redis unavailable."""
        try:
            acquired = await self._redis.set(
                lock_key, token, nx=True, px=int(self._lock_ttl.total_seconds() * 1000)
            )
        except RedisError as exc:
            logger.warning("idempotency_lock_unavailable", error=repr(exc))
            return None
        return bool(acquired)

    async def _release_lock(self, lock_key: str, token: str | None) -> None:
        if token is None:
            return
        try:
            await self._redis.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)  # type: ignore[misc]
        except RedisError as exc:
            # Not fatal: the lock expires on its own after the lock TTL.
            logger.warning("idempotency_lock_release_failed", error=repr(exc))


def _lock_key(user_id: str, key: str) -> str:
    # Hash the client-supplied key so arbitrary characters never reach the Redis key space.
    return f"idempotency:lock:{user_id}:{hashlib.sha256(key.encode()).hexdigest()}"


async def _load(session: AsyncSession, user_id: str, key: str) -> IdempotencyKey | None:
    record: IdempotencyKey | None = await session.scalar(
        select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
    )
    return record


def _classify(existing: IdempotencyKey, fingerprint: str) -> BeginResult:
    if existing.request_fingerprint != fingerprint:
        return BeginResult(BeginOutcome.CONFLICT_MISMATCH)
    if existing.status == IdempotencyStatus.COMPLETED:
        return _replay(existing)
    return BeginResult(BeginOutcome.CONFLICT_IN_PROGRESS)


def _replay(existing: IdempotencyKey) -> BeginResult:
    return BeginResult(
        BeginOutcome.REPLAY,
        status_code=existing.response_status_code,
        body=existing.response_body,
    )
