"""Request context middleware: assigns or propagates X-Request-ID, logs each request, and
turns unhandled exceptions into a generic RFC 7807 500 response.

Written as a pure ASGI middleware (not BaseHTTPMiddleware) so it adds no extra task per
request and never buffers response bodies. Catching exceptions here (rather than in
Starlette's outermost error middleware) keeps the request id on the log line and the
response.
"""

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import internal_error_response

REQUEST_ID_HEADER = "X-Request-ID"

# Accept a caller-supplied id only if it is short and safe to log; otherwise generate one.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._\-]{1,128}")

# Docker healthchecks hit these every few seconds; log them at DEBUG to keep logs readable.
_QUIET_PATHS = frozenset({"/health", "/health/live", "/health/ready"})

logger = structlog.get_logger(__name__)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        if incoming and _VALID_REQUEST_ID.fullmatch(incoming):
            request_id = incoming
        else:
            request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("unhandled_exception", method=scope["method"], path=scope["path"])
            if response_started:
                raise  # headers already sent: the server can only close the connection
            response = internal_error_response(instance=scope["path"], request_id=request_id)
            await response(scope, receive, send_with_request_id)
        finally:
            log = logger.debug if scope["path"] in _QUIET_PATHS else logger.info
            log(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
