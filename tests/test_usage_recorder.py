"""Tests for `UsageRecorder`, `UsageRecord` and `AttemptUsage` (M15, D-095, D-099).

The recorder is how usage reaches the database without the response path ever paying for it
or failing on it: `meter` builds a record, `record` queues it, one writer task writes batches.
What is pinned:

- the shape of the two dataclasses, and that `meter` builds a record from the attempt, the
  principal, the alias, the injected wall clock (UTC, read when the meter is called, never
  rounded) and a cost computed for that record's UTC day (D-096, D-098);
- `record` is synchronous and never raises, whatever the queue or the store is doing;
- one writer task, started by the first record on the running loop, writes batches of at most
  `batch_size` records that were already queued, never waiting to fill one, and each batch is
  written once and never retried;
- every failure is one fixed line on `llmgateway.usage`: `usage_write_failed` at WARNING for a
  timeout (limit 5 s) and for `UsageStoreUnavailable`, at ERROR for anything else, and the writer
  goes on. A record offered to a full queue is dropped and counted, and the count is written once
  after each batch as `usage_dropped ... reason=queue_full`;
- `aclose` stops accepting records (`reason=closed`, at once), drains for up to 5 s, cancels what is
  left (`reason=shutdown`, queued plus the batch in flight), closes the store exactly once, is
  idempotent, and works when nothing was ever recorded;
- no line holds a request id, tenant, alias, DSN or exception message (Lesson 9), at any level.

Time is the fake clock: `virtual_loop_time` points the event loop at it, so a limit fires on
its exact second and `until` counts event loop turns, never seconds (Lesson 15, D-084).
"""

import asyncio
import dataclasses
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from stream_support import WALL_START, FakeClock, pass_time, until
from usage_support import (
    ALIAS,
    ALIAS_MARKER,
    DSN_MARKER,
    MESSAGE_MARKER,
    PRICED_NOW,
    PRINCIPAL,
    REQUEST_ID,
    REQUEST_MARKER,
    TENANT_MARKER,
    FakeUsageStore,
    attempt,
    everything_written,
    make_prices,
    make_record,
    make_recorder,
    usage_lines,
    usage_messages,
    usage_module,
    virtual_loop_time,
)

from llmgateway.auth import Principal
from llmgateway.schema import Provider

if TYPE_CHECKING:
    from llmgateway.usage import UsageRecorder


@asynccontextmanager
async def running(
    store: FakeUsageStore | None = None, **kwargs: Any
) -> "AsyncIterator[tuple[UsageRecorder, FakeUsageStore]]":
    """A recorder and its store; whatever the test did, the writer is stopped at the end."""
    recorder, held = make_recorder(store, **kwargs)
    try:
        yield recorder, held
    finally:
        if held.gate is not None:
            held.release()
        await recorder.aclose()


def unavailable(name: str = "OSError") -> Exception:
    error: Exception = usage_module().UsageStoreUnavailable(name)
    return error


def line_count_is(caplog: pytest.LogCaptureFixture, count: int) -> Callable[[], bool]:
    return lambda: len(usage_lines(caplog)) == count


def lines_exceed(caplog: pytest.LogCaptureFixture, seen: int) -> Callable[[], bool]:
    return lambda: len(usage_lines(caplog)) > seen


async def written(store: FakeUsageStore, count: int) -> None:
    await until(lambda: len(store.records) >= count, f"{count} record(s) to be written")


# The dataclasses (D-095)


def test_the_attempt_usage_fields_are_in_the_documented_order() -> None:
    names = [f.name for f in dataclasses.fields(usage_module().AttemptUsage)]
    assert names == [
        "provider_instance",
        "provider_type",
        "model",
        "stream",
        "attempt",
        "outcome",
        "reason",
        "provider_status",
        "partial",
        "usage",
        "delivered_chars",
        "cache_hit",
    ]


def test_the_usage_record_fields_are_in_the_documented_order() -> None:
    names = [f.name for f in dataclasses.fields(usage_module().UsageRecord)]
    assert names == [
        "recorded_at",
        "request_id",
        "tenant_id",
        "key_id",
        "alias",
        "provider_instance",
        "provider_type",
        "model",
        "stream",
        "attempt",
        "outcome",
        "reason",
        "provider_status",
        "partial",
        "input_tokens",
        "output_tokens",
        "cost_nanousd",
        "delivered_chars",
        "estimated_output_tokens",
        "cache_hit",
    ]


def test_attempt_usage_and_usage_record_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        attempt().outcome = "failed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_record().outcome = "failed"  # type: ignore[misc]


def test_usage_store_unavailable_is_an_exception_whose_message_is_the_type_name() -> None:
    error = unavailable("ConnectionRefusedError")
    assert isinstance(error, Exception)
    assert str(error) == "ConnectionRefusedError"


# The meter builds the record (D-095, D-096)


@pytest.mark.asyncio
async def test_the_meter_builds_a_record_from_the_attempt_the_request_and_the_clock() -> None:
    recorder, store = make_recorder(wall_clock=lambda: PRICED_NOW + 0.25)
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(
        attempt(
            instance="claude-instance",
            provider_type="claude",
            model="claude-haiku-5-5",
            stream=True,
            number=2,
            outcome="failed",
            reason="http_status",
            provider_status=503,
            partial=True,
            usage=(12, 3),
            delivered_chars=70,
        )
    )
    await recorder.aclose()
    assert store.records == [
        usage_module().UsageRecord(
            recorded_at=datetime.fromtimestamp(PRICED_NOW + 0.25, UTC),
            request_id=REQUEST_ID,
            tenant_id=PRINCIPAL.tenant_id,
            key_id=PRINCIPAL.key_id,
            alias=ALIAS,
            provider_instance="claude-instance",
            provider_type="claude",
            model="claude-haiku-5-5",
            stream=True,
            attempt=2,
            outcome="failed",
            reason="http_status",
            provider_status=503,
            partial=True,
            input_tokens=12,
            output_tokens=3,
            cost_nanousd=12 * 100 + 3 * 500,
            # 70 characters at Claude's rate: 70 * 13 / 35 is exactly 26 (D-101).
            delivered_chars=70,
            estimated_output_tokens=26,
            # D-113: a provider attempt is never a hit.
            cache_hit=False,
        )
    ]


@pytest.mark.asyncio
async def test_the_meter_returns_none_and_the_record_waits_for_the_writer() -> None:
    async with running() as (recorder, store):
        meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
        assert meter(attempt()) is None
        assert store.attempted == []
        await written(store, 1)


@pytest.mark.asyncio
async def test_the_recorded_time_is_utc_and_keeps_the_fraction_of_a_second() -> None:
    recorder, store = make_recorder(wall_clock=lambda: WALL_START + 0.123456)
    recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)(attempt())
    await recorder.aclose()
    (record,) = store.records
    assert record.recorded_at.utcoffset() == timedelta(0)
    assert record.recorded_at.tzinfo is not None
    assert record.recorded_at == datetime.fromtimestamp(WALL_START + 0.123456, UTC)
    assert record.recorded_at.microsecond == 123456


@pytest.mark.asyncio
async def test_the_meter_reads_the_clock_when_it_is_called_not_when_it_is_made() -> None:
    wall = FakeClock(WALL_START)
    recorder, store = make_recorder(wall_clock=wall)
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    wall.advance(1000)
    meter(attempt())
    wall.advance(1000)
    meter(attempt())
    await recorder.aclose()
    assert [r.recorded_at for r in store.records] == [
        datetime.fromtimestamp(WALL_START + 1000, UTC),
        datetime.fromtimestamp(WALL_START + 2000, UTC),
    ]


@pytest.mark.asyncio
async def test_the_default_wall_clock_is_the_real_time_in_utc() -> None:
    store = FakeUsageStore()
    recorder = usage_module().UsageRecorder(store, make_prices())
    before = datetime.now(UTC)
    recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)(attempt())
    await recorder.aclose()
    after = datetime.now(UTC)
    (record,) = store.records
    assert before - timedelta(seconds=1) <= record.recorded_at <= after + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_a_meter_per_request_keeps_each_requests_own_identity() -> None:
    recorder, store = make_recorder()
    other = Principal(key_id="0" * 16, tenant_id="tenant-b")
    first = recorder.meter(request_id="gw_one", principal=PRINCIPAL, alias="fast")
    second = recorder.meter(request_id="gw_two", principal=other, alias="smart")
    first(attempt())
    second(attempt())
    first(attempt(number=2))
    await recorder.aclose()
    assert [(r.request_id, r.tenant_id, r.key_id, r.alias, r.attempt) for r in store.records] == [
        ("gw_one", PRINCIPAL.tenant_id, PRINCIPAL.key_id, "fast", 1),
        ("gw_two", "tenant-b", "0" * 16, "smart", 1),
        ("gw_one", PRINCIPAL.tenant_id, PRINCIPAL.key_id, "fast", 2),
    ]


@pytest.mark.asyncio
async def test_a_mock_attempt_costs_zero_and_keeps_its_counts() -> None:
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(attempt(provider_type="mock", model="mock-1", usage=(7, 9)))
    await recorder.aclose()
    (record,) = store.records
    assert (record.provider_type, record.input_tokens, record.output_tokens) == ("mock", 7, 9)
    assert record.cost_nanousd == 0
    assert type(record.cost_nanousd) is int


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "model"),
    [("claude", "claude-haiku-5-5"), ("gemini", "gemini-3.6-flash"), ("mock", "mock-1")],
)
async def test_an_attempt_with_no_usage_has_null_counts_and_a_null_cost(
    provider_type: Provider, model: str
) -> None:
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(
        attempt(
            provider_type=provider_type,
            model=model,
            outcome="failed",
            reason="timeout",
            usage=None,
        )
    )
    await recorder.aclose()
    (record,) = store.records
    assert (record.input_tokens, record.output_tokens, record.cost_nanousd) == (None, None, None)
    assert (record.outcome, record.reason) == ("failed", "timeout")


@pytest.mark.asyncio
async def test_zero_counts_are_counts_and_cost_zero() -> None:
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(0, 0)))
    await recorder.aclose()
    (record,) = store.records
    assert (record.input_tokens, record.output_tokens, record.cost_nanousd) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("moment", "per_million_in", "per_million_out"),
    [
        pytest.param(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), 750, 3750, id="last_second"),
        pytest.param(datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC), 1500, 7500, id="first_second"),
        pytest.param(datetime(2027, 1, 1, 12, 0, 0, tzinfo=UTC), 1500, 7500, id="noon"),
    ],
)
async def test_the_cost_uses_the_price_in_effect_on_the_records_utc_day(
    moment: datetime, per_million_in: int, per_million_out: int
) -> None:
    recorder, store = make_recorder(wall_clock=lambda: moment.timestamp())
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(attempt(provider_type="gemini", model="gemini-3.6-flash", usage=(1000, 100)))
    await recorder.aclose()
    (record,) = store.records
    assert record.cost_nanousd == 1000 * per_million_in + 100 * per_million_out


@pytest.mark.asyncio
async def test_the_long_prompt_prices_reach_the_record() -> None:
    recorder, store = make_recorder()
    meter = recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(100_000, 10)))
    meter(attempt(provider_type="claude", model="claude-haiku-5-5", usage=(100_001, 10)))
    await recorder.aclose()
    assert [r.cost_nanousd for r in store.records] == [
        100_000 * 100 + 10 * 500,
        100_001 * 500 + 10 * 2500,
    ]
    assert make_prices() is not None


# record(): synchronous, queued, never raises


@pytest.mark.asyncio
async def test_record_returns_none_at_once_and_writes_nothing_before_the_loop_runs() -> None:
    async with running() as (recorder, store):
        assert cast(Any, recorder).record(make_record(1)) is None
        assert store.attempted == []
        await written(store, 1)
        assert store.records == [make_record(1)]


@pytest.mark.asyncio
async def test_the_writer_is_not_started_until_the_first_record() -> None:
    before = asyncio.all_tasks()
    recorder, store = make_recorder()
    await asyncio.sleep(0)
    assert asyncio.all_tasks() == before
    recorder.record(make_record(1))
    started = asyncio.all_tasks() - before
    assert len(started) == 1
    await recorder.aclose()
    assert store.records == [make_record(1)]


@pytest.mark.asyncio
async def test_there_is_one_writer_task_however_many_records() -> None:
    before = asyncio.all_tasks()
    recorder, _ = make_recorder()
    for n in range(250):
        recorder.record(make_record(n))
    assert len(asyncio.all_tasks() - before) == 1
    await recorder.aclose()


@pytest.mark.asyncio
async def test_after_aclose_no_writer_task_is_left() -> None:
    before = asyncio.all_tasks()
    recorder, _ = make_recorder()
    recorder.record(make_record(1))
    await recorder.aclose()
    assert asyncio.all_tasks() == before


# Batches (D-099)


@pytest.mark.asyncio
async def test_records_queued_together_are_written_in_batches_of_100_in_order() -> None:
    async with running() as (recorder, store):
        for n in range(250):
            recorder.record(make_record(n))
        await written(store, 250)
        assert [len(batch) for batch in store.batches] == [100, 100, 50]
        assert [r.request_id for r in store.records] == [f"gw_{n}" for n in range(250)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("batch_size", "total", "sizes"),
    [(3, 7, [3, 3, 1]), (1, 3, [1, 1, 1]), (5, 5, [5]), (2, 5, [2, 2, 1])],
)
async def test_the_batch_size_is_the_given_one(
    batch_size: int, total: int, sizes: list[int]
) -> None:
    async with running(batch_size=batch_size) as (recorder, store):
        for n in range(total):
            recorder.record(make_record(n))
        await written(store, total)
        assert [len(batch) for batch in store.batches] == sizes


@pytest.mark.asyncio
async def test_a_batch_is_written_with_one_write_call_and_never_waits_to_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    virtual_loop_time(monkeypatch)
    async with running() as (recorder, store):
        recorder.record(make_record(1))
        await written(store, 1)
        assert [len(batch) for batch in store.attempted] == [1]
        recorder.record(make_record(2))
        await written(store, 2)
        assert [len(batch) for batch in store.attempted] == [1, 1]


@pytest.mark.asyncio
async def test_records_that_arrive_during_a_write_go_in_the_next_batch() -> None:
    held = FakeUsageStore()
    held.hang()
    async with running(held) as (recorder, store):
        recorder.record(make_record(1))
        await until(lambda: len(store.attempted) == 1, "the first write to start")
        for n in range(2, 6):
            recorder.record(make_record(n))
        await asyncio.sleep(0)
        assert len(store.attempted) == 1
        store.release()
        await written(store, 5)
        assert [[r.request_id for r in batch] for batch in store.batches] == [
            ["gw_1"],
            ["gw_2", "gw_3", "gw_4", "gw_5"],
        ]


@pytest.mark.asyncio
async def test_the_writer_keeps_going_after_a_batch_has_been_written() -> None:
    async with running() as (recorder, store):
        for round_ in range(3):
            recorder.record(make_record(round_))
            await written(store, round_ + 1)
        assert len(store.batches) == 3


# Failures: one fixed line, the writer goes on, a batch is never retried (D-099)


@pytest.mark.asyncio
async def test_a_store_error_is_a_warning_with_the_batch_size_and_the_class_name_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=unavailable("OSError"))
    async with running(store) as (recorder, _):
        for n in range(3):
            recorder.record(make_record(n))
        await until(lambda: bool(usage_lines(caplog)), "the failure to be logged")
    assert usage_lines(caplog) == [
        (
            logging.WARNING,
            "usage_write_failed count=3 reason=store_error error=UsageStoreUnavailable",
        )
    ]


@pytest.mark.asyncio
async def test_a_failed_batch_is_written_once_and_the_next_batch_is_a_new_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=unavailable())
    async with running(store) as (recorder, _):
        recorder.record(make_record(1))
        await until(lambda: bool(usage_lines(caplog)), "the failure to be logged")
        store.error = None
        recorder.record(make_record(2))
        await written(store, 1)
        for _ in range(50):
            await asyncio.sleep(0)
    assert [[r.request_id for r in batch] for batch in store.attempted] == [["gw_1"], ["gw_2"]]
    assert [r.request_id for r in store.records] == ["gw_2"]


@pytest.mark.asyncio
async def test_the_writer_keeps_running_after_every_kind_of_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=unavailable())
    async with running(store) as (recorder, _):
        for n, error in enumerate(
            [unavailable(), RuntimeError("x"), OSError("y"), ValueError("z")]
        ):
            store.error = error
            recorder.record(make_record(n))
            await until(line_count_is(caplog, n + 1), "the failure line")
        store.error = None
        recorder.record(make_record(9))
        await written(store, 1)
    assert [r.request_id for r in store.records] == ["gw_9"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError(MESSAGE_MARKER), id="runtime_error"),
        pytest.param(KeyError(MESSAGE_MARKER), id="key_error"),
        pytest.param(ValueError(MESSAGE_MARKER), id="value_error"),
        pytest.param(TypeError(MESSAGE_MARKER), id="type_error"),
        pytest.param(AssertionError(MESSAGE_MARKER), id="assertion_error"),
        pytest.param(OSError(MESSAGE_MARKER), id="a_raw_os_error_is_not_wrapped"),
        pytest.param(ConnectionRefusedError(MESSAGE_MARKER), id="a_raw_connection_error"),
    ],
)
async def test_any_other_exception_from_the_store_is_an_error_line_with_reason_bug(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeUsageStore(error=error)
    async with running(store) as (recorder, _):
        recorder.record(make_record(1))
        recorder.record(make_record(2))
        await until(lambda: bool(usage_lines(caplog)), "the failure to be logged")
    assert usage_lines(caplog) == [
        (logging.ERROR, f"usage_write_failed count=2 reason=bug error={type(error).__name__}")
    ]
    assert MESSAGE_MARKER not in everything_written(caplog)
    assert [r.exc_info for r in caplog.records if r.name == "llmgateway.usage"] == [None]


@pytest.mark.asyncio
async def test_a_store_error_line_never_holds_the_errors_own_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=unavailable(f"OSError {DSN_MARKER} {MESSAGE_MARKER}"))
    async with running(store) as (recorder, _):
        recorder.record(make_record(1))
        await until(lambda: bool(usage_lines(caplog)), "the failure to be logged")
    assert usage_messages(caplog) == [
        "usage_write_failed count=1 reason=store_error error=UsageStoreUnavailable"
    ]
    assert DSN_MARKER not in everything_written(caplog)
    assert MESSAGE_MARKER not in everything_written(caplog)


@pytest.mark.asyncio
async def test_a_write_that_outlives_the_default_5_seconds_is_a_timeout_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    async with running(store) as (recorder, _):
        for n in range(3):
            recorder.record(make_record(n))
        await until(lambda: len(store.attempted) == 1, "the write to start")
        await pass_time(clock, 4.75)
        assert usage_lines(caplog) == []
        assert store.cancelled == 0
        await pass_time(clock, 0.5)
        await until(lambda: bool(usage_lines(caplog)), "the timeout to be logged")
    assert usage_lines(caplog) == [
        (logging.WARNING, "usage_write_failed count=3 reason=timeout error=TimeoutError")
    ]
    assert store.cancelled == 1
    assert store.records == []


@pytest.mark.asyncio
async def test_the_write_timeout_is_the_given_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    async with running(store, write_timeout=1.0) as (recorder, _):
        recorder.record(make_record(1))
        await until(lambda: len(store.attempted) == 1, "the write to start")
        await pass_time(clock, 0.75)
        assert usage_lines(caplog) == []
        await pass_time(clock, 0.5)
        await until(lambda: bool(usage_lines(caplog)), "the timeout to be logged")
    assert usage_messages(caplog) == [
        "usage_write_failed count=1 reason=timeout error=TimeoutError"
    ]


@pytest.mark.asyncio
async def test_after_a_timeout_the_writer_writes_the_next_record_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    async with running(store) as (recorder, _):
        recorder.record(make_record(1))
        await until(lambda: len(store.attempted) == 1, "the write to start")
        await pass_time(clock, 5.25)
        await until(lambda: bool(usage_lines(caplog)), "the timeout to be logged")
        store.gate = None
        recorder.record(make_record(2))
        await written(store, 1)
    assert [[r.request_id for r in b] for b in store.attempted] == [["gw_1"], ["gw_2"]]
    assert [r.request_id for r in store.records] == ["gw_2"]


# Dropped records (D-099)


@pytest.mark.asyncio
async def test_a_record_offered_to_a_full_queue_is_dropped_and_counted_once_per_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running(max_queue=3) as (recorder, store):
        for n in range(5):
            assert cast(Any, recorder).record(make_record(n)) is None
        assert usage_lines(caplog) == []
        await written(store, 3)
        await until(lambda: bool(usage_lines(caplog)), "the drop to be logged")
    assert [r.request_id for r in store.records] == ["gw_0", "gw_1", "gw_2"]
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=2 reason=queue_full")]


@pytest.mark.asyncio
async def test_the_dropped_count_starts_again_after_it_is_written(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running(max_queue=2) as (recorder, store):
        for n in range(3):
            recorder.record(make_record(n))
        await until(lambda: len(usage_lines(caplog)) == 1, "the first drop line")
        recorder.record(make_record(10))
        await written(store, 3)
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(usage_lines(caplog)) == 1
        for n in range(11, 14):
            recorder.record(make_record(n))
        await until(lambda: len(usage_lines(caplog)) == 2, "the second drop line")
    assert usage_messages(caplog) == [
        "usage_dropped count=1 reason=queue_full",
        "usage_dropped count=1 reason=queue_full",
    ]


@pytest.mark.asyncio
async def test_the_default_queue_holds_10000_records_and_drops_the_next(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with running() as (recorder, store):
        for n in range(10_001):
            recorder.record(make_record(n))
        await written(store, 10_000)
        await until(lambda: bool(usage_lines(caplog)), "the drop to be logged")
    assert len(store.records) == 10_000
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=1 reason=queue_full")]


@pytest.mark.asyncio
async def test_a_failed_batch_is_reported_before_the_records_dropped_meanwhile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=unavailable())
    async with running(store, max_queue=2) as (recorder, _):
        for n in range(5):
            recorder.record(make_record(n))
        await until(lambda: len(usage_lines(caplog)) == 2, "both lines")
    assert usage_lines(caplog) == [
        (
            logging.WARNING,
            "usage_write_failed count=2 reason=store_error error=UsageStoreUnavailable",
        ),
        (logging.WARNING, "usage_dropped count=3 reason=queue_full"),
    ]


@pytest.mark.asyncio
async def test_drops_during_a_slow_write_are_one_line_after_that_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore()
    store.hang()
    async with running(store, max_queue=2) as (recorder, _):
        recorder.record(make_record(1))
        await until(lambda: len(store.attempted) == 1, "the first write to start")
        for n in range(2, 7):
            recorder.record(make_record(n))
        assert usage_lines(caplog) == []
        store.release()
        await written(store, 3)
        await until(lambda: bool(usage_lines(caplog)), "the drop to be logged")
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=3 reason=queue_full")]
    assert [r.request_id for r in store.records] == ["gw_1", "gw_2", "gw_3"]


@pytest.mark.asyncio
async def test_record_never_raises_whatever_the_queue_holds() -> None:
    async with running(max_queue=1) as (recorder, _):
        for n in range(20):
            assert cast(Any, recorder).record(make_record(n)) is None


# Shutdown (D-099)


@pytest.mark.asyncio
async def test_aclose_with_no_record_ever_made_closes_the_store_once_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = asyncio.all_tasks()
    recorder, store = make_recorder()
    await recorder.aclose()
    assert store.closed == 1
    assert store.attempted == []
    assert usage_lines(caplog) == []
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_aclose_writes_what_is_queued_before_it_closes_the_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder, store = make_recorder()
    for n in range(5):
        recorder.record(make_record(n))
    await recorder.aclose()
    assert [r.request_id for r in store.records] == [f"gw_{n}" for n in range(5)]
    assert store.log == ["write", "close"]
    assert store.closed == 1
    assert usage_lines(caplog) == []


@pytest.mark.asyncio
async def test_aclose_drains_more_than_one_batch() -> None:
    recorder, store = make_recorder(batch_size=2)
    for n in range(5):
        recorder.record(make_record(n))
    await recorder.aclose()
    assert [len(b) for b in store.batches] == [2, 2, 1]
    assert store.log == ["write", "write", "write", "close"]


@pytest.mark.asyncio
async def test_aclose_is_idempotent_and_closes_the_store_once() -> None:
    recorder, store = make_recorder()
    recorder.record(make_record(1))
    await recorder.aclose()
    await recorder.aclose()
    await recorder.aclose()
    assert store.closed == 1
    assert store.log == ["write", "close"]


@pytest.mark.asyncio
async def test_concurrent_aclose_calls_close_the_store_once() -> None:
    recorder, store = make_recorder()
    recorder.record(make_record(1))
    await asyncio.gather(recorder.aclose(), recorder.aclose(), recorder.aclose())
    assert store.closed == 1


@pytest.mark.asyncio
async def test_a_record_offered_after_aclose_is_dropped_at_once_with_reason_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder, store = make_recorder()
    await recorder.aclose()
    assert cast(Any, recorder).record(make_record(1)) is None
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=1 reason=closed")]
    recorder.meter(request_id=REQUEST_ID, principal=PRINCIPAL, alias=ALIAS)(attempt())
    assert usage_messages(caplog) == ["usage_dropped count=1 reason=closed"] * 2
    await asyncio.sleep(0)
    assert store.attempted == []
    assert store.log == ["close"]


@pytest.mark.asyncio
async def test_a_record_offered_while_the_drain_is_running_is_dropped_as_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store)
    recorder.record(make_record(1))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await asyncio.sleep(0)
    recorder.record(make_record(2))
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=1 reason=closed")]
    store.release()
    await closing
    assert [r.request_id for r in store.records] == ["gw_1"]


@pytest.mark.asyncio
async def test_aclose_waits_for_a_slow_write_that_finishes_inside_the_drain_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store, write_timeout=60.0)
    for n in range(3):
        recorder.record(make_record(n))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await pass_time(clock, 3.0)
    assert not closing.done()
    store.release()
    await until(closing.done, "aclose to return")
    await closing
    assert [r.request_id for r in store.records] == ["gw_0", "gw_1", "gw_2"]
    assert store.closed == 1
    assert usage_lines(caplog) == []


@pytest.mark.asyncio
async def test_what_is_left_when_the_5_second_drain_ends_is_cancelled_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store, batch_size=2, write_timeout=60.0)
    for n in range(5):
        recorder.record(make_record(n))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await pass_time(clock, 4.75)
    assert not closing.done()
    assert usage_lines(caplog) == []
    await pass_time(clock, 0.5)
    await until(closing.done, "aclose to return")
    await closing
    # The batch in flight (2) and what was still queued (3).
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=5 reason=shutdown")]
    assert store.cancelled == 1
    assert store.closed == 1
    assert store.records == []
    assert not any("usage_write_failed" in m for m in usage_messages(caplog))
    assert store.log == ["write", "close"]


@pytest.mark.asyncio
async def test_the_drain_time_is_the_given_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store, drain_timeout=1.0, write_timeout=60.0)
    recorder.record(make_record(1))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await pass_time(clock, 0.75)
    assert not closing.done()
    await pass_time(clock, 0.5)
    await until(closing.done, "aclose to return")
    assert usage_lines(caplog) == [(logging.WARNING, "usage_dropped count=1 reason=shutdown")]


@pytest.mark.asyncio
async def test_a_failing_batch_during_the_drain_is_logged_and_the_store_is_still_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=unavailable())
    recorder, _ = make_recorder(store, batch_size=2)
    for n in range(3):
        recorder.record(make_record(n))
    await recorder.aclose()
    assert usage_messages(caplog) == [
        "usage_write_failed count=2 reason=store_error error=UsageStoreUnavailable",
        "usage_write_failed count=1 reason=store_error error=UsageStoreUnavailable",
    ]
    assert store.closed == 1
    assert not any("shutdown" in m for m in usage_messages(caplog))


@pytest.mark.asyncio
async def test_aclose_without_a_hang_logs_no_shutdown_line_when_everything_was_written(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder, store = make_recorder()
    for n in range(300):
        recorder.record(make_record(n))
    await recorder.aclose()
    assert len(store.records) == 300
    assert usage_lines(caplog) == []


# Leaks (Lesson 9): fixed lines, at every level, for every path


ALLOWED = re.compile(
    r"usage_write_failed count=\d+ reason=(timeout|store_error|bug) error=\w+"
    r"|usage_dropped count=\d+ reason=(queue_full|closed|shutdown)"
)


def assert_lines_are_fixed(caplog: pytest.LogCaptureFixture) -> None:
    text = everything_written(caplog)
    for marker in (REQUEST_MARKER, TENANT_MARKER, ALIAS_MARKER, DSN_MARKER, MESSAGE_MARKER):
        assert marker not in text, marker
    for level, message in usage_lines(caplog):
        if level >= logging.WARNING:
            assert ALLOWED.fullmatch(message), message


def marked(n: int) -> Any:
    return make_record(
        n, request_id=f"{REQUEST_MARKER}-{n}", tenant_id=TENANT_MARKER, alias=ALIAS_MARKER
    )


@pytest.mark.asyncio
async def test_no_line_for_a_failure_a_drop_a_timeout_or_a_closed_record_holds_request_data(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    principal = Principal(key_id="f" * 16, tenant_id=TENANT_MARKER)
    store = FakeUsageStore()
    async with running(store, max_queue=2, write_timeout=1.0) as (rec, _):
        meter = rec.meter(request_id=REQUEST_MARKER, principal=principal, alias=ALIAS_MARKER)
        for n in range(4):
            rec.record(marked(n))
        await until(lambda: len(usage_lines(all_logs)) == 1, "the drop line")
        for error in (
            unavailable(f"{MESSAGE_MARKER} {DSN_MARKER}"),
            RuntimeError(f"{MESSAGE_MARKER} {DSN_MARKER}"),
            OSError(f"could not connect to postgresql://u:{DSN_MARKER}@h/db {MESSAGE_MARKER}"),
        ):
            seen = len(usage_lines(all_logs))
            store.error = error
            meter(attempt())
            await until(lines_exceed(all_logs, seen), "a failure line")
        store.error = None
        store.hang()
        seen = len(usage_lines(all_logs))
        meter(attempt())
        await until(lambda: len(store.attempted) == 5, "a write to hang")
        await pass_time(clock, 1.5)
        await until(lines_exceed(all_logs, seen), "the timeout line")
        await rec.aclose()
        rec.record(marked(50))
        meter(attempt())
    assert {m.split("reason=")[1].split()[0] for _, m in usage_lines(all_logs)} == {
        "queue_full",
        "store_error",
        "bug",
        "timeout",
        "closed",
    }
    assert_lines_are_fixed(all_logs)


@pytest.mark.asyncio
async def test_no_line_for_what_a_shutdown_drops_holds_request_data(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store, write_timeout=60.0, drain_timeout=1.0)
    for n in range(3):
        recorder.record(marked(n))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await pass_time(clock, 1.5)
    await until(closing.done, "aclose to return")
    assert usage_messages(all_logs) == ["usage_dropped count=3 reason=shutdown"]
    assert_lines_are_fixed(all_logs)


# Added by the M15 mutation checks: gaps a mutant got through


class SlowUnwindStore(FakeUsageStore):
    """A store whose hung write takes a few event loop turns to finish unwinding a cancel."""

    async def write(self, records: Any) -> None:
        try:
            await super().write(records)
        except asyncio.CancelledError:
            for _ in range(5):
                await asyncio.sleep(0)
            self.log.append("unwound")
            raise


@pytest.mark.asyncio
async def test_aclose_waits_for_the_cancelled_writer_to_finish_before_it_closes_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = SlowUnwindStore()
    store.hang()
    recorder, _ = make_recorder(store, write_timeout=60.0)
    recorder.record(make_record(1))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await pass_time(clock, 5.5)
    await until(closing.done, "aclose to return")
    await closing
    assert store.log == ["write", "unwound", "close"]


@pytest.mark.asyncio
async def test_a_cancelled_aclose_still_closes_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store, write_timeout=60.0)
    recorder.record(make_record(1))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    await pass_time(clock, 1.0)
    assert not closing.done()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert store.closed == 1
    store.release()


@pytest.mark.asyncio
async def test_a_write_cancelled_at_the_drain_limit_ends_the_writer_task_for_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cancel must reach the writer and end it: a writer that swallowed it would loop on and
    # `aclose` would never return. Whatever happens, the test ends every task it started.
    clock = virtual_loop_time(monkeypatch)
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store, write_timeout=60.0)
    recorder.record(make_record(1))
    await until(lambda: len(store.attempted) == 1, "the write to start")
    closing = asyncio.ensure_future(recorder.aclose())
    try:
        await pass_time(clock, 5.5)
        await until(closing.done, "aclose to return")
        await closing
        assert store.cancelled == 1
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert [t for t in others if not t.done()] == []
    finally:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()


def test_the_recorders_limits_are_keyword_only() -> None:
    # D-099 gives `UsageRecorder(store, prices, *, max_queue=..., ...)`.
    with pytest.raises(TypeError):
        usage_module().UsageRecorder(FakeUsageStore(), make_prices(), 5)
