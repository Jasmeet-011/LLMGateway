"""Server-sent events decoder: bytes in, one `SseEvent` per frame out.

Why it exists: Claude and Gemini both stream over SSE, and the parser is subtle
enough that there should be exactly one copy, tested once (D-076).

Key design choices:

- It splits lines on bytes, not with httpx's `aiter_lines`. httpx's `LineDecoder` has
  the semantics of `str.splitlines()`, so it also breaks on U+2028, U+2029 and U+0085.
  Those are legal unescaped inside a JSON string, so a model that writes one would cut
  a frame in half. SSE ends lines only at `\\n`, `\\r\\n` and `\\r`.
- A line is decoded only once it is complete, so a multi-byte character split across
  two chunks is whole by then. Invalid UTF-8 raises `ValueError`, which the adapters
  report as a malformed body.
- A `\\r` ends its line at once, and a `\\n` that follows it in the next chunk is
  dropped, so a frame is delivered as soon as it is complete and never waits on the
  next chunk.
- Data left over when the body ends is dropped, as WHATWG says, so a frame cut off
  partway looks like a truncated stream and not like a short event.
- Only `data` and `event` are read. Comments and other fields are skipped, and the
  caller decides what the data means.
"""

import re
from collections.abc import AsyncGenerator, AsyncIterable, Iterator
from dataclasses import dataclass
from typing import Final

_LINE_END: Final = re.compile(rb"\r\n|\n|\r")
_BYTE_ORDER_MARK: Final = b"\xef\xbb\xbf"
_LINE_FEED: Final = 0x0A


@dataclass(frozen=True)
class SseEvent:
    """One dispatched frame: its `event` name, if it had one, and its joined `data`."""

    event: str | None
    data: str


class _Lines:
    """Turns chunks of bytes into complete decoded lines."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._drop_line_feed = False
        self._first = True

    def feed(self, chunk: bytes) -> Iterator[str]:
        if self._drop_line_feed and chunk:
            self._drop_line_feed = False
            if chunk[0] == _LINE_FEED:
                chunk = chunk[1:]
        self._buffer += chunk
        start = 0
        for match in _LINE_END.finditer(self._buffer):
            yield self._decode(bytes(self._buffer[start : match.start()]))
            start = match.end()
            # At the end of the buffer this `\r` may be the first half of a `\r\n`.
            if match.group() == b"\r" and start == len(self._buffer):
                self._drop_line_feed = True
        del self._buffer[:start]

    def _decode(self, line: bytes) -> str:
        if self._first:
            self._first = False
            line = line.removeprefix(_BYTE_ORDER_MARK)
        return line.decode("utf-8")


async def iter_sse(chunks: AsyncIterable[bytes]) -> AsyncGenerator[SseEvent, None]:
    """The frames of an SSE body; raises `ValueError` on a line that is not UTF-8."""
    lines = _Lines()
    name: str | None = None
    data: list[str] = []
    try:
        async for chunk in chunks:
            for line in lines.feed(chunk):
                if not line:
                    if data:
                        yield SseEvent(name, "\n".join(data))
                    name = None
                    data = []
                    continue
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "data":
                    data.append(value)
                elif field == "event":
                    name = value or None
    finally:
        # An async generator left suspended is only closed by the garbage collector.
        close = getattr(chunks, "aclose", None)
        if close is not None:
            await close()
