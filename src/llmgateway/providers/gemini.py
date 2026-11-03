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
- The model ID goes in the URL path and is client text until model aliases
  (D-027), so it must fully match a strict pattern before any network call
  (D-042); otherwise it could send the keyed request to another endpoint. More
  than 5 stop sequences, Gemini's documented limit, are rejected the same way.
- `temperature` and `top_p` are passed through when set (D-040). Google documents
  that Gemini 3.6 Flash and newer ignore them.
- Error mapping by HTTP status only, as for Claude (D-041): 401 and 403 are auth
  errors; 429 is a rate limit with `retry-after` when numeric; 5xx (including 504
  `deadline_exceeded`) and connection errors are unavailable; timeouts are
  timeouts; other 4xx are bad requests. The error body is never read. Redirects
  are never followed, and an undecodable body is unavailable (D-043).
- On a 2xx (D-041): a blocked prompt is a bad request; otherwise the first
  candidate's non-thought text parts are the content, and thought tokens are
  counted as output tokens because Google bills them as output. A body that is
  not the expected JSON is unavailable.
- Error messages are fixed strings. They never contain the provider's error text,
  the prompt, or the API key, and httpx exceptions are not chained onto them.
"""

import re
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
    ProviderTimeout,
    ProviderUnavailable,
    parse_retry_after,
)
from llmgateway.schema import ChatRequest, FinishReason, Provider, Role, Usage

API_BASE: Final = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
MAX_STOP_SEQUENCES: Final = 5

# No "/", "?", "#", "%" or ":", so the model cannot change the request path (D-042).
_MODEL_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

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


def _status_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    if status in (401, 403):
        return ProviderAuthError("gemini: authentication or permission failure")
    if status == 429:
        return ProviderRateLimited(
            "gemini: rate limited",
            retry_after_seconds=parse_retry_after(response.headers.get("retry-after")),
        )
    if 400 <= status < 500:
        return ProviderBadRequest(f"gemini: request rejected with HTTP {status}")
    # 5xx, including 504 deadline_exceeded, and anything else unexpected (such as
    # a redirect, which httpx does not follow).
    return ProviderUnavailable(f"gemini: provider failure with HTTP {status}")


def _request_body(request: ChatRequest) -> dict[str, Any]:
    config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
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

    async def complete(self, request: ChatRequest) -> ProviderResult:
        # fullmatch, not match with "$", which would also accept a trailing newline.
        if _MODEL_ID.fullmatch(request.model) is None:
            raise ProviderBadRequest("gemini: invalid model ID")
        if request.stop is not None and len(request.stop) > MAX_STOP_SEQUENCES:
            raise ProviderBadRequest("gemini: at most 5 stop sequences are supported")

        failure: ProviderError | None = None
        try:
            response = await self._client.post(
                f"{API_BASE}/{request.model}:generateContent", json=_request_body(request)
            )
        except httpx.TimeoutException:
            failure = ProviderTimeout("gemini: request timed out")
        except httpx.TransportError:
            failure = ProviderUnavailable("gemini: connection failed")
        except httpx.DecodingError:
            # Not a TransportError: the body's content-encoding could not be decoded.
            failure = ProviderUnavailable("gemini: undecodable response body")
        # Raised here, not inside the except blocks, so the httpx exception is not
        # chained onto the provider error.
        if failure is not None:
            raise failure

        if not response.is_success:
            raise _status_error(response)

        try:
            body = _BODY.validate_json(response.content)
            if _is_blocked(body):
                failure = ProviderBadRequest("gemini: prompt blocked")
            else:
                return _result(body)
        except (ValidationError, ValueError):
            failure = ProviderUnavailable("gemini: malformed response body")
        raise failure
