from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.reconciliation import ReconciliationPolicy

POLICY = ReconciliationPolicy(
    lease=timedelta(seconds=60),
    stuck_after=timedelta(seconds=60),
    not_found_grace=timedelta(seconds=60),
    max_attempts=5,
    backoff_base=timedelta(seconds=10),
    backoff_max=timedelta(seconds=60),
)


@pytest.mark.parametrize(("attempts", "seconds"), [(1, 10), (2, 20), (3, 40), (4, 60), (9, 60)])
def test_verification_backoff_doubles_and_is_capped(attempts: int, seconds: int) -> None:
    assert POLICY.backoff(attempts) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "setting",
    [
        "reconciliation_stuck_after_seconds",
        "reconciliation_not_found_grace_seconds",
        "reconciliation_lease_seconds",
    ],
)
def test_reconciliation_windows_must_outlast_the_partner_deadline(setting: str) -> None:
    """Otherwise the reconciler could reverse a transfer whose credit is still in flight."""
    with pytest.raises(ValidationError, match="PARTNER_TOTAL_DEADLINE_SECONDS"):
        Settings.model_validate({"partner_total_deadline_seconds": 30, setting: 30})


def test_policy_is_built_from_settings() -> None:
    policy = ReconciliationPolicy.from_settings(Settings(reconciliation_max_attempts=7))

    assert policy.max_attempts == 7
    assert policy.stuck_after == timedelta(seconds=60)
