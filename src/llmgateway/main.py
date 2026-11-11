"""Module-level ASGI app, the entry point for uvicorn: `uvicorn llmgateway.main:app`.

Why it exists: uvicorn needs an importable app object, while tests build their own
apps with `create_app` so they never share state. This module is the only place
the server's app is created and its config read (D-024, D-044).

Key design choices:

- It gives the `llmgateway` logger an INFO handler if it has none, because
  uvicorn configures only its own loggers and the request log lines (D-026) would
  otherwise be dropped. Each line starts with its time in UTC with a `+00:00`
  offset, from `UtcFormatter` (D-082).
- The config is the TOML file named by `LLMGATEWAY_CONFIG`, or the built-in
  mock-only default when that is unset (D-044). API keys come only from the
  environment: `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`, each required only when
  the config uses that provider type.
- `LLMGATEWAY_FAULTS` is read through the same environment, unset by default (D-064).
- Every configured Claude or Gemini model must have a price in effect today in the
  packaged price table, checked after the provider config (D-098), so a missing
  price stops startup instead of leaving records without a cost.
- `LLMGATEWAY_DATABASE_URL` is required: the key store's Postgres (D-091), which also
  holds usage records (D-099). A missing or malformed URL stops startup, but an
  unreachable database does not, because both pools connect on first use; requests
  get 503 until Postgres answers, and usage writes fail and are logged.
  `LLMGATEWAY_KEY_CACHE_SECONDS` sets the key cache (D-090).
- Bad configuration stops the process at import, before uvicorn serves anything,
  with a `SystemExit` message and no traceback. The messages never echo the
  values that were set, so a key pasted into the wrong variable is not printed.
- The process environment is read directly; no `.env` file is loaded.
"""

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime

from llmgateway import keystore, usagestore
from llmgateway.app import create_app
from llmgateway.auth import Authenticator
from llmgateway.config import (
    ConfigError,
    GatewayConfig,
    database_url,
    key_cache_seconds,
    load_config,
)
from llmgateway.logtime import UtcFormatter
from llmgateway.prices import PriceTable, load_prices
from llmgateway.router import Router, build_router
from llmgateway.usage import UsageRecorder


def router_from_env(environ: Mapping[str, str]) -> Router:
    return build_router(load_config(environ), environ)


def checked_prices(config: GatewayConfig) -> PriceTable:
    prices = load_prices()
    prices.check(config, datetime.now(UTC).date())
    return prices


def authenticator_from_env(environ: Mapping[str, str]) -> Authenticator:
    # Looked up on the module at call time, so a test child can swap the store in.
    store = keystore.PostgresKeyStore(database_url(environ))
    return Authenticator(store, cache_seconds=key_cache_seconds(environ))


def recorder_from_env(environ: Mapping[str, str], prices: PriceTable) -> UsageRecorder:
    # Looked up on the module at call time, as the key store is.
    return UsageRecorder(usagestore.PostgresUsageStore(database_url(environ)), prices)


_logger = logging.getLogger("llmgateway")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(UtcFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

try:
    # The provider config first, so its errors are still the ones reported (D-091),
    # then the prices it needs (D-098), then the database settings.
    _config = load_config(os.environ)
    _router = build_router(_config, os.environ)
    _prices = checked_prices(_config)
    _authenticator = authenticator_from_env(os.environ)
    _recorder = recorder_from_env(os.environ, _prices)
except ConfigError as exc:
    raise SystemExit(f"llmgateway: configuration error: {exc}") from None

app = create_app(router=_router, authenticator=_authenticator, recorder=_recorder)
