"""One rule for calling Redis: a time budget, a circuit breaker, and pool pre-warming.

Why it exists: the rate limiter (D-106) and, from M17, the response cache (D-114) call
Redis on a request's path, and both must tell "Redis is down" apart from "this gateway is
busy".
Holding that rule once keeps the two from drifting apart.

Key design choices:

- Each user holds its own `RedisGuard` with its own `CircuitBreaker` (breaker.py), so a
  cache problem can never open the limiter's breaker and switch the rate limit off.
- `call` runs one Redis operation under `asyncio.timeout(budget)` and classifies the
  outcome: a timeout (the asyncio one, or redis-py's own socket or connect timeout,
  `redis.exceptions.TimeoutError`, which is a `RedisError` and so is caught first) is
  `GuardTimeout`; any other `RedisError` or an `OSError` is `GuardStoreError`; a reply the
  caller rejects is `GuardBadReply`. The caller decides what each means for its request.
- The breaker's outcomes: a store error or a rejected reply is a failure; an accepted reply
  is a success; a cancellation, or any other exception from the call or from `accept`,
  is neutral and propagates unchanged, so a probe always gives its slot back. A timeout is a
  failure only if no call of this guard succeeded between the moment it started and the
  moment it timed out. If Redis answered other calls during the wait, the wait was the
  gateway's own (a busy event loop, a full pool), and counting it would let an overloaded
  gateway open the breaker with Redis healthy (D-106).
- While the breaker refuses, `call` raises `GuardOpen` at once, without touching Redis.
- `warm_pool` opens a pool's connections at startup, all at once and holding each until
  all are open, so a first burst does not spend its budget connecting. It never raises.
- Errors carry only a class name: a redis-py message can contain the URL.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from llmgateway.breaker import CircuitBreaker

Clock = Callable[[], float]


class GuardOpen(Exception):
    """The breaker refused the call; Redis was not asked."""


class GuardTimeout(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(f"redis timeout ({error_type})")
        self.error_type = error_type


class GuardStoreError(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(f"redis store error ({error_type})")
        self.error_type = error_type


class GuardBadReply(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(f"redis bad reply ({error_type})")
        self.error_type = error_type


def _accept_all(reply: object) -> bool:
    return True


class RedisGuard:
    def __init__(
        self, breaker: CircuitBreaker, *, timeout: float, clock: Clock = time.monotonic
    ) -> None:
        self._breaker = breaker
        self._timeout = timeout
        self._clock = clock
        self._last_success = -math.inf

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def call(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        accept: Callable[[object], bool] = _accept_all,
    ) -> Any:
        permit = self._breaker.acquire()
        if permit is None:
            raise GuardOpen()
        started = self._clock()
        try:
            async with asyncio.timeout(self._timeout):
                reply = await operation()
        except (TimeoutError, RedisTimeoutError) as exc:
            if self._last_success >= started:
                self._breaker.record_neutral(permit)
            else:
                self._breaker.record_failure(permit)
            raise GuardTimeout(type(exc).__name__) from None
        except (RedisError, OSError) as exc:
            self._breaker.record_failure(permit)
            raise GuardStoreError(type(exc).__name__) from None
        except BaseException:
            self._breaker.record_neutral(permit)
            raise
        try:
            accepted = accept(reply)
        except BaseException:
            # A bug in the check: neutral, so a probe still gives its slot back (D-106).
            self._breaker.record_neutral(permit)
            raise
        if not accepted:
            self._breaker.record_failure(permit)
            raise GuardBadReply(type(reply).__name__)
        self._last_success = self._clock()
        self._breaker.record_success(permit)
        return reply


async def warm_pool(
    client: Any, *, connections: int, budget: float, script: str | None
) -> tuple[int, str | None]:
    """Loads `script` if given and opens `connections` pool connections within `budget` s.

    Returns how many connections opened and the class name of the first error, if any.
    """
    pool = client.connection_pool
    held: list[Any] = []

    async def open_one() -> None:
        held.append(await pool.get_connection())

    error: str | None = None
    try:
        async with asyncio.timeout(budget):
            if script is not None:
                await client.script_load(script)
            results = await asyncio.gather(
                *(open_one() for _ in range(connections)), return_exceptions=True
            )
        for result in results:
            if isinstance(result, BaseException):
                error = type(result).__name__
                break
    except (TimeoutError, RedisError, OSError) as exc:
        error = type(exc).__name__
    finally:
        for connection in held:
            await pool.release(connection)
    return len(held), error
