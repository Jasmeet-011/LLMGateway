"""Module-level ASGI app, the entry point for uvicorn: `uvicorn llmgateway.main:app`.

Why it exists: uvicorn needs an importable app object, while tests build their own
apps with `create_app` so they never share state. This module is the only place
the server's app is created and its provider chosen (D-024, D-036).

Key design choices:

- It gives the `llmgateway` logger an INFO handler if it has none, because
  uvicorn configures only its own loggers and the request log lines (D-026) would
  otherwise be dropped.
- Temporary, until model aliases (D-010): `LLMGATEWAY_PROVIDER` picks the one
  provider, `mock` (the default) or `claude`. `claude` needs `ANTHROPIC_API_KEY`.
- Bad configuration stops the process at import, before uvicorn serves anything,
  with a `SystemExit` message and no traceback. The messages never echo the
  values that were set, so a key pasted into the wrong variable is not printed.
- The process environment is read directly; no `.env` file is loaded.
"""

import logging
import os
from collections.abc import Mapping

from llmgateway.app import create_app
from llmgateway.providers.base import ProviderAdapter
from llmgateway.providers.claude import ClaudeAdapter
from llmgateway.providers.mock import MockProvider


class ConfigError(Exception):
    """The environment does not describe a valid gateway configuration."""


def provider_from_env(environ: Mapping[str, str]) -> ProviderAdapter:
    name = environ.get("LLMGATEWAY_PROVIDER", "mock")
    if name == "mock":
        return MockProvider()
    if name == "claude":
        api_key = environ.get("ANTHROPIC_API_KEY", "")
        if not api_key.strip():
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set; it is required when LLMGATEWAY_PROVIDER=claude"
            )
        return ClaudeAdapter(api_key)
    raise ConfigError("LLMGATEWAY_PROVIDER must be 'mock' or 'claude'")


_logger = logging.getLogger("llmgateway")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

try:
    _provider = provider_from_env(os.environ)
except ConfigError as exc:
    raise SystemExit(f"llmgateway: configuration error: {exc}") from None

app = create_app(provider=_provider)
