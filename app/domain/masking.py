"""Masking of sensitive identifiers for API responses and logs."""

VISIBLE_SUFFIX = 4
MASK = "****"


def mask_member_id(member_id: str | None) -> str | None:
    """Show only the last 4 characters: 'SKY100200301' -> '****0301'.

    The mask has a fixed width, so it does not reveal the original length. Identifiers of
    four characters or fewer are fully masked.
    """
    if member_id is None:
        return None
    if len(member_id) <= VISIBLE_SUFFIX:
        return MASK
    return MASK + member_id[-VISIBLE_SUFFIX:]
