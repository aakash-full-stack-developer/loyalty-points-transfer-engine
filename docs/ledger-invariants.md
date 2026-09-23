# Ledger invariants

The ledger is double-entry: every journal is balanced per program, and for every account
`balance = sum(CREDIT entries) - sum(DEBIT entries)`. The consequences below must always
hold. Each query returns **zero rows** when the ledger is healthy.

```bash
make check-invariants
# OK: all 4 ledger invariants hold (20 accounts, 10 journals)
```

`make check-invariants` runs every check in one REPEATABLE READ snapshot and exits with a
non-zero code on any violation, printing each offending row. The queries live in
`app/services/ledger_invariants.py` (the test suite uses the same code). To run one by hand,
open `make psql` and paste it. A fourth check, "no user balance is negative", duplicates the
CHECK constraint as a belt-and-braces report.

Example of a detected problem (a balance edited directly in the database):

```text
FAIL: 2 ledger invariant violation(s)
  - program_balances_sum_to_zero: {'program': 'NOVA_REWARDS', 'total': 7}
  - account_balance_matches_entries: {'account_id': 16, 'stored_balance': 50007, 'ledger_balance': 50000}
```

## 1. Balances per program sum to zero

Points are neither created nor destroyed: user balances are mirrored by the negative
balance of the program's system accounts. Opening balances are seeded as journals
(CREDIT user, DEBIT `PARTNER_SETTLEMENT`), so this holds from the first row.

```sql
SELECT p.code, SUM(a.balance) AS total
FROM accounts a
JOIN programs p ON p.id = a.program_id
GROUP BY p.code
HAVING SUM(a.balance) <> 0;
```

To see the breakdown instead of only violations:

```sql
SELECT p.code,
       SUM(a.balance) FILTER (WHERE a.owner_type = 'USER')   AS user_total,
       SUM(a.balance) FILTER (WHERE a.owner_type = 'SYSTEM') AS system_total,
       SUM(a.balance)                                        AS total
FROM accounts a
JOIN programs p ON p.id = a.program_id
GROUP BY p.code
ORDER BY p.code;
```

## 2. Every journal is balanced per program

Also enforced at commit time by the deferred trigger `trg_ledger_entries_journal_balanced`.

```sql
SELECT journal_id, program_id,
       SUM(amount) FILTER (WHERE direction = 'DEBIT')  AS debits,
       SUM(amount) FILTER (WHERE direction = 'CREDIT') AS credits
FROM ledger_entries
GROUP BY journal_id, program_id
HAVING COALESCE(SUM(amount) FILTER (WHERE direction = 'DEBIT'), 0)
    <> COALESCE(SUM(amount) FILTER (WHERE direction = 'CREDIT'), 0);
```

## 3. Every account balance equals the sum of its entries

The stored `balance` is a running total kept for fast reads and row locking; the entries
are the record of truth. This query detects any drift between them.

```sql
SELECT a.id, a.balance,
       COALESCE(SUM(CASE WHEN e.direction = 'CREDIT' THEN e.amount ELSE -e.amount END), 0)
           AS ledger_balance
FROM accounts a
LEFT JOIN ledger_entries e ON e.account_id = a.id
GROUP BY a.id, a.balance
HAVING a.balance
    <> COALESCE(SUM(CASE WHEN e.direction = 'CREDIT' THEN e.amount ELSE -e.amount END), 0);
```

## What the database enforces on its own

| Guarantee | Mechanism |
| --- | --- |
| User balances never negative | `ck_accounts_user_balance_non_negative` |
| Entry program matches account program | composite FK `fk_ledger_entries_account_program` |
| Journals balanced per program | deferred constraint trigger `trg_ledger_entries_journal_balanced` |
| Journals, entries, transfer events are never modified | `trg_*_append_only` triggers |
| A transfer is debited / settled / reversed at most once | `uq_ledger_journals_transfer_type` |
