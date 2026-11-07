"""Claude adapter: translates the unified schema to the Anthropic Messages API.

Why it exists: the gateway must call Claude without letting Claude's request
shape, response shape or error codes leak into the router, cache or accounting
(D-014, D-015). This module is the only place that knows them.

Key design choices:

- Raw async httpx, not the Anthropic SDK (D-032). The SDK retries by default,
  which would stack with the router's retries, and it hides the status codes and
  timeouts this adapter needs to control. The Gemini adapter follows the same
  pattern (D-039).
- No retries anywhere in here. The transport is built with `retries=0`, and every
  failure is raised once as a `ProviderError`; retrying is the router's job.
- One `httpx.AsyncClient` per adapter, reused for every call so connections are
  pooled. Explicit timeouts (D-034): connect 5s, read 60s, write 10s, pool 5s.
  `aclose()` closes it; the app does that on shutdown.
- API version `2026-10-21` (D-033). On that version `temperature` and `top_p`
  return a 400 on every model, so a request that sets either is rejected here with
  `ProviderBadRequest` before any network call, instead of being dropped silently.
- `request.model` is sent as the Claude model ID. The router sets it to the alias
  entry's provider model from the config (D-045).
- Error mapping (D-014, D-035, D-048, D-053): 401, 402 (billing) and 403 are
  auth errors, because each is the gateway's account problem; 429 is a rate limit
  with `retry-after` when numeric; 408, 5xx (including 529 overloaded) and
  connection errors are unavailable; timeouts are timeouts; other 4xx are bad
  requests. A 2xx body that is not the expected JSON is unavailable, and so is a
  body whose content-encoding cannot be decoded. Redirects are never followed
  (D-043).
- Every error carries the HTTP status when there was one and a fixed reason code
  (D-054) for the router's attempt log; neither reaches the client.
- Error messages are fixed strings. They never contain the provider's error text,
  the prompt, or the API key. Provider errors are raised outside the `except`
  block, so the httpx exception, whose request carries the key header, is not
  attached as `__context__`.
- The API key lives only in the client's default headers, never in an attribute
  of the adapter, and `repr()` does not show it.
- `stream` sends the same request with `"stream": true` and keeps the same table for
  the status (D-074). Events are read by their JSON `type`; thinking, `ping` and
  anything unknown produce nothing, because new event types may be added. Usage is
  cumulative, so a later count replaces an earlier one, and counts are never added.
  An in-stream `error` is treated as the HTTP status its `error.type` stands for, so
  an overload right after the 200 retries and falls back like a real 529. Its
  `message` is never read. `async with client.stream` around the yields closes the
  connection on every exit, cancellation included.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Annotated, Any, Final

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

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
    parse_retry_after,
)
from llmgateway.providers.sse import iter_sse
from llmgateway.schema import ChatRequest, FinishReason, Provider, StreamDelta, Usage

API_URL: Final = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final = "2026-10-21"
DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_STOP_REASONS: Final[dict[str, FinishReason]] = {
    "end_turn": "stop",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}

# What an in-stream `error.type` stands for (D-074), from Anthropic's error table.
_IN_STREAM_ERRORS: Final[dict[str, int]] = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "overloaded_error": 529,
}

# pydantic's parser, not json.loads: it rejects a frame that is not a JSON object, and
# it turns very deep nesting into a ValidationError instead of a RecursionError.
_FRAME: Final = TypeAdapter(dict[str, Any])

_Count = Annotated[StrictInt, Field(ge=0)]


class _Block(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: Annotated[StrictInt, Field(ge=0)]
    output_tokens: Annotated[StrictInt, Field(ge=0)]


class _Message(BaseModel):
    """The fields of a Messages API response that the adapter reads."""

    model_config = ConfigDict(extra="ignore")

    id: Annotated[str, Field(min_length=1)]
    content: list[_Block]
    stop_reason: str
    usage: _Usage


class _StartUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: _Count
    # Left out is fine, and `model_fields_set` tells it from a real 0; a null is not.
    output_tokens: _Count = 0


class _StartMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Annotated[str, Field(min_length=1)]
    usage: _StartUsage


class _StopDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stop_reason: StrictStr | None = None


class _DeltaUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # A null keeps the earlier count, like a count left out (D-074).
    input_tokens: _Count | None = None
    output_tokens: _Count | None = None


class _MessageDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    delta: _StopDelta = Field(default_factory=_StopDelta)
    usage: _DeltaUsage | None = None


class _TextDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: StrictStr


def _status_error(response: httpx.Response) -> ProviderError:
    return _failure_for_status(
        response.status_code,
        # Anthropic sends whole seconds.
        parse_retry_after(response.headers.get("retry-after")),
    )


def _failure_for_status(status: int, retry_after: float | None) -> ProviderError:
    if status in (401, 403):
        return ProviderAuthError(
            "claude: authentication or permission failure", status=status, reason="auth"
        )
    if status == 402:
        # billing_error: the gateway's account, not the client's request (D-048).
        return ProviderAuthError("claude: billing failure", status=status, reason="billing")
    if status == 408:
        # The request was not rejected, so sending it again may work (D-053).
        return ProviderUnavailable(
            "claude: provider request timeout (HTTP 408)", status=status, reason="http_status"
        )
    if status == 429:
        return ProviderRateLimited(
            "claude: rate limited",
            retry_after_seconds=retry_after,
            status=status,
            reason="rate_limited",
        )
    if 400 <= status < 500:
        return ProviderBadRequest(
            f"claude: request rejected with HTTP {status}", status=status, reason="http_status"
        )
    # 5xx, including 529 overloaded, and anything else unexpected (such as a
    # redirect, which httpx does not follow).
    return ProviderUnavailable(
        f"claude: provider failure with HTTP {status}", status=status, reason="http_status"
    )


def _request_body(request: ChatRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
    }
    if request.system is not None:
        body["system"] = request.system
    if request.stop is not None:
        body["stop_sequences"] = list(request.stop)
    return body


def _result(message: _Message) -> ProviderResult:
    texts: list[str] = []
    for block in message.content:
        if block.type != "text":
            continue
        if block.text is None:
            raise ValueError("text block without text")
        texts.append(block.text)
    return ProviderResult(
        content="".join(texts),
        finish_reason=_STOP_REASONS.get(message.stop_reason, "other"),
        usage=Usage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        ),
        provider_response_id=message.id,
    )


def _in_stream_error(frame: dict[str, Any]) -> ProviderError:
    """The failure an `error` event stands for; `error.message` is never read (D-074)."""
    error = frame.get("error")
    kind = error.get("type") if isinstance(error, dict) else None
    if isinstance(kind, str) and kind in _IN_STREAM_ERRORS:
        return _failure_for_status(_IN_STREAM_ERRORS[kind], None)
    return ProviderUnavailable("claude: error in the stream", reason="stream_error")


class _StreamProgress:
    """What the frames of one stream have said so far (D-074)."""

    def __init__(self) -> None:
        self.response_id: str | None = None
        self.stop_reason: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None

    def read(self, frame: dict[str, Any]) -> ProviderStreamEvent | None:
        """The event this frame produces, if any; raises `ValueError` on a bad one."""
        match frame.get("type"):
            case "content_block_delta":
                return self._text(frame)
            case "message_start":
                self._start(frame)
            case "message_delta":
                self._delta(frame)
            case "message_stop":
                return self._end()
            case "error":
                raise _in_stream_error(frame)
        return None

    def _text(self, frame: dict[str, Any]) -> StreamDelta | None:
        delta = frame.get("delta")
        if not isinstance(delta, dict):
            raise ValueError("content_block_delta without a delta")
        if delta.get("type") != "text_delta":
            return None
        text = _TextDelta.model_validate(delta).text
        return StreamDelta(text=text) if text else None

    def _start(self, frame: dict[str, Any]) -> None:
        message = _StartMessage.model_validate(frame.get("message"))
        self.response_id = message.id
        self.input_tokens = message.usage.input_tokens
        if "output_tokens" in message.usage.model_fields_set:
            self.output_tokens = message.usage.output_tokens

    def _delta(self, frame: dict[str, Any]) -> None:
        parsed = _MessageDelta.model_validate(frame)
        if parsed.delta.stop_reason is not None:
            self.stop_reason = parsed.delta.stop_reason
        usage = parsed.usage
        if usage is None:
            return
        # Cumulative, so a later count replaces an earlier one; never added.
        if usage.input_tokens is not None:
            self.input_tokens = usage.input_tokens
        if usage.output_tokens is not None:
            self.output_tokens = usage.output_tokens

    def _end(self) -> ProviderStreamEnd:
        if (
            self.response_id is None
            or self.stop_reason is None
            or self.input_tokens is None
            or self.output_tokens is None
        ):
            raise ValueError("message_stop before the stream said everything")
        return ProviderStreamEnd(
            finish_reason=_STOP_REASONS.get(self.stop_reason, "other"),
            usage=Usage(input_tokens=self.input_tokens, output_tokens=self.output_tokens),
            provider_response_id=self.response_id,
        )


async def _stream_events(
    response: httpx.Response,
) -> AsyncGenerator[ProviderStreamEvent, None]:
    progress = _StreamProgress()
    try:
        async with aclosing(iter_sse(response.aiter_bytes())) as frames:
            async for frame in frames:
                event = progress.read(_FRAME.validate_json(frame.data))
                if event is not None:
                    yield event
                if isinstance(event, ProviderStreamEnd):
                    return
    except ValueError:
        # ValidationError and UnicodeDecodeError are both ValueErrors. The 2xx status
        # tells the router this generation was billed (D-052).
        failure = ProviderUnavailable(
            "claude: malformed stream", status=response.status_code, reason="malformed_body"
        )
    else:
        failure = ProviderUnavailable(
            "claude: stream ended early", status=response.status_code, reason="stream_truncated"
        )
    # Outside the except block, so the ValueError is not chained onto it.
    raise failure


class ClaudeAdapter:
    name: Final[Provider] = "claude"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            # Tests inject an httpx.MockTransport; production never retries.
            transport=transport if transport is not None else httpx.AsyncHTTPTransport(retries=0),
            timeout=timeout,
            # Explicit, not httpx's default: on a cross-host redirect httpx drops
            # only Authorization, so a followed redirect would forward the key (D-043).
            follow_redirects=False,
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        )

    def __repr__(self) -> str:
        return f"ClaudeAdapter(timeout={self._timeout!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check(self, request: ChatRequest) -> None:
        if request.temperature is not None or request.top_p is not None:
            raise ProviderBadRequest(
                "claude: temperature and top_p are not supported", reason="local_check"
            )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ProviderStreamEvent]:
        self._check(request)
        failure: ProviderError | None = None
        try:
            async with self._client.stream(
                "POST", API_URL, json={**_request_body(request), "stream": True}
            ) as response:
                if response.is_success:
                    async with aclosing(_stream_events(response)) as events:
                        async for event in events:
                            yield event
                else:
                    # The status table reads the body, and a stream has not read it yet.
                    await response.aread()
                    failure = _status_error(response)
        except httpx.TimeoutException:
            failure = ProviderTimeout("claude: request timed out", reason="timeout")
        except httpx.TransportError:
            failure = ProviderUnavailable("claude: connection failed", reason="connection")
        except httpx.DecodingError:
            failure = ProviderUnavailable(
                "claude: undecodable response body", reason="undecodable_body"
            )
        # Raised here, not inside the except blocks, so the httpx exception is not
        # chained onto the provider error.
        if failure is not None:
            raise failure

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self._check(request)

        failure: ProviderError | None = None
        try:
            response = await self._client.post(API_URL, json=_request_body(request))
        except httpx.TimeoutException:
            failure = ProviderTimeout("claude: request timed out", reason="timeout")
        except httpx.TransportError:
            failure = ProviderUnavailable("claude: connection failed", reason="connection")
        except httpx.DecodingError:
            # Not a TransportError: the body's content-encoding could not be decoded.
            # httpx raises before the status is visible, so there is none (D-052).
            failure = ProviderUnavailable(
                "claude: undecodable response body", reason="undecodable_body"
            )
        # Raised here, not inside the except blocks, so the httpx exception is not
        # chained onto the provider error.
        if failure is not None:
            raise failure

        if not response.is_success:
            raise _status_error(response)

        try:
            return _result(_Message.model_validate_json(response.content))
        except (ValidationError, ValueError):
            # The 2xx status tells the router this generation was billed (D-052).
            failure = ProviderUnavailable(
                "claude: malformed response body",
                status=response.status_code,
                reason="malformed_body",
            )
        raise failure
