# API examples

Captured from the running stack after `make reset`. Timestamps, ids and `request_id`
values will differ on your machine. The admin key is the `.env.example` default.

## List programs

```bash
curl -s localhost:8000/v1/programs
```

```json
{"data": [
  {"code": "NOVA_REWARDS", "name": "Nova Rewards", "type": "CARD", "active": true},
  {"code": "ZENITH_POINTS", "name": "Zenith Points", "type": "CARD", "active": true},
  {"code": "HARBOR_CRUISE_POINTS", "name": "Harbor Cruise Points", "type": "LOYALTY", "active": true},
  {"code": "SKYWARD_MILES", "name": "Skyward Miles", "type": "LOYALTY", "active": true},
  {"code": "STAYWELL_POINTS", "name": "Staywell Points", "type": "LOYALTY", "active": true}
]}
```

## Quote a transfer (card -> airline, with the seeded +25% bonus)

```bash
curl -s -X POST localhost:8000/v1/quotes \
  -H "Content-Type: application/json" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":10000}'
```

```json
{
  "source_program": "NOVA_REWARDS",
  "destination_program": "SKYWARD_MILES",
  "source_points": 10000,
  "base_points": 10000,
  "bonus_points": 2500,
  "destination_points": 12500,
  "rate": {"rate_id": 1, "version": 1, "numerator": 1, "denominator": 1, "bonus_id": 1, "bonus_bps": 2500},
  "quoted_at": "2026-09-23T14:40:12.786039Z",
  "expires_at": "2026-09-23T14:41:12.786039Z"
}
```

Rounding down on an uneven ratio (2:3): `ZENITH_POINTS -> HARBOR_CRUISE_POINTS`, 5000 points
gives `"destination_points": 3333`.

## Errors (RFC 7807, `application/problem+json`)

Unsupported route:

```bash
curl -s -X POST localhost:8000/v1/quotes \
  -H "Content-Type: application/json" \
  -d '{"source_program":"SKYWARD_MILES","destination_program":"ZENITH_POINTS","source_points":10000}'
```

```json
{
  "source_program": "SKYWARD_MILES",
  "destination_program": "ZENITH_POINTS",
  "type": "urn:loyalty-engine:problem:route-not-supported",
  "title": "Transfer route is not supported",
  "status": 422,
  "detail": "Transfers from SKYWARD_MILES to ZENITH_POINTS are not supported.",
  "code": "ROUTE_NOT_SUPPORTED",
  "instance": "/v1/quotes",
  "request_id": "e7f518e999da4ed8875138cb97afe1b8"
}
```

Business rule, with the limit exposed as a field (`"source_points": 500`):

```json
{
  "min_source_points": 1000,
  "type": "urn:loyalty-engine:problem:below-minimum",
  "title": "Amount is below the route minimum",
  "status": 422,
  "detail": "Minimum transfer for this route is 1000 points.",
  "code": "BELOW_MINIMUM",
  "instance": "/v1/quotes",
  "request_id": "54ea66e501074315938f60ed4d360afc"
}
```

Validation (`"source_points": 1000.5` — floats are never accepted for points):

```json
{
  "errors": [{"loc": ["body", "source_points"], "message": "Input should be a valid integer", "type": "int_type"}],
  "type": "urn:loyalty-engine:problem:validation-error",
  "title": "Request validation failed",
  "status": 422,
  "detail": "One or more fields are invalid.",
  "code": "VALIDATION_ERROR",
  "instance": "/v1/quotes",
  "request_id": "2fe1000a58424d7ea5a11b4d60c53f50"
}
```

## Admin: create a new rate version

Closes the current version and inserts version + 1 in one transaction.

```bash
curl -s -X POST localhost:8000/v1/admin/rates \
  -H "Content-Type: application/json" -H "X-Admin-Key: change-me-admin-key" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"STAYWELL_POINTS",
       "numerator":3,"denominator":1,"min_source_points":1000,"source_increment":1000,
       "max_source_points":500000}'
```

```json
{
  "id": 7, "source_program": "NOVA_REWARDS", "destination_program": "STAYWELL_POINTS",
  "numerator": 3, "denominator": 1, "min_source_points": 1000, "source_increment": 1000,
  "max_source_points": 500000, "version": 2,
  "effective_from": "2026-09-23T14:40:12.906977Z", "effective_to": null,
  "active": true, "is_current": true
}
```

## Admin: rate history for a route

```bash
curl -s "localhost:8000/v1/admin/rates?source_program=NOVA_REWARDS&destination_program=STAYWELL_POINTS" \
  -H "X-Admin-Key: change-me-admin-key"
```

```json
{"data": [
  {"id": 7, "version": 2, "numerator": 3, "denominator": 1,
   "effective_from": "2026-09-23T14:40:12.906977Z", "effective_to": null, "is_current": true, "...": "..."},
  {"id": 2, "version": 1, "numerator": 2, "denominator": 1,
   "effective_from": "2026-09-23T14:20:31.416193Z", "effective_to": "2026-09-23T14:40:12.906977Z",
   "is_current": false, "...": "..."}
]}
```

Version 1 ends at exactly the instant version 2 starts: no gap, no overlap.

## Admin: create a time-bound bonus

```bash
curl -s -X POST localhost:8000/v1/admin/bonuses \
  -H "Content-Type: application/json" -H "X-Admin-Key: change-me-admin-key" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"STAYWELL_POINTS","bonus_bps":1000,
       "starts_at":"2026-10-01T00:00:00Z","ends_at":"2026-10-15T00:00:00Z"}'
```

```json
{"id": 2, "source_program": "NOVA_REWARDS", "destination_program": "STAYWELL_POINTS",
 "bonus_bps": 1000, "starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-15T00:00:00Z"}
```

An overlapping bonus on the same route returns `409` with `"code": "BONUS_OVERLAP"`.
Missing or wrong `X-Admin-Key` returns `401` with `"code": "ADMIN_AUTH_REQUIRED"`.
