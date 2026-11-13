"""Measurement 1's baseline: the mock provider over HTTP with no gateway in front (D-120).

Why it exists: gateway overhead is latency through the gateway minus latency calling the mock
directly (PLAN.md, measurement 1). The gateway's mock runs inside the gateway's process, so
"directly" needs a server of its own. This is the smallest one that serves the same thing: the
same `MockProvider` with the same 20 ms latency as `mock-steady` in config/loadtest.toml, on the
same uvicorn, one worker, from the same image, reached by k6 over the same Compose network.

Key design choices:

- The body is read and discarded, and `complete` is called on one fixed request. Parsing and
  validating the body is part of what the gateway does, so leaving it out here counts it against
  the gateway, the conservative side.
- No auth, no limiter, no cache, no usage record, no request log: those are the gateway.
- Run by loadtest/compose.loadtest.yaml's `direct-mock` service with
  `uvicorn direct_mock:app --app-dir /loadtest`; it is not part of the `llmgateway` package.
"""

from typing import Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from llmgateway.providers.mock import MockProvider
from llmgateway.schema import ChatRequest, Message

# mock-steady's latency in config/loadtest.toml; a test pins that they agree.
LATENCY_MS: Final = 20

_MOCK: Final = MockProvider(latency_ms=LATENCY_MS)
_REQUEST: Final = ChatRequest(
    model="steady",
    messages=(Message(role="user", content="steady request"),),
    max_tokens=64,
)


async def chat(request: Request) -> JSONResponse:
    await request.body()
    result = await _MOCK.complete(_REQUEST)
    return JSONResponse(
        {
            "content": result.content,
            "finish_reason": result.finish_reason,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        }
    )


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/v1/chat", chat, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)
