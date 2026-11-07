"""Tests for the byte-level SSE decoder (M12, D-076).

One decoder under both adapters, so every WHATWG rule the gateway needs is pinned here,
and against bytes, because the reason it exists is what a line decoder gets wrong:
httpx's `aiter_lines` splits on U+2028, U+0085 and other characters that are legal inside
a JSON string, and a model that writes one would turn a good answer into a
`malformed_body`.

`llmgateway.providers.sse` is resolved at call time, so this file always collects and
each test fails on its own until it exists. Nothing here touches a network.
"""

import dataclasses
import importlib
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

import pytest


def _module() -> Any:
    return importlib.import_module("llmgateway.providers.sse")


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _events(source: AsyncIterable[bytes]) -> list[tuple[str | None, str]]:
    return [(event.event, event.data) async for event in _module().iter_sse(source)]


async def _decode(*parts: bytes) -> list[tuple[str | None, str]]:
    return await _events(_chunks(*parts))


# The two public names


def test_sse_event_is_a_frozen_dataclass_of_event_and_data() -> None:
    event = _module().SseEvent(event="ping", data="{}")
    assert [f.name for f in dataclasses.fields(event)] == ["event", "data"]
    assert (event.event, event.data) == ("ping", "{}")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.data = "other"


def test_sse_event_name_may_be_none() -> None:
    assert _module().SseEvent(event=None, data="x").event is None


@pytest.mark.asyncio
async def test_iter_sse_yields_sse_events() -> None:
    (event,) = [e async for e in _module().iter_sse(_chunks(b"event: a\ndata: b\n\n"))]
    assert event == _module().SseEvent(event="a", data="b")


# Frames


@pytest.mark.asyncio
async def test_one_frame_with_a_name_and_data() -> None:
    assert await _decode(b"event: message_stop\ndata: {}\n\n") == [("message_stop", "{}")]


@pytest.mark.asyncio
async def test_a_frame_without_an_event_field_has_no_name() -> None:
    assert await _decode(b"data: {}\n\n") == [(None, "{}")]


@pytest.mark.asyncio
async def test_frames_come_out_in_order() -> None:
    body = b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\ndata: 3\n\n"
    assert await _decode(body) == [("a", "1"), ("b", "2"), (None, "3")]


@pytest.mark.asyncio
async def test_a_name_does_not_outlive_its_frame() -> None:
    assert await _decode(b"event: a\ndata: 1\n\ndata: 2\n\n") == [("a", "1"), (None, "2")]


@pytest.mark.asyncio
async def test_a_name_with_no_data_is_forgotten_at_the_blank_line() -> None:
    assert await _decode(b"event: a\n\ndata: 2\n\n") == [(None, "2")]


@pytest.mark.asyncio
async def test_the_last_event_field_of_a_frame_wins() -> None:
    assert await _decode(b"event: a\nevent: b\ndata: 1\n\n") == [("b", "1")]


@pytest.mark.asyncio
async def test_field_order_in_a_frame_does_not_matter() -> None:
    assert await _decode(b"data: 1\nevent: a\n\n") == [("a", "1")]


@pytest.mark.asyncio
async def test_data_lines_join_with_a_line_feed() -> None:
    assert await _decode(b"data: one\ndata: two\ndata: three\n\n") == [(None, "one\ntwo\nthree")]


@pytest.mark.parametrize(
    ("line", "data"),
    [
        pytest.param(b"data: x", "x", id="one_space_removed"),
        pytest.param(b"data:x", "x", id="no_space"),
        pytest.param(b"data:  x", " x", id="only_one_space_removed"),
        pytest.param(b"data: x ", "x ", id="trailing_space_kept"),
        pytest.param(b"data:\tx", "\tx", id="a_tab_is_not_a_space"),
    ],
)
@pytest.mark.asyncio
async def test_only_one_space_after_the_colon_is_removed(line: bytes, data: str) -> None:
    assert await _decode(line + b"\n\n") == [(None, data)]


@pytest.mark.asyncio
async def test_one_space_after_the_colon_is_removed_from_a_name_too() -> None:
    assert await _decode(b"event:a\ndata: 1\n\nevent:  b\ndata: 2\n\n") == [
        ("a", "1"),
        (" b", "2"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"id: 7", id="id"),
        pytest.param(b"retry: 100", id="retry"),
        pytest.param(b"unknown: value", id="unknown"),
        pytest.param(b"dataful: value", id="a_longer_name"),
        pytest.param(b"Data: value", id="field_names_are_case_sensitive"),
        pytest.param(b"EVENT: value", id="event_is_case_sensitive"),
        pytest.param(b"nocolon", id="a_field_with_no_colon"),
    ],
)
@pytest.mark.asyncio
async def test_other_fields_are_ignored(line: bytes) -> None:
    assert await _decode(line + b"\ndata: 1\n\n") == [(None, "1")]


@pytest.mark.asyncio
async def test_a_frame_of_other_fields_only_dispatches_nothing() -> None:
    assert await _decode(b"id: 7\nretry: 100\n\ndata: 1\n\n") == [(None, "1")]


# Comments


@pytest.mark.parametrize(
    "comment",
    [
        pytest.param(b": keep-alive", id="with_text"),
        pytest.param(b":", id="bare"),
        pytest.param(b": data: not this", id="that_looks_like_a_field"),
    ],
)
@pytest.mark.asyncio
async def test_a_comment_line_is_skipped(comment: bytes) -> None:
    assert await _decode(comment + b"\ndata: 1\n" + comment + b"\ndata: 2\n\n") == [(None, "1\n2")]


@pytest.mark.asyncio
async def test_a_frame_of_comments_only_dispatches_nothing() -> None:
    assert await _decode(b": one\n: two\n\n") == []


# Line endings (D-076): \n, \r\n and \r, and nothing else


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"], ids=["lf", "crlf", "cr"])
@pytest.mark.asyncio
async def test_each_line_ending_works(ending: bytes) -> None:
    body = ending.join([b"event: a", b"data: 1", b"data: 2", b"", b"data: 3", b"", b""])
    assert await _decode(body) == [("a", "1\n2"), (None, "3")]


@pytest.mark.asyncio
async def test_line_endings_may_be_mixed() -> None:
    assert await _decode(b"event: a\r\ndata: 1\rdata: 2\n\r\ndata: 3\r\r") == [
        ("a", "1\n2"),
        (None, "3"),
    ]


@pytest.mark.asyncio
async def test_a_crlf_split_across_chunks_is_one_line_ending() -> None:
    # Read as two endings, the \n would be a blank line and dispatch too early.
    assert await _decode(b"data: 1\r", b"\ndata: 2\r\n\r\n") == [(None, "1\n2")]


@pytest.mark.asyncio
async def test_a_blank_crlf_split_across_chunks_dispatches_once() -> None:
    assert await _decode(b"data: 1\r\n\r", b"\ndata: 2\r\n\r\n") == [(None, "1"), (None, "2")]


@pytest.mark.asyncio
async def test_a_cr_at_the_end_of_a_chunk_followed_by_text_ends_the_line() -> None:
    assert await _decode(b"data: 1\r", b"data: 2\r\r") == [(None, "1\n2")]


@pytest.mark.parametrize(
    ("character", "name"),
    [
        ("\u2028", "line_separator"),
        ("\u2029", "paragraph_separator"),
        ("\u0085", "next_line"),
        ("\x0b", "vertical_tab"),
        ("\x0c", "form_feed"),
        ("\x1c", "file_separator"),
        ("\x1d", "group_separator"),
        ("\x1e", "record_separator"),
    ],
)
@pytest.mark.asyncio
async def test_a_unicode_line_break_inside_data_is_not_a_line_ending(
    character: str, name: str
) -> None:
    # Each is a str.splitlines() separator, and each is legal raw inside a JSON string.
    text = f'{{"text":"one{character}two"}}'
    assert await _decode(f"data: {text}\n\n".encode()) == [(None, text)]


@pytest.mark.parametrize("character", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
@pytest.mark.asyncio
async def test_a_unicode_line_break_split_across_chunks_survives(character: str) -> None:
    frame = f"data: one{character}two\n\n".encode()
    start = frame.index(character.encode())
    for cut in range(start + 1, start + len(character.encode())):
        assert await _decode(frame[:cut], frame[cut:]) == [(None, f"one{character}two")]


# Chunk boundaries

FRAME = 'event: message\r\ndata: {"t":"héllo \u2028 😀"}\r\ndata: tail\r\n\r\n'.encode()
FRAME_EVENTS: list[tuple[str | None, str]] = [("message", '{"t":"héllo \u2028 😀"}\ntail')]


@pytest.mark.asyncio
async def test_every_split_point_of_a_frame_gives_the_same_event() -> None:
    for cut in range(len(FRAME) + 1):
        assert await _decode(FRAME[:cut], FRAME[cut:]) == FRAME_EVENTS, cut


@pytest.mark.asyncio
async def test_every_pair_of_split_points_gives_the_same_event() -> None:
    for first in range(len(FRAME) + 1):
        for second in range(first, len(FRAME) + 1):
            parts = (FRAME[:first], FRAME[first:second], FRAME[second:])
            assert await _decode(*parts) == FRAME_EVENTS, (first, second)


@pytest.mark.asyncio
async def test_a_frame_delivered_one_byte_at_a_time_gives_the_same_event() -> None:
    assert await _decode(*[FRAME[i : i + 1] for i in range(len(FRAME))]) == FRAME_EVENTS


@pytest.mark.parametrize("text", ["é", "€", "😀"], ids=["two_bytes", "three_bytes", "four_bytes"])
@pytest.mark.asyncio
async def test_a_multi_byte_character_split_across_chunks_survives(text: str) -> None:
    frame = f"data: a{text}b\n\n".encode()
    start = frame.index(text.encode())
    for cut in range(start + 1, start + len(text.encode())):
        assert await _decode(frame[:cut], frame[cut:]) == [(None, f"a{text}b")]


@pytest.mark.asyncio
async def test_empty_chunks_change_nothing() -> None:
    assert await _decode(b"", b"data: 1\n", b"", b"\n", b"") == [(None, "1")]


@pytest.mark.asyncio
async def test_many_frames_in_one_chunk_all_come_out() -> None:
    assert await _decode(b"data: 1\n\n" * 50) == [(None, "1")] * 50


@pytest.mark.parametrize("ending", [b"\n\n", b"\r\n\r\n"], ids=["lf", "crlf"])
@pytest.mark.asyncio
async def test_a_complete_frame_is_dispatched_before_the_next_chunk_is_asked_for(
    ending: bytes,
) -> None:
    # Holding it back until more bytes come would delay every delta by one chunk, and
    # the first delta is what opens the response (D-068).
    pulled: list[bytes] = []

    async def source() -> AsyncIterator[bytes]:
        for part in (b"data: 1" + ending, b"data: 2" + ending):
            pulled.append(part)
            yield part

    decoder = _module().iter_sse(source())
    first = await anext(decoder)
    assert first.data == "1"
    assert len(pulled) == 1
    await decoder.aclose()


# The byte order mark


@pytest.mark.asyncio
async def test_a_leading_byte_order_mark_is_removed() -> None:
    assert await _decode(b"\xef\xbb\xbfdata: 1\n\n") == [(None, "1")]


@pytest.mark.parametrize("cut", [1, 2], ids=["after_one_byte", "after_two_bytes"])
@pytest.mark.asyncio
async def test_a_leading_byte_order_mark_split_across_chunks_is_removed(cut: int) -> None:
    body = b"\xef\xbb\xbfdata: 1\n\n"
    assert await _decode(body[:cut], body[cut:]) == [(None, "1")]


@pytest.mark.asyncio
async def test_a_byte_order_mark_on_a_later_line_is_not_removed() -> None:
    # The field name is then "﻿data", which is an unknown field and is ignored.
    assert await _decode(b"data: 1\n\n\xef\xbb\xbfdata: 2\n\n") == [(None, "1")]


@pytest.mark.asyncio
async def test_only_one_leading_byte_order_mark_is_removed() -> None:
    assert await _decode(b"\xef\xbb\xbf\xef\xbb\xbfdata: 1\n\n") == []


@pytest.mark.asyncio
async def test_a_byte_order_mark_inside_data_is_kept() -> None:
    assert await _decode("data: ﻿x\n\n".encode()) == [(None, "﻿x")]


# Strict UTF-8


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"data: \xff", id="data"),
        pytest.param(b"data: \xc3", id="a_character_cut_short"),
        pytest.param(b"data: \xed\xa0\x80", id="a_surrogate"),
        pytest.param(b"event: \xff", id="event"),
        pytest.param(b": \xff", id="comment"),
        pytest.param(b"\xff: value", id="field_name"),
    ],
)
@pytest.mark.asyncio
async def test_a_complete_line_that_is_not_utf8_raises_value_error(line: bytes) -> None:
    with pytest.raises(ValueError):
        await _decode(line + b"\n\n")


@pytest.mark.asyncio
async def test_a_frame_before_a_bad_line_is_still_delivered() -> None:
    decoder = _module().iter_sse(_chunks(b"data: 1\n\n", b"data: \xff\n\n"))
    assert (await anext(decoder)).data == "1"
    with pytest.raises(ValueError):
        await anext(decoder)


# The end of the body (D-076): leftovers are dropped, never dispatched


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"data: 1", id="a_line_with_no_ending"),
        pytest.param(b"data: 1\n", id="a_line_with_no_blank_line"),
        pytest.param(b"event: a\ndata: 1\n", id="a_named_frame_with_no_blank_line"),
        pytest.param(b"data: 1\ndata: 2", id="a_second_line_cut_off"),
        pytest.param(b"data: 1\r", id="a_cr_at_the_very_end"),
    ],
)
@pytest.mark.asyncio
async def test_data_left_over_at_the_end_of_the_body_is_dropped(body: bytes) -> None:
    assert await _decode(body) == []


@pytest.mark.asyncio
async def test_frames_before_a_leftover_are_kept() -> None:
    assert await _decode(b"data: 1\n\ndata: 2\n") == [(None, "1")]


@pytest.mark.asyncio
async def test_an_incomplete_line_at_the_end_is_dropped_and_never_decoded() -> None:
    # D-076 decodes each complete line. This one never completes, so its cut-off
    # character is not a decoding error: it reads as truncation, like any other leftover.
    assert await _decode(b"data: 1\n\ndata: \xe2\x80") == [(None, "1")]


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param((), id="no_chunks"),
        pytest.param((b"",), id="an_empty_chunk"),
        pytest.param((b"\n\n\n",), id="blank_lines"),
    ],
)
@pytest.mark.asyncio
async def test_a_body_with_no_frames_gives_no_events(parts: tuple[bytes, ...]) -> None:
    assert await _decode(*parts) == []


@pytest.mark.asyncio
async def test_a_failure_of_the_source_propagates_after_earlier_frames() -> None:
    # The adapters map httpx's errors, so the decoder must not swallow or wrap them.
    class Dropped(Exception):
        pass

    error = Dropped()

    async def source() -> AsyncIterator[bytes]:
        yield b"data: 1\n\n"
        raise error

    decoder = _module().iter_sse(source())
    assert (await anext(decoder)).data == "1"
    with pytest.raises(Dropped) as info:
        await anext(decoder)
    assert info.value is error


# The providers' own frames


@pytest.mark.asyncio
async def test_a_claude_body_with_a_ping_decodes_by_frame() -> None:
    body = (
        b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: ping\ndata: {"type": "ping"}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    assert await _decode(body) == [
        ("message_start", '{"type":"message_start"}'),
        ("ping", '{"type": "ping"}'),
        ("message_stop", '{"type":"message_stop"}'),
    ]


@pytest.mark.asyncio
async def test_a_gemini_body_with_crlf_frames_decodes_by_frame() -> None:
    assert await _decode(b'data: {"a":1}\r\n\r\ndata: {"b":2}\r\n\r\n') == [
        (None, '{"a":1}'),
        (None, '{"b":2}'),
    ]
