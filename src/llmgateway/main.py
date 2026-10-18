"""Module-level ASGI app, the entry point for uvicorn: `uvicorn llmgateway.main:app`.

Why it exists: uvicorn needs an importable app object, while tests build their own
apps with `create_app` so they never share state. This module is the only place
the default app (mock provider, random request IDs) is created (D-024).

Key design choice: it gives the `llmgateway` logger an INFO handler if it has
none, because uvicorn configures only its own loggers and the request log lines
(D-026) would otherwise be dropped.
"""

import logging

from llmgateway.app import create_app

_logger = logging.getLogger("llmgateway")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

app = create_app()
