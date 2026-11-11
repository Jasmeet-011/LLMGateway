"""Tests for the key CLI, `llmgateway.keys` (D-092), over fakes, plus three real children.

`main(argv, *, environ, store_factory, migrate, stdout, stderr) -> int` is driven with an
in-memory `FakeAdminStore`, a recording `migrate` and two `StringIO`s, so every output is
compared exactly. Written from D-092's list of outputs, before the implementation exists.

These tests are plain functions, not coroutines: `main` is synchronous and runs its own
event loop for the store's async methods.

Children: `python -m llmgateway.keys` is the entry point an operator and Compose run, so
three real processes check that it wires the real pieces (an invalid tenant needs no
database; a missing URL stops with the configuration message; an unreachable database is
`database unavailable (<type>)`, for `list` through asyncpg and for `migrate` through
Alembic). None prints the URL or its password.
"""

import os
import re
import socket
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from io import StringIO

import pytest
from auth_support import FakeAdminStore, RevokeResult, key_info
from llmgateway.auth import KEY_PATTERN, KeyStoreUnavailable, hash_key, key_id_of
from llmgateway.keys import main

from llmgateway.config import ConfigError, database_url

DSN_MARKER = "planted-cli-password-marker"
DSN = f"postgresql://cli-user:{DSN_MARKER}@127.0.0.1:5432/cli-db"
ENVIRON = {"LLMGATEWAY_DATABASE_URL": DSN}
ID = "0123456789abcdef"
INVALID_TENANT = (
    "llmgateway.keys: invalid tenant: 1 to 64 characters from A-Z a-z 0-9 . _ -, "
    "starting with a letter or digit\n"
)
NO_SUCH_KEY = "llmgateway.keys: no such key\n"


class Run:
    def __init__(self) -> None:
        self.code = -1
        self.out = ""
        self.err = ""
        self.factory_dsns: list[str] = []
        self.migrations: list[str] = []


def run(
    *argv: str,
    environ: Mapping[str, str] = ENVIRON,
    store: FakeAdminStore | None = None,
    migrate_error: BaseException | None = None,
) -> Run:
    result = Run()
    held = store if store is not None else FakeAdminStore()
    stdout, stderr = StringIO(), StringIO()

    def factory(dsn: str) -> FakeAdminStore:
        result.factory_dsns.append(dsn)
        return held

    def migrate(dsn: str) -> None:
        result.migrations.append(dsn)
        if migrate_error is not None:
            raise migrate_error

    try:
        result.code = main(
            list(argv),
            environ=environ,
            store_factory=factory,
            migrate=migrate,
            stdout=stdout,
            stderr=stderr,
        )
    except SystemExit as exit_:
        # argparse's own errors: its exit code, written to the real stderr.
        assert isinstance(exit_.code, int)
        result.code = exit_.code
    result.out = stdout.getvalue()
    result.err = stderr.getvalue()
    return result


def _config_message(environ: Mapping[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        database_url(environ)
    return str(info.value)


# migrate


def test_migrate_runs_the_migration_with_the_url_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run("migrate")
    assert (result.code, result.out, result.err) == (0, "migrations applied\n", "")
    assert result.migrations == [DSN]
    assert result.factory_dsns == []
    assert capsys.readouterr() == ("", "")


def test_migrate_with_an_unreachable_database_is_database_unavailable() -> None:
    result = run("migrate", migrate_error=KeyStoreUnavailable("ConnectionRefusedError"))
    assert result.code == 1
    assert result.out == ""
    assert result.err == "llmgateway.keys: database unavailable (ConnectionRefusedError)\n"


def test_migrate_does_not_say_it_applied_anything_when_it_failed() -> None:
    result = run("migrate", migrate_error=KeyStoreUnavailable("OSError"))
    assert "applied" not in result.out + result.err


# create: the tenant


@pytest.mark.parametrize(
    "tenant",
    ["a", "A", "0", "tenant-1", "a.b_c-d", "0abc", "Acme.Corp_2", "x" * 64, "a" + "-" * 63],
)
def test_create_accepts_a_valid_tenant(tenant: str) -> None:
    store = FakeAdminStore()
    result = run("create", "--tenant", tenant, store=store)
    assert result.code == 0, result.err
    assert store.created[0][1] == tenant


@pytest.mark.parametrize(
    "tenant",
    [
        "",
        "-a",
        ".a",
        "_a",
        "a b",
        " a",
        "a ",
        "a/b",
        "a@b",
        "a:b",
        "a\n",
        "\na",
        "ä",
        "tenänt",
        "٣abc",
        "x" * 65,
        "x" * 128,
    ],
)
def test_create_rejects_an_invalid_tenant_before_touching_the_database(tenant: str) -> None:
    store = FakeAdminStore()
    result = run("create", f"--tenant={tenant}", store=store)
    assert result.code == 1
    assert result.out == ""
    assert result.err == INVALID_TENANT
    assert result.factory_dsns == []
    assert store.created == []
    assert result.migrations == []


def test_a_tenant_of_exactly_64_characters_is_accepted_and_65_is_not() -> None:
    assert run("create", "--tenant", "t" * 64).code == 0
    assert run("create", f"--tenant={'t' * 65}").code == 1


# create: the key


def test_create_prints_the_key_alone_on_stdout_and_the_note_on_stderr() -> None:
    store = FakeAdminStore()
    result = run("create", "--tenant", "acme", store=store)
    assert result.code == 0
    assert result.out.endswith("\n")
    assert result.out.count("\n") == 1
    key = result.out[:-1]
    assert KEY_PATTERN.fullmatch(key)
    key_id = key_id_of(key)
    assert key_id is not None
    assert result.err == (
        f"created key_id={key_id} tenant=acme; the key above is shown once and is not stored\n"
    )


def test_create_stores_the_id_the_tenant_and_the_hash_of_the_printed_key() -> None:
    store = FakeAdminStore()
    result = run("create", "--tenant", "acme", store=store)
    key = result.out[:-1]
    assert store.created == [(key_id_of(key), "acme", hash_key(key))]
    assert isinstance(store.created[0][2], bytes)
    assert len(store.created[0][2]) == 32


def test_create_never_stores_or_prints_the_key_beyond_stdout() -> None:
    store = FakeAdminStore()
    result = run("create", "--tenant", "acme", store=store)
    key = result.out[:-1]
    secret = key.split("_")[2]
    assert key not in result.err
    assert secret not in result.err
    assert hash_key(key).hex() not in result.out + result.err
    for stored in store.created[0]:
        assert key != stored
        if isinstance(stored, str):
            assert secret not in stored


def test_two_creates_make_two_different_keys() -> None:
    first = run("create", "--tenant", "acme").out
    second = run("create", "--tenant", "acme").out
    assert first != second


def test_create_uses_the_url_from_the_environment_and_closes_the_store(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeAdminStore()
    result = run("create", "--tenant", "acme", store=store)
    assert result.factory_dsns == [DSN]
    assert store.closed == 1
    assert result.migrations == []
    assert capsys.readouterr() == ("", "")


def test_create_with_the_database_down_prints_no_key_and_closes_the_store() -> None:
    store = FakeAdminStore(error=KeyStoreUnavailable("OSError"))
    result = run("create", "--tenant", "acme", store=store)
    assert result.code == 1
    assert result.out == ""
    assert result.err == "llmgateway.keys: database unavailable (OSError)\n"
    assert store.closed == 1


# revoke


def test_revoke_says_revoked_with_the_key_id() -> None:
    store = FakeAdminStore(revoke_result="revoked")
    result = run("revoke", ID, store=store)
    assert (result.code, result.out, result.err) == (0, f"revoked key_id={ID}\n", "")
    assert store.revoked == [ID]
    assert store.closed == 1


def test_revoke_of_a_revoked_key_says_already_revoked_and_exits_zero() -> None:
    store = FakeAdminStore(revoke_result="already_revoked")
    result = run("revoke", ID, store=store)
    assert (result.code, result.out, result.err) == (0, f"already revoked key_id={ID}\n", "")
    assert store.closed == 1


def test_revoke_of_an_id_with_no_row_is_no_such_key_and_exit_1() -> None:
    store = FakeAdminStore(revoke_result="not_found")
    result = run("revoke", ID, store=store)
    assert (result.code, result.out, result.err) == (1, "", NO_SUCH_KEY)
    assert store.revoked == [ID]
    assert store.closed == 1


@pytest.mark.parametrize(
    "key_id",
    [
        "",
        "0123456789abcde",
        "0123456789abcdef0",
        "0123456789ABCDEF",
        "0123456789abcdeg",
        "0123456789abcdef\n",
        " 0123456789abcdef",
        "lgw_0123456789abcdef_" + "ab" * 32,
        "٣" * 16,
        "../0123456789abcd",
    ],
)
def test_revoke_of_a_malformed_id_is_no_such_key_with_no_database_call(key_id: str) -> None:
    store = FakeAdminStore(revoke_result="revoked")
    result = run("revoke", key_id, store=store)
    assert (result.code, result.out, result.err) == (1, "", NO_SUCH_KEY)
    assert result.factory_dsns == []
    assert store.revoked == []


@pytest.mark.parametrize("result_name", ["revoked", "already_revoked", "not_found"])
def test_revoke_closes_the_store_whatever_it_answers(result_name: RevokeResult) -> None:
    store = FakeAdminStore(revoke_result=result_name)
    run("revoke", ID, store=store)
    assert store.closed == 1


def test_revoke_with_the_database_down_is_database_unavailable() -> None:
    store = FakeAdminStore(error=KeyStoreUnavailable("ConnectionRefusedError"))
    result = run("revoke", ID, store=store)
    assert result.code == 1
    assert result.out == ""
    assert result.err == "llmgateway.keys: database unavailable (ConnectionRefusedError)\n"
    assert store.closed == 1


# list

T1 = datetime(2026, 11, 11, 12, 30, 45, tzinfo=UTC)
T2 = datetime(2026, 11, 12, 0, 0, 0, tzinfo=UTC)


def test_list_writes_one_line_per_key_in_the_order_the_store_gives() -> None:
    store = FakeAdminStore(
        infos=[
            key_info("bbbbbbbbbbbbbbbb", "acme", T2),
            key_info("aaaaaaaaaaaaaaaa", "other", T1, T2),
        ]
    )
    result = run("list", store=store)
    assert result.code == 0
    assert result.err == ""
    assert result.out == (
        "bbbbbbbbbbbbbbbb tenant=acme created=2026-11-12T00:00:00+00:00 revoked=-\n"
        "aaaaaaaaaaaaaaaa tenant=other created=2026-11-11T12:30:45+00:00 "
        "revoked=2026-11-12T00:00:00+00:00\n"
    )
    assert store.listed == [None]
    assert store.closed == 1


def test_list_with_no_keys_prints_nothing_and_exits_zero() -> None:
    result = run("list")
    assert (result.code, result.out, result.err) == (0, "", "")


def test_list_with_a_tenant_asks_the_store_for_that_tenant() -> None:
    store = FakeAdminStore(infos=[key_info(ID, "acme", T1)])
    result = run("list", "--tenant", "acme", store=store)
    assert result.code == 0
    assert store.listed == ["acme"]
    assert result.out == f"{ID} tenant=acme created=2026-11-11T12:30:45+00:00 revoked=-\n"


def test_list_with_an_empty_tenant_asks_for_all_keys() -> None:
    store = FakeAdminStore()
    run("list", "--tenant=", store=store)
    assert store.listed == [None]


def test_list_converts_a_time_in_another_zone_to_utc() -> None:
    zone = timezone(timedelta(hours=-5))
    created = datetime(2026, 11, 11, 20, 0, 0, tzinfo=zone)
    revoked = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=9, minutes=30)))
    store = FakeAdminStore(infos=[key_info(ID, "acme", created, revoked)])
    result = run("list", store=store)
    assert result.out == (
        f"{ID} tenant=acme created=2026-11-12T01:00:00+00:00 revoked=2026-12-31T14:29:59+00:00\n"
    )


def test_list_writes_whole_seconds_only() -> None:
    created = datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC)
    store = FakeAdminStore(infos=[key_info(ID, "acme", created)])
    assert run("list", store=store).out == (
        f"{ID} tenant=acme created=2026-01-02T03:04:05+00:00 revoked=-\n"
    )


def test_list_with_the_database_down_is_database_unavailable() -> None:
    store = FakeAdminStore(error=KeyStoreUnavailable("OSError"))
    result = run("list", store=store)
    assert (result.code, result.out) == (1, "")
    assert result.err == "llmgateway.keys: database unavailable (OSError)\n"
    assert store.closed == 1


# Configuration errors

BAD_ENVIRONMENTS = [
    pytest.param({}, id="unset"),
    pytest.param({"LLMGATEWAY_DATABASE_URL": ""}, id="empty"),
    pytest.param({"LLMGATEWAY_DATABASE_URL": "   "}, id="blank"),
    pytest.param({"LLMGATEWAY_DATABASE_URL": f"mysql://u:{DSN_MARKER}@h/db"}, id="wrong_scheme"),
    pytest.param({"LLMGATEWAY_DATABASE_URL": f"postgresql://u:{DSN_MARKER}@/db"}, id="no_host"),
    pytest.param(
        {"LLMGATEWAY_DATABASE_URL": f"postgresql://u:{DSN_MARKER}@[::1/db"}, id="unparsable"
    ),
]
COMMANDS = [
    pytest.param(["migrate"], id="migrate"),
    pytest.param(["create", "--tenant", "acme"], id="create"),
    pytest.param(["revoke", ID], id="revoke"),
    pytest.param(["list"], id="list"),
]


@pytest.mark.parametrize("argv", COMMANDS)
@pytest.mark.parametrize("environ", BAD_ENVIRONMENTS)
def test_a_bad_database_url_is_a_configuration_error_and_nothing_is_called(
    argv: list[str], environ: dict[str, str]
) -> None:
    store = FakeAdminStore()
    result = run(*argv, environ=environ, store=store)
    message = _config_message(environ)
    assert result.code == 1
    assert result.out == ""
    assert result.err == f"llmgateway.keys: configuration error: {message}\n"
    assert "LLMGATEWAY_DATABASE_URL" in result.err
    assert DSN_MARKER not in result.err
    assert result.factory_dsns == []
    assert result.migrations == []
    assert (store.created, store.revoked, store.listed) == ([], [], [])


# Arguments (argparse's own errors, exit 2)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["create"], id="create_without_tenant"),
        pytest.param(["revoke"], id="revoke_without_id"),
        pytest.param(["frobnicate"], id="unknown_command"),
        pytest.param(["create", "--tenant", "a", "--nope"], id="unknown_option"),
        pytest.param(["list", "--tenant"], id="option_without_value"),
    ],
)
def test_an_argument_error_exits_with_2_and_touches_nothing(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeAdminStore()
    result = run(*argv, store=store)
    assert result.code == 2
    assert result.out == ""
    assert result.factory_dsns == []
    assert result.migrations == []
    assert store.created == []
    capsys.readouterr()


# The real entry point, in children


def _child(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k != "LLMGATEWAY_DATABASE_URL"}
    base.update(env)
    # The command is `python -m llmgateway.keys` with fixed arguments.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "llmgateway.keys", *args],
        env=base,
        capture_output=True,
        text=True,
        check=False,
    )


def _closed_port_dsn() -> str:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"postgresql://cli-user:{DSN_MARKER}@127.0.0.1:{port}/cli-db"


def test_the_real_entry_point_rejects_a_bad_tenant_without_a_database() -> None:
    result = _child("create", "--tenant=bad tenant", LLMGATEWAY_DATABASE_URL=_closed_port_dsn())
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == INVALID_TENANT


def test_the_real_entry_point_without_a_url_is_the_configuration_error() -> None:
    result = _child("list")
    message = _config_message({})
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"llmgateway.keys: configuration error: {message}\n"


@pytest.mark.parametrize("command", ["list", "migrate"])
def test_the_real_entry_point_with_an_unreachable_database_is_database_unavailable(
    command: str,
) -> None:
    result = _child(command, LLMGATEWAY_DATABASE_URL=_closed_port_dsn())
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert re.fullmatch(r"llmgateway\.keys: database unavailable \(\w+\)\n", result.stderr), (
        result.stderr
    )
    assert DSN_MARKER not in result.stderr
