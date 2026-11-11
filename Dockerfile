# The gateway's image (D-078). Pins are tag plus digest, under the age floor (D-079).
# Two stages: uv and the build cache stay in the first; the second holds only the
# virtual environment, the config examples and a non-root user.

FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS build
COPY --from=uv /uv /bin/uv
# Compile bytecode now so the container does not on first import; copy rather than
# hard-link out of the cache; use this image's Python, never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
# Dependencies first, in a layer that changes only with uv.lock.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway
COPY --from=build /app/.venv /app/.venv
COPY config /app/config
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER gateway
EXPOSE 8000
# The slim image has no curl, so the standard library fetches /healthz.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=6 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
# Exec form, so uvicorn is PID 1 and receives the stop signal.
CMD ["uvicorn", "llmgateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
