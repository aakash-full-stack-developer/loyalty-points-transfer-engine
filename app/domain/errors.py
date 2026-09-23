"""Domain errors and the catalogue of stable error codes.

Every error the API returns carries one of these codes in its RFC 7807 body. Codes are part
of the public contract: clients branch on them, so they never change meaning. The table
below is the single place that maps a code to its HTTP status and human-readable title.
"""

from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any


class ErrorCode(StrEnum):
    # Request / protocol
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    ADMIN_AUTH_REQUIRED = "ADMIN_AUTH_REQUIRED"
    HTTP_ERROR = "HTTP_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # Programs and routes
    PROGRAM_NOT_FOUND = "PROGRAM_NOT_FOUND"
    PROGRAM_INACTIVE = "PROGRAM_INACTIVE"
    SAME_PROGRAM = "SAME_PROGRAM"
    ROUTE_NOT_SUPPORTED = "ROUTE_NOT_SUPPORTED"

    # Amount rules
    BELOW_MINIMUM = "BELOW_MINIMUM"
    ABOVE_MAXIMUM = "ABOVE_MAXIMUM"
    INVALID_INCREMENT = "INVALID_INCREMENT"
    ZERO_DESTINATION_POINTS = "ZERO_DESTINATION_POINTS"

    # Rate administration
    INVALID_RATE = "INVALID_RATE"
    INVALID_BONUS = "INVALID_BONUS"
    BONUS_OVERLAP = "BONUS_OVERLAP"


@dataclass(frozen=True)
class ErrorSpec:
    status: HTTPStatus
    title: str


ERROR_CATALOGUE: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.VALIDATION_ERROR: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Request validation failed"
    ),
    ErrorCode.NOT_FOUND: ErrorSpec(HTTPStatus.NOT_FOUND, "Resource not found"),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed"),
    ErrorCode.ADMIN_AUTH_REQUIRED: ErrorSpec(
        HTTPStatus.UNAUTHORIZED, "A valid X-Admin-Key header is required"
    ),
    # Other protocol-level errors raised by the framework; the real status is used.
    ErrorCode.HTTP_ERROR: ErrorSpec(HTTPStatus.BAD_REQUEST, "HTTP error"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal error"),
    ErrorCode.PROGRAM_NOT_FOUND: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Unknown loyalty program"
    ),
    ErrorCode.PROGRAM_INACTIVE: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Loyalty program is inactive"
    ),
    ErrorCode.SAME_PROGRAM: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Source and destination programs are the same"
    ),
    ErrorCode.ROUTE_NOT_SUPPORTED: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Transfer route is not supported"
    ),
    ErrorCode.BELOW_MINIMUM: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Amount is below the route minimum"
    ),
    ErrorCode.ABOVE_MAXIMUM: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Amount is above the route maximum"
    ),
    ErrorCode.INVALID_INCREMENT: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Amount is not a valid increment for the route"
    ),
    ErrorCode.ZERO_DESTINATION_POINTS: ErrorSpec(
        HTTPStatus.UNPROCESSABLE_ENTITY, "Amount converts to zero destination points"
    ),
    ErrorCode.INVALID_RATE: ErrorSpec(HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid rate definition"),
    ErrorCode.INVALID_BONUS: ErrorSpec(HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid bonus definition"),
    ErrorCode.BONUS_OVERLAP: ErrorSpec(
        HTTPStatus.CONFLICT, "Bonus overlaps an existing bonus on the same route"
    ),
}


class DomainError(Exception):
    """An expected, client-facing failure with a stable code.

    `detail` is safe to show to API clients. `extra` adds machine-readable fields to the
    problem body (for example the minimum amount), so clients need not parse the message.
    """

    def __init__(self, code: ErrorCode, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra

    @property
    def status(self) -> HTTPStatus:
        return ERROR_CATALOGUE[self.code].status

    @property
    def title(self) -> str:
        return ERROR_CATALOGUE[self.code].title
