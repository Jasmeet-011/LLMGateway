"""Usage records: one per adapter attempt, queued on the response path, written later.

Why it exists: the gateway exists partly to control cost, so every call it makes to a
provider must leave a record of what it used and cost: retries, fallback attempts and
streams cut short included, not just the answer the client got (D-095). And recording
must never slow down or fail an answer (D-099).

Key design choices:

- The router reports each attempt as an `AttemptUsage` through a `Meter`, a plain
  callable it gets per request. The router knows the attempt (instance, model, number,
  outcome, counts); the meter built here adds what only the app knows: the request ID,
  the key's tenant and id, the alias, the time and the cost. So the router stays free of
  prices, principals and storage.
- `record` only builds the record and puts it on a bounded `asyncio.Queue` with
  `put_nowait`. That is all the response path pays. One writer task per recorder takes
  records off in batches and writes each batch with one call to the store, under a
  timeout. A full queue drops records rather than growing without bound during an
  outage (D-099).
- A failed write is logged and its records are lost; it is never retried, because a
  timeout after the commit would insert the batch twice. Drops are counted and logged
  once per batch, not once per record, so an outage cannot flood the log. Lines hold
  only counts, fixed reasons and class names, never a prompt, a key, the database URL or
  an exception message.
- A stream cut short also records the characters it delivered and an estimated output
  token count from them, in their own fields (D-101). They are estimates for an operator:
  never added to the provider's counts and never priced, so the cost stays a lower bound
  from what the provider reported.
- `aclose` drains the queue for a few seconds at shutdown, then gives up and says how
  many records were lost. Records still queued when the process dies are lost too, so
  spend summed from the table is a lower bound whenever a drop or failure was logged.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from llmgateway.auth import Principal
from llmgateway.prices import PriceTable
from llmgateway.schema import Usage

logger = logging.getLogger("llmgateway.usage")

Outcome = Literal["success", "failed", "abandoned"]


@dataclass(frozen=True)
class AttemptUsage:
    """What the router knows about one adapter attempt (D-095)."""

    provider_instance: str
    provider_type: str
    model: str
    stream: bool
    attempt: int
    outcome: Outcome
    reason: str | None
    provider_status: int | None
    partial: bool
    usage: Usage | None
    # Characters of delta text sent to the client: an int for every stream attempt that
    # did not reach its end, else None (D-101).
    delivered_chars: int | None


Meter = Callable[[AttemptUsage], None]


def estimate_output_tokens(provider_type: str, chars: int) -> int:
    """An estimate, never a provider count: characters at a documented rate (D-101).

    Claude 5.5's tokenizer gives about 30% more tokens than 3.5 characters each; Gemini and
    the mock about 4 characters each. Integer arithmetic, rounded up.
    """
    if provider_type == "claude":
        return -(-chars * 13 // 35)
    return -(-chars // 4)


@dataclass(frozen=True)
class UsageRecord:
    """One row of `usage_records` (D-095, D-099)."""

    recorded_at: datetime
    request_id: str
    tenant_id: str
    key_id: str
    alias: str
    provider_instance: str
    provider_type: str
    model: str
    stream: bool
    attempt: int
    outcome: Outcome
    reason: str | None
    provider_status: int | None
    partial: bool
    input_tokens: int | None
    output_tokens: int | None
    cost_nanousd: int | None
    # Estimates for a stream cut short, never billed or added to the counts (D-101).
    delivered_chars: int | None
    estimated_output_tokens: int | None


class UsageStoreUnavailable(Exception):
    """The store could not be reached; carries the driver exception's type name only."""


class UsageStore(Protocol):
    async def write(self, records: Sequence[UsageRecord]) -> None: ...

    async def aclose(self) -> None: ...


class UsageRecorder:
    def __init__(
        self,
        store: UsageStore,
        prices: PriceTable,
        *,
        max_queue: int = 10_000,
        batch_size: int = 100,
        write_timeout: float = 5.0,
        drain_timeout: float = 5.0,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._prices = prices
        self._queue: asyncio.Queue[UsageRecord] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._write_timeout = write_timeout
        self._drain_timeout = drain_timeout
        self._wall_clock = wall_clock
        self._writer: asyncio.Task[None] | None = None
        self._in_flight = 0
        self._dropped = 0
        self._closing = False
        self._closed = False

    def meter(self, *, request_id: str, principal: Principal, alias: str) -> Meter:
        """The meter for one request: it turns each attempt into a queued record."""

        def report(attempt: AttemptUsage) -> None:
            recorded_at = datetime.fromtimestamp(self._wall_clock(), UTC)
            usage = attempt.usage
            delivered = attempt.delivered_chars
            cost = (
                None
                if usage is None
                else self._prices.cost_nanousd(
                    attempt.provider_type, attempt.model, usage, recorded_at.date()
                )
            )
            self.record(
                UsageRecord(
                    recorded_at=recorded_at,
                    request_id=request_id,
                    tenant_id=principal.tenant_id,
                    key_id=principal.key_id,
                    alias=alias,
                    provider_instance=attempt.provider_instance,
                    provider_type=attempt.provider_type,
                    model=attempt.model,
                    stream=attempt.stream,
                    attempt=attempt.attempt,
                    outcome=attempt.outcome,
                    reason=attempt.reason,
                    provider_status=attempt.provider_status,
                    partial=attempt.partial,
                    input_tokens=None if usage is None else usage.input_tokens,
                    output_tokens=None if usage is None else usage.output_tokens,
                    cost_nanousd=cost,
                    delivered_chars=delivered,
                    estimated_output_tokens=(
                        None
                        if delivered is None
                        else estimate_output_tokens(attempt.provider_type, delivered)
                    ),
                )
            )

        return report

    def record(self, record: UsageRecord) -> None:
        """Queues a record; never raises and never waits (D-099)."""
        if self._closing:
            logger.warning("usage_dropped count=1 reason=closed")
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped += 1
            return
        if self._writer is None:
            self._writer = asyncio.get_running_loop().create_task(self._write_forever())

    async def _write_forever(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._in_flight = len(batch)
            await self._write(batch)
            self._in_flight = 0
            for _ in batch:
                self._queue.task_done()
            if self._dropped:
                count, self._dropped = self._dropped, 0
                logger.warning("usage_dropped count=%d reason=queue_full", count)

    async def _write(self, batch: list[UsageRecord]) -> None:
        try:
            async with asyncio.timeout(self._write_timeout):
                await self._store.write(batch)
        except TimeoutError:
            logger.warning(
                "usage_write_failed count=%d reason=timeout error=TimeoutError", len(batch)
            )
        except UsageStoreUnavailable:
            logger.warning(
                "usage_write_failed count=%d reason=store_error error=UsageStoreUnavailable",
                len(batch),
            )
        except Exception as exc:
            # By class name only: a message could hold anything the store saw.
            logger.error(
                "usage_write_failed count=%d reason=bug error=%s", len(batch), type(exc).__name__
            )

    async def aclose(self) -> None:
        """Drains for up to `drain_timeout`, then drops what is left and closes the store."""
        if self._closed:
            return
        self._closed = True
        self._closing = True
        try:
            writer = self._writer
            if writer is not None:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self._drain_timeout):
                        await self._queue.join()
                left = self._queue.qsize() + self._in_flight
                writer.cancel()
                await asyncio.wait({writer})
                if left:
                    logger.warning("usage_dropped count=%d reason=shutdown", left)
        finally:
            await self._store.aclose()
