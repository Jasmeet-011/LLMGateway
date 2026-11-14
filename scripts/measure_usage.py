"""D-099 measurement: what recording usage costs on the response path.

Run from the repo root: uv run python scripts/measure_usage.py
Results and conditions of the run at 620d7ad are in docs/decisions/D-099.md ("Measured").
It is a design check that recording stays off the response path, not a
docs/RESULTS.md performance figure.
In-process app (httpx ASGITransport), mock provider with 0 latency, fake key store.
Three arms, interleaved in rounds to spread machine noise:
  none    - create_app without a recorder
  instant - recorder over a store whose write returns at once
  slow    - recorder over a store whose write sleeps 5 ms per batch
Plus a microbenchmark of the meter call alone (build record, cost, put_nowait).
"""

import asyncio
import platform
import statistics
import sys
import time
from collections.abc import Sequence

sys.path.insert(0, "tests")

import httpx
from auth_support import AUTH_HEADERS, TEST_KEY_ID, TEST_TENANT, make_authenticator

from llmgateway.app import create_app
from llmgateway.auth import Principal
from llmgateway.prices import load_prices
from llmgateway.schema import Usage
from llmgateway.usage import AttemptUsage, UsageRecord, UsageRecorder

N = 2000
WARMUP = 200
ROUNDS = 3
BODY = {"model": "mock", "max_tokens": 32, "messages": [{"role": "user", "content": "hello there"}]}


class InstantStore:
    def __init__(self) -> None:
        self.rows = 0

    async def write(self, records: Sequence[UsageRecord]) -> None:
        self.rows += len(records)

    async def aclose(self) -> None:
        pass


class SlowStore(InstantStore):
    async def write(self, records: Sequence[UsageRecord]) -> None:
        await asyncio.sleep(0.005)
        self.rows += len(records)


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


async def arm(name: str) -> tuple[list[float], int]:
    store: InstantStore | None = None
    recorder = None
    if name == "instant":
        store = InstantStore()
    elif name == "slow":
        store = SlowStore()
    if store is not None:
        recorder = UsageRecorder(store, load_prices())
    app = create_app(authenticator=make_authenticator(), recorder=recorder)
    latencies: list[float] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        for i in range(WARMUP + N):
            started = time.perf_counter()
            response = await client.post("/v1/chat", json=BODY, headers=AUTH_HEADERS)
            elapsed = time.perf_counter() - started
            if response.status_code != 200:
                raise RuntimeError(f"request {i} answered {response.status_code}")
            if i >= WARMUP:
                latencies.append(elapsed * 1000)
    rows = 0
    if recorder is not None and store is not None:
        await recorder.aclose()
        rows = store.rows
    return latencies, rows


async def meter_alone(calls: int) -> float:
    recorder = UsageRecorder(InstantStore(), load_prices(), max_queue=calls + 1)
    meter = recorder.meter(
        request_id="gw_measure",
        principal=Principal(key_id=TEST_KEY_ID, tenant_id=TEST_TENANT),
        alias="fast",
    )
    report = AttemptUsage(
        provider_instance="gemini",
        provider_type="gemini",
        model="gemini-3.5-flash-lite",
        stream=False,
        attempt=1,
        outcome="success",
        reason=None,
        provider_status=None,
        partial=False,
        usage=Usage(input_tokens=15, output_tokens=76),
    )
    started = time.perf_counter()
    for _ in range(calls):
        meter(report)
    elapsed = time.perf_counter() - started
    await recorder.aclose()
    return elapsed / calls * 1e6


async def main() -> None:
    print(f"python {sys.version.split()[0]} on {platform.platform()}, {platform.processor()}")
    print(f"N={N} sequential requests per arm per round, {WARMUP} warmup, {ROUNDS} rounds")
    results: dict[str, list[float]] = {"none": [], "instant": [], "slow": []}
    for round_ in range(ROUNDS):
        for name in ("none", "instant", "slow"):
            latencies, rows = await arm(name)
            results[name].extend(latencies)
            print(
                f"round {round_ + 1} {name:8s} p50={pct(latencies, 0.5):.3f} ms "
                f"p95={pct(latencies, 0.95):.3f} ms p99={pct(latencies, 0.99):.3f} ms "
                f"mean={statistics.fmean(latencies):.3f} ms rows={rows}"
            )
    for name, latencies in results.items():
        print(
            f"ALL {name:8s} n={len(latencies)} p50={pct(latencies, 0.5):.3f} ms "
            f"p95={pct(latencies, 0.95):.3f} ms p99={pct(latencies, 0.99):.3f} ms"
        )
    for calls in (100_000, 100_000):
        print(f"meter call alone: {await meter_alone(calls):.2f} us per call over {calls} calls")


asyncio.run(main())
