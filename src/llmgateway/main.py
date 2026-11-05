"""Module-level ASGI app, the entry point for uvicorn: `uvicorn llmgateway.main:app`.

Why it exists: uvicorn needs an importable app object, while tests build their own
apps with `create_app` so they never share state. This module is the only place
the server's app is created and its config read (D-024, D-044).

Key design choices:

- It gives the `llmgateway` logger an INFO handler if it has none, because
  uvicorn configures only its own loggers and the request log lines (D-026) would
  otherwise be dropped.
- The config is the TOML file named by `LLMGATEWAY_CONFIG`, or the built-in
  mock-only default when that is unset (D-044). API keys come only from the
  environment: `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`, each required only when
  the config uses that provider type.
- `LLMGATEWAY_FAULTS` is read through the same environment, unset by default (D-064).
- Bad configuration stops the process at import, before uvicorn serves anything,
  with a `SystemExit` message and no traceback. The messages never echo the
  values that were set, so a key pasted into the wrong variable is not printed.
- The process environment is read directly; no `.env` file is loaded.
"""

import logging
import os
from collections.abc import Mapping

from llmgateway.app import create_app
from llmgateway.config import ConfigError, load_config
from llmgateway.router import Router, build_router


def router_from_env(environ: Mapping[str, str]) -> Router:
    return build_router(load_config(environ), environ)


_logger = logging.getLogger("llmgateway")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

try:
    _router = router_from_env(os.environ)
except ConfigError as exc:
    raise SystemExit(f"llmgateway: configuration error: {exc}") from None

app = create_app(router=_router)
