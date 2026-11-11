"""Fakes and constants for the key-auth tests (M14, D-085 to D-094). Not a test module.

Everything from `llmgateway.auth` and `llmgateway.keystore` is reached at call time, as
`stream_support` does for M12. Most test files only need `AUTH_HEADERS` and
`make_authenticator()`, and a module-level import of code that does not exist before M14
would turn every one of those files into a collection error. This way each test fails on
its own, for its own reason, on the commit that is run red. The names under
`TYPE_CHECKING` give mypy the real types.

- `TEST_KEY`, `TEST_KEY_ID`, `TEST_TENANT`: one well-formed key and its tenant.
- `AUTH_HEADERS`: the header a client sends it with, for `httpx` clients.
- `AUTH_SCOPE_HEADER`: the same header as an ASGI scope pair.
- `FakeKeyStore`: the `KeyStore` protocol in memory. It counts `find` calls, can be told
  to raise `KeyStoreUnavailable`, to raise anything else, or to hang until released, and
  counts the cancels a timeout causes and the `aclose` calls.
- `make_authenticator`: an `Authenticator` over a `FakeKeyStore` that holds `TEST_KEY`.
- `FakeAdminStore`: the CLI's `KeyAdminStore` in memory (D-092).
- `child_prelude`: Python source for a child process that swaps
  `llmgateway.keystore.PostgresKeyStore` for an in-memory fake before `llmgateway.main`
  is imported, as D-091's startup wiring allows.
"""

import asyncio
import dataclasses
import importlib
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from llmgateway.auth import Authenticator, KeyRecord
    from llmgateway.keystore import KeyInfo

TEST_KEY_ID = "a1b2c3d4e5f60718"
TEST_SECRET = "0123456789abcdef" * 4
TEST_KEY = f"lgw_{TEST_KEY_ID}_{TEST_SECRET}"
TEST_TENANT = "tenant-a"

AUTH_HEADERS = {"authorization": f"Bearer {TEST_KEY}"}
AUTH_SCOPE_HEADER = (b"authorization", f"Bearer {TEST_KEY}".encode("ascii"))


def auth_module() -> Any:
    return importlib.import_module("llmgateway.auth")


def make_key(n: int) -> str:
    """A well-formed key that depends only on `n`: its id is n in hex, its secret is n too."""
    return f"lgw_{n:016x}_{n:064x}"


def key_id(key: str) -> str:
    """The id half of a well-formed key (fixed text, not the code under test)."""
    return key.split("_")[1]


def with_secret(key: str, secret: str) -> str:
    """The same key id with another secret."""
    return f"lgw_{key_id(key)}_{secret}"


def make_record(key: str, *, tenant: str = TEST_TENANT, revoked: bool = False) -> "KeyRecord":
    module = auth_module()
    record: KeyRecord = module.KeyRecord(
        key_id=key_id(key), tenant_id=tenant, key_hash=module.hash_key(key), revoked=revoked
    )
    return record


class FakeKeyStore:
    """A `KeyStore` in memory.

    `find` records the id, then raises `error` if one is set, then waits for `release()`
    if `hang()` was called, then answers from `records`. A cancel while waiting is counted,
    so a test can see that the timeout stopped the lookup.
    """

    def __init__(self, *keys: str, tenant: str = TEST_TENANT) -> None:
        self.records: dict[str, KeyRecord] = {}
        self.find_calls: list[str] = []
        self.cancelled = 0
        self.closed = 0
        self.error: BaseException | None = None
        self.gate: asyncio.Event | None = None
        for key in keys:
            self.add(key, tenant=tenant)

    def add(self, key: str, *, tenant: str = TEST_TENANT, revoked: bool = False) -> None:
        self.records[key_id(key)] = make_record(key, tenant=tenant, revoked=revoked)

    def revoke(self, key: str) -> None:
        record = self.records[key_id(key)]
        self.records[key_id(key)] = dataclasses.replace(record, revoked=True)

    def remove(self, key: str) -> None:
        del self.records[key_id(key)]

    def fail_with(self, error: BaseException | None) -> None:
        self.error = error

    def unavailable(self, error_type: str = "OSError") -> None:
        self.error = auth_module().KeyStoreUnavailable(error_type)

    def hang(self) -> None:
        self.gate = asyncio.Event()

    def release(self) -> None:
        assert self.gate is not None
        self.gate.set()
        self.gate = None

    async def find(self, key_id: str) -> "KeyRecord | None":
        self.find_calls.append(key_id)
        try:
            if self.error is not None:
                raise self.error
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.records.get(key_id)

    async def aclose(self) -> None:
        self.closed += 1


def make_authenticator(store: FakeKeyStore | None = None, **kwargs: Any) -> "Authenticator":
    """An `Authenticator` over `store`, or over a fresh store that holds `TEST_KEY`."""
    held = store if store is not None else FakeKeyStore(TEST_KEY)
    authenticator: Authenticator = auth_module().Authenticator(held, **kwargs)
    return authenticator


RevokeResult = Literal["revoked", "already_revoked", "not_found"]


class FakeAdminStore:
    """The CLI's store (`KeyAdminStore`, D-092) in memory.

    `error` is raised by `create`, `revoke` and `list_keys` when set.
    """

    def __init__(
        self,
        *,
        revoke_result: RevokeResult = "revoked",
        infos: "Sequence[KeyInfo]" = (),
        error: BaseException | None = None,
    ) -> None:
        self.revoke_result: RevokeResult = revoke_result
        self.infos = list(infos)
        self.error = error
        self.created: list[tuple[str, str, bytes]] = []
        self.revoked: list[str] = []
        self.listed: list[str | None] = []
        self.closed = 0

    async def create(self, key_id: str, tenant_id: str, key_hash: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.created.append((key_id, tenant_id, key_hash))

    async def revoke(self, key_id: str) -> RevokeResult:
        if self.error is not None:
            raise self.error
        self.revoked.append(key_id)
        return self.revoke_result

    async def list_keys(self, tenant_id: str | None = None) -> "list[KeyInfo]":
        if self.error is not None:
            raise self.error
        self.listed.append(tenant_id)
        return list(self.infos)

    async def aclose(self) -> None:
        self.closed += 1


def key_info(
    key_id: str, tenant_id: str, created_at: datetime, revoked_at: datetime | None = None
) -> "KeyInfo":
    module = importlib.import_module("llmgateway.keystore")
    info: KeyInfo = module.KeyInfo(
        key_id=key_id, tenant_id=tenant_id, created_at=created_at, revoked_at=revoked_at
    )
    return info


CHILD_PRELUDE = """
import llmgateway.keystore as _keystore
from llmgateway.auth import KeyRecord, hash_key

_RECORDS = {{}}
for _key, _revoked in {keys!r}:
    _id = _key.split("_")[1]
    _RECORDS[_id] = KeyRecord(
        key_id=_id, tenant_id={tenant!r}, key_hash=hash_key(_key), revoked=_revoked
    )


class _FakeStore:
    def __init__(self, dsn, *, max_size=10):
        pass

    async def find(self, key_id):
        return _RECORDS.get(key_id)

    async def aclose(self):
        pass


_keystore.PostgresKeyStore = _FakeStore
"""


def child_prelude(revoked: Sequence[str] = (), *, tenant: str = TEST_TENANT) -> str:
    """Source that makes a child's `main` use an in-memory store holding `TEST_KEY`.

    Run it before `import llmgateway.main`: main looks `PostgresKeyStore` up on the
    `llmgateway.keystore` module when it is imported. `revoked` keys are held revoked.
    """
    keys = [(TEST_KEY, False), *[(key, True) for key in revoked]]
    return CHILD_PRELUDE.format(keys=keys, tenant=tenant)
