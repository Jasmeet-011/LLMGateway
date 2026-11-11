"""Gemini adapter: translates the unified schema to Gemini's `generateContent` API.

Why it exists: the gateway must call Gemini without letting Gemini's request
shape, response shape or error codes leak into the router, cache or accounting
(D-014, D-015). This module is the only place that knows them.

Key design choices:

- `generateContent` on `v1beta`, not the newer Interactions API (D-039). It is
  stateless and its response fields are documented; the Interactions API stores
  interactions server-side by default. Google labels it "Legacy" but supported.
- Raw async httpx, not the Google SDK, for the same reason as Claude (D-032): the
  SDK retries on its own. The transport is built with `retries=0`, every failure
  is raised once as a `ProviderError`, and retrying is the router's job. One
  reused `httpx.AsyncClient` with the D-034 timeouts; `aclose()` closes it.
- The key is sent in the `x-goog-api-key` header, never as the `key=` query
  parameter, so it is never part of a URL.
- The model ID goes in the URL path, so it must fully match a strict pattern
  before any network call (D-042); otherwise it could send the keyed request to
  another endpoint. Since aliases the model comes from the config, which checks
  the same `MODEL_ID_PATTERN` at startup, but the adapter keeps its own check so
  its safety does not depend on its caller (D-046). More than 5 stop sequences,
  Gemini's documented limit, are rejected before any network call too.
- `temperature` and `top_p` are passed through when set (D-040). Google documents
  that Gemini 3.6 Flash and newer ignore them.
- Every request asks for `thinkingLevel` `MINIMAL` (D-049). `gemini-3.6-flash`
  defaults to medium thinking, which spent a whole 256-token `max_tokens` before
  writing any text. `max_tokens` is sent unchanged, and `output_tokens`, which
  includes thinking, is reported as Google sends it even above `max_tokens`. An
  empty answer with finish reason `max_tokens` is a success, not an error.
- Error mapping by HTTP status, as for Claude (D-041): 401, 402 (billing) and 403
  are auth errors; 429 is a rate limit with `retry-after` when numeric; 408
  (D-053), 5xx (including 504 `deadline_exceeded`) and connection errors are unavailable;
  timeouts are timeouts; other 4xx are bad requests. One exception (D-048): Google
  answers an invalid key with 400, so a 400 body is read for its `ErrorInfo`
  reason `API_KEY_INVALID`, and only that machine-readable field, never the
  message text. No other error body is read. Redirects are never followed, and an
  undecodable body is unavailable (D-043).
- On a 2xx (D-041): a blocked prompt is a bad request; otherwise the first
  candidate's non-thought text parts are the content, and thought tokens are
  counted as output tokens because Google bills them as output. A body that is
  not the expected JSON is unavailable.
- Error messages are fixed strings. They never contain the provider's error text,
  the prompt, or the API key, and httpx exceptions are not chained onto them.
- Every error carries the HTTP status when there was one and a fixed reason code
  (D-054) for the router's attempt log; neither reaches the client.
- `stream` posts the same body to `streamGenerateContent?alt=sse` (D-075), built by the
  same function, so it cannot lose `MINIMAL` thinking (D-049). Thought parts produce no
  event. It reads to the end of the body, because usage may follow the finish reason. A
  later count replaces an earlier one, a count left out keeps it, and counts are never
  added: the owner's live run showed them cumulative (D-075), so adding them would
  overstate usage several times over. `thoughtsTokenCount` follows the same rule. Each
  chunk with `usageMetadata` yields a `ProviderUsage` of the merged counts, before that
  chunk's text, so a stream cut short keeps the last counts (D-097). The final
  `output_tokens` is the same `candidatesTokenCount + thoughtsTokenCount` as
  `complete`. Gemini has no terminator, so the finish reason is the only proof that a
  stream ended.
"""

import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Annotated, Any, Final

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
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
    ProviderUsage,
    parse_retry_after,
)
from llmgateway.providers.sse import iter_sse
from llmgateway.schema import ChatRequest, FinishReason, Provider, Role, StreamDelta, Usage

API_BASE: Final = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
MAX_STOP_SEQUENCES: Final = 5
# The lowest ThinkingLevel. Google's thinking guide (Interactions API) lists it for
# both configured Gemini models; unverified on generateContent (D-049).
THINKING_LEVEL: Final = "MINIMAL"

# No "/", "?", "#", "%" or ":", so the model cannot change the request path (D-042).
# Public because config validation applies it at startup too (D-046).
MODEL_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_ROLES: Final[dict[Role, str]] = {"user": "user", "assistant": "model"}

_FINISH_REASONS: Final[dict[str, FinishReason]] = {
    "STOP": "stop",
    "MAX_TOKENS": "max_tokens",
}

# pydantic's parser, not json.loads: it rejects a body that is not a JSON object,
# and it turns very deep nesting into a ValidationError instead of a RecursionError.
_BODY: Final = TypeAdapter(dict[str, Any])

# Proto3 JSON leaves out fields whose value is 0, so an absent count means 0.
_Count = Annotated[StrictInt, Field(ge=0)]


class _Part(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: StrictStr | None = None
    thought: StrictBool = False


class _Content(BaseModel):
    model_config = ConfigDict(extra="ignore")

    parts: list[_Part] = Field(default_factory=list)


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: _Content | None = None
    finish_reason: StrictStr = Field(alias="finishReason")


class _UsageMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_token_count: _Count = Field(default=0, alias="promptTokenCount")
    candidates_token_count: _Count = Field(default=0, alias="candidatesTokenCount")
    thoughts_token_count: _Count = Field(default=0, alias="thoughtsTokenCount")


class _Response(BaseModel):
    """The fields of a `generateContent` response that the adapter reads."""

    model_config = ConfigDict(extra="ignore")

    # Only the first candidate is read, so later ones are not validated.
    candidates: list[Any] = Field(default_factory=list)
    usage_metadata: _UsageMetadata = Field(alias="usageMetadata")
    response_id: Any = Field(default=None, alias="responseId")


class _StreamCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: _Content | None = None
    # Optional on a chunk, unlike D-041's whole response, but a string when present.
    finish_reason: StrictStr = Field(default="", alias="finishReason")


class _StreamChunk(BaseModel):
    """The fields of one `streamGenerateContent` chunk that the adapter reads."""

    model_config = ConfigDict(extra="ignore")

    candidates: list[Any] = Field(default_factory=list)
    usage_metadata: _UsageMetadata | None = Field(default=None, alias="usageMetadata")
    response_id: Any = Field(default=None, alias="responseId")


_ERROR_INFO_TYPE: Final = "type.googleapis.com/google.rpc.ErrorInfo"


def _is_invalid_key(content: bytes) -> bool:
    """Whether a 400 body carries Google's `API_KEY_INVALID` ErrorInfo (D-048).

    Only the machine-readable (`@type`, `reason`, `domain`) triple is read; the
    message text is never looked at. Any other shape is simply not a match.
    """
    try:
        body = _BODY.validate_json(content)
    except ValidationError:
        return False
    error = body.get("error")
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return False
    return any(
        isinstance(detail, dict)
        and detail.get("@type") == _ERROR_INFO_TYPE
        and detail.get("reason") == "API_KEY_INVALID"
        and detail.get("domain") == "googleapis.com"
        for detail in details
    )


def _status_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    if status in (401, 403):
        return ProviderAuthError(
            "gemini: authentication or permission failure", status=status, reason="auth"
        )
    # Google answers an invalid key with 400 INVALID_ARGUMENT, not 401 (D-048). The
    # reason is the constant compared against, never text taken from the body.
    if status == 400 and _is_invalid_key(response.content):
        return ProviderAuthError("gemini: invalid API key", status=status, reason="API_KEY_INVALID")
    if status == 402:
        return ProviderAuthError(
            "gemini: billing failure, prepay credit depleted", status=status, reason="billing"
        )
    if status == 408:
        # The request was not rejected, so sending it again may work (D-053).
        return ProviderUnavailable(
            "gemini: provider request timeout (HTTP 408)", status=status, reason="http_status"
        )
    if status == 429:
        return ProviderRateLimited(
            "gemini: rate limited",
            retry_after_seconds=parse_retry_after(response.headers.get("retry-after")),
            status=status,
            reason="rate_limited",
        )
    if 400 <= status < 500:
        return ProviderBadRequest(
            f"gemini: request rejected with HTTP {status}", status=status, reason="http_status"
        )
    # 5xx, including 504 deadline_exceeded, and anything else unexpected (such as
    # a redirect, which httpx does not follow).
    return ProviderUnavailable(
        f"gemini: provider failure with HTTP {status}", status=status, reason="http_status"
    )


def _request_body(request: ChatRequest) -> dict[str, Any]:
    # max_tokens is passed unchanged, with no thinking allowance. Google's guide
    # says it covers thinking too (unverified on generateContent), so the lowest
    # level is meant to leave it for text (D-049).
    config: dict[str, Any] = {
        "maxOutputTokens": request.max_tokens,
        "thinkingConfig": {"thinkingLevel": THINKING_LEVEL},
    }
    if request.stop is not None:
        config["stopSequences"] = list(request.stop)
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.top_p is not None:
        config["topP"] = request.top_p
    body: dict[str, Any] = {
        "contents": [
            {"role": _ROLES[m.role], "parts": [{"text": m.content}]} for m in request.messages
        ],
        "generationConfig": config,
    }
    if request.system is not None:
        body["systemInstruction"] = {"parts": [{"text": request.system}]}
    return body


def _is_blocked(body: dict[str, Any]) -> bool:
    feedback = body.get("promptFeedback")
    return isinstance(feedback, dict) and feedback.get("blockReason") is not None


def _result(body: dict[str, Any]) -> ProviderResult:
    parsed = _Response.model_validate(body)
    if not parsed.candidates:
        raise ValueError("no candidates")
    candidate = _Candidate.model_validate(parsed.candidates[0])
    parts = candidate.content.parts if candidate.content is not None else []
    usage = parsed.usage_metadata
    response_id = parsed.response_id
    return ProviderResult(
        content="".join(p.text for p in parts if p.text is not None and not p.thought),
        finish_reason=_FINISH_REASONS.get(candidate.finish_reason, "other"),
        usage=Usage(
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count + usage.thoughts_token_count,
        ),
        provider_response_id=response_id if isinstance(response_id, str) and response_id else None,
    )


class _StreamProgress:
    """What the chunks of one stream have said so far (D-075)."""

    def __init__(self) -> None:
        self.finish_reason = ""
        self.response_id: str | None = None
        self.usage: _UsageMetadata | None = None

    def read(self, body: dict[str, Any], status: int) -> list[ProviderStreamEvent]:
        """This chunk's usage, if it has any, then its text; raises `ValueError` on a bad one."""
        if _is_blocked(body):
            raise ProviderBadRequest(
                "gemini: prompt blocked", status=status, reason="prompt_blocked"
            )
        # Nothing in it is read; a provider's error text never goes further.
        if isinstance(body.get("error"), dict):
            raise ProviderUnavailable("gemini: error in the stream", reason="stream_error")
        chunk = _StreamChunk.model_validate(body)
        if isinstance(chunk.response_id, str) and chunk.response_id:
            self.response_id = chunk.response_id
        events: list[ProviderStreamEvent] = []
        if chunk.usage_metadata is not None:
            self._merge(chunk.usage_metadata)
            events.append(ProviderUsage(usage=self._usage()))
        if not chunk.candidates:
            return events
        candidate = _StreamCandidate.model_validate(chunk.candidates[0])
        if candidate.finish_reason:
            self.finish_reason = candidate.finish_reason
        parts = candidate.content.parts if candidate.content is not None else []
        text = "".join(p.text for p in parts if p.text is not None and not p.thought)
        if text:
            events.append(StreamDelta(text=text))
        return events

    def _usage(self) -> Usage:
        usage = self.usage
        if usage is None:
            raise ValueError("no usageMetadata in any chunk")
        # The same arithmetic as `complete`: thinking is billed as output (D-049).
        return Usage(
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count + usage.thoughts_token_count,
        )

    def _merge(self, usage: _UsageMetadata) -> None:
        # Per field: a later count replaces an earlier one, one left out keeps it.
        if self.usage is None:
            self.usage = usage
            return
        fresh = {name: getattr(usage, name) for name in usage.model_fields_set}
        self.usage = self.usage.model_copy(update=fresh)

    def end(self) -> ProviderStreamEnd:
        return ProviderStreamEnd(
            finish_reason=_FINISH_REASONS.get(self.finish_reason, "other"),
            usage=self._usage(),
            provider_response_id=self.response_id,
        )


async def _stream_events(
    response: httpx.Response,
) -> AsyncGenerator[ProviderStreamEvent, None]:
    progress = _StreamProgress()
    status = response.status_code
    failure: ProviderError
    try:
        async with aclosing(iter_sse(response.aiter_bytes())) as frames:
            async for frame in frames:
                for event in progress.read(_BODY.validate_json(frame.data), status):
                    yield event
        if progress.finish_reason:
            yield progress.end()
            return
    except ValueError:
        # ValidationError and UnicodeDecodeError are both ValueErrors. The 2xx status
        # tells the router this generation was billed (D-052).
        failure = ProviderUnavailable(
            "gemini: malformed stream", status=status, reason="malformed_body"
        )
    else:
        failure = ProviderUnavailable(
            "gemini: stream ended early", status=status, reason="stream_truncated"
        )
    # Outside the except block, so the ValueError is not chained onto it.
    raise failure


class GeminiAdapter:
    name: Final[Provider] = "gemini"

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
            headers={"x-goog-api-key": api_key},
        )

    def __repr__(self) -> str:
        return f"GeminiAdapter(timeout={self._timeout!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check(self, request: ChatRequest) -> None:
        # fullmatch, not match with "$", which would also accept a trailing newline.
        if MODEL_ID_PATTERN.fullmatch(request.model) is None:
            raise ProviderBadRequest("gemini: invalid model ID", reason="local_check")
        if request.stop is not None and len(request.stop) > MAX_STOP_SEQUENCES:
            raise ProviderBadRequest(
                "gemini: at most 5 stop sequences are supported", reason="local_check"
            )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ProviderStreamEvent]:
        self._check(request)
        failure: ProviderError | None = None
        try:
            async with self._client.stream(
                "POST",
                f"{API_BASE}/{request.model}:streamGenerateContent",
                params={"alt": "sse"},
                json=_request_body(request),
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
            failure = ProviderTimeout("gemini: request timed out", reason="timeout")
        except httpx.TransportError:
            failure = ProviderUnavailable("gemini: connection failed", reason="connection")
        except httpx.DecodingError:
            failure = ProviderUnavailable(
                "gemini: undecodable response body", reason="undecodable_body"
            )
        # Raised here, not inside the except blocks, so the httpx exception is not
        # chained onto the provider error.
        if failure is not None:
            raise failure

    async def complete(self, request: ChatRequest) -> ProviderResult:
        self._check(request)

        failure: ProviderError | None = None
        try:
            response = await self._client.post(
                f"{API_BASE}/{request.model}:generateContent", json=_request_body(request)
            )
        except httpx.TimeoutException:
            failure = ProviderTimeout("gemini: request timed out", reason="timeout")
        except httpx.TransportError:
            failure = ProviderUnavailable("gemini: connection failed", reason="connection")
        except httpx.DecodingError:
            # Not a TransportError: the body's content-encoding could not be decoded.
            # httpx raises before the status is visible, so there is none (D-052).
            failure = ProviderUnavailable(
                "gemini: undecodable response body", reason="undecodable_body"
            )
        # Raised here, not inside the except blocks, so the httpx exception is not
        # chained onto the provider error.
        if failure is not None:
            raise failure

        if not response.is_success:
            raise _status_error(response)

        try:
            body = _BODY.validate_json(response.content)
            if _is_blocked(body):
                failure = ProviderBadRequest(
                    "gemini: prompt blocked", status=response.status_code, reason="prompt_blocked"
                )
            else:
                return _result(body)
        except (ValidationError, ValueError):
            # The 2xx status tells the router this generation was billed (D-052).
            failure = ProviderUnavailable(
                "gemini: malformed response body",
                status=response.status_code,
                reason="malformed_body",
            )
        raise failure
