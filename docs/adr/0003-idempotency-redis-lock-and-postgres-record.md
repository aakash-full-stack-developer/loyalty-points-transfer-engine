# ADR 0003: Idempotency with a Redis lock and a PostgreSQL durable record

Status: accepted

## Context

Clients retry: networks drop responses, mobile apps resend, users double-click. Without
protection, a retried `POST /v1/transfers` would move points twice. Retries also arrive
concurrently with the original request.

## Decision

`POST /v1/transfers` requires an `Idempotency-Key` header, scoped to the user.

- **Fingerprint**: SHA-256 of method, path and the canonical JSON of the validated body.
  The same key with a different body is rejected with 422 `IDEMPOTENCY_KEY_REUSED`.
- **Redis lock** (`SET NX PX`, released with a compare-and-delete script): rejects
  concurrent duplicates cheaply with 409 `IDEMPOTENCY_REQUEST_IN_PROGRESS`.
- **PostgreSQL record** (`idempotency_keys`, `UNIQUE (user_id, key)`): the durable record
  and the real guarantee. It stores the final response, which is replayed with
  `Idempotent-Replayed: true`.
- Final responses are stored: 2xx and 4xx business errors. 5xx responses are not stored;
  the key is released so the client can retry.
- An in-progress record older than the lock TTL is treated as abandoned (the process
  died) and taken over. Keys expire after 24 hours.
- A third safety net: `UNIQUE (user_id, idempotency_key)` on `transfers`, so even a bypass
  of this layer returns the existing transfer instead of creating a second one.

## Consequences

- If Redis is down, the PostgreSQL unique constraint alone still admits exactly one
  request (tested). Correctness never depends on Redis.
- A retry after a timeout returns the original outcome, including a stored business error.
- This is client-to-us idempotency. Us-to-partner idempotency is separate: the transfer id
  is the partner reference, so retrying a partner call cannot credit twice.

## Alternatives considered

- **Redis only.** Fast, but a Redis restart would forget keys and allow duplicates.
- **PostgreSQL only.** Correct, but concurrent duplicates would all reach the database and
  contend on the unique index; Redis answers them earlier and cheaper.
- **Deduplicate on request content, without a key.** Two legitimate identical transfers
  would be merged.
