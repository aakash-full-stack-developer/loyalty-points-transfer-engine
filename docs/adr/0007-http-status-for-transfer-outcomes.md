# ADR 0007: HTTP status semantics for transfer outcomes (201 / 202)

Status: accepted

## Context

A transfer request can end in several ways: completed, reversed (the partner refused and
the points came back), still being verified, or refused before anything happened. Clients
need a simple, predictable rule, and retries (idempotent replays) must return exactly the
same answer.

## Decision

| Response | When | Body |
| --- | --- | --- |
| `201 Created` | the transfer reached a final state: `COMPLETED` **or** `REVERSED` | the transfer; `status` says which |
| `202 Accepted` | the transfer exists but is not final (`PENDING_VERIFICATION`) | the transfer; poll `GET /v1/transfers/{id}` |
| `4xx` | nothing was created: validation, business rule, idempotency or rate-limit errors | RFC 7807 problem with a stable `code` |
| `5xx` | unexpected error; the idempotency key is released so the request can be retried | generic problem with `request_id` |

A reversed transfer is still `201`: a transfer resource was created and reached a final,
consistent state. The `status` field and the `failure` object are the source of truth for
the business outcome.

## Consequences

- Clients branch on two things only: the HTTP class (created / accepted / error) and, for
  created transfers, `status`.
- Replays return the stored status code and body, so a retried request sees exactly what
  the first one would have.
- A `4xx` guarantees no side effects, which makes client retries safe to reason about.

## Alternatives considered

- **`4xx` or `5xx` for a reversed transfer.** Suggests nothing was created, although a
  transfer with a full audit trail exists and the client needs its id.
- **Always `200`.** Hides the difference between "done" and "still in progress".
- **`202` for every transfer.** Forces polling even when the result is already known.
