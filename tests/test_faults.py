"""Tests for fault injection (M11, D-064).

Measurement 8 needs a provider that goes down for a fixed time in the middle of a
run, and measurement 4 needs the same committed config to serve both the baseline
and the failure run. `LLMGATEWAY_FAULTS` is how both are done, so these tests pin
its grammar, every way it stops startup, what the window does to a mock, and that
it can never touch a real provider. They are derived from PLAN.md M11, D-064 and
FLOW.md section 13's tables, not from an implementation. Fallback itself is in
`test_fallback.py`.

Nothing here depends on real time: every elapsed second comes from a fake clock, and
a recording sleep moves that clock by the mock's latency. The subprocess tests run
the real startup path as `test_main.py` does, with no time limit, for the same
reason given there, and their probe makes no real waits.

The new API is resolved at call time by `_fault_window`, `_parse_faults` and
`_mock`, and values from it are typed `Any`, so this file always collects and each
test fails on its own until the API exists.

The wording of a startup message is not part of the contract, so it is not asserted.
What is: it names the variable, the entry's position counting from 1 and the rule,
and never a value. Since the wording is free, the position is read as the number that
changes when the entry moves, and "names the rule" is asked as: two different rules
never give the same message for the same position.
"""

import asyncio
import dataclasses
import importlib
import inspect
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from llmgateway.app import create_app
from llmgateway.breaker import BreakerPolicy
from llmgateway.config import ConfigError, GatewayConfig, load_config, parse_config
from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.providers.mock import FailureType, MockProvider
from llmgateway.router import RetryPolicy, build_router
from llmgateway.schema import ChatRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_FALLBACK_TOML = REPO_ROOT / "config" / "mock-fallback.toml"

VARIABLE = "LLMGATEWAY_FAULTS"
ALIAS = "fast"
PREFIX = "llmgateway: configuration error:"

MARKER = "planted-marker-value"
INSTANCE_MARKER = "planted-instance-marker"
CLAUDE_MARKER = "planted-claude-marker"
GEMINI_MARKER = "planted-gemini-marker"
RATE_MARKER = "7.31962"
TIME_MARKER = "271828"
# Digits the grammar accepts, and that no float can hold: inf, and 0 for the second.
OVERFLOWS = TIME_MARKER * 70
UNDERFLOWS = "0." + "0" * 400 + TIME_MARKER
PROMPT_MARKER = "secret-prompt-text-marker"
SYSTEM_MARKER = "secret-system-prompt-marker"
ANTHROPIC_KEY = "sk-ant-faults-test-key-marker"
GEMINI_KEY = "gemini-faults-test-key-marker"
BOTH_KEYS = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY}

CLOCK_START = 100.0
ONE_ATTEMPT = RetryPolicy(max_attempts=1)

# mock-a, mock-b and one of each real type. parse_config needs no keys.
MIXED_CONFIG = (
    '[providers.mock-a]\ntype = "mock"\n\n'
    '[providers.mock-b]\ntype = "mock"\n\n'
    '[providers.claude-a]\ntype = "claude"\n\n'
    '[providers.gemini-a]\ntype = "gemini"\n\n'
    "[aliases]\n"
    f'{ALIAS} = [{{ provider = "mock-a", model = "m1" }}, '
    '{ provider = "mock-b", model = "m2" }]\n'
    'real = [{ provider = "claude-a", model = "claude-haiku-5-5" }, '
    '{ provider = "gemini-a", model = "gemini-3.5-flash-lite" }]\n'
)

# Names that are themselves the planted values, for the no-echo tests.
MARKED_CONFIG = (
    f'[providers.{INSTANCE_MARKER}]\ntype = "mock"\n\n'
    f'[providers.{CLAUDE_MARKER}]\ntype = "claude"\n\n'
    f'[providers.{GEMINI_MARKER}]\ntype = "gemini"\n\n'
    "[aliases]\n"
    f'{ALIAS} = [{{ provider = "{INSTANCE_MARKER}", model = "m1" }}, '
    f'{{ provider = "{CLAUDE_MARKER}", model = "claude-haiku-5-5" }}, '
    f'{{ provider = "{GEMINI_MARKER}", model = "gemini-3.5-flash-lite" }}]\n'
)

ERROR_CLASSES: dict[FailureType, type[ProviderError]] = {
    "timeout": ProviderTimeout,
    "rate_limited": ProviderRateLimited,
    "unavailable": ProviderUnavailable,
    "bad_request": ProviderBadRequest,
    "auth": ProviderAuthError,
}


# The new API, resolved at call time


def _fault_window(*args: Any, **kwargs: Any) -> Any:
    return importlib.import_module("llmgateway.providers.mock").FaultWindow(*args, **kwargs)


def _parse_faults(environ: Mapping[str, str], config: GatewayConfig) -> Any:
    return importlib.import_module("llmgateway.config").parse_faults(environ, config)


# Fakes and helpers


class FakeClock:
    """A clock that only moves when a test or the fake sleep moves it."""

    def __init__(self, start: float = CLOCK_START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Records every wait and, when given a clock, moves it forward by the wait."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)


class CancellingSleep:
    """Raises `CancelledError` on the first wait, as a client cancel would, then returns."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls == 1:
            raise asyncio.CancelledError


def _request(**overrides: Any) -> ChatRequest:
    body: dict[str, Any] = {
        "model": "m1",
        "messages": [{"role": "user", "content": "hello there"}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return ChatRequest.model_validate(body)


def _chat_body(alias: str = ALIAS, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": alias,
        "messages": [{"role": "user", "content": PROMPT_MARKER}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


def _mock(clock: FakeClock, fault: Any, **settings: Any) -> MockProvider:
    """A mock on the fake clock; its latency, if any, moves that clock."""
    settings.setdefault("sleep", RecordingSleep(clock))
    extras: dict[str, Any] = {"fault": fault, "clock": clock}
    return MockProvider(**settings, **extras)


def _control(**settings: Any) -> MockProvider:
    """A mock with no fault window, built as before M11, to compare a sequence with."""
    return MockProvider(sleep=RecordingSleep(), **settings)


async def _fails(provider: MockProvider) -> bool:
    try:
        await provider.complete(_request())
    except ProviderError:
        return True
    return False


async def _failures(provider: MockProvider, calls: int) -> list[bool]:
    return [await _fails(provider) for _ in range(calls)]


async def _failures_with_time(
    provider: MockProvider, clock: FakeClock, calls: int, step: float = 1.0
) -> list[bool]:
    """One call per `step` seconds, so call `i` runs `i * step` after the first."""
    seen: list[bool] = []
    for _ in range(calls):
        seen.append(await _fails(provider))
        clock.advance(step)
    return seen


def _just_before(moment: float) -> float:
    return math.nextafter(moment, -math.inf)


def _faults(value: str, config_text: str = MIXED_CONFIG) -> Any:
    return _parse_faults({VARIABLE: value}, parse_config(config_text))


def _error(value: str, config_text: str = MIXED_CONFIG) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        _faults(value, config_text)
    return info.value


def _printed(error: BaseException) -> str:
    """The error as a traceback print or `logger.exception` would show it."""
    return "".join(traceback.format_exception(error))


def _record_http_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Every httpx.AsyncClient created from now on, in creation order."""
    opened: list[httpx.AsyncClient] = []
    original_init = httpx.AsyncClient.__init__

    def recording_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        opened.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording_init)
    return opened


def _build(
    text: str,
    value: str | None,
    *,
    clock: FakeClock | None = None,
    sleep: RecordingSleep | None = None,
    environ: dict[str, str] | None = None,
    **extras: Any,
) -> Any:
    """A router from config text with the variable set to `value`, on a fake clock."""
    env = dict(environ or {})
    if value is not None:
        env[VARIABLE] = value
    extras["clock"] = clock if clock is not None else FakeClock()
    return build_router(
        parse_config(text), env, sleep=sleep if sleep is not None else RecordingSleep(), **extras
    )


def _two_mocks(primary: str = "", secondary: str = "") -> str:
    return (
        f'[providers.mock-primary]\ntype = "mock"\n{primary}\n\n'
        f'[providers.mock-secondary]\ntype = "mock"\n{secondary}\n\n'
        f"[aliases]\n{ALIAS} = [\n"
        '    { provider = "mock-primary", model = "mock-a" },\n'
        '    { provider = "mock-secondary", model = "mock-b" },\n]\n'
    )


def _client(router: Any) -> httpx.AsyncClient:
    app = create_app(router=router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _fault_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("fault_injection_on")
    ]


def _numbers(message: str) -> Counter[str]:
    return Counter(re.findall(r"[0-9]+", message))


# FaultWindow (D-064)


def test_fault_window_is_a_frozen_dataclass_of_rate_start_and_duration() -> None:
    window = _fault_window(0.5)
    # dataclasses.fields raises TypeError for anything that is not a dataclass.
    assert [f.name for f in dataclasses.fields(window)] == ["rate", "start", "duration"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        window.rate = 1.0


def test_fault_window_defaults_to_no_delay_and_no_end() -> None:
    window = _fault_window(0.5)
    assert (window.rate, window.start, window.duration) == (0.5, 0.0, None)


def test_fault_window_keeps_the_given_values_in_order() -> None:
    window = _fault_window(1.0, 60.0, 30.0)
    assert (window.rate, window.start, window.duration) == (1.0, 60.0, 30.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate": 0},
        {"rate": 1},
        {"rate": 0.0},
        {"rate": 1.0},
        {"rate": 0.5, "start": 0},
        {"rate": 0.5, "start": 12.5, "duration": 0.001},
        {"rate": 0.5, "start": 12, "duration": 30},
    ],
    ids=["zero_int", "one_int", "zero", "one", "start_zero", "fractions", "whole_numbers"],
)
def test_fault_window_accepts_the_boundary_values(kwargs: dict[str, Any]) -> None:
    window = _fault_window(**kwargs)
    assert window.rate == kwargs["rate"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate": -0.01},
        {"rate": 1.01},
        {"rate": math.nan},
        {"rate": math.inf},
        {"rate": True},
        {"rate": False},
        {"rate": "0.5"},
        {"rate": None},
        {"rate": 0.5, "start": -0.001},
        {"rate": 0.5, "start": -1},
        {"rate": 0.5, "start": -math.inf},
        {"rate": 0.5, "start": math.nan},
        {"rate": 0.5, "start": math.inf},
        {"rate": 0.5, "start": True},
        {"rate": 0.5, "start": "1"},
        {"rate": 0.5, "start": None},
        {"rate": 0.5, "duration": 0},
        {"rate": 0.5, "duration": 0.0},
        {"rate": 0.5, "duration": -1.0},
        {"rate": 0.5, "duration": math.nan},
        {"rate": 0.5, "duration": math.inf},
        {"rate": 0.5, "duration": True},
        {"rate": 0.5, "duration": "1"},
    ],
    ids=[
        "rate_below_zero",
        "rate_above_one",
        "rate_nan",
        "rate_inf",
        "rate_true",
        "rate_false",
        "rate_string",
        "rate_none",
        "start_negative",
        "start_negative_int",
        "start_minus_inf",
        "start_nan",
        "start_inf",
        "start_true",
        "start_string",
        "start_none",
        "duration_zero_int",
        "duration_zero",
        "duration_negative",
        "duration_nan",
        "duration_inf",
        "duration_true",
        "duration_string",
    ],
)
def test_fault_window_rejects_each_bad_value(kwargs: dict[str, Any]) -> None:
    # D-064: rate from 0 to 1, start finite and >= 0, duration None or finite and > 0,
    # never a bool.
    with pytest.raises(ValueError):
        _fault_window(**kwargs)


# MockProvider's new arguments (D-064)


def test_mock_provider_takes_fault_and_clock_with_their_defaults() -> None:
    parameters = inspect.signature(MockProvider).parameters
    assert parameters["fault"].default is None
    assert parameters["clock"].default is time.monotonic


@pytest.mark.asyncio
async def test_a_mock_with_a_window_needs_no_injected_clock() -> None:
    extras: dict[str, Any] = {"fault": _fault_window(1.0)}
    provider = MockProvider(sleep=RecordingSleep(), **extras)
    assert await _fails(provider) is True


# The window (D-064)


@pytest.mark.asyncio
async def test_a_window_with_no_times_applies_from_the_first_call_on() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0))
    assert await _fails(provider) is True
    clock.advance(1_000_000.0)
    assert await _fails(provider) is True


@pytest.mark.asyncio
async def test_the_origin_is_the_first_call_and_not_when_the_mock_was_built() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 10.0, 5.0))
    clock.advance(1_000.0)
    assert await _fails(provider) is False
    clock.advance(10.0)
    assert await _fails(provider) is True


@pytest.mark.parametrize(
    ("elapsed", "failed"),
    [
        pytest.param(_just_before(10.0), False, id="just_before_the_start"),
        pytest.param(10.0, True, id="exactly_the_start_is_inside"),
        pytest.param(12.5, True, id="inside"),
        pytest.param(_just_before(15.0), True, id="just_before_the_end_is_inside"),
        pytest.param(15.0, False, id="exactly_the_end_is_outside"),
        pytest.param(1_000.0, False, id="long_after"),
    ],
)
@pytest.mark.asyncio
async def test_the_window_runs_from_its_start_up_to_but_not_including_its_end(
    elapsed: float, failed: bool
) -> None:
    clock = FakeClock(0.0)
    provider = _mock(clock, _fault_window(1.0, 10.0, 5.0))
    assert await _fails(provider) is False
    clock.now = elapsed
    assert await _fails(provider) is failed


@pytest.mark.parametrize(
    ("elapsed", "failed"),
    [
        pytest.param(_just_before(10.0), False, id="just_before_the_start"),
        pytest.param(10.0, True, id="exactly_the_start"),
        pytest.param(1_000_000.0, True, id="long_after"),
    ],
)
@pytest.mark.asyncio
async def test_a_window_with_no_duration_still_waits_for_its_start_and_then_has_no_end(
    elapsed: float, failed: bool
) -> None:
    clock = FakeClock(0.0)
    provider = _mock(clock, _fault_window(1.0, 10.0))
    assert await _fails(provider) is False
    clock.now = elapsed
    assert await _fails(provider) is failed


@pytest.mark.asyncio
async def test_a_window_starting_at_zero_includes_the_first_call() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 0.0, 5.0))
    assert await _fails(provider) is True


@pytest.mark.asyncio
async def test_the_origin_is_taken_before_the_latency_and_the_roll_after_it() -> None:
    # 1 s of latency moves the clock between the origin and the roll, so the first
    # call rolls 1 s after the origin: inside a window that starts at 1.
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 1.0, 1.0), latency_ms=1000)
    assert await _fails(provider) is True


@pytest.mark.asyncio
async def test_a_first_call_whose_latency_outlasts_the_window_rolls_outside_it() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 0.0, 1.0), latency_ms=1000)
    assert await _fails(provider) is False


@pytest.mark.asyncio
async def test_later_calls_also_roll_after_their_latency() -> None:
    # From the origin: call 1 rolls at 1, call 2 starts at 1 and rolls at 2.
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 2.0, 1.0), latency_ms=1000)
    assert await _failures(provider, 3) == [False, True, False]


@pytest.mark.asyncio
async def test_later_calls_do_not_move_the_origin() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 0.0, 5.0))
    assert await _fails(provider) is True
    clock.advance(4.0)
    assert await _fails(provider) is True
    clock.advance(1.0)
    assert await _fails(provider) is False


@pytest.mark.asyncio
async def test_a_first_call_cancelled_during_its_latency_still_sets_the_origin() -> None:
    clock = FakeClock()
    sleep = CancellingSleep()
    provider = _mock(clock, _fault_window(1.0, 10.0, 5.0), latency_ms=1, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await provider.complete(_request())
    clock.advance(10.0)
    assert await _fails(provider) is True


@pytest.mark.asyncio
async def test_elapsed_time_comes_from_the_injected_clock_only() -> None:
    # The fake clock never moves, so a real clock would be the only thing that could
    # end this window.
    clock = FakeClock(time.monotonic() + 1_000_000.0)
    provider = _mock(clock, _fault_window(1.0, 0.0, 1e-9))
    assert await _failures(provider, 3) == [True, True, True]


# The roll (D-064)


@pytest.mark.asyncio
async def test_inside_the_window_the_roll_uses_the_window_rate_on_the_same_sequence() -> None:
    provider = _mock(FakeClock(), _fault_window(0.5), failure_rate=0.0, seed=42)
    assert await _failures(provider, 200) == await _failures(
        _control(failure_rate=0.5, seed=42), 200
    )


@pytest.mark.parametrize(
    ("failure_rate", "window_rate"),
    [(0.2, 0.6), (0.6, 0.2)],
    ids=["window_higher", "config_higher"],
)
@pytest.mark.asyncio
async def test_inside_the_window_the_rate_is_the_larger_of_the_two(
    failure_rate: float, window_rate: float
) -> None:
    provider = _mock(FakeClock(), _fault_window(window_rate), failure_rate=failure_rate, seed=7)
    expected = await _failures(_control(failure_rate=0.6, seed=7), 200)
    assert await _failures(provider, 200) == expected


@pytest.mark.asyncio
async def test_a_window_rate_of_zero_does_not_stop_the_configured_failures() -> None:
    provider = _mock(FakeClock(), _fault_window(0.0), failure_rate=1.0)
    assert await _failures(provider, 3) == [True, True, True]


@pytest.mark.asyncio
async def test_outside_the_window_the_rate_is_the_configured_one() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 1_000.0, 1.0), failure_rate=0.3, seed=11)
    expected = await _failures(_control(failure_rate=0.3, seed=11), 200)
    assert await _failures(provider, 200) == expected


@pytest.mark.asyncio
async def test_a_window_does_not_shift_which_later_calls_fail_by_chance() -> None:
    # Calls 5 to 9 run inside the window. Everything after must be what the seed gives
    # with no window at all, which only holds if each of them rolled exactly once.
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 5.0, 5.0), failure_rate=0.5, seed=42)
    seen = await _failures_with_time(provider, clock, 40)
    expected = await _failures(_control(failure_rate=0.5, seed=42), 40)
    assert seen[:5] == expected[:5]
    assert seen[5:10] == [True] * 5
    assert seen[10:] == expected[10:]
    # The window must have changed something, or the rest proves nothing.
    assert seen[5:10] != expected[5:10]


@pytest.mark.asyncio
async def test_a_call_outside_the_window_rolls_even_when_the_configured_rate_is_zero() -> None:
    # No failure can happen before the window, but its calls still use up the sequence.
    clock = FakeClock()
    provider = _mock(clock, _fault_window(0.5, 5.0, 20.0), failure_rate=0.0, seed=42)
    seen = await _failures_with_time(provider, clock, 25)
    expected = await _failures(_control(failure_rate=0.5, seed=42), 25)
    assert seen[:5] == [False] * 5
    assert seen[5:] == expected[5:]


@pytest.mark.asyncio
async def test_a_window_keeps_the_fixed_regression_count_when_it_is_never_reached() -> None:
    # test_failure_count_reproducible_at_fixed_seed's snapshot, with a window present.
    provider = _mock(FakeClock(), _fault_window(1.0, 1e9, 1.0), failure_rate=0.3, seed=1234)
    assert (await _failures(provider, 1000)).count(True) == 271


@pytest.mark.asyncio
async def test_no_window_keeps_the_fixed_regression_count() -> None:
    provider = _mock(FakeClock(), None, failure_rate=0.3, seed=1234)
    assert (await _failures(provider, 1000)).count(True) == 271


@pytest.mark.parametrize("failure_type", list(ERROR_CLASSES))
@pytest.mark.asyncio
async def test_a_window_failure_is_the_instances_own_type_with_reason_injected(
    failure_type: FailureType,
) -> None:
    provider = _mock(FakeClock(), _fault_window(1.0), failure_type=failure_type)
    with pytest.raises(ProviderError) as info:
        await provider.complete(_request())
    assert type(info.value) is ERROR_CLASSES[failure_type]
    assert info.value.status is None
    assert info.value.reason == "injected"


@pytest.mark.asyncio
async def test_a_window_failure_keeps_the_default_failure_type() -> None:
    provider = _mock(FakeClock(), _fault_window(1.0))
    with pytest.raises(ProviderUnavailable):
        await provider.complete(_request())


@pytest.mark.asyncio
async def test_a_window_failure_still_waits_the_latency_and_counts_the_call() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    provider = _mock(clock, _fault_window(1.0), latency_ms=250, sleep=sleep)
    assert await _fails(provider) is True
    assert sleep.calls == [0.25]
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_an_answer_outside_the_window_is_the_usual_reply() -> None:
    clock = FakeClock()
    provider = _mock(clock, _fault_window(1.0, 10.0, 5.0))
    result = await provider.complete(_request())
    assert result.content == "mock reply to: hello there"
    assert result.provider_response_id == "mock-1"


# LLMGATEWAY_FAULTS: what it accepts (D-064)


def test_an_unset_variable_gives_no_windows() -> None:
    assert _parse_faults({}, parse_config(MIXED_CONFIG)) == {}


def test_parse_faults_reads_the_given_environ_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VARIABLE, "not valid at all")
    assert _parse_faults({}, parse_config(MIXED_CONFIG)) == {}


@pytest.mark.parametrize(
    ("value", "rate", "start", "duration"),
    [
        pytest.param("mock-a=0", 0.0, 0.0, None, id="rate_zero"),
        pytest.param("mock-a=1", 1.0, 0.0, None, id="rate_one"),
        pytest.param("mock-a=0.0", 0.0, 0.0, None, id="rate_zero_decimal"),
        pytest.param("mock-a=1.0", 1.0, 0.0, None, id="rate_one_decimal"),
        pytest.param("mock-a=1.000", 1.0, 0.0, None, id="rate_one_long_decimal"),
        pytest.param("mock-a=0.25", 0.25, 0.0, None, id="rate_fraction"),
        pytest.param("mock-a=00.5", 0.5, 0.0, None, id="rate_leading_zeros"),
        pytest.param("mock-a=1.0@60+30", 1.0, 60.0, 30.0, id="measurement_8"),
        pytest.param("mock-a=0.5@0+1", 0.5, 0.0, 1.0, id="start_zero"),
        pytest.param("mock-a=0.5@0.0+1", 0.5, 0.0, 1.0, id="start_zero_decimal"),
        pytest.param("mock-a=0.5@1.5+2.25", 0.5, 1.5, 2.25, id="decimal_times"),
        pytest.param("mock-a=0@60+30", 0.0, 60.0, 30.0, id="rate_zero_with_a_window"),
        pytest.param("mock-a=0.5@10+0.001", 0.5, 10.0, 0.001, id="small_duration"),
        pytest.param("mock-a=0.5@86400+604800", 0.5, 86400.0, 604800.0, id="long_times"),
    ],
)
def test_each_valid_entry_gives_its_window(
    value: str, rate: float, start: float, duration: float | None
) -> None:
    assert _faults(value) == {"mock-a": _fault_window(rate, start, duration)}


def test_the_parsed_numbers_are_floats_whatever_was_written() -> None:
    window = _faults("mock-a=1@60+30")["mock-a"]
    assert [type(n) for n in (window.rate, window.start, window.duration)] == [float] * 3


def test_several_entries_give_a_window_each() -> None:
    assert _faults("mock-a=1.0@60+30,mock-b=0.5") == {
        "mock-a": _fault_window(1.0, 60.0, 30.0),
        "mock-b": _fault_window(0.5),
    }


def test_an_instance_name_may_use_every_character_the_config_allows() -> None:
    name = "Mock.a_b-1"
    text = (
        f'[providers."{name}"]\ntype = "mock"\n\n'
        f'[aliases]\n{ALIAS} = [{{ provider = "{name}", model = "m" }}]\n'
    )
    assert _faults(f"{name}=1", text) == {name: _fault_window(1.0)}


# LLMGATEWAY_FAULTS: what stops startup (D-064, FLOW.md "Configuration errors")


@pytest.mark.parametrize(
    "value",
    ["", " ", "   ", "\t", "\n", " \t\n "],
    ids=["empty", "space", "spaces", "tab", "newline", "mixed"],
)
def test_set_but_blank_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("mock-a", id="no_rate"),
        pytest.param("mock-a=", id="empty_rate"),
        pytest.param("=1", id="no_instance"),
        pytest.param("=", id="only_equals"),
        pytest.param("mock-a=1=1", id="two_equals"),
        pytest.param("mock-a:1", id="wrong_separator"),
        pytest.param("mock-a=.5", id="no_digit_before_the_point"),
        pytest.param("mock-a=1.", id="no_digit_after_the_point"),
        pytest.param("mock-a=-0.5", id="negative_rate"),
        pytest.param("mock-a=+0.5", id="signed_rate"),
        pytest.param("mock-a=1e0", id="exponent"),
        pytest.param("mock-a=1E0", id="capital_exponent"),
        pytest.param("mock-a=inf", id="inf"),
        pytest.param("mock-a=nan", id="nan"),
        pytest.param("mock-a=0x1", id="hex"),
        pytest.param("mock-a=1_0", id="underscore"),
        pytest.param("mock-a=50%", id="percent"),
        pytest.param("mock-a=1@", id="at_and_nothing"),
        pytest.param("mock-a=1@60", id="start_without_duration"),
        pytest.param("mock-a=1@60+", id="empty_duration"),
        pytest.param("mock-a=1@+30", id="empty_start"),
        pytest.param("mock-a=1+30", id="duration_without_start"),
        pytest.param("mock-a=1@60+30+5", id="two_durations"),
        pytest.param("mock-a=1@60@30", id="two_starts"),
        pytest.param("mock-a=1@-1+30", id="negative_start"),
        pytest.param("mock-a=1@60+-30", id="negative_duration"),
        pytest.param("mock-a=1@1e2+30", id="exponent_start"),
        pytest.param("mock-a=1@60+1e2", id="exponent_duration"),
        pytest.param("mock-a=1@.5+30", id="start_without_a_leading_digit"),
        pytest.param("mock-a=1@60+30.", id="duration_without_a_trailing_digit"),
        pytest.param("mock-a=1@inf+30", id="inf_start"),
        pytest.param("mock-a=1@60+inf", id="inf_duration"),
        pytest.param("mock-a = 1", id="spaces_around_the_equals"),
        pytest.param(" mock-a=1", id="leading_space"),
        pytest.param("mock-a=1 ", id="trailing_space"),
        pytest.param("mock-a= 1", id="space_before_the_rate"),
        pytest.param("mock-a=1 @60+30", id="space_before_the_at"),
        pytest.param("mock-a=1@ 60+30", id="space_after_the_at"),
        pytest.param("mock-a=1@60 +30", id="space_before_the_plus"),
        pytest.param("mock-a=1@60+ 30", id="space_after_the_plus"),
        pytest.param("mock-a=1, mock-b=1", id="space_after_a_comma"),
        pytest.param("mock-a=1 ,mock-b=1", id="space_before_a_comma"),
        pytest.param("mock-a=1\n", id="trailing_newline"),
        pytest.param("\nmock-a=1", id="leading_newline"),
        pytest.param("mock-a=1\t", id="trailing_tab"),
        pytest.param("mock-a=1,", id="trailing_comma"),
        pytest.param(",mock-a=1", id="leading_comma"),
        pytest.param("mock-a=1,,mock-b=1", id="empty_entry_in_the_middle"),
        pytest.param(",", id="only_a_comma"),
        pytest.param("mock-a=1;mock-b=1", id="wrong_entry_separator"),
        pytest.param("mock-a=0,5", id="decimal_comma"),
    ],
)
def test_an_entry_that_does_not_match_the_grammar_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("mock-a=1.01", id="just_above"),
        pytest.param("mock-a=1.0000001", id="barely_above"),
        pytest.param("mock-a=2", id="whole"),
        pytest.param("mock-a=100", id="a_percentage"),
        pytest.param("mock-a=1.5@60+30", id="with_a_window"),
        pytest.param(f"mock-a={OVERFLOWS}", id="overflows_to_infinity"),
    ],
)
def test_a_rate_above_1_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


@pytest.mark.parametrize(
    "value",
    ["mock-a=1@60+0", "mock-a=1@60+0.0", "mock-a=1@0+0", "mock-a=0@60+0.000"],
    ids=["zero", "zero_decimal", "zero_start_too", "long_zero"],
)
def test_a_duration_of_0_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


@pytest.mark.parametrize(
    "value",
    [
        "mock-a=1,mock-a=1",
        "mock-a=1,mock-a=0.5",
        "mock-a=1@1+1,mock-a=1@5+1",
        "mock-a=1,mock-b=1,mock-a=1",
    ],
    ids=["identical", "different_rates", "two_windows", "not_adjacent"],
)
def test_a_repeated_instance_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


@pytest.mark.parametrize(
    "value",
    ["nobody=1", "fast=1", "m1=1", "MOCK-A=1", "mock-a=1,nobody=1"],
    ids=["unknown", "an_alias", "a_model", "wrong_case", "second_entry"],
)
def test_an_unknown_instance_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


@pytest.mark.parametrize(
    "value",
    ["claude-a=1", "gemini-a=1", "claude-a=1.0@60+30", "claude-a=0", "mock-a=1,gemini-a=0.5"],
    ids=["claude", "gemini", "claude_window", "claude_rate_zero", "second_entry"],
)
def test_a_real_provider_instance_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


def test_a_config_with_no_mock_instance_cannot_start_with_the_variable_set() -> None:
    text = (
        '[providers.claude-a]\ntype = "claude"\n\n'
        f'[aliases]\n{ALIAS} = [{{ provider = "claude-a", model = "claude-haiku-5-5" }}]\n'
    )
    assert VARIABLE in str(_error("claude-a=1", text))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("mock-a=\u0661", id="arabic_indic_rate"),
        pytest.param("mock-a=\u0660.\u0665", id="arabic_indic_decimal"),
        pytest.param("mock-a=0.\u0665", id="non_ascii_after_the_point"),
        pytest.param("mock-a=\uff11", id="fullwidth_rate"),
        pytest.param("mock-a=\u0967", id="devanagari_rate"),
        pytest.param("mock-a=1@\u0666\u0660+30", id="non_ascii_start"),
        pytest.param("mock-a=1@60+\u0663\u0660", id="non_ascii_duration"),
    ],
)
def test_a_digit_that_is_not_ascii_stops(value: str) -> None:
    assert VARIABLE in str(_error(value))


NOT_A_FINITE_WINDOW = [
    pytest.param(f"mock-b=1@{OVERFLOWS}+30", id="start_overflows"),
    pytest.param(f"mock-b=1@60+{OVERFLOWS}", id="duration_overflows"),
    pytest.param(f"mock-b=1@60+{UNDERFLOWS}", id="duration_underflows_to_0"),
]


@pytest.mark.parametrize("value", NOT_A_FINITE_WINDOW)
def test_digits_no_float_can_hold_stop_with_the_same_clean_message(value: str) -> None:
    # A ConfigError, so the startup line, and not FaultWindow's ValueError as a bug.
    error = _error(value)
    assert VARIABLE in str(error)
    assert TIME_MARKER not in _printed(error)
    assert _numbers(str(_error(f"mock-a=1,{value}"))) - _numbers(str(error)) == {"2": 1}


# One bad value per rule, and where it sits, for the tests of the message below.
RULE_CASES = [
    pytest.param(MARKER, id="shape"),
    pytest.param(f"{INSTANCE_MARKER}={RATE_MARKER}", id="rate_above_1"),
    pytest.param(f"{INSTANCE_MARKER}=1@{TIME_MARKER}+0", id="duration_of_0"),
    pytest.param(f"{MARKER}=0.75319", id="unknown_instance"),
    pytest.param(f"{CLAUDE_MARKER}=0.75319", id="claude_instance"),
    pytest.param(f"{GEMINI_MARKER}=1@{TIME_MARKER}+{TIME_MARKER}", id="gemini_instance"),
]
HIDDEN = (
    MARKER,
    INSTANCE_MARKER,
    CLAUDE_MARKER,
    GEMINI_MARKER,
    RATE_MARKER,
    TIME_MARKER,
    "0.75319",
)


@pytest.mark.parametrize("value", RULE_CASES)
def test_a_startup_error_names_the_variable_and_echoes_no_value(value: str) -> None:
    error = _error(value, MARKED_CONFIG)
    assert VARIABLE in str(error)
    for hidden in HIDDEN:
        assert hidden not in str(error)


@pytest.mark.parametrize("value", RULE_CASES)
def test_a_startup_error_echoes_no_value_through_a_chained_exception(value: str) -> None:
    # What the message leaves out must not come back through a traceback, as with
    # `float("...")` raising inside the parser (D-044: console and CI logs).
    printed = _printed(_error(value, MARKED_CONFIG))
    for hidden in HIDDEN:
        assert hidden not in printed


def test_a_repeated_instance_error_echoes_no_value() -> None:
    value = f"{INSTANCE_MARKER}=0.75319,{INSTANCE_MARKER}={TIME_MARKER}.5"
    error = _error(value, MARKED_CONFIG)
    assert VARIABLE in str(error)
    for hidden in HIDDEN:
        assert hidden not in _printed(error)


def test_a_startup_error_shows_no_other_environment_value() -> None:
    environ = {VARIABLE: MARKER, "ANTHROPIC_API_KEY": ANTHROPIC_KEY, "GEMINI_API_KEY": GEMINI_KEY}
    with pytest.raises(ConfigError) as info:
        _parse_faults(environ, parse_config(MIXED_CONFIG))
    assert ANTHROPIC_KEY not in _printed(info.value)
    assert GEMINI_KEY not in _printed(info.value)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("not-an-entry", id="shape"),
        pytest.param("mock-b=2", id="rate_above_1"),
        pytest.param("mock-b=1@1+0", id="duration_of_0"),
        pytest.param("nobody=1", id="unknown_instance"),
        pytest.param("claude-a=1", id="real_instance"),
    ],
)
def test_a_startup_error_names_the_entrys_position_counting_from_1(bad: str) -> None:
    # Every entry before it is valid, so the position is the only number that changes.
    at_first = _numbers(str(_error(bad)))
    at_second = _numbers(str(_error(f"mock-a=1,{bad}")))
    assert at_first - at_second == {"1": 1}
    assert at_second - at_first == {"2": 1}


def test_a_startup_error_counts_past_the_second_entry() -> None:
    at_first = _numbers(str(_error("nobody=1")))
    at_third = _numbers(str(_error("mock-a=1,mock-b=1,nobody=1")))
    assert at_first - at_third == {"1": 1}
    assert at_third - at_first == {"3": 1}


def test_a_repeated_instance_error_names_a_position_that_follows_the_entry() -> None:
    # The repeat is the later entry, so it sits at 2, then at 3.
    at_second = _numbers(str(_error("mock-a=1,mock-a=1")))
    at_third = _numbers(str(_error("mock-b=1,mock-a=1,mock-a=1")))
    assert at_second - at_third == {"2": 1}
    assert at_third - at_second == {"3": 1}


def test_each_rule_has_its_own_message() -> None:
    messages = [
        str(_error(value))
        for value in (
            "not-an-entry",
            "mock-a=2",
            "mock-a=1@1+0",
            "nobody=1",
            "claude-a=1",
        )
    ]
    assert len(set(messages)) == len(messages)


def test_a_repeated_instance_has_its_own_message() -> None:
    # Position 2 here, so the others are built to fail at position 2 as well.
    repeated = str(_error("mock-a=1,mock-a=1"))
    for other in ("mock-a=1,not-an-entry", "mock-a=1,mock-b=2", "mock-a=1,nobody=1"):
        assert repeated != str(_error(other))


def test_set_but_blank_has_its_own_message() -> None:
    assert str(_error("")) != str(_error("not-an-entry"))


# build_router (D-064)


def test_build_router_takes_a_keyword_only_clock() -> None:
    parameters = inspect.signature(build_router).parameters
    assert parameters["clock"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["clock"].default is None


@pytest.mark.parametrize(
    "value",
    ["", "mock-a", "nobody=1", "claude-a=1", "mock-a=2", "mock-a=1@1+0", "mock-a=1,mock-a=1"],
    ids=["blank", "shape", "unknown", "real", "rate", "duration", "repeated"],
)
def test_build_router_stops_on_each_bad_value_before_any_adapter_is_built(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    config = parse_config(MIXED_CONFIG)
    opened = _record_http_clients(monkeypatch)
    with pytest.raises(ConfigError) as info:
        build_router(config, {**BOTH_KEYS, VARIABLE: value}, sleep=RecordingSleep())
    assert VARIABLE in str(info.value)
    assert opened == []


@pytest.mark.parametrize(
    ("type_", "model", "key"),
    [
        ("claude", "claude-haiku-5-5", "ANTHROPIC_API_KEY"),
        ("gemini", "gemini-3.5-flash-lite", "GEMINI_API_KEY"),
    ],
    ids=["claude", "gemini"],
)
@pytest.mark.parametrize(
    "value",
    ["", "not-an-entry", "nobody=1", "mock-a=2", "real-a=1"],
    ids=["blank", "shape", "unknown", "rate", "real_instance"],
)
def test_a_missing_key_is_reported_before_a_bad_window(
    monkeypatch: pytest.MonkeyPatch, type_: str, model: str, key: str, value: str
) -> None:
    text = (
        '[providers.mock-a]\ntype = "mock"\n\n'
        f'[providers.real-a]\ntype = "{type_}"\n\n'
        f'[aliases]\n{ALIAS} = [{{ provider = "mock-a", model = "m" }}, '
        f'{{ provider = "real-a", model = "{model}" }}]\n'
    )
    config = parse_config(text)
    opened = _record_http_clients(monkeypatch)
    with pytest.raises(ConfigError) as info:
        build_router(config, {VARIABLE: value})
    assert key in str(info.value)
    assert VARIABLE not in str(info.value)
    assert opened == []


@pytest.mark.asyncio
async def test_build_router_reads_the_variable_from_the_given_environ_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VARIABLE, "not valid at all")
    router = _build(_two_mocks(), None)
    await router.aclose()


@pytest.mark.asyncio
async def test_build_router_gives_the_window_to_the_named_instance_only() -> None:
    router = _build(_two_mocks(), "mock-primary=1.0")
    primary, secondary = router.routes(ALIAS)
    assert await _fails(primary.adapter) is True
    assert await _fails(secondary.adapter) is False


@pytest.mark.asyncio
async def test_build_router_passes_its_clock_to_the_mock_instances() -> None:
    clock = FakeClock()
    router = _build(_two_mocks(), "mock-primary=1.0@10+5", clock=clock)
    primary = router.select(ALIAS)
    assert await _fails(primary.adapter) is False
    clock.advance(10.0)
    assert await _fails(primary.adapter) is True
    clock.advance(5.0)
    assert await _fails(primary.adapter) is False


@pytest.mark.asyncio
async def test_build_router_passes_its_clock_to_the_router() -> None:
    # The breaker's cooldown runs on the router's clock (D-057), so moving only the
    # fake clock has to end it.
    clock = FakeClock()
    router = _build(
        _two_mocks(),
        "mock-primary=1.0@0+5",
        clock=clock,
        policy=ONE_ATTEMPT,
        breaker_policy=BreakerPolicy(window_size=1, failure_threshold=1, cooldown=30.0),
    )
    async with _client(router) as client:
        first = await client.post("/v1/chat", json=_chat_body())
        clock.advance(30.0)
        second = await client.post("/v1/chat", json=_chat_body())
    assert first.json()["provider_instance"] == "mock-secondary"
    assert second.json()["provider_instance"] == "mock-primary"


@pytest.mark.asyncio
async def test_each_instance_has_its_own_origin() -> None:
    clock = FakeClock()
    router = _build(_two_mocks(), "mock-primary=1.0@10+5,mock-secondary=1.0@10+5", clock=clock)
    primary, secondary = router.routes(ALIAS)
    assert await _fails(primary.adapter) is False
    clock.advance(10.0)
    # The secondary's first call starts its own clock, so it is at 0 here, not at 10.
    assert await _fails(secondary.adapter) is False
    assert await _fails(primary.adapter) is True


@pytest.mark.asyncio
async def test_entries_on_one_instance_share_its_window_and_origin() -> None:
    text = (
        '[providers.m]\ntype = "mock"\n\n[aliases]\n'
        f'{ALIAS} = [{{ provider = "m", model = "a" }}, {{ provider = "m", model = "b" }}]\n'
    )
    clock = FakeClock()
    router = _build(text, "m=1.0@10+5", clock=clock)
    one, two = router.routes(ALIAS)
    assert await _fails(one.adapter) is False
    clock.advance(10.0)
    assert await _fails(two.adapter) is True


@pytest.mark.asyncio
async def test_a_fault_uses_the_instances_own_failure_type_through_the_router() -> None:
    text = _two_mocks('failure_type = "auth"')
    router = _build(text, "mock-primary=1.0", policy=ONE_ATTEMPT)
    primary = router.select(ALIAS)
    with pytest.raises(ProviderAuthError) as info:
        await router.complete(primary, _request(model=ALIAS))
    assert info.value.reason == "injected"
    assert info.value.status is None


@pytest.mark.asyncio
async def test_a_config_rate_above_the_window_still_applies_inside_it() -> None:
    router = _build(_two_mocks("failure_rate = 1.0"), "mock-primary=0.0")
    assert await _fails(router.select(ALIAS).adapter) is True


@pytest.mark.asyncio
async def test_the_router_logs_an_injected_failure_like_any_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    router = _build(_two_mocks(), "mock-primary=1.0", policy=ONE_ATTEMPT)
    caplog.clear()
    async with _client(router) as client:
        response = await client.post("/v1/chat", json=_chat_body())
    request_id = response.headers["x-request-id"]
    lines = [r.getMessage() for r in caplog.records if r.name == "llmgateway.router"]
    assert lines == [
        f"attempt_failed request_id={request_id} alias={ALIAS!r} provider=mock-primary "
        "model='mock-a' attempt=1 status=- reason=injected error=ProviderUnavailable retry=no",
        f"fallback_taken request_id={request_id} alias={ALIAS!r} from=mock-primary "
        "from_model='mock-a' to=mock-secondary to_model='mock-b' reason=injected "
        "error=ProviderUnavailable",
    ]


# The startup line (D-064)


@pytest.mark.parametrize(
    ("value", "line"),
    [
        pytest.param(
            "mock-primary=1.0@60+30",
            "fault_injection_on provider=mock-primary rate=1.0 start=60.0 duration=30.0",
            id="measurement_8",
        ),
        pytest.param(
            "mock-primary=1@60+30",
            "fault_injection_on provider=mock-primary rate=1.0 start=60.0 duration=30.0",
            id="whole_numbers_are_written_as_floats",
        ),
        pytest.param(
            "mock-primary=0.50@1.5+2.25",
            "fault_injection_on provider=mock-primary rate=0.5 start=1.5 duration=2.25",
            id="fractions",
        ),
        pytest.param(
            "mock-primary=1@007+030",
            "fault_injection_on provider=mock-primary rate=1.0 start=7.0 duration=30.0",
            id="the_parsed_value_and_not_the_text",
        ),
        pytest.param(
            "mock-secondary=0.25",
            "fault_injection_on provider=mock-secondary rate=0.25 start=0.0 duration=-",
            id="no_end_writes_a_dash",
        ),
        pytest.param(
            "mock-primary=0",
            "fault_injection_on provider=mock-primary rate=0.0 start=0.0 duration=-",
            id="rate_zero_is_still_a_window",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_window_writes_its_exact_line_at_startup(
    caplog: pytest.LogCaptureFixture, value: str, line: str
) -> None:
    caplog.set_level(logging.DEBUG)
    _build(_two_mocks(), value)
    assert _fault_lines(caplog) == [line]


@pytest.mark.asyncio
async def test_each_instance_with_a_window_writes_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _build(_two_mocks(), "mock-primary=1.0@60+30,mock-secondary=0.5")
    assert sorted(_fault_lines(caplog)) == [
        "fault_injection_on provider=mock-primary rate=1.0 start=60.0 duration=30.0",
        "fault_injection_on provider=mock-secondary rate=0.5 start=0.0 duration=-",
    ]


@pytest.mark.asyncio
async def test_the_startup_line_goes_to_the_router_logger_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _build(_two_mocks(), "mock-primary=1.0")
    (record,) = [r for r in caplog.records if r.getMessage().startswith("fault_injection_on")]
    assert record.name == "llmgateway.router"
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_the_line_is_written_once_at_startup_and_not_per_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    router = _build(_two_mocks(), "mock-primary=1.0")
    async with _client(router) as client:
        await client.post("/v1/chat", json=_chat_body())
        await client.post("/v1/chat", json=_chat_body())
    assert len(_fault_lines(caplog)) == 1


# An unset variable changes nothing (D-064)


@pytest.mark.asyncio
async def test_an_unset_variable_writes_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _build(_two_mocks(), None)
    assert _fault_lines(caplog) == []
    assert [r for r in caplog.records if r.name == "llmgateway.router"] == []


@pytest.mark.asyncio
async def test_an_unset_variable_leaves_a_real_only_config_starting() -> None:
    text = (
        '[providers.claude-a]\ntype = "claude"\n\n'
        f'[aliases]\n{ALIAS} = [{{ provider = "claude-a", model = "claude-haiku-5-5" }}]\n'
    )
    router = _build(text, None, environ={"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
    await router.aclose()


@pytest.mark.asyncio
async def test_an_unset_variable_leaves_each_seeded_sequence_as_the_config_gives_it() -> None:
    text = _two_mocks("failure_rate = 0.5\nseed = 1", "failure_rate = 0.5\nseed = 2")
    router = _build(text, None)
    primary, secondary = router.routes(ALIAS)
    assert await _failures(primary.adapter, 40) == await _failures(
        _control(failure_rate=0.5, seed=1), 40
    )
    assert await _failures(secondary.adapter, 40) == await _failures(
        _control(failure_rate=0.5, seed=2), 40
    )


@pytest.mark.asyncio
async def test_an_instance_with_no_entry_in_the_variable_behaves_as_if_it_were_unset() -> None:
    text = _two_mocks("", "failure_rate = 0.5\nseed = 2")
    router = _build(text, "mock-primary=1.0")
    secondary = router.routes(ALIAS)[1]
    assert await _failures(secondary.adapter, 40) == await _failures(
        _control(failure_rate=0.5, seed=2), 40
    )


# Measurement 8's window, end to end (PLAN.md, D-064)

MEASUREMENT_8 = "mock-primary=1.0@60+30"


async def _served_by(client: httpx.AsyncClient, calls: int = 1) -> list[str]:
    responses = [await client.post("/v1/chat", json=_chat_body()) for _ in range(calls)]
    assert [r.status_code for r in responses] == [200] * calls
    return [r.json()["provider_instance"] for r in responses]


@pytest.mark.asyncio
async def test_measurement_8_the_window_opens_at_60_s_and_closes_at_90_s() -> None:
    # A breaker that never opens, so the window alone decides who serves.
    clock = FakeClock(0.0)
    router = _build(
        _two_mocks(),
        MEASUREMENT_8,
        clock=clock,
        breaker_policy=BreakerPolicy(window_size=2000, failure_threshold=2000),
    )
    primary, secondary = (route.adapter for route in router.routes(ALIAS))
    async with _client(router) as client:
        # The first request starts the window's clock.
        assert await _served_by(client) == ["mock-primary"]
        clock.now = _just_before(60.0)
        assert await _served_by(client) == ["mock-primary"]
        clock.now = 60.0
        assert await _served_by(client) == ["mock-secondary"]
        clock.now = 75.0
        assert await _served_by(client) == ["mock-secondary"]
        clock.now = _just_before(90.0)
        assert await _served_by(client) == ["mock-secondary"]
        clock.now = 90.0
        assert await _served_by(client) == ["mock-primary"]
    # Two healthy calls, three failed attempts for each of three requests, one healthy.
    assert primary.call_count == 2 + 9 + 1
    assert secondary.call_count == 3


@pytest.mark.asyncio
async def test_measurement_8_the_window_is_relative_to_the_first_request_not_to_startup() -> None:
    clock = FakeClock(0.0)
    router = _build(
        _two_mocks(),
        MEASUREMENT_8,
        clock=clock,
        breaker_policy=BreakerPolicy(window_size=2000, failure_threshold=2000),
    )
    clock.now = 500.0
    async with _client(router) as client:
        assert await _served_by(client) == ["mock-primary"]
        clock.now = 559.75
        assert await _served_by(client) == ["mock-primary"]
        clock.now = 560.0
        assert await _served_by(client) == ["mock-secondary"]


@pytest.mark.asyncio
async def test_measurement_8_with_the_default_breaker_the_outage_opens_it_and_it_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every request is served throughout. 15 failed attempts open the breaker at 60 s;
    # its 30 s cooldown ends at 90 s, when the window has closed, so the probe passes.
    caplog.set_level(logging.DEBUG)
    clock = FakeClock(0.0)
    router = _build(_two_mocks(), MEASUREMENT_8, clock=clock)
    primary, secondary = (route.adapter for route in router.routes(ALIAS))
    async with _client(router) as client:
        assert await _served_by(client) == ["mock-primary"]
        clock.now = 60.0
        assert await _served_by(client, 5) == ["mock-secondary"] * 5
        assert router.breaker("mock-primary").state == "open"
        assert primary.call_count == 1 + 15
        assert await _served_by(client, 5) == ["mock-secondary"] * 5
        assert primary.call_count == 1 + 15
        clock.now = 89.75
        assert await _served_by(client) == ["mock-secondary"]
        assert primary.call_count == 1 + 15
        clock.now = 90.0
        assert await _served_by(client) == ["mock-primary"]
    assert router.breaker("mock-primary").state == "closed"
    assert secondary.call_count == 11
    changes = [
        re.sub(r" ts=\S+", "", r.getMessage())
        for r in caplog.records
        if r.name == "llmgateway.breaker"
    ]
    assert changes == [
        "breaker_state_change provider=mock-primary from=closed to=open reason=threshold_hit",
        "breaker_state_change provider=mock-primary from=open to=half_open reason=cooldown_over",
        "breaker_state_change provider=mock-primary from=half_open to=closed reason=probe_passed",
    ]


@pytest.mark.asyncio
async def test_measurement_8_on_the_committed_config_keeps_every_request_served() -> None:
    # Inside the window mock-primary's rate is max(0.3, 1.0), so the secondary serves
    # everything; outside it the seeded 30% is retried or moved, and nothing fails.
    clock = FakeClock(0.0)
    extras: dict[str, Any] = {"clock": clock}
    config = load_config({"LLMGATEWAY_CONFIG": str(MOCK_FALLBACK_TOML)})
    router = build_router(
        config,
        {VARIABLE: MEASUREMENT_8},
        sleep=RecordingSleep(),
        breaker_policy=BreakerPolicy(window_size=2000, failure_threshold=2000),
        **extras,
    )
    body = _chat_body("mock-fallback")
    async with _client(router) as client:
        before = [await client.post("/v1/chat", json=body) for _ in range(6)]
        clock.now = 60.0
        during = [await client.post("/v1/chat", json=body) for _ in range(6)]
        clock.now = 90.0
        after = [await client.post("/v1/chat", json=body) for _ in range(6)]
    assert [r.status_code for r in before + during + after] == [200] * 18
    assert {r.json()["provider_instance"] for r in during} == {"mock-secondary"}
    assert "mock-primary" in {r.json()["provider_instance"] for r in before}
    assert "mock-primary" in {r.json()["provider_instance"] for r in after}


# The startup path (FLOW.md "Configuration errors", as in test_main.py)

STARTUP_VARS = (
    "LLMGATEWAY_PROVIDER",
    "LLMGATEWAY_CONFIG",
    "LLMGATEWAY_FAULTS",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)

# Posts one body per alias to main.app and prints [status, provider_instance] for each.
# asyncio.sleep is replaced before main is imported, because build_router reads it
# then, so the router's backoff makes no real wait.
PROBE = """
import asyncio, json, os

yield_once = asyncio.sleep

async def instant(seconds):
    await yield_once(0)

asyncio.sleep = instant

import httpx
import llmgateway.main as main

async def probe():
    out = []
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for alias in os.environ["PROBE_ALIASES"].split(","):
            body = {"model": alias, "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8}
            response = await client.post("/v1/chat", json=body)
            data = response.json()
            out.append([response.status_code, data.get("provider_instance")])
    print(json.dumps(out))

asyncio.run(probe())
"""


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STARTUP_VARS}
    env.update(overrides)
    return env


def _import_main(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import llmgateway.main"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


RouterFromEnv = Callable[[Mapping[str, str]], Any]


@pytest.fixture
def router_from_env(monkeypatch: pytest.MonkeyPatch) -> RouterFromEnv:
    # Importing llmgateway.main builds its app from os.environ.
    for name in STARTUP_VARS:
        monkeypatch.delenv(name, raising=False)
    module: ModuleType = importlib.import_module("llmgateway.main")
    fn: RouterFromEnv = module.router_from_env
    return fn


@pytest.mark.parametrize(
    ("value", "hidden"),
    [
        pytest.param("", (), id="blank"),
        pytest.param(MARKER, (MARKER,), id="shape"),
        pytest.param(f"{MARKER}=0.75319", (MARKER, "0.75319"), id="unknown_instance"),
        pytest.param(f"mock={RATE_MARKER}", (RATE_MARKER,), id="rate_above_1"),
        pytest.param(f"mock=1@{TIME_MARKER}+0", (TIME_MARKER,), id="duration_of_0"),
        pytest.param(f"mock=1@{OVERFLOWS}+30", (TIME_MARKER,), id="start_overflows"),
        pytest.param(f"mock=1@60+{UNDERFLOWS}", (TIME_MARKER,), id="duration_underflows"),
    ],
)
def test_startup_with_a_bad_value_fails_cleanly(value: str, hidden: tuple[str, ...]) -> None:
    result = _import_main(_env(LLMGATEWAY_FAULTS=value))
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith(PREFIX), result.stderr
    assert "Traceback" not in result.stderr
    assert VARIABLE in result.stderr
    for text in hidden:
        assert text not in result.stderr
        assert text not in result.stdout


def test_startup_with_a_real_instance_named_fails_cleanly_with_no_key_shown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gateway.toml"
    path.write_text(MARKED_CONFIG, encoding="utf-8")
    result = _import_main(
        _env(
            LLMGATEWAY_CONFIG=str(path),
            LLMGATEWAY_FAULTS=f"{CLAUDE_MARKER}=1",
            ANTHROPIC_API_KEY=ANTHROPIC_KEY,
            GEMINI_API_KEY=GEMINI_KEY,
        )
    )
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith(PREFIX), result.stderr
    assert "Traceback" not in result.stderr
    for hidden in (CLAUDE_MARKER, ANTHROPIC_KEY, GEMINI_KEY, str(path)):
        assert hidden not in result.stderr


def test_startup_with_the_measurement_8_value_starts_and_writes_the_line() -> None:
    result = _import_main(
        _env(LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML), LLMGATEWAY_FAULTS=MEASUREMENT_8)
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert PREFIX not in result.stderr
    line = "fault_injection_on provider=mock-primary rate=1.0 start=60.0 duration=30.0"
    assert line in result.stderr


def test_startup_without_the_variable_writes_no_line() -> None:
    result = _import_main(_env(LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML)))
    assert result.returncode == 0, result.stderr
    assert "fault_injection_on" not in result.stderr


def test_a_real_startup_with_mock_primary_down_is_served_by_mock_secondary() -> None:
    env = _env(
        LLMGATEWAY_CONFIG=str(MOCK_FALLBACK_TOML),
        LLMGATEWAY_FAULTS="mock-primary=1.0",
        PROBE_ALIASES="mock-fallback,mock-fallback,mock-fallback",
    )
    # PROBE is the fixed script above, not outside input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == [[200, "mock-secondary"]] * 3


@pytest.mark.asyncio
async def test_router_from_env_applies_the_variable_to_the_built_in_mock(
    router_from_env: RouterFromEnv,
) -> None:
    router = router_from_env({VARIABLE: "mock=1.0"})
    assert await _fails(router.select("mock").adapter) is True
    await router.aclose()


def test_router_from_env_stops_on_a_bad_value(router_from_env: RouterFromEnv) -> None:
    with pytest.raises(ConfigError) as info:
        router_from_env({VARIABLE: MARKER})
    assert VARIABLE in str(info.value)
    assert MARKER not in str(info.value)


# What the startup line and a fault never reveal (CLAUDE.md, D-064)

SECRETS = (ANTHROPIC_KEY, GEMINI_KEY, PROMPT_MARKER, SYSTEM_MARKER)


@pytest.fixture
def watched(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """Captures every level, with both keys in the environment as in production."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    return caplog


def _written(caplog: pytest.LogCaptureFixture) -> str:
    # caplog.text has each formatted line and any traceback. A record's fields hold what
    # the message does not show, such as `extra`, which a structured handler would write.
    return caplog.text + "".join(str(vars(record)) for record in caplog.records)


async def _injected_message() -> str:
    """The text of an injected failure, which is the adapter's message."""
    provider = MockProvider(failure_rate=1.0, sleep=RecordingSleep())
    with pytest.raises(ProviderError) as info:
        await provider.complete(_request())
    return info.value.message


@pytest.mark.asyncio
async def test_the_startup_line_leaks_no_key_from_a_config_with_real_providers(
    watched: pytest.LogCaptureFixture,
) -> None:
    router = _build(MIXED_CONFIG, "mock-a=1.0@60+30,mock-b=0.5", environ=BOTH_KEYS)
    await router.aclose()
    assert len(_fault_lines(watched)) == 2
    written = _written(watched)
    for secret in SECRETS:
        assert secret not in written


@pytest.mark.asyncio
async def test_an_injected_fault_leaks_neither_its_message_nor_the_prompt_nor_the_answer(
    watched: pytest.LogCaptureFixture,
) -> None:
    message = await _injected_message()
    router = _build(_two_mocks(), "mock-primary=1.0")
    async with _client(router) as client:
        response = await client.post("/v1/chat", json=_chat_body(system=SYSTEM_MARKER))
    assert response.status_code == 200
    answer = response.json()["content"]
    assert answer
    written = _written(watched)
    for secret in (*SECRETS, message, answer):
        assert secret not in written


@pytest.mark.asyncio
async def test_a_fault_on_every_entry_leaks_nothing_to_the_logs_or_the_client(
    watched: pytest.LogCaptureFixture,
) -> None:
    message = await _injected_message()
    router = _build(_two_mocks(), "mock-primary=1.0,mock-secondary=1.0")
    async with _client(router) as client:
        response = await client.post("/v1/chat", json=_chat_body(system=SYSTEM_MARKER))
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "The provider is unavailable.",
            "request_id": response.headers["x-request-id"],
        }
    }
    written = _written(watched)
    for secret in (*SECRETS, message):
        assert secret not in written


@pytest.mark.parametrize("real", ["claude", "gemini"])
@pytest.mark.asyncio
async def test_a_fault_on_a_mock_before_a_real_provider_leaks_nothing_from_the_real_one(
    watched: pytest.LogCaptureFixture, real: str
) -> None:
    # A real adapter on a mock transport serves after the mock's injected failure.
    provider_text = "provider-error-text-marker"
    model = "claude-haiku-5-5" if real == "claude" else "gemini-3.5-flash-lite"
    text = (
        '[providers.mock-a]\ntype = "mock"\n\n'
        f'[providers.real-a]\ntype = "{real}"\n\n'
        f'[aliases]\n{ALIAS} = [{{ provider = "mock-a", model = "m" }}, '
        f'{{ provider = "real-a", model = "{model}" }}]\n'
    )

    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": provider_text}})

    router = _build(
        text,
        "mock-a=1.0",
        environ=BOTH_KEYS,
        transport=httpx.MockTransport(answer),
        policy=ONE_ATTEMPT,
    )
    async with _client(router) as client:
        response = await client.post("/v1/chat", json=_chat_body(system=SYSTEM_MARKER))
    await router.aclose()
    assert response.status_code == 502
    written = _written(watched)
    for secret in (*SECRETS, provider_text):
        assert secret not in written
        assert secret not in response.text
    assert len(_fault_lines(watched)) == 1
