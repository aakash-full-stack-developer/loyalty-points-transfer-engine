"""Public identifiers.

Transfer ids are `tr_` + a ULID: 26 Crockford base32 characters, a 48-bit millisecond
timestamp followed by 80 random bits. They sort by creation time, are safe in URLs, and the
prefix makes the resource type obvious in logs and support tickets.

Implemented here (about 15 lines) instead of adding a dependency.
"""

import secrets
import time

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TRANSFER_ID_PREFIX = "tr_"


def new_ulid(timestamp_ms: int | None = None) -> str:
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("timestamp_ms must fit in 48 bits")
    value = (timestamp_ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD_BASE32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_transfer_id() -> str:
    return TRANSFER_ID_PREFIX + new_ulid()
