"""Request and response models shared across routes.

Request models forbid unknown fields and use strict integers, so "1000" (a string) or
1000.5 (a float) is rejected rather than silently coerced. Points never pass through a float.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from app.domain.enums import ProgramType

# Upper bound for any single point amount. Far above any real transfer, and it keeps every
# intermediate product (points * numerator) comfortably inside PostgreSQL BIGINT.
MAX_POINTS = 1_000_000_000_000
MAX_RATE_TERM = 1_000_000

ProgramCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,49}$")]
Points = Annotated[StrictInt, Field(gt=0, le=MAX_POINTS)]
RateTerm = Annotated[StrictInt, Field(gt=0, le=MAX_RATE_TERM)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProblemDetails(BaseModel):
    """RFC 7807 error body (documented in OpenAPI for every error response)."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str | None
    code: str
    instance: str
    request_id: str | None


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation or business rule error"},
}


# ---------------------------------------------------------------- programs
class ProgramOut(BaseModel):
    code: str
    name: str
    type: ProgramType
    active: bool


class ProgramList(BaseModel):
    data: list[ProgramOut]


# ---------------------------------------------------------------- accounts
class AccountOut(BaseModel):
    program: str
    program_name: str
    program_type: ProgramType
    balance: int
    external_member_id: str | None = Field(description="Masked: only the last 4 characters")
    updated_at: datetime


class AccountList(BaseModel):
    user_id: str
    data: list[AccountOut]


# ---------------------------------------------------------------- quotes
class QuoteRequest(RequestModel):
    source_program: ProgramCode
    destination_program: ProgramCode
    source_points: Points


class RateSnapshotOut(BaseModel):
    rate_id: int
    version: int
    numerator: int
    denominator: int
    bonus_id: int | None
    bonus_bps: int | None


class QuoteResponse(BaseModel):
    source_program: str
    destination_program: str
    source_points: int
    base_points: int
    bonus_points: int
    destination_points: int
    rate: RateSnapshotOut
    quoted_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------- admin
class RateCreateRequest(RequestModel):
    source_program: ProgramCode
    destination_program: ProgramCode
    numerator: RateTerm = Field(description="Destination points per `denominator` source points")
    denominator: RateTerm
    min_source_points: Points
    source_increment: Points
    max_source_points: Points | None = None


class RateOut(BaseModel):
    id: int
    source_program: str
    destination_program: str
    numerator: int
    denominator: int
    min_source_points: int
    source_increment: int
    max_source_points: int | None
    version: int
    effective_from: datetime
    effective_to: datetime | None
    active: bool
    is_current: bool


class RateList(BaseModel):
    data: list[RateOut]


class BonusCreateRequest(RequestModel):
    source_program: ProgramCode
    destination_program: ProgramCode
    bonus_bps: Annotated[StrictInt, Field(gt=0, le=10_000, description="2500 = +25%")]
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class BonusOut(BaseModel):
    id: int
    source_program: str
    destination_program: str
    bonus_bps: int
    starts_at: datetime
    ends_at: datetime
