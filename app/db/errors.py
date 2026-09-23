"""Helpers for interpreting database errors."""

from sqlalchemy.exc import IntegrityError


def violated_constraint(error: IntegrityError) -> str | None:
    """Name of the constraint an IntegrityError violated, or None if unknown.

    asyncpg exposes it as `constraint_name` on the driver exception, which SQLAlchemy keeps
    as the cause of `error.orig`. Matching on the name (all names are explicit, see
    app/db/base.py) is far more robust than matching on message text.
    """
    driver_error = getattr(error.orig, "__cause__", None)
    name = getattr(driver_error, "constraint_name", None)
    return name if isinstance(name, str) else None
