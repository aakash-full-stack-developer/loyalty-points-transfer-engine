# Partner simulator

A separate FastAPI service (`partner-simulator`, port 8001) that stands in for card-issuer
and loyalty-program APIs. The engine reaches it over real HTTP, so timeouts, refused
connections and 5xx responses are real, not mocked in-process.

State (credits and modes) is in memory: it resets when the container restarts, or with
`POST /simulator/reset`. The integration tests reset it too.

## Partner API

| Method | Path | Behaviour |
| --- | --- | --- |
| POST | `/partners/{partner_code}/credits` | Body `{reference, member_id, points}`. `201` with `confirmation_id`. Idempotent on `reference`: a repeat returns the original credit with `200` and `Idempotent-Replayed: true`; a repeat with a different member or amount returns `409`. |
| GET | `/partners/{partner_code}/credits/{reference}` | `200` with the credit, or `404` if it was never applied. Used by the reconciler. |

Partner codes of the seeded programs: `NOVA`, `ZENITH`, `SKYWARD`, `STAYWELL`, `HARBOR`.

## Failure modes

Modes are set per partner at runtime. Unconfigured partners use `success`.

| Mode | Credit response | Credit applied? | Engine classification | Status check (GET) |
| --- | --- | --- | --- | --- |
| `success` | 201 | yes | SUCCESS | 200 |
| `reject` | 422 `MEMBER_NOT_FOUND` | no | REJECTED | 404 |
| `error` | 500 | no | UNKNOWN (retried) | 500 |
| `unavailable` | 503 | no | UNKNOWN (retried) | 503 |
| `timeout` | sleeps `delay_ms` (default 10 s) | no | UNKNOWN | 404 |
| `timeout_after_commit` | applies, then sleeps `delay_ms` (default 10 s) | **yes** | UNKNOWN | 200 |
| `slow` | sleeps `delay_ms` (default 1 s), then 201 | yes | SUCCESS | 200 |
| `flaky` | 500 with probability `failure_rate` (default 0.5), else 201 | only on success | UNKNOWN or SUCCESS | random 500 |

`timeout` and `timeout_after_commit` look identical to the client (no response within the
3 s read timeout). The only way to know which happened is to ask the partner later, which
is exactly what the reconciler does. That is why a timeout is UNKNOWN, never FAILED.

## Commands

```bash
# Set a mode (one partner at a time)
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"success"}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"reject"}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"error"}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"unavailable"}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"timeout","delay_ms":10000}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"timeout_after_commit","delay_ms":10000}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"slow","delay_ms":1000}'

curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"flaky","failure_rate":0.3}'

# Inspect
curl -s localhost:8001/simulator/config                          # modes currently set
curl -s localhost:8001/simulator/credits                         # every credit applied
curl -s "localhost:8001/simulator/credits?partner_code=SKYWARD"  # one partner
curl -s localhost:8001/partners/SKYWARD/credits/<reference>      # one credit (200 / 404)

# Clear all credits and modes
curl -s -X POST localhost:8001/simulator/reset
```

Observed behaviour of each mode against the running container (client read timeout 3 s;
`000` means the client gave up):

```text
success                credit_http=201  (0s)  then GET status=200
reject                 credit_http=422  (0s)  then GET status=404
error                  credit_http=500  (0s)  then GET status=500
unavailable            credit_http=503  (0s)  then GET status=503
timeout                credit_http=000  (3s)  then GET status=404
timeout_after_commit   credit_http=000  (3s)  then GET status=200
slow                   credit_http=201  (1s)  then GET status=200
flaky                  credit_http=500  (0s)  then GET status=404
```

## How the engine talks to partners

`app/partners/` keeps business logic independent of HTTP:

- `base.py`: the `PartnerAdapter` interface and the four outcomes (SUCCESS, REJECTED,
  NOT_SENT, UNKNOWN).
- `http_partner.py`: the httpx adapter and its classification table.
- `resilience.py`: retries (exponential backoff, full jitter), a circuit breaker per
  partner, and an overall deadline.
- `registry.py`: partner_code -> adapter.
