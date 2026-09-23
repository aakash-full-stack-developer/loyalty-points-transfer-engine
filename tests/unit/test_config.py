import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARTNER_READ_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("RATE_LIMIT_TRANSFERS_PER_WINDOW", "5")

    settings = Settings()

    assert settings.partner_read_timeout_seconds == 7.5
    assert settings.rate_limit_transfers_per_window == 5


def test_admin_key_is_not_exposed_in_repr() -> None:
    settings = Settings(admin_api_key="super-secret")  # type: ignore[arg-type]

    assert "super-secret" not in repr(settings)
    assert settings.admin_api_key.get_secret_value() == "super-secret"


def test_invalid_values_are_rejected_at_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(partner_retry_max_attempts=0)
