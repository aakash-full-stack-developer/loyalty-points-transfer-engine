import re

import pytest

from app.domain.ids import new_transfer_id, new_ulid

ULID_PATTERN = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def test_transfer_id_has_prefix_and_valid_ulid() -> None:
    transfer_id = new_transfer_id()

    assert transfer_id.startswith("tr_")
    assert ULID_PATTERN.fullmatch(transfer_id.removeprefix("tr_"))


def test_ulids_sort_by_creation_time() -> None:
    earlier = new_ulid(timestamp_ms=1_700_000_000_000)
    later = new_ulid(timestamp_ms=1_700_000_000_001)

    assert earlier < later


def test_ulids_are_unique() -> None:
    assert len({new_ulid() for _ in range(10_000)}) == 10_000


@pytest.mark.parametrize("timestamp_ms", [-1, 2**48])
def test_ulid_rejects_out_of_range_timestamps(timestamp_ms: int) -> None:
    with pytest.raises(ValueError, match="48 bits"):
        new_ulid(timestamp_ms=timestamp_ms)
