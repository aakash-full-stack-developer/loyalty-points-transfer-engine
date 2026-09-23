"""Partner simulator: stands in for external card-issuer and loyalty-program APIs.

It runs as its own service and is reached over real HTTP, so the engine exercises real
timeouts, connection errors and status codes rather than in-process mocks.

Credit API (per partner, e.g. /partners/SKYWARD/credits):
- POST credits: idempotent on `reference`. A repeat with the same reference returns the
  original credit (200, Idempotent-Replayed: true) and never credits twice. A repeat with a
  different member or amount is a client bug and returns 409.
- GET credits/{reference}: 200 with the credit, or 404 if it was never applied.

Failure modes (set per partner at runtime with POST /simulator/config):
- error, unavailable, flaky: infrastructure failures. They happen before the request is
  processed (nothing is applied) and affect both POST and GET.
- timeout: the POST sleeps past the client timeout and applies nothing.
- timeout_after_commit: the POST applies the credit, then sleeps past the client timeout.
  The client sees a timeout although the credit exists: the "unknown outcome" case.
  Replays of an applied reference are just as slow, so client retries time out too and
  only a later status check (the reconciler) can find the credit.
- slow: the POST is delayed but answers within the client timeout.
- reject: the POST is refused with 422 (for example an unknown member) and applies nothing.
"""

import asyncio
import os
import random
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import FastAPI, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.observability.logging import configure_logging
from app.observability.middleware import RequestContextMiddleware
from partner_simulator.state import Credit, Mode, PartnerConfig, SimulatorState

logger = structlog.get_logger("partner_simulator")

PartnerCode = Annotated[str, Path(pattern=r"^[A-Z][A-Z0-9_]{1,49}$")]


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "partner-simulator"


class CreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=64)
    member_id: str = Field(min_length=1, max_length=100)
    points: StrictInt = Field(gt=0)


class CreditResponse(BaseModel):
    confirmation_id: str
    status: str = "COMPLETED"
    reference: str
    member_id: str
    points: int
    created_at: datetime

    @classmethod
    def of(cls, credit: Credit) -> "CreditResponse":
        return cls(
            confirmation_id=credit.confirmation_id,
            reference=credit.reference,
            member_id=credit.member_id,
            points=credit.points,
            created_at=credit.created_at,
        )


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,49}$")
    mode: Mode
    delay_ms: int | None = Field(default=None, ge=0, le=120_000)
    failure_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class ConfigView(BaseModel):
    partner_code: str
    mode: Mode
    delay_ms: int
    failure_rate: float


def _config_view(partner_code: str, config: PartnerConfig) -> ConfigView:
    return ConfigView(
        partner_code=partner_code,
        mode=config.mode,
        delay_ms=config.delay_ms,
        failure_rate=config.failure_rate,
    )


def _failure(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status_code)


def _infrastructure_failure(config: PartnerConfig) -> JSONResponse | None:
    """Failures that happen before the partner processes anything (POST and GET)."""
    if config.mode == Mode.ERROR:
        return _failure(500, "INTERNAL_ERROR", "Simulated partner error")
    if config.mode == Mode.UNAVAILABLE:
        return _failure(503, "UNAVAILABLE", "Simulated partner outage")
    if config.mode == Mode.FLAKY and random.random() < config.failure_rate:
        return _failure(500, "INTERNAL_ERROR", "Simulated intermittent error")
    return None


def create_app(state: SimulatorState | None = None) -> FastAPI:
    configure_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        json_logs=os.getenv("LOG_JSON", "false").lower() == "true",
    )
    app = FastAPI(title="Partner Simulator", version="0.1.0")
    app.add_middleware(RequestContextMiddleware)
    sim = state or SimulatorState()
    app.state.simulator = sim

    @app.get("/health", tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/partners/{partner_code}/credits",
        tags=["partner"],
        status_code=status.HTTP_201_CREATED,
        response_model=CreditResponse,
    )
    async def create_credit(
        partner_code: PartnerCode, body: CreditRequest, response: Response
    ) -> CreditResponse | JSONResponse:
        config = sim.config_for(partner_code)
        log = logger.bind(partner=partner_code, reference=body.reference, mode=config.mode)
        log.info("partner_credit_received", points=body.points)

        if failure := _infrastructure_failure(config):
            log.info("partner_credit_failed", status_code=failure.status_code)
            return failure

        if config.mode == Mode.TIMEOUT:
            await asyncio.sleep(config.delay_ms / 1000)
            log.info("partner_credit_timed_out_without_applying")
            return _failure(504, "TIMEOUT", "Simulated timeout; nothing applied")

        if config.mode == Mode.SLOW:
            await asyncio.sleep(config.delay_ms / 1000)

        existing = sim.find(partner_code, body.reference)
        if existing is not None:
            if not existing.same_request(body.member_id, body.points):
                log.warning("partner_credit_reference_conflict")
                return _failure(
                    409, "REFERENCE_CONFLICT", "Reference already used for a different credit"
                )
            log.info("partner_credit_replayed", confirmation_id=existing.confirmation_id)
            if config.mode == Mode.TIMEOUT_AFTER_COMMIT:
                # A slow partner is slow for every request, replays included.
                await asyncio.sleep(config.delay_ms / 1000)
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replayed"] = "true"
            return CreditResponse.of(existing)

        if config.mode == Mode.REJECT:
            log.info("partner_credit_rejected")
            return _failure(422, "MEMBER_NOT_FOUND", "Member account not found or not eligible")

        credit = sim.apply(partner_code, body.reference, body.member_id, body.points)
        log.info("partner_credit_applied", confirmation_id=credit.confirmation_id)

        if config.mode == Mode.TIMEOUT_AFTER_COMMIT:
            await asyncio.sleep(config.delay_ms / 1000)
        return CreditResponse.of(credit)

    @app.get(
        "/partners/{partner_code}/credits/{reference}",
        tags=["partner"],
        response_model=CreditResponse,
        responses={404: {"description": "No credit with this reference"}},
    )
    async def get_credit(
        partner_code: PartnerCode, reference: str
    ) -> CreditResponse | JSONResponse:
        config = sim.config_for(partner_code)
        if failure := _infrastructure_failure(config):
            logger.info(
                "partner_status_failed",
                partner=partner_code,
                reference=reference,
                status_code=failure.status_code,
            )
            return failure
        credit = sim.find(partner_code, reference)
        logger.info(
            "partner_status_checked",
            partner=partner_code,
            reference=reference,
            found=credit is not None,
        )
        if credit is None:
            return _failure(404, "NOT_FOUND", "No credit with this reference")
        return CreditResponse.of(credit)

    @app.post("/simulator/config", tags=["simulator"])
    async def set_config(body: ConfigRequest) -> ConfigView:
        config = PartnerConfig.build(body.mode, body.delay_ms, body.failure_rate)
        sim.configs[body.partner_code] = config
        logger.info("simulator_config_changed", partner=body.partner_code, mode=config.mode)
        return _config_view(body.partner_code, config)

    @app.get("/simulator/config", tags=["simulator"])
    async def get_config() -> list[ConfigView]:
        return [_config_view(code, config) for code, config in sorted(sim.configs.items())]

    @app.get("/simulator/credits", tags=["simulator"])
    async def list_credits(partner_code: str | None = None) -> list[CreditResponse]:
        return [
            CreditResponse.of(credit)
            for (code, _), credit in sorted(sim.credits.items())
            if partner_code is None or code == partner_code
        ]

    @app.post("/simulator/reset", tags=["simulator"], status_code=status.HTTP_204_NO_CONTENT)
    async def reset() -> None:
        sim.reset()
        logger.info("simulator_reset")

    return app


app = create_app()
