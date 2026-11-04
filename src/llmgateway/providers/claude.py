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
- Error mapping (D-014, D-035, D-048): 401, 402 (billing) and 403 are auth
  errors, because each is the gateway's account problem; 429 is a rate limit
  with `retry-after` when numeric; 5xx (including 529 overloaded) and connection
  errors are unavailable; timeouts are timeouts; other 4xx are bad requests. A 2xx
  body that is not the expected JSON is unavailable, and so is a body whose
  content-encoding cannot be decoded. Redirects are never followed (D-043).
- Error messages are fixed strings. They never contain the provider's error text,
  the prompt, or the API key. Provider errors are raised outside the `except`
  block, so the httpx exception, whose request carries the key header, is not
  attached as `__context__`.
- The API key lives only in the client's default headers, never in an attribute
  of the adapter, and `repr()` does not show it.
"""

from typing import Annotated, Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

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
from llmgateway.schema import ChatRequest, FinishReason, Provider, Usage

API_URL: Final = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final = "2026-10-21"
DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_STOP_REASONS: Final[dict[str, FinishReason]] = {
    "end_turn": "stop",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


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


def _status_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    if status in (401, 403):
        return ProviderAuthError("claude: authentication or permission failure")
    if status == 402:
        # billing_error: the gateway's account, not the client's request (D-048).
        return ProviderAuthError("claude: billing failure")
    if status == 429:
        return ProviderRateLimited(
            "claude: rate limited",
            # Anthropic sends whole seconds.
            retry_after_seconds=parse_retry_after(response.headers.get("retry-after")),
        )
    if 400 <= status < 500:
        return ProviderBadRequest(f"claude: request rejected with HTTP {status}")
    # 5xx, including 529 overloaded, and anything else unexpected (such as a
    # redirect, which httpx does not follow).
    return ProviderUnavailable(f"claude: provider failure with HTTP {status}")


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

    async def complete(self, request: ChatRequest) -> ProviderResult:
        if request.temperature is not None or request.top_p is not None:
            raise ProviderBadRequest("claude: temperature and top_p are not supported")

        failure: ProviderError | None = None
        try:
            response = await self._client.post(API_URL, json=_request_body(request))
        except httpx.TimeoutException:
            failure = ProviderTimeout("claude: request timed out")
        except httpx.TransportError:
            failure = ProviderUnavailable("claude: connection failed")
        except httpx.DecodingError:
            # Not a TransportError: the body's content-encoding could not be decoded.
            failure = ProviderUnavailable("claude: undecodable response body")
        # Raised here, not inside the except blocks, so the httpx exception is not
        # chained onto the provider error.
        if failure is not None:
            raise failure

        if not response.is_success:
            raise _status_error(response)

        try:
            return _result(_Message.model_validate_json(response.content))
        except (ValidationError, ValueError):
            failure = ProviderUnavailable("claude: malformed response body")
        raise failure
