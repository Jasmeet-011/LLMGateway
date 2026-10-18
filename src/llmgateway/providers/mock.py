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
- `sleep` is injectable so tests run instantly and can check the delay.
"""

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from typing import Final, Literal, get_args

from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from llmgateway.schema import ChatRequest, Provider, Usage

FailureType = Literal["timeout", "rate_limited", "unavailable", "bad_request", "auth"]
Sleep = Callable[[float], Awaitable[None]]

_FAILURE_TYPES: Final = frozenset(get_args(FailureType))


def _injected_error(failure_type: FailureType) -> ProviderError:
    message = f"mock provider: injected {failure_type} failure"
    match failure_type:
        case "timeout":
            return ProviderTimeout(message)
        case "rate_limited":
            return ProviderRateLimited(message)
        case "unavailable":
            return ProviderUnavailable(message)
        case "bad_request":
            return ProviderBadRequest(message)
        case "auth":
            return ProviderAuthError(message)


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
        # Failure injection only, not security: a seeded PRNG is the point.
        self._rng = random.Random(seed)  # noqa: S311
        self.call_count = 0

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self.call_count += 1
        # Captured before awaiting, so concurrent calls keep distinct IDs.
        call_number = self.call_count
        await self._sleep(self._latency_ms / 1000)
        if self._rng.random() < self._failure_rate:
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
