"""In-memory state of the partner simulator: credits and per-partner failure modes.

State lives in one process and is lost on restart, which is fine for a test double. Run the
simulator with a single uvicorn worker so every request sees the same state.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Mode(StrEnum):
    SUCCESS = "success"  # credit applied, 201
    REJECT = "reject"  # business rejection, 422, nothing applied
    ERROR = "error"  # 500, nothing applied
    UNAVAILABLE = "unavailable"  # 503, nothing applied
    TIMEOUT = "timeout"  # sleeps past the client timeout, nothing applied
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"  # credit applied, then slow response
    SLOW = "slow"  # delayed but within the client timeout, credit applied
    FLAKY = "flaky"  # random 500s (failure_rate), otherwise success


# Default delays, chosen relative to the engine's default read timeout (3 s).
DEFAULT_DELAY_MS: dict[Mode, int] = {
    Mode.TIMEOUT: 10_000,
    Mode.TIMEOUT_AFTER_COMMIT: 10_000,
    Mode.SLOW: 1_000,
}


@dataclass
class PartnerConfig:
    mode: Mode = Mode.SUCCESS
    delay_ms: int = 0
    failure_rate: float = 0.5  # only used by FLAKY

    @classmethod
    def build(cls, mode: Mode, delay_ms: int | None, failure_rate: float | None) -> "PartnerConfig":
        return cls(
            mode=mode,
            delay_ms=delay_ms if delay_ms is not None else DEFAULT_DELAY_MS.get(mode, 0),
            failure_rate=failure_rate if failure_rate is not None else 0.5,
        )


@dataclass(frozen=True)
class Credit:
    partner_code: str
    reference: str
    member_id: str
    points: int
    confirmation_id: str
    created_at: datetime

    def same_request(self, member_id: str, points: int) -> bool:
        return self.member_id == member_id and self.points == points


@dataclass
class SimulatorState:
    configs: dict[str, PartnerConfig] = field(default_factory=dict)
    credits: dict[tuple[str, str], Credit] = field(default_factory=dict)

    def config_for(self, partner_code: str) -> PartnerConfig:
        return self.configs.get(partner_code, PartnerConfig())

    def find(self, partner_code: str, reference: str) -> Credit | None:
        return self.credits.get((partner_code, reference))

    def apply(self, partner_code: str, reference: str, member_id: str, points: int) -> Credit:
        """Record a credit. Callers check `find` first; there is no await between the check
        and this call, so on a single event loop the pair is atomic."""
        credit = Credit(
            partner_code=partner_code,
            reference=reference,
            member_id=member_id,
            points=points,
            confirmation_id=f"{partner_code}-{uuid.uuid4().hex[:12].upper()}",
            created_at=datetime.now(UTC),
        )
        self.credits[(partner_code, reference)] = credit
        return credit

    def reset(self) -> None:
        self.configs.clear()
        self.credits.clear()
