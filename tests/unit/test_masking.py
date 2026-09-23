import pytest

from app.domain.masking import mask_member_id


@pytest.mark.parametrize(
    ("member_id", "masked"),
    [
        ("SKY100200301", "****0301"),
        ("NOVA-4410-7730-0001", "****0001"),
        ("ABCDE", "****BCDE"),
        ("ABCD", "****"),
        ("", "****"),
        (None, None),
    ],
)
def test_only_last_four_characters_are_visible(member_id: str | None, masked: str | None) -> None:
    assert mask_member_id(member_id) == masked
