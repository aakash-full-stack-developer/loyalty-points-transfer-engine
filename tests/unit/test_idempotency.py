import pytest

from app.domain.errors import DomainError, ErrorCode
from app.services.idempotency import compute_fingerprint, validate_idempotency_key

BODY = {"source_program": "NOVA_REWARDS", "destination_program": "SKYWARD_MILES", "points": 1000}


def test_fingerprint_ignores_key_order() -> None:
    reordered = dict(reversed(list(BODY.items())))

    assert compute_fingerprint("POST", "/v1/transfers", BODY) == compute_fingerprint(
        "POST", "/v1/transfers", reordered
    )


def test_fingerprint_is_a_sha256_hex_digest() -> None:
    fingerprint = compute_fingerprint("post", "/v1/transfers", BODY)

    assert len(fingerprint) == 64
    assert fingerprint == compute_fingerprint("POST", "/v1/transfers", BODY)  # method case


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/v1/transfers", BODY),
        ("POST", "/v1/other", BODY),
        ("POST", "/v1/transfers", {**BODY, "points": 2000}),
        ("POST", "/v1/transfers", {**BODY, "points": "1000"}),
    ],
)
def test_fingerprint_changes_with_method_path_or_body(
    method: str, path: str, body: dict[str, object]
) -> None:
    assert compute_fingerprint(method, path, body) != compute_fingerprint(
        "POST", "/v1/transfers", BODY
    )


@pytest.mark.parametrize(
    "key", ["a", "0f8fad5b-d9cb-469f-a165-70867728950e", "order-42:retry", "x" * 255]
)
def test_valid_keys_are_accepted(key: str) -> None:
    assert validate_idempotency_key(key) == key


@pytest.mark.parametrize(
    ("key", "code"),
    [
        (None, ErrorCode.IDEMPOTENCY_KEY_REQUIRED),
        ("", ErrorCode.IDEMPOTENCY_KEY_REQUIRED),
        ("x" * 256, ErrorCode.IDEMPOTENCY_KEY_INVALID),
        ("tab\tkey", ErrorCode.IDEMPOTENCY_KEY_INVALID),
        ("clé", ErrorCode.IDEMPOTENCY_KEY_INVALID),
    ],
)
def test_invalid_keys_are_rejected(key: str | None, code: ErrorCode) -> None:
    with pytest.raises(DomainError) as raised:
        validate_idempotency_key(key)

    assert raised.value.code == code
