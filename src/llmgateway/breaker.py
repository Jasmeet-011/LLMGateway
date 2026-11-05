"""Circuit breaker: stops calling a provider that keeps failing, then tries again.

Why it exists: retries help with a blip, but during an outage they multiply load on
the provider and make every client wait for a call that was never going to work. A
breaker turns that into a fast refusal, and M11 uses the refusal to move straight
to the next entry in the alias list.

Key design choices:

- One breaker per config instance, held in process memory (D-055). It does no I/O,
  and its methods never await, so on one event loop each runs whole with no lock.
- It counts every adapter call, retries included (D-056). `counts_as_failure` is the
  one place that says what counts. Timeouts, unavailable and auth errors do; a rate
  limit or a bad request does not, because neither says the provider is down, and
  one client's bad requests must not close a provider for everyone.
- The trip rule is a rate over a count window, 15 failures in the last 20 outcomes,
  not a run of consecutive failures (D-057). At measurement 4's 30% failure rate, 5
  in a row happens by chance every few seconds under load, and one lucky success
  would reset a run of consecutive failures during a brownout.
- Half-open is entered lazily by the first `acquire` after the cooldown, so there is
  no timer task to manage, and it lets exactly one probe through at a time.
- `acquire` returns a `Permit` carrying the current generation, and every state
  change starts a new one. A result is counted only under the generation it was
  allowed in, so a slow call from before an outage cannot close a breaker, restart
  its cooldown, or stand in for the probe (D-057).
- Every state change writes one fixed line on its own logger (D-059). Measurement 8
  reads these, and Prometheus is stretch. Elapsed time uses the injected monotonic
  `clock`; the line's `ts` uses the injected `wall_clock`, so it can be lined up
  with a load test's output.
"""

import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

BreakerState = Literal["closed", "open", "half_open"]
Clock = Callable[[], float]

logger = logging.getLogger("llmgateway.breaker")


def counts_as_failure(error: ProviderError) -> bool:
    """Whether this error says the provider cannot serve requests (D-056)."""
    return isinstance(error, ProviderTimeout | ProviderUnavailable | ProviderAuthError)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class BreakerPolicy:
    """When a breaker opens and how long it stays open (D-057); seconds."""

    window_size: int = 20
    failure_threshold: int = 15
    cooldown: float = 30.0

    def __post_init__(self) -> None:
        if not _is_int(self.window_size) or self.window_size < 1:
            raise ValueError("window_size must be an int of at least 1")
        if (
            not _is_int(self.failure_threshold)
            or self.failure_threshold < 1
            or self.failure_threshold > self.window_size
        ):
            raise ValueError("failure_threshold must be an int from 1 to window_size")
        cooldown = self.cooldown
        if (
            not isinstance(cooldown, int | float)
            or isinstance(cooldown, bool)
            or not math.isfinite(cooldown)
            or cooldown <= 0
        ):
            raise ValueError("cooldown must be a finite number > 0")


@dataclass(frozen=True)
class Permit:
    """Leave to make one call, valid for the generation it was given in."""

    generation: int


def _timestamp(seconds: float) -> str:
    # Whole milliseconds first, so the seconds and the fraction cannot disagree.
    millis = math.floor(seconds * 1000)
    moment = datetime.fromtimestamp(millis // 1000, UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{millis % 1000:03d}Z"


class CircuitBreaker:
    def __init__(
        self,
        provider: str,
        policy: BreakerPolicy,
        *,
        clock: Clock = time.monotonic,
        wall_clock: Clock = time.time,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._clock = clock
        self._wall_clock = wall_clock
        self._state: BreakerState = "closed"
        self._generation = 0
        # True for a failure, False for a success; neutral outcomes never enter.
        self._window: deque[bool] = deque(maxlen=policy.window_size)
        self._reopens_at = 0.0
        self._probe_out = False

    @property
    def state(self) -> BreakerState:
        return self._state

    def acquire(self) -> Permit | None:
        """Leave to make one call, or None when the breaker refuses it."""
        if self._state == "open":
            if self._clock() < self._reopens_at:
                return None
            self._move("half_open", "cooldown_over")
        if self._state == "half_open":
            if self._probe_out:
                return None
            self._probe_out = True
        return Permit(self._generation)

    def retry_after(self) -> float | None:
        """Seconds of cooldown left while open, else None."""
        if self._state != "open":
            return None
        return max(0.0, self._reopens_at - self._clock())

    def record_success(self, permit: Permit) -> None:
        if not self._is_current(permit):
            return
        if self._state == "half_open":
            self._probe_out = False
            self._window.clear()
            self._move("closed", "probe_passed")
        else:
            self._window.append(False)

    def record_failure(self, permit: Permit) -> None:
        if not self._is_current(permit):
            return
        if self._state == "half_open":
            self._open("probe_failed")
            return
        self._window.append(True)
        if sum(self._window) >= self._policy.failure_threshold:
            self._open("threshold_hit")

    def record_neutral(self, permit: Permit) -> None:
        # Says nothing about the provider, but a probe must give its slot back, or
        # the breaker would stay half-open and refuse every request for good.
        if self._is_current(permit) and self._state == "half_open":
            self._probe_out = False

    def _is_current(self, permit: Permit) -> bool:
        return permit.generation == self._generation

    def _open(self, reason: str) -> None:
        self._probe_out = False
        self._window.clear()
        self._reopens_at = self._clock() + self._policy.cooldown
        self._move("open", reason)

    def _move(self, to: BreakerState, reason: str) -> None:
        # Fixed fields only: the instance name and fixed codes, nothing from a request.
        logger.warning(
            "breaker_state_change ts=%s provider=%s from=%s to=%s reason=%s",
            _timestamp(self._wall_clock()),
            self._provider,
            self._state,
            to,
            reason,
        )
        self._state = to
        self._generation += 1
