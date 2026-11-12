"""Tests for the app's usage wiring: `create_app(..., recorder=...)` (M15, D-095, D-099).

With a recorder, `/v1/chat` builds `recorder.meter(request_id=<the gateway's id>, principal=
<the verified key's>, alias=<the client's model>)` and hands it to the router, for a plain request
and for a stream. So every attempt becomes one `UsageRecord` carrying who asked, which alias, and
how the attempt ended, and the client's answer is the same whatever the accounting does:

- success, a fallback (the failed attempt and the success are two rows), a stream, a stream that
  fails partway (an `error` event, and a `failed` row with the last `ProviderUsage` and `partial`
  true), a client that leaves mid-stream (`abandoned` `client_gone`) or while the stream is still
  opening (`abandoned` `cancelled`), and the real mock provider end to end (cost 0);
- a request that never reaches the router (401, 422, an unknown alias) leaves no row;
- a store that always fails, a `record` that raises and a meter that raises all leave the client's
  200 and body untouched; a store whose `write` never returns does not delay the response;
- no row or line holds a prompt, a system prompt, a stop sequence or response text;
- the lifespan closes the router, then the recorder, then the authenticator, each still closed if
  an earlier one raised, so a stream the router's shutdown closes is still recorded.

Time is the fake clock where a limit is involved; waits are event loop turns (`until`).
"""

import asyncio
import itertools
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from auth_support import (
    TEST_KEY,
    TEST_KEY_ID,
    TEST_TENANT,
    FakeKeyStore,
    make_authenticator,
)
from fastapi import FastAPI
from stream_support import (
    CLAUDE_ENTRY,
    PROMPT_MARKER,
    RESPONSE_MARKER,
    SECOND,
    STOP_MARKER,
    SYSTEM_MARKER,
    Done,
    Fail,
    Gateway,
    Hold,
    Text,
    Wire,
    app_client,
    chat_body,
    event_types,
    parse_events,
    stream_body,
    unavailable,
    until,
)
from usage_support import (
    MESSAGE_MARKER,
    PRICED_NOW,
    Answer,
    FakeUsageStore,
    Report,
    UsageAdapter,
    make_recorder,
    router_errors,
    usage_messages,
    usage_module,
)

from llmgateway.app import create_app

IDS = itertools.count(1)


def new_id() -> str:
    return f"gw_app_{next(IDS)}"


def wall() -> float:
    return PRICED_NOW


def app_over(
    gateway: Gateway,
    recorder: Any,
    *,
    key_store: FakeKeyStore | None = None,
    id_factory: Any = None,
) -> FastAPI:
    return create_app(
        router=gateway.router,
        authenticator=make_authenticator(key_store),
        recorder=recorder,
        id_factory=id_factory or new_id,
    )


async def written(store: FakeUsageStore, count: int) -> None:
    await until(lambda: len(store.records) >= count, f"{count} record(s) to be written")


async def quiet(turns: int = 200) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def record_dict(record: Any) -> dict[str, Any]:
    return {name: getattr(record, name) for name in record.__dataclass_fields__}


def expected_record(request_id: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "recorded_at": datetime.fromtimestamp(PRICED_NOW, UTC),
        "request_id": request_id,
        "tenant_id": TEST_TENANT,
        "key_id": TEST_KEY_ID,
        "alias": "fast",
        "provider_instance": CLAUDE_ENTRY.instance,
        "provider_type": "claude",
        "model": CLAUDE_ENTRY.model,
        "stream": False,
        "attempt": 1,
        "outcome": "success",
        "reason": None,
        "provider_status": None,
        "partial": False,
        "input_tokens": 12,
        "output_tokens": 3,
        "cost_nanousd": 12 * 100 + 3 * 500,
        "delivered_chars": None,
        "estimated_output_tokens": None,
    }
    base.update(fields)
    return base


def claude_gateway(adapter: UsageAdapter, **kwargs: Any) -> Gateway:
    return Gateway(adapter, entries=(CLAUDE_ENTRY,), **kwargs)


# Non-streaming


@pytest.mark.asyncio
async def test_a_plain_success_is_one_row_with_every_field() -> None:
    recorder, store = make_recorder(wall_clock=wall)
    adapter = UsageAdapter(
        provider_type="claude", results=[Answer(input_tokens=12, output_tokens=3)]
    )
    app = app_over(claude_gateway(adapter), recorder, id_factory=lambda: "gw_one")
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=chat_body())
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "gw_one"
    await written(store, 1)
    assert [record_dict(r) for r in store.records] == [expected_record("gw_one")]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_the_row_carries_the_verified_keys_tenant_and_key_id() -> None:
    recorder, store = make_recorder()
    keys = FakeKeyStore(TEST_KEY, tenant="tenant-b")
    app = app_over(Gateway(UsageAdapter()), recorder, key_store=keys)
    async with app_client(app) as client:
        assert (await client.post("/v1/chat", json=chat_body())).status_code == 200
    await written(store, 1)
    (record,) = store.records
    assert (record.tenant_id, record.key_id) == ("tenant-b", TEST_KEY_ID)
    await recorder.aclose()


@pytest.mark.asyncio
async def test_the_rows_alias_is_the_clients_model_and_its_model_is_the_providers() -> None:
    recorder, store = make_recorder()
    app = app_over(Gateway(UsageAdapter()), recorder)
    async with app_client(app) as client:
        await client.post("/v1/chat", json=chat_body())
    await written(store, 1)
    (record,) = store.records
    assert (record.alias, record.model, record.provider_instance) == (
        "fast",
        "model-x",
        "primary-instance",
    )
    await recorder.aclose()


@pytest.mark.asyncio
async def test_the_real_mock_provider_is_one_row_with_word_counts_and_cost_zero() -> None:
    recorder, store = make_recorder()
    app = create_app(authenticator=make_authenticator(), recorder=recorder)
    body = {"model": "mock", "messages": [{"role": "user", "content": "say hi"}], "max_tokens": 8}
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=body)
    assert response.status_code == 200
    data = response.json()
    await written(store, 1)
    (record,) = store.records
    assert (record.alias, record.provider_instance, record.provider_type, record.model) == (
        "mock",
        "mock",
        "mock",
        "mock-1",
    )
    assert (record.request_id, record.stream, record.attempt) == (data["id"], False, 1)
    assert (record.outcome, record.reason, record.provider_status, record.partial) == (
        "success",
        None,
        None,
        False,
    )
    assert (record.input_tokens, record.output_tokens) == (
        data["usage"]["input_tokens"],
        data["usage"]["output_tokens"],
    )
    assert record.cost_nanousd == 0
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_fallback_is_a_failed_row_then_a_success_row_with_the_same_request_id() -> None:
    recorder, store = make_recorder(wall_clock=wall)
    first = UsageAdapter(provider_type="claude", results=[Fail(unavailable(503, "http_status"))])
    second = UsageAdapter(provider_type="mock", results=[Answer(input_tokens=5, output_tokens=6)])
    gateway = Gateway(first, second, entries=(CLAUDE_ENTRY, SECOND))
    app = app_over(gateway, recorder, id_factory=lambda: "gw_fb")
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=chat_body())
    assert response.status_code == 200
    assert response.json()["provider_instance"] == SECOND.instance
    await written(store, 2)
    assert [record_dict(r) for r in store.records] == [
        expected_record(
            "gw_fb",
            outcome="failed",
            reason="http_status",
            provider_status=503,
            input_tokens=None,
            output_tokens=None,
            cost_nanousd=None,
        ),
        expected_record(
            "gw_fb",
            provider_instance=SECOND.instance,
            provider_type="mock",
            model=SECOND.model,
            input_tokens=5,
            output_tokens=6,
            cost_nanousd=0,
        ),
    ]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_retry_is_its_own_row_with_the_next_attempt_number() -> None:
    recorder, store = make_recorder()
    adapter = UsageAdapter(results=[Fail(unavailable()), Answer()])
    app = app_over(Gateway(adapter, retry={"max_attempts": 2}), recorder)
    async with app_client(app) as client:
        assert (await client.post("/v1/chat", json=chat_body())).status_code == 200
    await written(store, 2)
    assert [(r.attempt, r.outcome) for r in store.records] == [(1, "failed"), (2, "success")]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_request_that_every_entry_fails_is_a_failed_row_and_a_502() -> None:
    recorder, store = make_recorder()
    adapter = UsageAdapter(results=[Fail(unavailable(503, "http_status"))])
    app = app_over(Gateway(adapter), recorder)
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=chat_body())
    assert response.status_code == 502
    await written(store, 1)
    (record,) = store.records
    assert (record.outcome, record.reason, record.provider_status) == ("failed", "http_status", 503)
    assert (record.input_tokens, record.output_tokens, record.cost_nanousd) == (None, None, None)
    # D-101: a non-streaming attempt has no delivered text, whatever its outcome.
    assert (record.delivered_chars, record.estimated_output_tokens) == (None, None)
    await recorder.aclose()


# Streaming


@pytest.mark.asyncio
async def test_a_stream_success_is_one_stream_row_with_the_ends_usage_and_no_usage_event() -> None:
    recorder, store = make_recorder(wall_clock=wall)
    adapter = UsageAdapter(
        [Report(1, 0), Text("a"), Report(4, 2), Text("b"), Done(input_tokens=12, output_tokens=3)],
        provider_type="claude",
    )
    app = app_over(claude_gateway(adapter), recorder, id_factory=lambda: "gw_s1")
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    events = parse_events(response.content)
    assert event_types(events) == ["delta", "delta", "end"]
    assert events[-1]["usage"] == {"input_tokens": 12, "output_tokens": 3}
    await written(store, 1)
    assert [record_dict(r) for r in store.records] == [expected_record("gw_s1", stream=True)]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_the_real_mock_stream_is_one_row_with_the_clients_usage_and_cost_zero() -> None:
    recorder, store = make_recorder()
    app = create_app(authenticator=make_authenticator(), recorder=recorder)
    body = {
        "model": "mock",
        "messages": [{"role": "user", "content": "say hi to me"}],
        "max_tokens": 8,
        "stream": True,
    }
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=body)
    assert response.status_code == 200
    end = parse_events(response.content)[-1]
    assert end["type"] == "end"
    await written(store, 1)
    (record,) = store.records
    assert (record.stream, record.outcome, record.provider_type, record.cost_nanousd) == (
        True,
        "success",
        "mock",
        0,
    )
    assert (record.input_tokens, record.output_tokens) == (
        end["usage"]["input_tokens"],
        end["usage"]["output_tokens"],
    )
    assert record.request_id == end["id"]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_stream_that_fails_partway_is_an_error_event_and_a_partial_failed_row() -> None:
    recorder, store = make_recorder(wall_clock=wall)
    adapter = UsageAdapter(
        [Text("a"), Report(2, 3), Fail(unavailable(503, "http_status"))], provider_type="claude"
    )
    app = app_over(claude_gateway(adapter), recorder, id_factory=lambda: "gw_s2")
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert event_types(parse_events(response.content)) == ["delta", "error"]
    await written(store, 1)
    assert [record_dict(r) for r in store.records] == [
        expected_record(
            "gw_s2",
            stream=True,
            outcome="failed",
            reason="http_status",
            provider_status=503,
            partial=True,
            input_tokens=2,
            output_tokens=3,
            cost_nanousd=2 * 100 + 3 * 500,
            # D-101: the one delta "a" reached the client; Claude's rate gives ceil(13 / 35) = 1.
            delivered_chars=1,
            estimated_output_tokens=1,
        )
    ]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_stream_that_fails_before_its_first_event_is_a_json_error_and_a_failed_row() -> (
    None
):
    recorder, store = make_recorder()
    adapter = UsageAdapter([Report(2, 0), Fail(unavailable(503, "http_status"))])
    app = app_over(Gateway(adapter), recorder)
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 502
    await written(store, 1)
    (record,) = store.records
    assert (record.stream, record.outcome, record.partial) == (True, "failed", True)
    assert (record.input_tokens, record.output_tokens) == (2, 0)
    # D-101: nothing was sent before the first event, so 0 characters and 0 estimated tokens.
    assert (record.delivered_chars, record.estimated_output_tokens) == (0, 0)
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_client_that_leaves_mid_stream_is_an_abandoned_client_gone_row() -> None:
    recorder, store = make_recorder(wall_clock=wall)
    hold = Hold()
    adapter = UsageAdapter(
        [Text("a"), Report(2, 3), hold, Text("late"), Done()], provider_type="claude"
    )
    gateway = claude_gateway(adapter)
    app = app_over(gateway, recorder, id_factory=lambda: "gw_gone")
    wire = Wire(stream_body())
    async with wire.running(app):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        await wire.wait_for(lambda: wire.event_count == 1, "the first event to reach the client")
        wire.disconnect()
        await wire.finished()
    await written(store, 1)
    assert [record_dict(r) for r in store.records] == [
        expected_record(
            "gw_gone",
            stream=True,
            outcome="abandoned",
            reason="client_gone",
            provider_status=None,
            partial=True,
            input_tokens=2,
            output_tokens=3,
            cost_nanousd=2 * 100 + 3 * 500,
            # D-101: "a" was sent before the client left.
            delivered_chars=1,
            estimated_output_tokens=1,
        )
    ]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_client_that_leaves_while_the_stream_opens_is_an_abandoned_cancelled_row() -> None:
    recorder, store = make_recorder()
    hold = Hold()
    adapter = UsageAdapter([hold, Text("late"), Done()])
    app = app_over(Gateway(adapter), recorder, id_factory=lambda: "gw_open")
    wire = Wire(stream_body())
    async with wire.running(app):
        await wire.wait_for(hold.reached.is_set, "the adapter to wait for the provider")
        wire.disconnect()
        await wire.finished()
    await written(store, 1)
    (record,) = store.records
    assert (record.request_id, record.stream, record.outcome, record.reason) == (
        "gw_open",
        True,
        "abandoned",
        "cancelled",
    )
    assert (record.provider_status, record.partial, record.input_tokens) == (None, False, None)
    assert (record.output_tokens, record.cost_nanousd) == (None, None)
    # D-101: a stream attempt stopped while opening has 0 characters and 0 estimated tokens,
    # never null: null is for rows that are not cut streams.
    assert (record.delivered_chars, record.estimated_output_tokens) == (0, 0)
    await recorder.aclose()


# Requests that never reach the router leave no row


@pytest.mark.asyncio
async def test_a_401_a_422_and_an_unknown_alias_leave_no_row() -> None:
    recorder, store = make_recorder()
    app = app_over(Gateway(UsageAdapter()), recorder)
    async with app_client(app) as client:
        unauthenticated = await client.post(
            "/v1/chat", json=chat_body(), headers={"authorization": "Bearer nope"}
        )
        invalid = await client.post("/v1/chat", json={"model": "fast"})
        unknown = await client.post("/v1/chat", json=chat_body(model="no-such-alias"))
        unknown_stream = await client.post("/v1/chat", json=stream_body(model="no-such-alias"))
        control = await client.post("/v1/chat", json=chat_body())
    assert (unauthenticated.status_code, invalid.status_code) == (401, 422)
    assert (unknown.status_code, unknown_stream.status_code) == (400, 400)
    await written(store, 1)
    await quiet()
    assert [r.request_id for r in store.records] == [control.headers["x-request-id"]]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_request_with_no_key_leaves_no_row() -> None:
    recorder, store = make_recorder()
    app = app_over(Gateway(UsageAdapter()), recorder)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous:
        response = await anonymous.post("/v1/chat", json=chat_body())
    assert response.status_code == 401
    await quiet()
    assert store.attempted == []
    await recorder.aclose()


@pytest.mark.asyncio
async def test_with_no_recorder_the_app_works_and_records_nothing() -> None:
    gateway = Gateway(UsageAdapter())
    app = create_app(router=gateway.router, authenticator=make_authenticator())
    async with app_client(app) as client:
        assert (await client.post("/v1/chat", json=chat_body())).status_code == 200
        assert (await client.post("/v1/chat", json=stream_body())).status_code == 200


# What accounting does never changes the client's answer


@pytest.mark.asyncio
async def test_a_store_that_always_fails_leaves_the_clients_answer_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUsageStore(error=usage_module().UsageStoreUnavailable("OSError"))
    recorder, _ = make_recorder(store)
    app = app_over(Gateway(UsageAdapter(results=[Answer(content=RESPONSE_MARKER)])), recorder)
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=chat_body())
    assert response.status_code == 200
    assert response.json()["content"] == RESPONSE_MARKER
    await until(lambda: bool(usage_messages(caplog)), "the failure to be logged")
    assert usage_messages(caplog) == [
        "usage_write_failed count=1 reason=store_error error=UsageStoreUnavailable"
    ]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_store_that_always_fails_leaves_a_streams_events_intact() -> None:
    store = FakeUsageStore(error=usage_module().UsageStoreUnavailable("OSError"))
    recorder, _ = make_recorder(store)
    app = app_over(Gateway(UsageAdapter([Text("a"), Text("b"), Done()])), recorder)
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert event_types(parse_events(response.content)) == ["delta", "delta", "end"]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_record_that_raises_leaves_the_clients_answer_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    recorder, _ = make_recorder()

    def broken(_: Any) -> None:
        raise RuntimeError(MESSAGE_MARKER)

    monkeypatch.setattr(recorder, "record", broken)
    app = app_over(
        Gateway(UsageAdapter(results=[Answer(content="fine")])),
        recorder,
        id_factory=lambda: "gw_bad",
    )
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=chat_body())
    assert response.status_code == 200
    assert response.json()["content"] == "fine"
    assert router_errors(all_logs) == ["usage_report_failed request_id=gw_bad error=RuntimeError"]
    assert MESSAGE_MARKER not in all_logs.text
    await recorder.aclose()


@pytest.mark.asyncio
async def test_a_meter_that_raises_leaves_a_streams_events_intact(
    monkeypatch: pytest.MonkeyPatch, all_logs: pytest.LogCaptureFixture
) -> None:
    recorder, _ = make_recorder()

    def broken_meter(**_: Any) -> Any:
        def meter(_: Any) -> None:
            raise RuntimeError(MESSAGE_MARKER)

        return meter

    monkeypatch.setattr(recorder, "meter", broken_meter)
    app = app_over(Gateway(UsageAdapter([Text("a"), Done()])), recorder, id_factory=lambda: "gw_bm")
    async with app_client(app) as client:
        response = await client.post("/v1/chat", json=stream_body())
    assert response.status_code == 200
    assert event_types(parse_events(response.content)) == ["delta", "end"]
    assert router_errors(all_logs) == ["usage_report_failed request_id=gw_bm error=RuntimeError"]
    await recorder.aclose()


@pytest.mark.asyncio
async def test_the_meter_is_built_with_the_gateway_id_the_principal_and_the_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, store = make_recorder()
    seen: list[dict[str, Any]] = []
    real = recorder.meter

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(recorder, "meter", spy)
    app = app_over(Gateway(UsageAdapter()), recorder, id_factory=lambda: "gw_spy")
    async with app_client(app) as client:
        await client.post("/v1/chat", json=chat_body())
        await client.post("/v1/chat", json=stream_body())
    assert len(seen) == 2
    for kwargs in seen:
        assert set(kwargs) == {"request_id", "principal", "alias"}
        assert kwargs["request_id"] == "gw_spy"
        assert kwargs["alias"] == "fast"
        assert (kwargs["principal"].key_id, kwargs["principal"].tenant_id) == (
            TEST_KEY_ID,
            TEST_TENANT,
        )
    await recorder.aclose()
    assert len(store.records) == 2


@pytest.mark.asyncio
async def test_no_row_and_no_line_holds_a_prompt_a_system_prompt_a_stop_sequence_or_the_answer(
    all_logs: pytest.LogCaptureFixture,
) -> None:
    recorder, store = make_recorder()
    adapter = UsageAdapter(
        [Text(RESPONSE_MARKER), Done()], results=[Answer(content=RESPONSE_MARKER)]
    )
    app = app_over(Gateway(adapter), recorder)
    body = chat_body(system=SYSTEM_MARKER, stop=[STOP_MARKER])
    async with app_client(app) as client:
        await client.post("/v1/chat", json=body)
        await client.post("/v1/chat", json={**body, "stream": True})
    await written(store, 2)
    await recorder.aclose()
    text = repr(store.records) + all_logs.text
    for marker in (PROMPT_MARKER, SYSTEM_MARKER, STOP_MARKER, RESPONSE_MARKER, TEST_KEY):
        assert marker not in text, marker


# The latency guard (D-099): the insert is never awaited by a request


@pytest.mark.asyncio
async def test_a_store_whose_write_never_returns_does_not_delay_a_plain_response() -> None:
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store)
    app = app_over(Gateway(UsageAdapter()), recorder)
    try:
        async with app_client(app) as client:
            task = asyncio.ensure_future(client.post("/v1/chat", json=chat_body()))
            await until(task.done, "the response to complete")
            response = task.result()
        assert response.status_code == 200
        await until(lambda: len(store.attempted) == 1, "the write to start")
        assert store.batches == []
    finally:
        store.release()
        await recorder.aclose()


@pytest.mark.asyncio
async def test_a_store_whose_write_never_returns_does_not_delay_a_stream() -> None:
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store)
    app = app_over(Gateway(UsageAdapter([Text("a"), Text("b"), Done()])), recorder)
    try:
        async with app_client(app) as client:
            task = asyncio.ensure_future(client.post("/v1/chat", json=stream_body()))
            await until(task.done, "the stream to complete")
            response = task.result()
        assert event_types(parse_events(response.content)) == ["delta", "delta", "end"]
        await until(lambda: len(store.attempted) == 1, "the write to start")
        assert store.batches == []
    finally:
        store.release()
        await recorder.aclose()


@pytest.mark.asyncio
async def test_requests_keep_being_served_while_a_write_hangs_and_the_rows_follow() -> None:
    store = FakeUsageStore()
    store.hang()
    recorder, _ = make_recorder(store)
    app = app_over(Gateway(UsageAdapter()), recorder)
    try:
        async with app_client(app) as client:
            for _ in range(3):
                assert (await client.post("/v1/chat", json=chat_body())).status_code == 200
        store.release()
        await written(store, 3)
    finally:
        await recorder.aclose()


# The lifespan (D-099): router, then recorder, then authenticator


class Closer:
    """Stands in for something with `aclose`: writes its name to a shared log, may raise."""

    def __init__(self, name: str, log: list[str], error: Exception | None = None) -> None:
        self.name = name
        self.log = log
        self.error = error

    async def aclose(self) -> None:
        self.log.append(self.name)
        if self.error is not None:
            raise self.error


def lifespan_app(
    monkeypatch: pytest.MonkeyPatch,
    log: list[str],
    *,
    errors: Mapping[str, Exception] | None = None,
    with_recorder: bool = True,
) -> FastAPI:
    raising = errors or {}
    authenticator = make_authenticator()
    monkeypatch.setattr(
        authenticator, "aclose", Closer("authenticator", log, raising.get("authenticator")).aclose
    )
    recorder: Any = Closer("recorder", log, raising.get("recorder")) if with_recorder else None
    router: Any = Closer("router", log, raising.get("router"))
    return create_app(router=router, authenticator=authenticator, recorder=recorder)


async def run_lifespan(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_shutdown_closes_the_router_then_the_recorder_then_the_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log))
    assert log == ["router", "recorder", "authenticator"]


@pytest.mark.asyncio
async def test_shutdown_without_a_recorder_closes_the_router_then_the_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    await run_lifespan(lifespan_app(monkeypatch, log, with_recorder=False))
    assert log == ["router", "authenticator"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(["router"], id="router"),
        pytest.param(["recorder"], id="recorder"),
        pytest.param(["authenticator"], id="authenticator"),
        pytest.param(["router", "recorder"], id="router_and_recorder"),
        pytest.param(["router", "recorder", "authenticator"], id="all_three"),
    ],
)
async def test_each_is_still_closed_when_an_earlier_one_raises(
    monkeypatch: pytest.MonkeyPatch, failing: list[str]
) -> None:
    log: list[str] = []
    errors = {name: RuntimeError(f"{name}-boom") for name in failing}
    app = lifespan_app(monkeypatch, log, errors=errors)
    with pytest.raises(RuntimeError):
        await run_lifespan(app)
    assert log == ["router", "recorder", "authenticator"]
