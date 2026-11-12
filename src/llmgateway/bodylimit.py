"""The request body size limit, enforced before the body is parsed.

Why it exists: a key holder could otherwise make the gateway buffer and parse a body of
any size (D-025). The limit must also hold for a chunked body, which has no
`Content-Length`, and for a header that understates the body (D-108).

Key design choices:

- Pure ASGI middleware, inside auth and outside the rate limiter, so an unknown caller
  cannot make the gateway read anything, and an oversized request costs no Redis call
  and no token (D-108).
- One `Content-Length` made only of digits and over the limit is refused at once, before
  any `receive`, so no body byte is read and an `Expect: 100-continue` client is never
  told to send it.
- Otherwise the middleware reads the body itself and counts bytes, refusing as soon as
  the count passes the limit. The limit is on bytes received, whatever a header says.
- A refusal is 413 `request_too_large` with `Connection: close`, so the server closes the
  connection instead of reading the unsent rest of the body as a next request.
- A body within the limit is handed on whole, as one `http.request`; after that, the
  app's `receive` is the server's own, so a stream's disconnect watcher works as before.
  Reading it all first is what lets the rate limiter run before parsing for chunked
  bodies too, and FastAPI reads the whole body to parse it anyway.
- A client that leaves while its body is read gets the 499 nobody reads, so the request
  line says what happened (D-062).
"""

from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llmgateway.errors import error_response

DEFAULT_MAX_BODY_BYTES: Final = 1_048_576
_PUBLIC_PATH: Final = "/healthz"
_TOO_LONG: Final = 1 << 64


def _declared_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    values = [value for name, value in headers if name == b"content-length"]
    # `bytes.isdigit` accepts ASCII digits only; anything else gets the counted read.
    if len(values) != 1 or not values[0].isdigit():
        return None
    digits = values[0].lstrip(b"0") or b"0"
    # Past 19 digits it is over any limit, and `int` refuses very long digit strings.
    return int(digits) if len(digits) <= 19 else _TOO_LONG


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == _PUBLIC_PATH:
            await self.app(scope, receive, send)
            return
        request_id = scope["state"]["request_id"]
        too_large = error_response(
            request_id,
            413,
            "request_too_large",
            "The request body is too large.",
            headers={"Connection": "close"},
        )
        declared = _declared_length(scope["headers"])
        if declared is not None and declared > self.max_bytes:
            await too_large(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                gone = error_response(
                    request_id, 499, "client_disconnected", "The client disconnected."
                )
                await gone(scope, receive, send)
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await too_large(scope, receive, send)
                return
            body += chunk
            if not message.get("more_body", False):
                break

        handed_on = False

        async def replay() -> Message:
            nonlocal handed_on
            if not handed_on:
                handed_on = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
