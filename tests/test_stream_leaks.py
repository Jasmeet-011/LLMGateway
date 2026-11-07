"""Leak tests for every new path of M12, to the M11 standard (D-054, D-069, D-072).

Planted values go in every position a client or a provider can put them: the prompt, the
system prompt, a stop sequence, delta text, a thought, the provider's error text, the
frame that fails to parse, and the keys in the environment. Then each path runs: a failure
before the first event, a failure after it, a bug on either side, a client that left, a
client that stalled, the idle limit. After each one:

- nothing planted is in any log record at any level, in the text or in the record's
  other fields (`all_logs`, D-065: `caplog` alone once went blind to DEBUG lines);
- `stream_failed` and `stream_abandoned` are exactly the D-069 and D-072 formats, so
  nothing rides along in them;
- what the client gets holds no provider text and no key, and an error's code and message
  are the fixed pair. Delta text does reach the client, as the answer, and nothing else.

Names M12 adds are reached at call time, so this file collects and each test fails on its
own.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from stream_support import (
    ADAPTER_MESSAGE,
    ANTHROPIC_KEY,
    CLAUDE_ENTRY,
    CLAUDE_PING,
    FIRST,
    GEMINI_ENTRY,
    GEMINI_KEY,
    HANG,
    PROMPT_MARKER,
    PROVIDER_TEXT,
    RESPONSE_MARKER,
    SECOND,
    STOP_MARKER,
    SYSTEM_MARKER,
    THOUGHT_MARKER,
    Done,
    Entry,
    Fail,
    Gateway,
    Hold,
    Park,
    ScriptedStream,
    Stall,
    Text,
    Upstream,
    Wire,
    abandoned_line,
    body_response,
    claude_delta_of,
    claude_error,
    claude_frame,
    claude_start,
    claude_text,
    failed_line,
    full_stream_body,
    gemini_frame,
    gemini_text,
    gemini_thought,
    lines_starting,
    parse_events,
    router_lines,
    sse_response,
    unavailable,
)

from llmgateway.errors import ErrorBody
from llmgateway.providers.base import ProviderError
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.gemini import GeminiAdapter

LOG_SECRETS = (
    PROMPT_MARKER,
    SYSTEM_MARKER,
    STOP_MARKER,
    RESPONSE_MARKER,
    THOUGHT_MARKER,
    ADAPTER_MESSAGE,
    PROVIDER_TEXT,
    ANTHROPIC_KEY,
    GEMINI_KEY,
)
CLIENT_SECRETS = (
    PROMPT_MARKER,
    SYSTEM_MARKER,
    STOP_MARKER,
    THOUGHT_MARKER,
    ADAPTER_MESSAGE,
    PROVIDER_TEXT,
    ANTHROPIC_KEY,
    GEMINI_KEY,
)
# A real adapter's messages start this way; ADAPTER_MESSAGE only comes from fakes.
ADAPTER_PREFIXES = ("claude: ", "gemini: ")

FIXED_PAIRS = {
    ("provider_timeout", "The provider did not respond in time."),
    ("provider_unavailable", "The provider is unavailable."),
    ("provider_rate_limited", "The provider is rate limiting the gateway."),
    ("provider_circuit_open", "The provider is temporarily unavailable."),
    ("provider_bad_request", "The provider rejected the request."),
    ("provider_auth_error", "The gateway could not authenticate with the provider."),
    ("provider_error", "The provider request failed."),
    ("internal_error", "Internal server error."),
    ("client_disconnected", "The client disconnected."),
}


@pytest.fixture
def leaks(
    all_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """Every level, with both keys in the environment as in production."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    return all_logs


def _assert_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    # caplog.text has each formatted line and any traceback. A record's fields hold what
    # the message does not show, such as `extra`, which a structured handler would write.
    written = caplog.text + "".join(str(vars(record)) for record in caplog.records)
    for marker in LOG_SECRETS:
        assert marker not in written
    for prefix in ADAPTER_PREFIXES:
        assert prefix not in written


def _assert_body_clean(body: bytes, *, allowed_text: str | None = None) -> None:
    text = body.decode()
    for marker in CLIENT_SECRETS:
        assert marker not in text
    if allowed_text is None:
        assert RESPONSE_MARKER not in text
    for prefix in ADAPTER_PREFIXES:
        assert prefix not in text


def _assert_events_fixed(body: bytes, wanted_text: str) -> None:
    events = parse_events(body)
    assert "".join(e["text"] for e in events if e["type"] == "delta") == wanted_text
    for event in events:
        if event["type"] == "error":
            assert (event["code"], event["message"]) in FIXED_PAIRS
            assert set(event) == {"type", "code", "message", "request_id"}
            assert event["request_id"].startswith("gw_")


def _assert_json_fixed(body: bytes) -> None:
    error = ErrorBody.model_validate(json.loads(body)).error
    assert (error.code, error.message) in FIXED_PAIRS


def _assert_outcome(status: int, body: bytes, *, after: bool) -> None:
    """A failure after a delta is a 200 with an `error` event; before it, a JSON error."""
    if after:
        assert status == 200
        _assert_body_clean(body, allowed_text=RESPONSE_MARKER)
        _assert_events_fixed(body, RESPONSE_MARKER)
        assert parse_events(body)[-1]["type"] == "error"
    else:
        assert status >= 400
        _assert_body_clean(body)
        _assert_json_fixed(body)


# Failures through the real adapters


@dataclass(frozen=True)
class Scenario:
    """A provider answer, with planted text, and the client outcome it must give."""

    frames: tuple[bytes, ...]
    # True when a delta came first, so the failure is an `error` event.
    after: bool


def _claude(*frames: bytes, after: bool = False) -> Scenario:
    lead = (claude_start(), claude_text(RESPONSE_MARKER)) if after else (claude_start(),)
    return Scenario((*lead, *frames), after)


def _gemini(*frames: bytes, after: bool = False) -> Scenario:
    lead = (gemini_thought(THOUGHT_MARKER),)
    return Scenario((*lead, *(gemini_text(RESPONSE_MARKER),)[: int(after)], *frames), after)


NOT_JSON = f"data: {PROVIDER_TEXT}\n\n".encode()
BAD_BYTES = b"data: " + PROVIDER_TEXT.encode() + b"\xff\n\n"
CUT_OFF = f'data: {{"text": "{PROVIDER_TEXT}'.encode()
BAD_START = claude_start(message_id=5, input_tokens=PROVIDER_TEXT)
CLAUDE_THINKING = claude_delta_of("thinking_delta", thinking=THOUGHT_MARKER)


def _both_sides(name: str, make: Callable[..., Scenario], *frames: bytes) -> list[Any]:
    return [
        pytest.param(make(*frames), id=f"{name}-before_the_first_event"),
        pytest.param(make(*frames, after=True), id=f"{name}-after_the_first_event"),
    ]


CLAUDE_SCENARIOS = [
    *_both_sides("an_overload", _claude, claude_error("overloaded_error")),
    *_both_sides("a_bad_request", _claude, claude_error("invalid_request_error")),
    *_both_sides("an_auth_error", _claude, claude_error("authentication_error")),
    *_both_sides("an_unknown_error", _claude, claude_error(PROVIDER_TEXT)),
    *_both_sides(
        "a_frame_of_text", _claude, claude_frame({"type": "error", "error": PROVIDER_TEXT})
    ),
    *_both_sides("not_json", _claude, NOT_JSON),
    *_both_sides("bad_bytes", _claude, BAD_BYTES),
    *_both_sides("a_wrong_type", _claude, claude_text({"nested": PROVIDER_TEXT})),
    *_both_sides("cut_off", _claude, CUT_OFF),
    *_both_sides("thinking_then_nothing", _claude, CLAUDE_THINKING, CLAUDE_PING),
    pytest.param(Scenario((BAD_START,), False), id="a_bad_message_start"),
]

GEMINI_SCENARIOS = [
    *_both_sides("an_error_chunk", _gemini, gemini_frame({"error": {"message": PROVIDER_TEXT}})),
    *_both_sides(
        "a_block_reason", _gemini, gemini_frame({"promptFeedback": {"blockReason": PROVIDER_TEXT}})
    ),
    *_both_sides("not_json", _gemini, NOT_JSON),
    *_both_sides("bad_bytes", _gemini, BAD_BYTES),
    *_both_sides("a_wrong_type", _gemini, gemini_frame({"candidates": PROVIDER_TEXT})),
    *_both_sides("cut_off", _gemini, CUT_OFF),
]


def _claude_gateway(frames: tuple[bytes, ...], **kwargs: Any) -> Gateway:
    upstream = Upstream(lambda _: sse_response(*frames))
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    return Gateway(adapter, entries=(CLAUDE_ENTRY,), **kwargs)


def _gemini_gateway(frames: tuple[bytes, ...], **kwargs: Any) -> Gateway:
    upstream = Upstream(lambda _: sse_response(*frames))
    adapter = GeminiAdapter(GEMINI_KEY, transport=upstream.transport)
    return Gateway(adapter, entries=(GEMINI_ENTRY,), **kwargs)


async def _assert_scenario_clean(
    gateway: Gateway, scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    _assert_outcome(response.status_code, response.content, after=scenario.after)
    assert len(lines_starting(caplog, "stream_failed")) == int(scenario.after)
    _assert_logs_clean(caplog)


@pytest.mark.parametrize("scenario", CLAUDE_SCENARIOS)
@pytest.mark.asyncio
async def test_a_claude_failure_leaks_nothing(
    scenario: Scenario, leaks: pytest.LogCaptureFixture
) -> None:
    await _assert_scenario_clean(_claude_gateway(scenario.frames), scenario, leaks)


@pytest.mark.parametrize("scenario", GEMINI_SCENARIOS)
@pytest.mark.asyncio
async def test_a_gemini_failure_leaks_nothing(
    scenario: Scenario, leaks: pytest.LogCaptureFixture
) -> None:
    await _assert_scenario_clean(_gemini_gateway(scenario.frames), scenario, leaks)


@pytest.mark.parametrize(
    ("make", "entry"),
    [
        pytest.param(
            lambda t: ClaudeAdapter(ANTHROPIC_KEY, transport=t), CLAUDE_ENTRY, id="claude"
        ),
        pytest.param(lambda t: GeminiAdapter(GEMINI_KEY, transport=t), GEMINI_ENTRY, id="gemini"),
    ],
)
@pytest.mark.parametrize("status", [400, 401, 402, 429, 500, 529])
@pytest.mark.asyncio
async def test_a_non_2xx_body_with_provider_text_leaks_nothing(
    make: Callable[[httpx.MockTransport], Any],
    entry: Entry,
    status: int,
    leaks: pytest.LogCaptureFixture,
) -> None:
    body = json.dumps({"error": {"type": "x", "message": PROVIDER_TEXT}}).encode()
    upstream = Upstream(lambda _: body_response(status, body))
    gateway = Gateway(make(upstream.transport), entries=(entry,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    _assert_body_clean(response.content)
    _assert_json_fixed(response.content)
    _assert_logs_clean(leaks)


@pytest.mark.parametrize(
    "piece",
    [
        pytest.param(httpx.ReadError(PROVIDER_TEXT), id="a_read_error"),
        pytest.param(httpx.ReadTimeout(PROVIDER_TEXT), id="a_read_timeout"),
        pytest.param(httpx.RemoteProtocolError(PROVIDER_TEXT), id="a_protocol_error"),
    ],
)
@pytest.mark.parametrize("after", [False, True], ids=["before", "after"])
@pytest.mark.asyncio
async def test_httpx_error_text_leaks_nothing(
    piece: Exception, after: bool, leaks: pytest.LogCaptureFixture
) -> None:
    lead = (claude_start(), claude_text(RESPONSE_MARKER)) if after else (claude_start(),)
    upstream = Upstream(lambda _: sse_response(*lead, piece))
    adapter = ClaudeAdapter(ANTHROPIC_KEY, transport=upstream.transport)
    gateway = Gateway(adapter, entries=(CLAUDE_ENTRY,))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    _assert_outcome(response.status_code, response.content, after=after)
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_a_clean_claude_stream_logs_no_text_and_shows_no_thinking(
    leaks: pytest.LogCaptureFixture,
) -> None:
    frames = (
        claude_start(),
        CLAUDE_THINKING,
        claude_text(RESPONSE_MARKER),
        b'event: message_delta\ndata: {"type":"message_delta",'
        b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    )
    gateway = _claude_gateway(frames)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    assert response.status_code == 200
    _assert_body_clean(response.content, allowed_text=RESPONSE_MARKER)
    _assert_events_fixed(response.content, RESPONSE_MARKER)
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_a_clean_gemini_stream_logs_no_text_and_shows_no_thoughts(
    leaks: pytest.LogCaptureFixture,
) -> None:
    frames = (
        gemini_thought(THOUGHT_MARKER),
        gemini_text(RESPONSE_MARKER, finish="STOP", usage={"promptTokenCount": 1}),
    )
    gateway = _gemini_gateway(frames)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    assert response.status_code == 200
    _assert_body_clean(response.content, allowed_text=RESPONSE_MARKER)
    _assert_events_fixed(response.content, RESPONSE_MARKER)
    _assert_logs_clean(leaks)


# Failures from a fake, whose exception carries planted text


def _planted() -> ProviderError:
    return unavailable(503)


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="before_the_first_event"),
        pytest.param((Text(RESPONSE_MARKER),), id="after_the_first_event"),
    ],
)
@pytest.mark.asyncio
async def test_an_adapters_own_message_leaks_nothing(
    lead: tuple[Any, ...], leaks: pytest.LogCaptureFixture
) -> None:
    gateway = Gateway(ScriptedStream([*lead, Fail(_planted())]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    _assert_outcome(response.status_code, response.content, after=bool(lead))
    _assert_logs_clean(leaks)


@pytest.mark.parametrize(
    "lead",
    [
        pytest.param((), id="before_the_first_event"),
        pytest.param((Text(RESPONSE_MARKER),), id="after_the_first_event"),
    ],
)
@pytest.mark.asyncio
async def test_a_bugs_exception_text_leaks_nothing(
    lead: tuple[Any, ...], leaks: pytest.LogCaptureFixture
) -> None:
    bug = RuntimeError(f"{PROVIDER_TEXT} {PROMPT_MARKER} {ANTHROPIC_KEY}")
    gateway = Gateway(ScriptedStream([*lead, Fail(bug)]))
    wire = Wire(full_stream_body())
    async with wire.running(gateway.app()):
        await wire.finished()
    assert wire.status is not None
    _assert_outcome(wire.status, wire.body, after=bool(lead))
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_the_retries_and_the_move_before_the_first_event_leak_nothing(
    leaks: pytest.LogCaptureFixture,
) -> None:
    first = ScriptedStream([Text(""), Fail(_planted())])
    second = ScriptedStream([Text(RESPONSE_MARKER), Done()])
    gateway = Gateway(first, second, retry={"max_attempts": 3, "base_delay": 0.25})
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    assert response.status_code == 200
    _assert_body_clean(response.content, allowed_text=RESPONSE_MARKER)
    assert len(lines_starting(leaks, "attempt_failed")) == 3
    assert len(lines_starting(leaks, "fallback_taken")) == 1
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_stream_failed_holds_exactly_its_fields(leaks: pytest.LogCaptureFixture) -> None:
    gateway = Gateway(ScriptedStream([Text(RESPONSE_MARKER), Fail(_planted())]))
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    assert router_lines(leaks) == [
        failed_line(
            FIRST, 503, "http_status", "ProviderUnavailable",
            request_id=response.headers["x-request-id"],
        )
    ]  # fmt: skip
    _assert_logs_clean(leaks)


# Leaving, stalling and silence


@pytest.mark.asyncio
async def test_a_client_gone_mid_stream_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, leaks: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([Text(RESPONSE_MARKER), Text(RESPONSE_MARKER), Done()])
    gateway = Gateway(adapter, idle=3600.0)
    gateway.virtual_time(monkeypatch)
    wire = Wire(full_stream_body(), plans={2: Park()})
    async with wire.running(gateway.app()):
        await wire.wait_for(wire.send_blocked.is_set, "the send to block")
        wire.disconnect()
        await wire.finished()
    assert router_lines(leaks) == [
        abandoned_line(FIRST, "client_gone", request_id=wire.headers["x-request-id"])
    ]
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_a_client_gone_while_opening_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, leaks: pytest.LogCaptureFixture
) -> None:
    hold = Hold()
    adapter = ScriptedStream([hold, Text(RESPONSE_MARKER), Done()])
    gateway = Gateway(adapter)
    gateway.virtual_time(monkeypatch)
    wire = Wire(full_stream_body())
    async with wire.running(gateway.app()):
        await wire.wait_for(hold.reached.is_set, "the attempt to be in flight")
        wire.disconnect()
        await wire.finished()
    assert router_lines(leaks) == []
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_a_stalled_client_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, leaks: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([Text(RESPONSE_MARKER), Text(RESPONSE_MARKER), Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    wire = Wire(full_stream_body(), clock=gateway.clock, plans={1: Stall()})
    async with wire.running(gateway.app()):
        await wire.finished()
    assert router_lines(leaks) == [
        abandoned_line(FIRST, "client_stalled", request_id=wire.headers["x-request-id"])
    ]
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_the_idle_limit_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, leaks: pytest.LogCaptureFixture
) -> None:
    adapter = ScriptedStream([Text(RESPONSE_MARKER), HANG, Done()])
    gateway = Gateway(adapter, idle=10.0)
    gateway.virtual_time(monkeypatch)
    wire = Wire(full_stream_body(), clock=gateway.clock)
    async with wire.running(gateway.app()):
        await wire.finished()
    _assert_body_clean(wire.body, allowed_text=RESPONSE_MARKER)
    _assert_events_fixed(wire.body, RESPONSE_MARKER)
    assert len(lines_starting(leaks, "stream_failed")) == 1
    _assert_logs_clean(leaks)


@pytest.mark.asyncio
async def test_the_alias_and_the_entry_are_the_only_config_text_in_the_lines(
    leaks: pytest.LogCaptureFixture,
) -> None:
    second = ScriptedStream([Text(RESPONSE_MARKER), Fail(unavailable(None, "connection"))])
    gateway = Gateway(ScriptedStream([Fail(_planted())]), second)
    async with gateway.client() as client:
        response = await client.post("/v1/chat", json=full_stream_body())
    assert lines_starting(leaks, "stream_failed") == [
        failed_line(
            SECOND, "-", "connection", "ProviderUnavailable",
            request_id=response.headers["x-request-id"],
        )
    ]  # fmt: skip
    _assert_logs_clean(leaks)
