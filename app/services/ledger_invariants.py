"""Ledger invariant checks, used by `make check-invariants`, the test suite and the demo.

Each query returns one row per violation, so a healthy ledger returns no rows at all.
The same queries are explained in docs/ledger-invariants.md. Sums are cast back to BIGINT
(PostgreSQL returns SUM(bigint) as NUMERIC) so reports show plain integers.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_LEDGER_BALANCE = (
    "COALESCE(SUM(CASE WHEN e.direction = 'CREDIT' THEN e.amount ELSE -e.amount END), 0)::bigint"
)

INVARIANTS: dict[str, str] = {
    # Points are never created or destroyed: every program's balances sum to zero.
    "program_balances_sum_to_zero": """
        SELECT p.code AS program, SUM(a.balance)::bigint AS total
        FROM accounts a JOIN programs p ON p.id = a.program_id
        GROUP BY p.code
        HAVING SUM(a.balance) <> 0
    """,
    # Every journal balances within each program (also enforced at commit by a trigger).
    "journals_balanced_per_program": """
        SELECT journal_id, program_id,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'DEBIT'), 0)::bigint AS debits,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'CREDIT'), 0)::bigint AS credits
        FROM ledger_entries
        GROUP BY journal_id, program_id
        HAVING COALESCE(SUM(amount) FILTER (WHERE direction = 'DEBIT'), 0)
            <> COALESCE(SUM(amount) FILTER (WHERE direction = 'CREDIT'), 0)
    """,
    # The stored running balance never drifts from the entries that justify it.
    "account_balance_matches_entries": f"""
        SELECT a.id AS account_id, a.balance AS stored_balance,
               {_LEDGER_BALANCE} AS ledger_balance
        FROM accounts a
        LEFT JOIN ledger_entries e ON e.account_id = a.id
        GROUP BY a.id, a.balance
        HAVING a.balance <> {_LEDGER_BALANCE}
    """,
    # Users never hold a negative balance (also enforced by a CHECK constraint).
    "user_balances_non_negative": """
        SELECT id AS account_id, balance FROM accounts
        WHERE owner_type = 'USER' AND balance < 0
    """,
}


@dataclass(frozen=True)
class InvariantViolation:
    invariant: str
    detail: dict[str, Any]


async def find_violations(session: AsyncSession) -> list[InvariantViolation]:
    """Run every invariant query; an empty list means the ledger is consistent.

    Run inside a single REPEATABLE READ (or stricter) transaction when other writers are
    active, so all queries see the same snapshot.
    """
    violations: list[InvariantViolation] = []
    for name, query in INVARIANTS.items():
        result = await session.execute(text(query))
        violations.extend(
            InvariantViolation(invariant=name, detail=dict(row)) for row in result.mappings()
        )
    return violations
