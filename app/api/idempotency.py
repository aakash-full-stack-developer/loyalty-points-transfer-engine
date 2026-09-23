"""Reusable idempotency wrapper for write endpoints.

Usage in a route:

    return await run_idempotent(
        idempotency, request, user_id=user_id, key=key,
        fingerprint=compute_fingerprint("POST", request.url.path, body.model_dump(mode="json")),
        handler=lambda: do_the_work(body),
    )

The fingerprint uses the *validated* body, so requests that differ only in JSON formatting,
key order or explicit defaults count as the same request. Requests that fail validation
never reach this code: they have no side effects and are not recorded.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

import structlog
from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.errors import PROBLEM_MEDIA_TYPE, domain_error_body, request_id_of
from app.domain.errors import DomainError, ErrorCode
from app.observability import metrics
from app.services.idempotency import (
    BeginOutcome,
    IdempotencyService,
    validate_idempotency_key,
)

logger = structlog.get_logger(__name__)

REPLAY_HEADER = "Idempotent-Replayed"


async def get_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(description="Unique per logical request, e.g. a UUID. Retries reuse it."),
    ] = None,
) -> str:
    return validate_idempotency_key(idempotency_key)


IdempotencyKeyHeader = Annotated[str, Depends(get_idempotency_key)]


@dataclass(frozen=True)
class HandlerResult:
    status_code: int
    body: Any
    resource_id: str | None = None  # stored with the key, e.g. the transfer id


Handler = Callable[[], Awaitable[HandlerResult]]


def _response(status_code: int, body: Any, replayed: bool = False) -> JSONResponse:
    media_type = PROBLEM_MEDIA_TYPE if status_code >= 400 else "application/json"
    headers = {REPLAY_HEADER: "true"} if replayed else None
    return JSONResponse(body, status_code=status_code, media_type=media_type, headers=headers)


async def run_idempotent(
    service: IdempotencyService,
    request: Request,
    *,
    user_id: str,
    key: str,
    fingerprint: str,
    handler: Handler,
) -> JSONResponse:
    begin = await service.begin(user_id, key, fingerprint)

    if begin.outcome == BeginOutcome.REPLAY:
        if begin.status_code is None:
            raise RuntimeError("completed idempotency record has no status code")
        logger.info("idempotent_replay", status_code=begin.status_code)
        metrics.IDEMPOTENT_REPLAYS.inc()
        return _response(begin.status_code, begin.body, replayed=True)
    if begin.outcome == BeginOutcome.CONFLICT_MISMATCH:
        metrics.IDEMPOTENCY_CONFLICTS.labels(reason="key_reused").inc()
        raise DomainError(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            "This Idempotency-Key was used with a different request. Use a new key.",
        )
    if begin.outcome == BeginOutcome.CONFLICT_IN_PROGRESS:
        metrics.IDEMPOTENCY_CONFLICTS.labels(reason="in_progress").inc()
        raise DomainError(
            ErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS,
            "A request with this Idempotency-Key is still being processed. Retry shortly.",
        )

    record = begin.record
    if record is None:
        raise RuntimeError("NEW idempotency outcome without a record")
    try:
        result = await handler()
    except DomainError as error:
        if error.status >= 500:
            await service.release(record)
            raise
        # A business rejection is a final answer for this request: store and replay it.
        body = domain_error_body(error, request.url.path, request_id_of(request))
        await service.complete(record, error.status, body)
        return _response(error.status, body)
    except BaseException:
        # Unexpected failure (becomes a 500): forget the key so the client can retry.
        await service.release(record)
        raise

    body = jsonable_encoder(result.body)
    if result.status_code >= 500:
        await service.release(record)
    else:
        await service.complete(record, result.status_code, body, result.resource_id)
    return _response(result.status_code, body)
