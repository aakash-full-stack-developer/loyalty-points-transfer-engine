"""RFC 7807 problem+json responses for every error the API returns.

Body: {type, title, status, detail, code, instance, request_id, ...extra}. `code` is the
stable machine-readable field clients should branch on (see app/domain/errors.py).
Unexpected exceptions are turned into a generic 500 by RequestContextMiddleware, so stack
traces and internal messages never reach clients; the details are logged instead.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.errors import ERROR_CATALOGUE, DomainError, ErrorCode

PROBLEM_MEDIA_TYPE = "application/problem+json"


def problem_type(code: str) -> str:
    return f"urn:loyalty-engine:problem:{code.lower().replace('_', '-')}"


def problem_body(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None,
    instance: str,
    request_id: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(extra or {}),  # standard members below always win over extra fields
        "type": problem_type(code),
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "instance": instance,
        "request_id": request_id,
    }


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None,
    instance: str,
    request_id: str | None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = problem_body(
        status=status,
        code=code,
        title=title,
        detail=detail,
        instance=instance,
        request_id=request_id,
        extra=extra,
    )
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


def domain_error_body(error: DomainError, instance: str, request_id: str | None) -> dict[str, Any]:
    return problem_body(
        status=error.status,
        code=error.code,
        title=error.title,
        detail=error.detail,
        instance=instance,
        request_id=request_id,
        extra=error.extra,
    )


def internal_error_response(instance: str, request_id: str | None) -> JSONResponse:
    spec = ERROR_CATALOGUE[ErrorCode.INTERNAL_ERROR]
    return problem_response(
        status=spec.status,
        code=ErrorCode.INTERNAL_ERROR,
        title=spec.title,
        detail="An unexpected error occurred. Quote the request_id when contacting support.",
        instance=instance,
        request_id=request_id,
    )


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    body = domain_error_body(exc, request.url.path, request_id_of(request))
    return JSONResponse(body, status_code=exc.status, media_type=PROBLEM_MEDIA_TYPE)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Report where and why, but never echo submitted values back.
    errors = [
        {"loc": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    spec = ERROR_CATALOGUE[ErrorCode.VALIDATION_ERROR]
    return problem_response(
        status=spec.status,
        code=ErrorCode.VALIDATION_ERROR,
        title=spec.title,
        detail="One or more fields are invalid.",
        instance=request.url.path,
        request_id=request_id_of(request),
        extra={"errors": errors},
    )


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = {
        HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
        HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    }.get(HTTPStatus(exc.status_code), ErrorCode.HTTP_ERROR)
    title = (
        ERROR_CATALOGUE[code].title
        if code != ErrorCode.HTTP_ERROR
        else HTTPStatus(exc.status_code).phrase
    )
    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem_response(
        status=exc.status_code,
        code=code,
        title=title,
        detail=detail,
        instance=request.url.path,
        request_id=request_id_of(request),
        headers=dict(exc.headers) if exc.headers else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
