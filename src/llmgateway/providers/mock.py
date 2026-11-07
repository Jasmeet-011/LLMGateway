"""Mock provider: the default provider for tests and load tests (D-001, D-016).

Why it exists: real providers cost money, are rate-limited, are not deterministic,
and cannot be made to fail on demand. Retries, fallback, the cache and coalescing
need a provider that does all of that predictably.

Key design choices:

- Deterministic content: the reply is "mock reply to: <last user message>", so the
  same request always gets the same answer.
- Fake token counts: input and output tokens are whitespace word counts (input over
  `system` plus every message, output over the reply). This is not a tokenizer and
  does not match any provider's counts, so costs from mock runs are not real costs.
- If the reply has more words than `max_tokens`, it is cut to `max_tokens` words
  (joined by single spaces) with finish reason `max_tokens`.
- Stop sequences are ignored.
- Failures are injected with a seeded `random.Random` owned by the instance, so a
  seed reproduces the same success/failure sequence. One roll per call, in call
  order; under concurrency the sequence follows the order calls reach the roll.
- Failed calls wait the configured latency too, because real failures take time.
  Injected errors carry reason `injected` and no status (D-054).
- `sleep` is injectable so tests run instantly and can check the delay.
- `stream` is `complete` cut into words (D-077): one call, one roll, the same answer,
  and any failure comes before the first delta, so it falls back. A failure after
  the first event is not injectable here; tests use a fake adapter for that.
- A `FaultWindow` raises the failure rate for a stretch of time, counted from the
  instance's first call, because the configured rate is fixed for the process and
  measurement 8 needs an outage mid-run (D-064). It changes the rate and not the
  roll, so the seeded sequence does not shift.
"""

import asyncio
import math
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, get_args

from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderStreamEnd,
    ProviderStreamEvent,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.schema import ChatRequest, Provider, StreamDelta, Usage

FailureType = Literal["timeout", "rate_limited", "unavailable", "bad_request", "auth"]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

_FAILURE_TYPES: Final = frozenset(get_args(FailureType))
# Each word with the space after it, so the pieces join back to exactly the reply.
_PIECE: Final = re.compile(r"\S+\s*")


def _injected_error(failure_type: FailureType) -> ProviderError:
    message = f"mock provider: injected {failure_type} failure"
    # No HTTP status: nothing went over the network (D-054).
    match failure_type:
        case "timeout":
            return ProviderTimeout(message, reason="injected")
        case "rate_limited":
            return ProviderRateLimited(message, reason="injected")
        case "unavailable":
            return ProviderUnavailable(message, reason="injected")
        case "bad_request":
            return ProviderBadRequest(message, reason="injected")
        case "auth":
            return ProviderAuthError(message, reason="injected")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass(frozen=True)
class FaultWindow:
    """A failure rate for a stretch of time after the first call (D-064); seconds."""

    rate: float
    start: float = 0.0
    # None means no end.
    duration: float | None = None

    def __post_init__(self) -> None:
        # Written this way so NaN is rejected too.
        if not _is_number(self.rate) or not 0.0 <= self.rate <= 1.0:
            raise ValueError("rate must be a number between 0 and 1")
        if not _is_number(self.start) or not math.isfinite(self.start) or self.start < 0:
            raise ValueError("start must be a finite number >= 0")
        duration = self.duration
        if duration is not None and (
            not _is_number(duration) or not math.isfinite(duration) or duration <= 0
        ):
            raise ValueError("duration must be None or a finite number > 0")

    def is_active(self, elapsed: float) -> bool:
        if elapsed < self.start:
            return False
        return self.duration is None or elapsed < self.start + self.duration


def _word_count(text: str) -> int:
    return len(text.split())


class MockProvider:
    name: Final[Provider] = "mock"

    def __init__(
        self,
        *,
        latency_ms: float = 0.0,
        failure_rate: float = 0.0,
        failure_type: FailureType = "unavailable",
        seed: int | None = None,
        sleep: Sleep = asyncio.sleep,
        fault: FaultWindow | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int | float):
            raise ValueError("latency_ms must be a number")
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("latency_ms must be finite and >= 0")
        if isinstance(failure_rate, bool) or not isinstance(failure_rate, int | float):
            raise ValueError("failure_rate must be a number")
        # Written this way so NaN is rejected too.
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError("failure_rate must be between 0 and 1")
        if failure_type not in _FAILURE_TYPES:
            raise ValueError(f"failure_type must be one of {sorted(_FAILURE_TYPES)}")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("seed must be an int or None")

        self._latency_ms = float(latency_ms)
        self._failure_rate = float(failure_rate)
        self._failure_type: FailureType = failure_type
        self._sleep = sleep
        self._fault = fault
        self._clock = clock
        self._fault_origin: float | None = None
        # Failure injection only, not security: a seeded PRNG is the point.
        self._rng = random.Random(seed)  # noqa: S311
        self.call_count = 0

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.call_count += 1
        # Captured before awaiting, so concurrent calls keep distinct IDs.
        call_number = self.call_count
        if self._fault is not None and self._fault_origin is None:
            self._fault_origin = self._clock()
        await self._sleep(self._latency_ms / 1000)
        if self._rng.random() < self._current_rate():
            raise _injected_error(self._failure_type)

        last_user = next(m for m in reversed(request.messages) if m.role == "user")
        reply = f"mock reply to: {last_user.content}"
        finish_reason: Literal["stop", "max_tokens"] = "stop"
        words = reply.split()
        if len(words) > request.max_tokens:
            reply = " ".join(words[: request.max_tokens])
            finish_reason = "max_tokens"

        prompt_parts = [request.system or "", *(m.content for m in request.messages)]
        usage = Usage(
            input_tokens=sum(_word_count(part) for part in prompt_parts),
            output_tokens=_word_count(reply),
        )
        return ProviderResult(
            content=reply,
            finish_reason=finish_reason,
            usage=usage,
            provider_response_id=f"mock-{call_number}",
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ProviderStreamEvent]:
        result = await self.complete(request)
        for piece in _PIECE.findall(result.content):
            # A real await between deltas, so a client can leave partway.
            await self._sleep(0)
            yield StreamDelta(text=piece)
        yield ProviderStreamEnd(
            finish_reason=result.finish_reason,
            usage=result.usage,
            provider_response_id=result.provider_response_id,
        )

    def _current_rate(self) -> float:
        fault = self._fault
        if fault is None or self._fault_origin is None:
            return self._failure_rate
        if fault.is_active(self._clock() - self._fault_origin):
            return max(self._failure_rate, fault.rate)
        return self._failure_rate
