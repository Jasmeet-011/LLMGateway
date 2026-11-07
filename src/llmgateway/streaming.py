"""The HTTP side of a stream: SSE frames, and noticing a client that has left.

Why it exists: once a stream's first event exists, the response is committed
(D-068), and what is left is to write events until one side stops. A client that
leaves must close the upstream at once, because output nobody reads is still billed
(D-072). That needs more than Starlette gives.

Key design choices:

- `EventStream` subclasses `StreamingResponse` and replaces `__call__`. Starlette's
  version cancels the body task on a disconnect but never closes the iterator, so a
  cancel that lands in `send`, where a slow client's task waits, leaves a generator
  suspended and the upstream open until garbage collection. Under ASGI spec 2.4 it
  does not listen at all, and uvicorn's `send` returns silently after a disconnect. So
  this class owns the watcher and the close, and does not depend on either path.
- One watcher task is the only reader of `receive`, from before the stream opens until
  the response ends. It blocks, so a disconnect is noticed at once, even while the
  gateway is blocked on the provider, which polling between events would miss. The
  router's D-062 checks read its state.
- Every path ends in the `finally`: the body generator and the `ServedStream` are both
  closed there, and that closes the adapter's generator and its connection.
- Each `send` runs under `stream_idle_timeout`, the first one included. A client that
  stays connected and stops reading bills the same as one that left, and it would hold
  a breaker probe for good (D-071), so it is dropped. `EventStream` then returns
  without ending the body, and the server closes the connection.
- A failure after the first event is an `error` event and a clean end (D-069). The
  code and message come from `errors.py`, so provider text cannot reach the client. A
  bug is not re-raised, because uvicorn would log its text with a traceback; its class
  name goes to the request line through `request.state`.
- asyncio and not anyio: anyio is only a transitive dependency here (D-072).
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Final

from fastapi import Request
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from llmgateway.errors import provider_error_status, request_id_of
from llmgateway.providers.base import ProviderError, ProviderStreamEnd
from llmgateway.router import ClientDisconnected, Route, Router, ServedStream
from llmgateway.schema import ChatRequest, StreamDelta, StreamEnd, StreamError

_HEADERS: Final = {"cache-control": "no-cache", "x-accel-buffering": "no"}


def _frame(event: StreamDelta | StreamEnd | StreamError) -> bytes:
    # JSON has no raw line breaks, so this is always one `data` line (D-073).
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


class _ClientStalled(Exception):
    """A `send` took longer than the idle limit: the client is not reading."""


async def _until_disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _stop(task: "asyncio.Future[Any]") -> None:
    task.cancel()
    await asyncio.wait({task})


async def open_event_stream(gateway: Router, body: ChatRequest, request: Request) -> "EventStream":
    """Opens the stream, or raises what the client should see (D-068, D-072).

    Nothing has been sent when this returns or raises. A client that leaves meanwhile
    cancels the attempt in flight, which closes the upstream, and raises
    `ClientDisconnected`.
    """
    request_id = request_id_of(request)
    gone: asyncio.Future[None] = asyncio.ensure_future(_until_disconnect(request.receive))

    async def is_disconnected() -> bool:
        return gone.done()

    opening = asyncio.ensure_future(
        gateway.open_stream(body, request_id=request_id, is_disconnected=is_disconnected)
    )
    try:
        racing: set[asyncio.Future[Any]] = {opening, gone}
        await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
        if not opening.done():
            await _stop(opening)
    except BaseException:
        await _stop(opening)
        await _stop(gone)
        raise
    if opening.cancelled():
        await _stop(gone)
        raise ClientDisconnected("client disconnected")
    try:
        served = opening.result()
    except BaseException:
        await _stop(gone)
        raise
    return EventStream(
        served,
        request=request,
        gone=gone,
        send_timeout=gateway.policy.stream_idle_timeout,
    )


class EventStream(StreamingResponse):
    def __init__(
        self,
        served: ServedStream,
        *,
        request: Request,
        gone: "asyncio.Future[None]",
        send_timeout: float,
    ) -> None:
        self._served = served
        self._request = request
        self._gone = gone
        self._send_timeout = send_timeout
        self._frames = self._write_events()
        super().__init__(self._frames, media_type="text/event-stream", headers=_HEADERS)

    @property
    def route(self) -> Route:
        return self._served.route

    def _end(self, end: ProviderStreamEnd) -> StreamEnd:
        route = self._served.route
        return StreamEnd(
            id=request_id_of(self._request),
            provider=route.adapter.name,
            provider_instance=route.provider,
            model=route.model,
            finish_reason=end.finish_reason,
            usage=end.usage,
            provider_response_id=end.provider_response_id,
        )

    def _error(self, code: str, message: str) -> StreamError:
        return StreamError(code=code, message=message, request_id=request_id_of(self._request))

    async def _write_events(self) -> AsyncGenerator[bytes, None]:
        failure: StreamError | None = None
        try:
            async for event in self._served:
                if isinstance(event, ProviderStreamEnd):
                    yield _frame(self._end(event))
                else:
                    yield _frame(event)
        except ProviderError as exc:
            _, code, message = provider_error_status(exc)
            failure = self._error(code, message)
        except Exception as exc:
            # By class name only: its text can hold request or provider content.
            self._request.state.stream_error = type(exc).__name__
            failure = self._error("internal_error", "Internal server error.")
        # Outside the except blocks, so the generator is not suspended inside one.
        if failure is not None:
            yield _frame(failure)

    def _bounded(self, send: Send) -> Callable[[Message], Awaitable[None]]:
        async def bounded_send(message: Message) -> None:
            limit = asyncio.timeout(self._send_timeout)
            try:
                async with limit:
                    await send(message)
            except TimeoutError:
                if not limit.expired():
                    raise
                failure = _ClientStalled()
            else:
                return
            # Outside the except block, so nothing is chained onto it.
            raise failure

        return bounded_send

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        pump = asyncio.ensure_future(self.stream_response(self._bounded(send)))
        reason = "client_gone"
        try:
            await asyncio.wait({pump, self._gone}, return_when=asyncio.FIRST_COMPLETED)
            if not pump.done():
                await _stop(pump)
            failure = None if pump.cancelled() else pump.exception()
            if isinstance(failure, _ClientStalled):
                reason = "client_stalled"
            elif failure is not None and not isinstance(failure, OSError):
                raise failure
        finally:
            await _stop(pump)
            await _stop(self._gone)
            await self._frames.aclose()
            await self._served.aclose(reason)
