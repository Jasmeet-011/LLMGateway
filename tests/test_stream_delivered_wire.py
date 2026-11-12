"""Tests for D-101 through the real `EventStream`: which characters count as delivered.

The router only adds up what the consumer marks with `ServedStream.mark_delivered` (see
`test_stream_delivered.py`). The consumer is `EventStream`, and D-101 says when it marks: for each
`delta`, just after the send of that delta's frame has returned. So, driving the app on a real
ASGI connection (`Wire`, whose `send` the tests can park, stall or make raise) and reading the rows
the meter queued:

- every delta whose send returned counts, by the characters of its text (code points, not the
  bytes of the frame and not the JSON escapes), and the `end` and `error` frames add nothing;
- a delta whose send was cancelled (the client left), timed out (`client_stalled`) or raised
  (`OSError` is `client_gone`, anything else `send_failed`) does not count, and neither does one
  the router handed out that no send was ever tried for. Counting when the router yields would
  count the delta whose send is parked, and these tests would see one frame too many;
- the first event is not special: if its send fails the attempt has 0 delivered characters;
- a stream that reached its end has both new values null, whatever it sent;
- the row's estimate is the meter's: Claude's rate for a Claude attempt, `ceil(chars / 4)` for
  Gemini and the mock, never added to the reported counts or the cost.

A stream that stopped before its first event has 0 and 0 (an int, not null), also when it fell
back from one entry to the next. Time is the fake clock where a limit is involved, and waits are
event loop turns, as in `test_stream_disconnect.py`.
"""

from collections.abc import MutableMapping
from typing import Any, cast

import pytest
from auth_support import make_authenticator
from stream_support import (
    CLAUDE_ENTRY,
    Done,
    Entry,
    Fail,
    Gateway,
    Hold,
    Park,
    Stall,
    Takes,
    Text,
    Wire,
    app_client,
    stream_body,
    unavailable,
    until,
)
from usage_support import (
    FakeUsageStore,
    Report,
    UsageAdapter,
    expected_estimate,
    make_recorder,
)

from llmgateway.app import create_app
from llmgateway.providers.mock import MockProvider
from llmgateway.schema import Provider

SPECS = pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
TYPES = pytest.mark.parametrize("provider_type", ["claude", "gemini", "mock"])
# So high that nothing in these tests can be its doing.
NEVER = 3600.0

GEMINI_ENTRY = Entry("gemini-instance", "gemini-3.6-flash")
MOCK_ENTRY = Entry("mock-instance", "mock-1")
ENTRY_FOR = {"claude": CLAUDE_ENTRY, "gemini": GEMINI_ENTRY, "mock": MOCK_ENTRY}

# Text that is not ASCII, has quotes and a newline (JSON escapes), and an emoji (4 UTF-8 bytes).
FIRST_TEXT = 'héllo "q"\n'  # 10 characters, 12 bytes
SECOND_TEXT = "😀 wörld"  # 7 characters, 11 bytes


def app_over(gateway: Gateway, recorder: Any) -> Any:
    return create_app(router=gateway.router, authenticator=make_authenticator(), recorder=recorder)


def gateway_for(provider_type: str, *scripts: Any, **kwargs: Any) -> tuple[UsageAdapter, Gateway]:
    adapter = UsageAdapter(*scripts, provider_type=cast(Provider, provider_type))
    return adapter, Gateway(adapter, entries=(ENTRY_FOR[provider_type],), **kwargs)


async def rows(store: FakeUsageStore, recorder: Any, count: int) -> list[Any]:
    """Waits for `count` rows, closes the recorder, and returns every row written."""
    await until(lambda: len(store.records) >= count, f"{count} row(s) to be written")
    await recorder.aclose()
    assert len(store.records) == count
    return list(store.records)


def shape(record: Any) -> tuple[Any, ...]:
    return (
        record.outcome,
        record.reason,
        record.delivered_chars,
        record.estimated_output_tokens,
    )


class SendRaises(Wire):
    """A connection whose `send` raises on one message, as a dead socket's can."""

    def __init__(self, error: BaseException, at: int) -> None:
        super().__init__(stream_body())
        self.error = error
        self.at = at
        self.attempts = 0

    async def send(self, message: MutableMapping[str, Any]) -> None:
        index = self.attempts
        self.attempts += 1
        if index == self.at:
            raise self.error
        await super().send(message)


# Everything sent counts; the error frame and the end frame add nothing


@pytest.mark.asyncio
@TYPES
async def test_a_stream_that_fails_after_its_deltas_were_sent_counts_each_delivered_character(
    provider_type: str,
) -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for(
        provider_type, [Text(FIRST_TEXT), Text(SECOND_TEXT), Fail(unavailable(503, "http_status"))]
    )
    async with app_client(app_over(gateway, recorder)) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert response.text.count("event: delta") == 2
    assert response.text.count("event: error") == 1
    (record,) = await rows(store, recorder, 1)
    # 10 + 7 characters: not the 22 bytes of the text, and not the frames' JSON.
    assert (record.outcome, record.reason, record.provider_status) == ("failed", "http_status", 503)
    assert record.delivered_chars == 17
    assert record.estimated_output_tokens == expected_estimate(provider_type, 17)
    assert record.stream is True


@pytest.mark.asyncio
async def test_the_estimate_in_a_row_is_claudes_rate_for_claude_and_the_generic_one_otherwise() -> (
    None
):
    estimates = {}
    for provider_type in ("claude", "gemini", "mock"):
        recorder, store = make_recorder()
        _, gateway = gateway_for(provider_type, [Text("a" * 100), Fail(unavailable())])
        async with app_client(app_over(gateway, recorder)) as client:
            await client.post("/v1/chat", json=stream_body())
        (record,) = await rows(store, recorder, 1)
        assert record.delivered_chars == 100
        estimates[provider_type] = record.estimated_output_tokens
    # ceil(100 * 13 / 35) = 38 and ceil(100 / 4) = 25.
    assert estimates == {"claude": 38, "gemini": 25, "mock": 25}


@pytest.mark.asyncio
@TYPES
async def test_a_stream_that_reached_its_end_has_both_values_null_whatever_it_sent(
    provider_type: str,
) -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for(
        provider_type, [Text(FIRST_TEXT), Text(SECOND_TEXT), Report(3, 4), Done()]
    )
    async with app_client(app_over(gateway, recorder)) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.text.count("event: end") == 1
    (record,) = await rows(store, recorder, 1)
    assert (record.outcome, record.stream) == ("success", True)
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)


@pytest.mark.asyncio
async def test_a_stream_that_ends_at_once_has_both_values_null() -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for("claude", [Done()])
    async with app_client(app_over(gateway, recorder)) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    (record,) = await rows(store, recorder, 1)
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)


@pytest.mark.asyncio
async def test_a_slow_but_completed_send_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # A send that takes 9 s under a 10 s limit returns, so its delta was delivered.
    recorder, store = make_recorder()
    _, gateway = gateway_for(
        "claude", [Text("héllo"), Text("wörld"), Fail(unavailable())], idle=10.0
    )
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), clock=gateway.clock, plans={1: Takes(9.0), 2: Takes(9.0)})
    async with wire.running(app_over(gateway, recorder)):
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("failed", "http_status", 10, expected_estimate("claude", 10))


# A send that did not return does not count


@SPECS
@TYPES
@pytest.mark.asyncio
async def test_a_delta_whose_send_was_cancelled_by_a_disconnect_is_not_counted(
    spec_version: str, provider_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, store = make_recorder()
    adapter, gateway = gateway_for(
        provider_type,
        [Text("héllo"), Text("wörld!!"), Text("third"), Done()],
        idle=NEVER,
    )
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version, plans={2: Park()})
    async with wire.running(app_over(gateway, recorder)):
        await wire.wait_for(wire.send_blocked.is_set, "the second delta's send to block")
        # The router has handed out two deltas, and only the first was sent.
        assert adapter.open_generators == 1
        wire.disconnect()
        await wire.finished()
    assert wire.sends_cancelled == 1
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_gone", 5, expected_estimate(provider_type, 5))
    # Counting at the router's yield would have given 5 + 7.
    assert record.delivered_chars != 12


@SPECS
@pytest.mark.asyncio
async def test_a_first_delta_whose_send_was_cancelled_leaves_zero_delivered(
    spec_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for("claude", [Text("héllo"), Text("wörld"), Done()], idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version, plans={1: Park()})
    async with wire.running(app_over(gateway, recorder)):
        await wire.wait_for(wire.send_blocked.is_set, "the first delta's send to block")
        wire.disconnect()
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_gone", 0, 0)
    assert type(record.delivered_chars) is int


@SPECS
@pytest.mark.asyncio
async def test_a_delta_whose_send_timed_out_is_not_counted(
    spec_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, store = make_recorder()
    adapter, gateway = gateway_for(
        "gemini", [Text("héllo"), Text("wörld!!"), Text("third"), Done()], idle=10.0
    )
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version, clock=gateway.clock, plans={2: Stall()})
    async with wire.running(app_over(gateway, recorder)):
        await wire.finished()
    assert wire.sends_cancelled == 1
    assert adapter.open_generators == 0
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_stalled", 5, expected_estimate("gemini", 5))


@pytest.mark.asyncio
async def test_a_first_delta_whose_send_timed_out_leaves_zero_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for("claude", [Text("héllo"), Text("wörld"), Done()], idle=10.0)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), clock=gateway.clock, plans={1: Stall()})
    async with wire.running(app_over(gateway, recorder)):
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_stalled", 0, 0)


@pytest.mark.asyncio
async def test_a_stall_on_the_response_start_leaves_zero_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The router has the first delta in hand; the response never started, so nothing was sent.
    recorder, store = make_recorder()
    _, gateway = gateway_for("claude", [Text("héllo"), Done()], idle=10.0)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), clock=gateway.clock, plans={0: Stall()})
    async with wire.running(app_over(gateway, recorder)):
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_stalled", 0, 0)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionResetError(), id="connection_reset"),
        pytest.param(BrokenPipeError(), id="broken_pipe"),
        pytest.param(TimeoutError(), id="timeout_error_is_an_oserror"),
    ],
)
@pytest.mark.parametrize(
    ("at", "delivered"),
    [
        pytest.param(0, 0, id="the_start"),
        pytest.param(1, 0, id="the_first_delta"),
        pytest.param(2, 5, id="the_second_delta"),
        pytest.param(3, 12, id="the_third_delta"),
    ],
)
@pytest.mark.asyncio
async def test_a_delta_whose_send_raised_an_oserror_is_not_counted_and_the_client_is_gone(
    error: OSError, at: int, delivered: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for(
        "claude", [Text("héllo"), Text("wörld!!"), Text("third"), Done()], idle=NEVER
    )
    gateway.virtual_time(monkeypatch)
    wire = SendRaises(error, at)
    async with wire.running(app_over(gateway, recorder)):
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == (
        "abandoned",
        "client_gone",
        delivered,
        expected_estimate("claude", delivered),
    )


@pytest.mark.parametrize(
    ("at", "delivered"),
    [
        pytest.param(0, 0, id="the_start"),
        pytest.param(1, 0, id="the_first_delta"),
        pytest.param(2, 5, id="the_second_delta"),
        pytest.param(3, 12, id="the_third_delta"),
    ],
)
@pytest.mark.asyncio
async def test_a_delta_whose_send_raised_any_other_exception_is_not_counted_and_is_send_failed(
    at: int, delivered: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for(
        "gemini", [Text("héllo"), Text("wörld!!"), Text("third"), Done()], idle=NEVER
    )
    gateway.virtual_time(monkeypatch)
    wire = SendRaises(RuntimeError("send broke"), at)
    async with wire.running(app_over(gateway, recorder)):
        with pytest.raises(RuntimeError, match="send broke"):
            await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == (
        "abandoned",
        "send_failed",
        delivered,
        expected_estimate("gemini", delivered),
    )


# A client that leaves while the provider is silent, and a cancel from outside


@SPECS
@pytest.mark.asyncio
async def test_a_disconnect_while_the_provider_is_silent_keeps_what_was_delivered_and_the_counts(
    spec_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, store = make_recorder()
    hold = Hold()
    _, gateway = gateway_for(
        "claude", [Text("x" * 100), Report(2, 3), hold, Text("late"), Done()], idle=NEVER
    )
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), spec_version=spec_version)
    async with wire.running(app_over(gateway, recorder)):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        await wire.wait_for(lambda: wire.event_count == 1, "the first event to reach the client")
        wire.disconnect()
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_gone", 100, 38)
    # The reported counts, the partial flag and the cost are the provider's, and the estimate
    # (38) is in none of them.
    assert (record.input_tokens, record.output_tokens, record.partial) == (2, 3, True)
    assert record.cost_nanousd == 2 * 100 + 3 * 500


@pytest.mark.asyncio
async def test_a_cancel_from_outside_after_a_delivered_delta_is_client_gone_with_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A server shutting down cancels the app's task; the stream closes as client_gone (D-072).
    recorder, store = make_recorder()
    hold = Hold()
    _, gateway = gateway_for("gemini", [Text("héllo"), hold, Done()], idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body())
    async with wire.running(app_over(gateway, recorder)):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        assert wire.task is not None
        wire.task.cancel()
        await until(wire.task.done, "the app to stop")
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_gone", 5, 2)


@pytest.mark.asyncio
async def test_a_real_mock_stream_cut_between_deltas_counts_the_delivered_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mock has no provider usage (D-077): null counts and cost, and the generic estimate.
    recorder, store = make_recorder()
    gateway = Gateway(MockProvider(), entries=(MOCK_ENTRY,), idle=NEVER)
    gateway.virtual_time(monkeypatch)
    wire = Wire(stream_body(), plans={2: Park()})
    async with wire.running(app_over(gateway, recorder)):
        await wire.wait_for(wire.send_blocked.is_set, "the second delta's send to block")
        wire.disconnect()
        await wire.finished()
    (delivered_event,) = wire.events
    chars = len(delivered_event["text"])
    assert chars > 0
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "client_gone", chars, expected_estimate("mock", chars))
    assert (record.input_tokens, record.output_tokens, record.cost_nanousd) == (None, None, None)
    assert record.provider_type == "mock"


# Before the first event: 0 and 0, also across a fallback


@pytest.mark.asyncio
async def test_a_stream_that_fails_before_its_first_event_has_zero_and_zero() -> None:
    recorder, store = make_recorder()
    _, gateway = gateway_for("claude", [Report(2, 0), Fail(unavailable(503, "http_status"))])
    async with app_client(app_over(gateway, recorder)) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 502
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("failed", "http_status", 0, 0)
    assert type(record.delivered_chars) is int
    assert type(record.estimated_output_tokens) is int


@pytest.mark.asyncio
async def test_a_stream_that_falls_back_has_zero_and_zero_for_the_entry_that_sent_nothing() -> None:
    recorder, store = make_recorder()
    first = UsageAdapter([Fail(unavailable(503, "http_status"))], provider_type="claude")
    second = UsageAdapter(
        [Text("héllo"), Text("wörld"), Fail(unavailable())], provider_type="gemini"
    )
    gateway = Gateway(first, second, entries=(CLAUDE_ENTRY, GEMINI_ENTRY))
    async with app_client(app_over(gateway, recorder)) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    first_row, second_row = await rows(store, recorder, 2)
    assert (first_row.provider_type, shape(first_row)) == (
        "claude",
        ("failed", "http_status", 0, 0),
    )
    # The first entry's 0 is its own; the second entry delivered 10 characters at Gemini's rate.
    assert (second_row.provider_type, shape(second_row)) == (
        "gemini",
        ("failed", "http_status", 10, 3),
    )


@pytest.mark.asyncio
async def test_a_fallback_stream_that_succeeds_leaves_a_zero_row_then_a_null_row() -> None:
    recorder, store = make_recorder()
    first = UsageAdapter([Fail(unavailable(503, "http_status"))], provider_type="claude")
    second = UsageAdapter([Text("héllo"), Done()], provider_type="gemini")
    gateway = Gateway(first, second, entries=(CLAUDE_ENTRY, GEMINI_ENTRY))
    async with app_client(app_over(gateway, recorder)) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    first_row, second_row = await rows(store, recorder, 2)
    assert shape(first_row) == ("failed", "http_status", 0, 0)
    assert shape(second_row) == ("success", None, None, None)


@pytest.mark.asyncio
async def test_a_client_that_leaves_while_the_stream_opens_has_zero_and_zero() -> None:
    recorder, store = make_recorder()
    hold = Hold()
    _, gateway = gateway_for("claude", [hold, Text("late"), Done()])
    wire = Wire(stream_body())
    async with wire.running(app_over(gateway, recorder)):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        wire.disconnect()
        await wire.finished()
    (record,) = await rows(store, recorder, 1)
    assert shape(record) == ("abandoned", "cancelled", 0, 0)


# A plain request is never a cut stream


PLAIN = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}


@pytest.mark.asyncio
async def test_a_plain_success_has_both_values_null() -> None:
    recorder, store = make_recorder()
    gateway = Gateway(UsageAdapter(provider_type="claude"), entries=(CLAUDE_ENTRY,))
    async with app_client(app_over(gateway, recorder)) as client:
        assert (await client.post("/v1/chat", json=PLAIN)).status_code == 200
    (record,) = await rows(store, recorder, 1)
    assert record.stream is False
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)


@pytest.mark.asyncio
async def test_a_plain_failure_has_both_values_null() -> None:
    recorder, store = make_recorder()
    adapter = UsageAdapter(results=[Fail(unavailable(503, "http_status"))], provider_type="claude")
    gateway = Gateway(adapter, entries=(CLAUDE_ENTRY,))
    async with app_client(app_over(gateway, recorder)) as client:
        assert (await client.post("/v1/chat", json=PLAIN)).status_code == 502
    (record,) = await rows(store, recorder, 1)
    assert (record.stream, record.outcome) == (False, "failed")
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)
