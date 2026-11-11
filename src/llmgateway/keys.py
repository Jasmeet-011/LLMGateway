"""The key CLI: `python -m llmgateway.keys migrate | create | revoke | list`.

Why it exists: keys must be made somewhere, and the full key must reach the operator
once and nothing else (D-086, D-092). A CLI run next to the database prints it to the
operator's terminal and stores only its hash; there is no admin HTTP surface to secure.

Key design choices:

- `create` prints the full key alone on standard output, so `KEY=$(... create ...)`
  captures exactly the key; everything else goes to standard error.
- Bad input is refused before any database call: a tenant outside D-092's rule, or a
  key id that is not 16 lowercase hex characters.
- The database URL is read with the gateway's own rules (D-091) and never printed. An
  unreachable database prints only the exception's type name.
- `main` takes its store, migration runner, environment and streams as arguments, so
  the tests run every command against fakes.
"""

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, TextIO

from llmgateway.auth import KeyStoreUnavailable, generate_key, hash_key
from llmgateway.config import ConfigError, database_url
from llmgateway.keystore import KeyInfo, PostgresKeyStore, run_migrations

PREFIX: Final = "llmgateway.keys:"
TENANT_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_TENANT_LENGTH: Final = 64
KEY_ID_PATTERN: Final = re.compile(r"[0-9a-f]{16}")
INVALID_TENANT: Final = (
    f"{PREFIX} invalid tenant: 1 to 64 characters from A-Z a-z 0-9 . _ -, "
    "starting with a letter or digit"
)
NO_SUCH_KEY: Final = f"{PREFIX} no such key"


class KeyAdminStore(Protocol):
    async def create(self, key_id: str, tenant_id: str, key_hash: bytes) -> None: ...

    async def revoke(self, key_id: str) -> Literal["revoked", "already_revoked", "not_found"]: ...

    async def list_keys(self, tenant_id: str | None = None) -> list[KeyInfo]: ...

    async def aclose(self) -> None: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m llmgateway.keys",
        description="Manage LLMGateway API keys. Reads LLMGATEWAY_DATABASE_URL.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply the database migrations")
    create = commands.add_parser("create", help="make a key; it is printed once")
    create.add_argument("--tenant", required=True)
    revoke = commands.add_parser("revoke", help="revoke a key by its id")
    revoke.add_argument("key_id")
    listing = commands.add_parser("list", help="list keys, never their hashes")
    listing.add_argument("--tenant")
    return parser


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _valid_tenant(tenant: str) -> bool:
    return len(tenant) <= MAX_TENANT_LENGTH and TENANT_PATTERN.fullmatch(tenant) is not None


async def _with_store(
    store_factory: Callable[[str], KeyAdminStore],
    dsn: str,
    work: Callable[[KeyAdminStore], Coroutine[Any, Any, int]],
) -> int:
    store = store_factory(dsn)
    try:
        return await work(store)
    finally:
        await store.aclose()


def main(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    store_factory: Callable[[str], KeyAdminStore],
    migrate: Callable[[str], None],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    args = _parser().parse_args(list(argv))

    try:
        dsn = database_url(environ)
    except ConfigError as exc:
        print(f"{PREFIX} configuration error: {exc}", file=stderr)
        return 1

    async def create(store: KeyAdminStore) -> int:
        new = generate_key()
        await store.create(new.key_id, args.tenant, hash_key(new.key))
        print(new.key, file=stdout)
        print(
            f"created key_id={new.key_id} tenant={args.tenant}; "
            "the key above is shown once and is not stored",
            file=stderr,
        )
        return 0

    async def revoke(store: KeyAdminStore) -> int:
        result = await store.revoke(args.key_id)
        if result == "not_found":
            print(NO_SUCH_KEY, file=stderr)
            return 1
        word = "revoked" if result == "revoked" else "already revoked"
        print(f"{word} key_id={args.key_id}", file=stdout)
        return 0

    async def list_keys(store: KeyAdminStore) -> int:
        # An empty --tenant means every tenant, as no --tenant does.
        for info in await store.list_keys(args.tenant or None):
            revoked = "-" if info.revoked_at is None else _utc(info.revoked_at)
            print(
                f"{info.key_id} tenant={info.tenant_id} "
                f"created={_utc(info.created_at)} revoked={revoked}",
                file=stdout,
            )
        return 0

    try:
        if args.command == "migrate":
            migrate(dsn)
            print("migrations applied", file=stdout)
            return 0
        if args.command == "create":
            if not _valid_tenant(args.tenant):
                print(INVALID_TENANT, file=stderr)
                return 1
            return asyncio.run(_with_store(store_factory, dsn, create))
        if args.command == "revoke":
            if KEY_ID_PATTERN.fullmatch(args.key_id) is None:
                print(NO_SUCH_KEY, file=stderr)
                return 1
            return asyncio.run(_with_store(store_factory, dsn, revoke))
        return asyncio.run(_with_store(store_factory, dsn, list_keys))
    except KeyStoreUnavailable as exc:
        print(f"{PREFIX} database unavailable ({exc.error_type})", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1:],
            environ=os.environ,
            store_factory=PostgresKeyStore,
            migrate=run_migrations,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
