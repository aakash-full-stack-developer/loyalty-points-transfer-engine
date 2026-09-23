# ADR 0004: Integer ratio conversion with rate versioning and snapshots

Status: accepted

## Context

Conversion rates differ by direction (card to airline is not airline to card), change over
time, and have minimums, increments, maximums and time-bound bonuses. Floating point
cannot represent many ratios exactly and would create or lose fractions of points. A
transfer's terms must remain explainable after the rate changes.

## Decision

- A rate is a pair of integers, `numerator / denominator` = destination points per source
  point, keyed by **directed** route. A missing route is unsupported.
- The calculation is a **pure function** with integer arithmetic only:
  `base = floor(points * numerator / denominator)`,
  `bonus = floor(base * bonus_bps / 10000)`, `destination = base + bonus`.
  Both steps round **down**, so rounding never creates points.
- Rates are **versioned, never updated**: a new rate closes the current version
  (`effective_to`) and inserts version + 1 at the same instant, in one transaction, under
  a per-route advisory lock. A partial unique index allows one open version per route.
- Bonuses are time-bound; an exclusion constraint forbids overlapping bonuses on a route.
- Every transfer stores `rate_id` and a JSON **snapshot** of the terms it used.
- Transfers read the rate from PostgreSQL inside the debit transaction; only quotes use
  the Redis cache.

## Consequences

- The math is exhaustively testable, including a property-based test proving the result
  is never negative and never exceeds the exact value.
- Changing a rate never changes the history of past transfers.
- Rounding favours the platform by less than one point per transfer, which is the
  conservative choice for a currency-like asset.

## Alternatives considered

- **Decimal rates.** Exact, but invites decimal points into point amounts; integer ratios
  express every realistic rate (1:1, 2:1, 3:1 reverse, 2:3) exactly.
- **Updating rates in place.** Loses history and makes past transfers unexplainable.
- **Rounding half-up.** Can create a point from fractions; flooring cannot.
