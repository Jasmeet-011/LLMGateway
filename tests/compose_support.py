"""What the integration tests share: D-081's guard, and the `docker compose` helpers (D-094).

Not a test module. `tests/test_compose_stack.py` and `tests/test_compose_stack_auth.py`
import it, and `tests/test_integration_guard.py` copies it next to a copy of each of them,
so the guard can run an integration file alone, with no Docker.

The guard is `base_url()`. Every integration test calls it FIRST, inside the test, before
any docker command and before any request. It fails and never skips (D-081): a CI job that
lost the variable must not go green with nothing tested. It needs the scheme to be `http`
or `https` and the parsed host to be exactly `127.0.0.1` or `localhost`.

Keys are made and revoked only with the real CLI in the api container, and the table is read
only with `psql` in the postgres container (D-094). Nothing here imports the gateway's code,
so a mistake there cannot hide a bug in it. Postgres's port stays unpublished.
"""

import functools
import hashlib
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

VARIABLE = "LLMGATEWAY_BASE_URL"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# A stack that is up answers at once; a stalled build host is the only reason to wait.
TIMEOUT = 30.0
# One docker command: a start of Postgres is the slowest of them.
DOCKER_TIMEOUT = 180.0

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
KEY_PATTERN = re.compile(r"lgw_([0-9a-f]{16})_([0-9a-f]{64})")

BODY = {
    "model": "mock",
    "messages": [{"role": "user", "content": "say hello to the compose stack"}],
    "max_tokens": 32,
}


def base_url() -> str:
    """The stack's address, or a failure that says why not. Never a skip (D-081)."""
    value = os.environ.get(VARIABLE, "")
    if not value.strip():
        pytest.fail(
            f"{VARIABLE} is unset or empty: these tests need the Compose stack and its "
            f"address, for example {VARIABLE}=http://127.0.0.1:8000. They fail and never "
            "skip, so a run that lost the variable cannot pass with nothing tested."
        )
    parts = urlsplit(value.strip())
    host = parts.hostname
    if parts.scheme not in ("http", "https") or host not in LOCAL_HOSTS:
        pytest.fail(
            f"{VARIABLE} must be an http(s) URL whose host is one of {', '.join(LOCAL_HOSTS)}: "
            "these tests only ever talk to a local stack, and no request was sent."
        )
    return value.strip().rstrip("/")


def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


@contextmanager
def gateway(key: str | None = None) -> Iterator[httpx.Client]:
    """A client for the stack, sending `key` as a bearer token when one is given."""
    headers = bearer(key) if key is not None else None
    with httpx.Client(base_url=base_url(), timeout=TIMEOUT, headers=headers) as client:
        yield client


def compose(*args: str, timeout: float = DOCKER_TIMEOUT) -> "subprocess.CompletedProcess[str]":
    """`docker compose -f <repo>/compose.yaml <args>`. The caller has passed `base_url()`."""
    # The command is `docker compose` on this repo's own file, with arguments written in
    # the tests (or hex ids and fixed SQL), not outside input.
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def cli(*args: str) -> "subprocess.CompletedProcess[str]":
    return compose("exec", "-T", "api", "python", "-m", "llmgateway.keys", *args)


def create_key(tenant: str = "integration-test") -> str:
    """A new key from the real CLI: exit 0, the key alone on stdout."""
    result = cli("create", "--tenant", tenant)
    assert result.returncode == 0, result.stderr
    key = result.stdout.strip()
    assert KEY_PATTERN.fullmatch(key), "the CLI's stdout is not a key"
    assert result.stdout == key + "\n"
    return key


def key_id_of(key: str) -> str:
    match = KEY_PATTERN.fullmatch(key)
    assert match is not None
    return match.group(1)


def secret_of(key: str) -> str:
    match = KEY_PATTERN.fullmatch(key)
    assert match is not None
    return match.group(2)


def revoke_key(key: str) -> None:
    result = cli("revoke", key_id_of(key))
    assert result.returncode == 0, result.stderr
    assert result.stdout in (
        f"revoked key_id={key_id_of(key)}\n",
        f"already revoked key_id={key_id_of(key)}\n",
    )


@functools.cache
def shared_key() -> str:
    """One key for the tests that only need to be let in. Made after the guard has passed."""
    return create_key("integration-shared")


def sha256_hex(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def row_sql(columns: str, key: str) -> str:
    """`SELECT <columns>` for the row of `key`. The id is 16 hex characters, matched above."""
    return f"SELECT {columns} FROM api_keys WHERE key_id = '{key_id_of(key)}'"  # noqa: S608


def insert_sql(key_id: str, tenant: str, created_at: str) -> str:
    """An INSERT of a row with an all-zero hash, for tests that need a chosen creation time.

    The arguments are hex ids, a test-made tenant and a fixed timestamp, never outside input.
    """
    zero_hash = "decode('" + "00" * 32 + "', 'hex')"
    columns = "(key_id, tenant_id, key_hash, created_at)"
    values = f"('{key_id}', '{tenant}', {zero_hash}, '{created_at}')"
    return f"INSERT INTO api_keys {columns} VALUES {values}"  # noqa: S608


def psql(sql: str) -> "subprocess.CompletedProcess[str]":
    """One statement in the stack's Postgres: unaligned, tuples only (`-At`)."""
    return compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "llmgateway",
        "-d",
        "llmgateway",
        "-At",
        "-c",
        sql,
    )
