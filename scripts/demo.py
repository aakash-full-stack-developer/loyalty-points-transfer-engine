"""End-to-end walkthrough against the running stack: `make demo` (after `make up`).

Runs inside the tools container, so the only host requirements are Docker and make. Every
step checks its result and the script exits non-zero on anything unexpected, so the demo
doubles as a smoke test of the whole system. It can be run repeatedly: each run uses fresh
idempotency keys and checks balance changes rather than absolute balances.

Steps: balances, quote, card -> airline transfer with bonus, idempotent replay, key reuse,
airline -> card, partner rejection with automatic reversal, partner timeout resolved by the
reconciler, unsupported route, insufficient balance, final balances and ledger invariants.
"""

import asyncio
import os
import sys
import time
import uuid
from typing import Any

import httpx

from app.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.services.ledger_invariants import INVARIANTS, find_violations

API = os.environ.get("DEMO_API_URL", "http://api:8000")
SIMULATOR = os.environ.get("DEMO_SIMULATOR_URL", "http://partner-simulator:8001")
WORKER_METRICS = os.environ.get("DEMO_WORKER_METRICS_URL", "http://worker:9100/metrics")
USER = "user_alice"
RUN = uuid.uuid4().hex[:8]  # fresh idempotency keys on every run

_COLOR = sys.stdout.isatty()


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def step(number: int, title: str) -> None:
    print()
    print(_paint("1;36", f"[{number:>2}] {title}"))


def info(text: str) -> None:
    print(f"     {text}")


def ok(text: str) -> None:
    print(f"     {_paint('32', 'OK')}  {text}")


def fail(text: str) -> None:
    print(f"     {_paint('31', 'FAIL')}  {text}")
    sys.exit(1)


def expect(condition: bool, success: str, failure: str) -> None:
    if condition:
        ok(success)
    else:
        fail(failure)


class Client:
    def __init__(self) -> None:
        self.api = httpx.Client(base_url=API, timeout=30)
        self.sim = httpx.Client(base_url=SIMULATOR, timeout=10)

    def balances(self) -> dict[str, int]:
        response = self.api.get("/v1/accounts", headers={"X-User-Id": USER})
        return {a["program"]: a["balance"] for a in response.json()["data"]}

    def transfer(self, source: str, destination: str, points: int, key: str) -> httpx.Response:
        return self.api.post(
            "/v1/transfers",
            json={
                "source_program": source,
                "destination_program": destination,
                "source_points": points,
            },
            headers={"X-User-Id": USER, "Idempotency-Key": f"demo-{RUN}-{key}"},
        )

    def partner_mode(self, partner: str, mode: str, **extra: Any) -> None:
        self.sim.post(
            "/simulator/config", json={"partner_code": partner, "mode": mode, **extra}
        ).raise_for_status()


def show_balances(balances: dict[str, int]) -> None:
    for program, balance in sorted(balances.items()):
        info(f"{program:<22} {balance:>10,}")


def summarize(body: dict[str, Any]) -> str:
    destination = body["destination"]
    text = (
        f"{body['status']}: {body['source']['points']:,} {body['source']['program']} -> "
        f"{destination['points']:,} {destination['program']}"
    )
    if destination["bonus_points"]:
        text += f" (base {destination['base_points']:,} + bonus {destination['bonus_points']:,})"
    return text


async def ledger_violations() -> int:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            return len(await find_violations(session))
    finally:
        await engine.dispose()


def main() -> None:
    c = Client()
    try:
        c.api.get("/health/ready").raise_for_status()
    except httpx.HTTPError:
        fail(f"API not ready at {API}. Start the stack first: make up")
    c.sim.post("/simulator/reset").raise_for_status()
    print(_paint("1", f"Loyalty Points Transfer Engine demo (run {RUN}) against {API}"))

    try:
        run_steps(c)
    finally:
        c.sim.post("/simulator/reset")  # leave every partner in success mode
    print()
    print(_paint("1;32", "Demo finished: every step behaved as expected."))


def run_steps(c: Client) -> None:
    step(1, "Alice's balances")
    start = c.balances()
    show_balances(start)

    step(2, "Quote 10,000 NOVA_REWARDS -> SKYWARD_MILES (no side effects)")
    quote = c.api.post(
        "/v1/quotes",
        json={
            "source_program": "NOVA_REWARDS",
            "destination_program": "SKYWARD_MILES",
            "source_points": 10_000,
        },
    ).json()
    info(
        f"rate {quote['rate']['numerator']}:{quote['rate']['denominator']} "
        f"(version {quote['rate']['version']}), bonus {quote['rate']['bonus_bps']} bps"
    )
    expect(
        quote["destination_points"] == 12_500,
        f"base {quote['base_points']:,} + bonus {quote['bonus_points']:,} = "
        f"{quote['destination_points']:,} miles",
        f"unexpected quote: {quote}",
    )

    step(3, "Transfer card -> airline (with the +25% bonus)")
    first = c.transfer("NOVA_REWARDS", "SKYWARD_MILES", 10_000, "card-to-airline")
    body = first.json()
    info(" -> ".join(event["to_status"] for event in body["events"]))
    expect(
        (first.status_code, body["status"]) == (201, "COMPLETED"),
        f"HTTP 201, {summarize(body)}, partner confirmation {body['partner_confirmation_id']}",
        f"HTTP {first.status_code}: {body}",
    )

    step(4, "Retry with the same Idempotency-Key (a network retry)")
    before_replay = c.balances()
    replay = c.transfer("NOVA_REWARDS", "SKYWARD_MILES", 10_000, "card-to-airline")
    expect(
        replay.headers.get("idempotent-replayed") == "true"
        and replay.json()["id"] == body["id"]
        and c.balances() == before_replay,
        f"same response replayed (Idempotent-Replayed: true), transfer {body['id']}, "
        "no second debit",
        f"replay was not idempotent: {replay.status_code} {replay.text}",
    )

    step(5, "Reuse the key with a different amount")
    reused = c.transfer("NOVA_REWARDS", "SKYWARD_MILES", 20_000, "card-to-airline")
    expect(
        (reused.status_code, reused.json().get("code")) == (422, "IDEMPOTENCY_KEY_REUSED"),
        "HTTP 422 IDEMPOTENCY_KEY_REUSED: a key can never mean two different transfers",
        f"HTTP {reused.status_code}: {reused.text}",
    )

    step(6, "Reverse direction: airline -> card (a separate, worse rate)")
    back = c.transfer("SKYWARD_MILES", "NOVA_REWARDS", 9_000, "airline-to-card")
    expect(
        back.json().get("status") == "COMPLETED" and back.json()["destination"]["points"] == 3_000,
        f"{summarize(back.json())} at 3:1",
        f"HTTP {back.status_code}: {back.text}",
    )

    step(7, "Partner rejects the credit -> automatic reversal")
    c.partner_mode("SKYWARD", "reject")
    before_reject = c.balances()
    rejected = c.transfer("NOVA_REWARDS", "SKYWARD_MILES", 10_000, "rejected")
    rejected_body = rejected.json()
    info(" -> ".join(event["to_status"] for event in rejected_body["events"]))
    expect(
        rejected_body.get("status") == "REVERSED" and c.balances() == before_reject,
        f"REVERSED ({rejected_body['failure']['code']}); the 10,000 NOVA points were returned",
        f"HTTP {rejected.status_code}: {rejected.text}",
    )
    c.partner_mode("SKYWARD", "success")

    step(8, "Partner times out AFTER applying the credit -> the reconciler settles it")
    c.partner_mode("SKYWARD", "timeout_after_commit", delay_ms=10_000)
    info("partner applies the credit but answers too late; every retry times out too ...")
    started = time.monotonic()
    pending = c.transfer("NOVA_REWARDS", "SKYWARD_MILES", 10_000, "timeout")
    pending_body = pending.json()
    expect(
        (pending.status_code, pending_body.get("status")) == (202, "PENDING_VERIFICATION"),
        f"HTTP 202 PENDING_VERIFICATION after {time.monotonic() - started:.1f}s: the outcome "
        "is unknown, so nothing is guessed",
        f"HTTP {pending.status_code}: {pending.text}",
    )
    info("waiting for the reconciler worker to ask the partner ...")
    resolved: dict[str, Any] = pending_body
    for _ in range(45):
        time.sleep(2)
        resolved = c.api.get(
            f"/v1/transfers/{pending_body['id']}", headers={"X-User-Id": USER}
        ).json()
        if resolved["status"] != "PENDING_VERIFICATION":
            break
    expect(
        resolved["status"] == "COMPLETED",
        f"{resolved['status']} after {time.monotonic() - started:.0f}s: "
        f"the reconciler found the credit ({resolved['events'][-1]['reason']})",
        f"still {resolved['status']}; is the worker running? (make ps, make logs s=worker)",
    )
    c.partner_mode("SKYWARD", "success")

    step(9, "Unsupported route: SKYWARD_MILES -> ZENITH_POINTS")
    unsupported = c.transfer("SKYWARD_MILES", "ZENITH_POINTS", 3_000, "unsupported")
    expect(
        (unsupported.status_code, unsupported.json().get("code")) == (422, "ROUTE_NOT_SUPPORTED"),
        "HTTP 422 ROUTE_NOT_SUPPORTED (application/problem+json), nothing created",
        f"HTTP {unsupported.status_code}: {unsupported.text}",
    )

    step(10, "Insufficient balance")
    too_much = c.transfer("NOVA_REWARDS", "SKYWARD_MILES", 500_000, "too-much")
    expect(
        (too_much.status_code, too_much.json().get("code")) == (422, "INSUFFICIENT_BALANCE"),
        "HTTP 422 INSUFFICIENT_BALANCE, no transfer and no ledger rows created",
        f"HTTP {too_much.status_code}: {too_much.text}",
    )

    step(11, "Final balances and ledger invariants")
    end = c.balances()
    show_balances(end)
    delta_nova = end["NOVA_REWARDS"] - start["NOVA_REWARDS"]
    delta_miles = end["SKYWARD_MILES"] - start["SKYWARD_MILES"]
    expect(
        (delta_nova, delta_miles) == (-17_000, 16_000),
        f"NOVA {delta_nova:+,} (-10,000 -10,000 +3,000), "
        f"SKYWARD {delta_miles:+,} (+12,500 +12,500 -9,000); the rejected transfer left no trace",
        f"unexpected balance changes: NOVA {delta_nova:+,}, SKYWARD {delta_miles:+,}",
    )
    violations = asyncio.run(ledger_violations())
    expect(
        violations == 0,
        f"all {len(INVARIANTS)} ledger invariants hold: no points created or destroyed",
        f"{violations} ledger invariant violation(s); run make check-invariants",
    )
    metrics = c.api.get("/metrics").text
    for line in metrics.splitlines():
        if line.startswith(("transfers_total{", "idempotent_replays_total ")):
            info(line)
    try:
        worker_metrics = httpx.get(WORKER_METRICS, timeout=5).text
        for line in worker_metrics.splitlines():
            if line.startswith("reconciliation_resolved_total{"):
                info(f"worker: {line}")
    except httpx.HTTPError:
        info("(worker metrics not reachable)")


if __name__ == "__main__":
    main()
