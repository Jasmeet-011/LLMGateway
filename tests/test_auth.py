"""Tests for `llmgateway.auth`: the key format, the hash, the header, the Authenticator.

Written from PLAN.md's M14 proof and D-086 to D-092, before the implementation exists.
The middleware and the one 401 are in `tests/test_auth_app.py`; the real driver is in
`tests/test_keystore.py`.

The Authenticator is driven over `FakeKeyStore` with an injected clock, so the cache's
arithmetic is exact: an entry's expiry is the clock at insert plus the cache time, and it
is used only while `clock() < expiry`. Hangs run on fake time, with the technique of
`tests/stream_support.py` (the event loop's clock is the test's clock), so a lookup
timeout fires on an exact fake second and no test waits on real time.

`hmac.compare_digest` and `auth.hash_key` are spied on, because "always compare, exactly
once, even for an unknown id" and "never hash a malformed key" (D-088) cannot be seen
from the result alone.
"""

import asyncio
import dataclasses
import hashlib
import hmac
import inspect
import re
import secrets
from collections.abc import Sequence
from typing import Any

import llmgateway.auth as auth
import pytest
from auth_support import (
    TEST_KEY,
    TEST_KEY_ID,
    TEST_SECRET,
    TEST_TENANT,
    FakeKeyStore,
    key_id,
    make_authenticator,
    make_key,
    with_secret,
)
from llmgateway.auth import (
    AuthUnavailable,
    KeyRecord,
    KeyStoreUnavailable,
    NewKey,
    Principal,
    bearer_key,
    generate_key,
    hash_key,
    key_id_of,
)
from stream_support import FakeClock, at_about, pass_time, until

Headers = Sequence[tuple[bytes, bytes]]

# The SHA-256 of TEST_KEY's 85 ASCII characters, computed once with hashlib and written out,
# so a hash of only the secret, or of the id, cannot match it.
TEST_KEY_SHA256 = hashlib.sha256(TEST_KEY.encode("ascii")).hexdigest()

MALFORMED_KEYS = [
    pytest.param("", id="empty"),
    pytest.param("lgw_", id="prefix_only"),
    pytest.param(TEST_KEY.upper(), id="all_uppercase"),
    pytest.param(f"lgw_{TEST_KEY_ID.upper()}_{TEST_SECRET}", id="uppercase_id"),
    pytest.param(f"lgw_{TEST_KEY_ID}_{TEST_SECRET.upper()}", id="uppercase_secret"),
    pytest.param(TEST_KEY + " ", id="trailing_space"),
    pytest.param(" " + TEST_KEY, id="leading_space"),
    pytest.param(TEST_KEY + "\n", id="trailing_newline"),
    pytest.param(TEST_KEY + "\r\n", id="trailing_crlf"),
    pytest.param(TEST_KEY + "\t", id="trailing_tab"),
    pytest.param(TEST_KEY[:-1], id="secret_one_short"),
    pytest.param(TEST_KEY + "0", id="secret_one_long"),
    pytest.param(f"lgw_{TEST_KEY_ID[:-1]}_{TEST_SECRET}", id="id_one_short"),
    pytest.param(f"lgw_{TEST_KEY_ID}0_{TEST_SECRET}", id="id_one_long"),
    pytest.param("LGW_" + TEST_KEY[4:], id="prefix_uppercase"),
    pytest.param("lgx_" + TEST_KEY[4:], id="prefix_wrong"),
    pytest.param("sk_" + TEST_KEY[4:], id="prefix_other"),
    pytest.param(TEST_KEY[4:], id="no_prefix"),
    pytest.param(f"lgw-{TEST_KEY_ID}_{TEST_SECRET}", id="dash_after_prefix"),
    pytest.param(f"lgw_{TEST_KEY_ID}-{TEST_SECRET}", id="dash_as_separator"),
    pytest.param(f"lgw_{TEST_KEY_ID}{TEST_SECRET}", id="no_separator"),
    pytest.param(f"lgw_{TEST_KEY_ID}_{TEST_SECRET[:-1]}g", id="non_hex_secret_character"),
    pytest.param(f"lgw_{TEST_KEY_ID[:-1]}g_{TEST_SECRET}", id="non_hex_id_character"),
    pytest.param(f"lgw_{TEST_KEY_ID}_{'٣' * 64}", id="unicode_digits_in_the_secret"),
    pytest.param(f"lgw_{'٣' * 16}_{TEST_SECRET}", id="unicode_digits_in_the_id"),
    pytest.param(TEST_KEY + "_" + TEST_SECRET, id="a_second_secret"),
    pytest.param(TEST_KEY + TEST_KEY, id="two_keys_joined"),
    pytest.param(f"lgw_{TEST_KEY_ID}_", id="empty_secret"),
    pytest.param(f"lgw__{TEST_SECRET}", id="empty_id"),
]


def _headers(*values: bytes, extra: Headers = ()) -> list[tuple[bytes, bytes]]:
    return [(b"host", b"test"), *extra, *[(b"authorization", value) for value in values]]


class CompareSpy:
    """Records every `hmac.compare_digest` call and answers as the real one does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[Any, Any]] = []
        original = hmac.compare_digest

        def spy(a: Any, b: Any) -> bool:
            self.calls.append((a, b))
            return original(a, b)

        monkeypatch.setattr(hmac, "compare_digest", spy)


class HashSpy:
    """Records every key `auth.hash_key` is asked to hash."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.keys: list[str] = []
        original = auth.hash_key

        def spy(key: str) -> bytes:
            self.keys.append(key)
            return original(key)

        monkeypatch.setattr(auth, "hash_key", spy)


# The constants and the signature (D-090, D-091)


def test_the_constants_are_the_documented_ones() -> None:
    assert auth.DEFAULT_CACHE_SECONDS == 10.0
    assert auth.LOOKUP_TIMEOUT_SECONDS == 2.0
    assert auth.MAX_CACHE_ENTRIES == 10_000
    assert isinstance(auth.KEY_PATTERN, re.Pattern)
    assert auth.KEY_PATTERN.pattern == r"lgw_([0-9a-f]{16})_[0-9a-f]{64}"


def test_the_authenticator_defaults_are_10_seconds_2_seconds_and_10000_entries() -> None:
    parameters = inspect.signature(auth.Authenticator).parameters
    assert parameters["cache_seconds"].default == 10.0
    assert parameters["lookup_timeout"].default == 2.0
    assert parameters["max_entries"].default == 10_000
    for name in ("cache_seconds", "lookup_timeout", "clock", "max_entries"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name


# generate_key


def test_generate_key_makes_a_well_formed_key_whose_id_is_its_second_part() -> None:
    new = generate_key()
    assert isinstance(new, NewKey)
    assert re.fullmatch(r"lgw_[0-9a-f]{16}_[0-9a-f]{64}", new.key)
    assert new.key_id == new.key.split("_")[1]
    assert len(new.key) == 85


def test_generate_key_makes_a_different_key_each_time() -> None:
    keys = {generate_key().key for _ in range(20)}
    assert len(keys) == 20


def test_generate_key_uses_8_random_bytes_for_the_id_then_32_for_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[int] = []

    def fake_token_hex(nbytes: int) -> str:
        sizes.append(nbytes)
        return ("ab" if len(sizes) == 1 else "cd") * nbytes

    monkeypatch.setattr(secrets, "token_hex", fake_token_hex)
    new = generate_key()
    assert sizes == [8, 32]
    assert new.key == f"lgw_{'ab' * 8}_{'cd' * 32}"
    assert new.key_id == "ab" * 8


def test_a_new_key_leaves_the_key_out_of_its_repr() -> None:
    new = generate_key()
    text = repr(new) + str(new)
    assert new.key not in text
    assert new.key.split("_")[2] not in text


def test_a_new_key_is_frozen() -> None:
    new = generate_key()
    with pytest.raises(dataclasses.FrozenInstanceError):
        new.key = "x"  # type: ignore[misc]


# hash_key and key_id_of


def test_hash_key_is_sha256_of_the_whole_key_as_ascii() -> None:
    digest = hash_key(TEST_KEY)
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert digest.hex() == TEST_KEY_SHA256
    assert digest == hashlib.sha256(TEST_KEY.encode("ascii")).digest()


def test_hash_key_is_not_the_hash_of_the_secret_alone_or_of_the_id() -> None:
    assert hash_key(TEST_KEY) != hashlib.sha256(TEST_SECRET.encode("ascii")).digest()
    assert hash_key(TEST_KEY) != hashlib.sha256(TEST_KEY_ID.encode("ascii")).digest()


def test_hash_key_differs_when_one_character_differs() -> None:
    assert hash_key(TEST_KEY) != hash_key(TEST_KEY[:-1] + "0")


def test_key_id_of_a_well_formed_key_is_its_id() -> None:
    assert key_id_of(TEST_KEY) == TEST_KEY_ID
    assert key_id_of(make_key(255)) == "00000000000000ff"


@pytest.mark.parametrize("key", MALFORMED_KEYS)
def test_key_id_of_a_malformed_key_is_none(key: str) -> None:
    assert key_id_of(key) is None


# bearer_key: the Authorization header (D-089)


def test_bearer_key_returns_the_key_of_a_bearer_header() -> None:
    assert bearer_key(_headers(f"Bearer {TEST_KEY}".encode())) == TEST_KEY


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr", "bEARER"])
def test_bearer_key_ignores_the_case_of_the_scheme(scheme: str) -> None:
    assert bearer_key(_headers(f"{scheme} {TEST_KEY}".encode())) == TEST_KEY


def test_bearer_key_ignores_other_headers_around_the_authorization_header() -> None:
    headers = [
        (b"host", b"test"),
        (b"content-type", b"application/json"),
        (b"authorization", f"Bearer {TEST_KEY}".encode()),
        (b"accept", b"*/*"),
    ]
    assert bearer_key(headers) == TEST_KEY


def test_bearer_key_with_no_headers_at_all_is_none() -> None:
    assert bearer_key([]) is None


def test_bearer_key_with_no_authorization_header_is_none() -> None:
    assert bearer_key(_headers()) is None


def test_bearer_key_with_two_authorization_headers_is_none() -> None:
    value = f"Bearer {TEST_KEY}".encode()
    other = f"Bearer {make_key(7)}".encode()
    assert bearer_key(_headers(value, other)) is None
    assert bearer_key(_headers(other, value)) is None


def test_bearer_key_with_two_identical_authorization_headers_is_none() -> None:
    value = f"Bearer {TEST_KEY}".encode()
    assert bearer_key(_headers(value, value)) is None


def test_bearer_key_with_three_authorization_headers_is_none() -> None:
    value = f"Bearer {TEST_KEY}".encode()
    assert bearer_key(_headers(value, value, value)) is None


def test_bearer_key_never_reads_x_api_key() -> None:
    assert bearer_key(_headers(extra=[(b"x-api-key", TEST_KEY.encode())])) is None


def test_bearer_key_ignores_x_api_key_next_to_a_good_authorization_header() -> None:
    other = make_key(9).encode()
    headers = _headers(f"Bearer {TEST_KEY}".encode(), extra=[(b"x-api-key", other)])
    assert bearer_key(headers) == TEST_KEY


@pytest.mark.parametrize(
    "scheme", ["Basic", "Token", "Digest", "Bearer2", "Bear", "Bearerx", "xBearer", "ApiKey", ""]
)
def test_bearer_key_with_another_scheme_is_none(scheme: str) -> None:
    assert bearer_key(_headers(f"{scheme} {TEST_KEY}".encode())) is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(TEST_KEY.encode(), id="the_key_alone"),
        pytest.param(b"Bearer", id="scheme_alone"),
        pytest.param(b"Bearer ", id="scheme_and_a_space_only"),
        pytest.param(b"", id="empty"),
        pytest.param(b" ", id="one_space"),
        pytest.param(b" Bearer " + TEST_KEY.encode(), id="leading_space"),
        pytest.param(b"Bearer  " + TEST_KEY.encode(), id="two_spaces"),
        pytest.param(b"Bearer   " + TEST_KEY.encode(), id="three_spaces"),
        pytest.param(b"Bearer " + TEST_KEY.encode() + b" ", id="trailing_space"),
        pytest.param(b"Bearer " + TEST_KEY.encode() + b"\n", id="trailing_newline"),
        pytest.param(b"Bearer " + TEST_KEY.encode() + b"\r\n", id="trailing_crlf"),
        pytest.param(b"Bearer " + TEST_KEY.encode() + b"\t", id="trailing_tab"),
        pytest.param(b"Bearer\t" + TEST_KEY.encode(), id="tab_instead_of_the_space"),
        pytest.param(b"Bearer\xa0" + TEST_KEY.encode(), id="no_break_space_instead"),
        pytest.param(b"Bearer, " + TEST_KEY.encode(), id="comma_after_the_scheme"),
        pytest.param(b"Bearer " + TEST_KEY.upper().encode(), id="uppercase_key"),
        pytest.param(b"Bearer " + TEST_KEY[:-1].encode(), id="secret_one_short"),
        pytest.param(b"Bearer " + TEST_KEY.encode() + b"0", id="secret_one_long"),
        pytest.param(b"Bearer lgx_" + TEST_KEY[4:].encode(), id="wrong_prefix"),
        pytest.param(b"Bearer " + TEST_KEY[4:].encode(), id="no_prefix"),
        pytest.param(b"Bearer " + TEST_KEY.encode() + b" x", id="more_after_the_key"),
        pytest.param(b"Bearer x " + TEST_KEY.encode(), id="more_before_the_key"),
        pytest.param(b"Bearer " + TEST_KEY.encode()[:-1] + b"\xff", id="non_ascii_last_byte"),
        pytest.param(b"Bearer \xff\xfe" + TEST_KEY.encode(), id="non_ascii_before_the_key"),
        pytest.param(b"Bearer \xe9", id="latin_1_letter"),
    ],
)
def test_bearer_key_with_any_other_shape_of_value_is_none(value: bytes) -> None:
    assert bearer_key(_headers(value)) is None


def test_bearer_key_splits_at_the_first_space_only() -> None:
    # "Bearer" + " " + "x " + KEY: the rest is "x <key>", which is not a key.
    assert bearer_key(_headers(b"Bearer x " + TEST_KEY.encode())) is None
    # A key with a space inside it is not a key either: nothing is stripped or rejoined.
    assert (
        bearer_key(_headers(b"Bearer " + TEST_KEY[:30].encode() + b" " + TEST_KEY[30:].encode()))
        is None
    )


# The row and principal types


def test_a_key_record_leaves_the_hash_out_of_its_repr() -> None:
    digest = hash_key(TEST_KEY)
    record = KeyRecord(key_id=TEST_KEY_ID, tenant_id=TEST_TENANT, key_hash=digest, revoked=False)
    text = repr(record) + str(record)
    assert digest.hex() not in text
    assert repr(digest) not in text
    assert str(digest) not in text
    assert TEST_KEY_ID in text
    assert TEST_TENANT in text


def test_a_key_record_and_a_principal_are_frozen() -> None:
    record = KeyRecord(key_id=TEST_KEY_ID, tenant_id="t", key_hash=b"\x00" * 32, revoked=False)
    principal = Principal(key_id=TEST_KEY_ID, tenant_id="t")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.revoked = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.tenant_id = "other"  # type: ignore[misc]


def test_a_principal_is_its_key_id_and_tenant() -> None:
    principal = Principal(key_id=TEST_KEY_ID, tenant_id=TEST_TENANT)
    assert (principal.key_id, principal.tenant_id) == (TEST_KEY_ID, TEST_TENANT)
    assert principal == Principal(TEST_KEY_ID, TEST_TENANT)
    assert principal != Principal(TEST_KEY_ID, "other")


def test_key_store_unavailable_keeps_the_error_type_it_was_given() -> None:
    error = KeyStoreUnavailable("ConnectionRefusedError")
    assert isinstance(error, Exception)
    assert error.error_type == "ConnectionRefusedError"


# Authenticator: the verdicts


@pytest.mark.asyncio
async def test_a_valid_key_is_the_principal_of_its_row() -> None:
    store = FakeKeyStore()
    store.add(TEST_KEY, tenant="acme")
    principal = await make_authenticator(store).authenticate(TEST_KEY)
    assert principal == Principal(key_id=TEST_KEY_ID, tenant_id="acme")
    assert type(principal) is Principal
    assert store.find_calls == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_an_unknown_key_id_is_none() -> None:
    store = FakeKeyStore(TEST_KEY)
    assert await make_authenticator(store).authenticate(make_key(3)) is None
    assert store.find_calls == [key_id(make_key(3))]


@pytest.mark.asyncio
async def test_a_wrong_secret_for_a_known_id_is_none() -> None:
    store = FakeKeyStore(TEST_KEY)
    wrong = with_secret(TEST_KEY, "f" * 64)
    assert await make_authenticator(store).authenticate(wrong) is None
    assert store.find_calls == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_secret_that_differs_in_the_last_character_only_is_none() -> None:
    store = FakeKeyStore(TEST_KEY)
    wrong = with_secret(TEST_KEY, TEST_SECRET[:-1] + "e")
    assert wrong != TEST_KEY
    assert await make_authenticator(store).authenticate(wrong) is None


@pytest.mark.asyncio
async def test_a_revoked_key_with_the_right_secret_is_none() -> None:
    store = FakeKeyStore()
    store.add(TEST_KEY, revoked=True)
    assert await make_authenticator(store).authenticate(TEST_KEY) is None
    assert store.find_calls == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_row_for_another_id_is_not_used_for_this_key() -> None:
    store = FakeKeyStore(make_key(5))
    assert await make_authenticator(store).authenticate(TEST_KEY) is None


@pytest.mark.asyncio
async def test_no_key_is_none_without_a_lookup() -> None:
    store = FakeKeyStore(TEST_KEY)
    assert await make_authenticator(store).authenticate(None) is None
    assert store.find_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", MALFORMED_KEYS)
async def test_a_malformed_key_is_none_with_no_lookup_no_hash_and_no_compare(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    compares = CompareSpy(monkeypatch)
    store = FakeKeyStore(TEST_KEY)  # built first: its record is hashed by hash_key too
    hashes = HashSpy(monkeypatch)
    assert await make_authenticator(store).authenticate(key) is None
    assert store.find_calls == []
    assert compares.calls == []
    assert hashes.keys == []


@pytest.mark.asyncio
async def test_a_key_that_matches_a_stored_row_in_every_but_case_is_none() -> None:
    store = FakeKeyStore(TEST_KEY)
    assert await make_authenticator(store).authenticate(TEST_KEY.upper()) is None
    assert store.find_calls == []


# Authenticator: the compare (D-088)


@pytest.mark.asyncio
async def test_a_valid_key_is_compared_once_digest_against_the_stored_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compares = CompareSpy(monkeypatch)
    store = FakeKeyStore(TEST_KEY)  # built first: its record is hashed by hash_key too
    hashes = HashSpy(monkeypatch)
    assert await make_authenticator(store).authenticate(TEST_KEY) is not None
    assert compares.calls == [(hash_key(TEST_KEY), hash_key(TEST_KEY))]
    assert hashes.keys == [TEST_KEY]


@pytest.mark.asyncio
async def test_a_wrong_secret_is_compared_once_with_the_stored_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compares = CompareSpy(monkeypatch)
    store = FakeKeyStore(TEST_KEY)
    wrong = with_secret(TEST_KEY, "f" * 64)
    assert await make_authenticator(store).authenticate(wrong) is None
    assert len(compares.calls) == 1
    assert sorted(compares.calls[0]) == sorted([hash_key(wrong), hash_key(TEST_KEY)])


@pytest.mark.asyncio
async def test_a_revoked_key_is_compared_once_and_refused_after_the_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compares = CompareSpy(monkeypatch)
    store = FakeKeyStore()
    store.add(TEST_KEY, revoked=True)
    assert await make_authenticator(store).authenticate(TEST_KEY) is None
    assert compares.calls == [(hash_key(TEST_KEY), hash_key(TEST_KEY))]


@pytest.mark.asyncio
async def test_an_unknown_id_is_still_compared_once_against_a_32_byte_dummy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compares = CompareSpy(monkeypatch)
    store = FakeKeyStore(TEST_KEY)  # built first: its record is hashed by hash_key too
    hashes = HashSpy(monkeypatch)
    unknown = make_key(3)
    assert await make_authenticator(store).authenticate(unknown) is None
    assert hashes.keys == [unknown]
    assert len(compares.calls) == 1
    first, second = compares.calls[0]
    assert isinstance(first, bytes) and isinstance(second, bytes)
    assert hash_key(unknown) in (first, second)
    dummy = second if first == hash_key(unknown) else first
    assert len(dummy) == 32
    # The dummy is not the hash of this key or of the stored one.
    assert dummy != hash_key(unknown)
    assert dummy != hash_key(TEST_KEY)


@pytest.mark.asyncio
async def test_the_dummy_is_the_same_fixed_value_for_every_unknown_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compares = CompareSpy(monkeypatch)
    authenticator = make_authenticator(FakeKeyStore())
    assert await authenticator.authenticate(make_key(3)) is None
    assert await authenticator.authenticate(make_key(4)) is None
    assert len(compares.calls) == 2
    dummies = []
    for (first, second), key in zip(compares.calls, (make_key(3), make_key(4)), strict=True):
        dummies.append(second if first == hash_key(key) else first)
    assert dummies[0] == dummies[1]
    assert len(dummies[0]) == 32


@pytest.mark.asyncio
async def test_a_cached_row_is_still_compared_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compares = CompareSpy(monkeypatch)
    authenticator = make_authenticator(FakeKeyStore(TEST_KEY))
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert len(compares.calls) == 3


@pytest.mark.asyncio
async def test_a_wrong_secret_for_a_cached_id_is_refused() -> None:
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store)
    assert await authenticator.authenticate(TEST_KEY) is not None
    wrong = with_secret(TEST_KEY, "f" * 64)
    assert await authenticator.authenticate(wrong) is None
    # The refusal came from the cached row: the store was asked once, for the first key.
    assert store.find_calls == [TEST_KEY_ID]
    assert await authenticator.authenticate(TEST_KEY) is not None


@pytest.mark.asyncio
async def test_a_cached_revoked_row_still_refuses_the_right_secret() -> None:
    store = FakeKeyStore()
    store.add(TEST_KEY, revoked=True)
    authenticator = make_authenticator(store)
    assert await authenticator.authenticate(TEST_KEY) is None
    assert await authenticator.authenticate(TEST_KEY) is None
    assert store.find_calls == [TEST_KEY_ID]


# Authenticator: the cache (D-090)


@pytest.mark.asyncio
async def test_a_found_row_is_used_again_within_the_cache_time() -> None:
    clock = FakeClock()
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now += 9.0
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now = 110.0 - 1e-6
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert store.find_calls == [TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_cached_row_expires_exactly_at_insert_time_plus_the_cache_time() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock, cache_seconds=10.0)
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now = 110.0
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert store.find_calls == [TEST_KEY_ID, TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_hit_does_not_extend_the_entrys_life() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock, cache_seconds=10.0)
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now = 105.0
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now = 110.0
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert store.find_calls == [TEST_KEY_ID, TEST_KEY_ID]


@pytest.mark.asyncio
async def test_the_cache_time_is_the_one_given() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock, cache_seconds=2.5)
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now = 102.4
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert len(store.find_calls) == 1
    clock.now = 102.5
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert len(store.find_calls) == 2


@pytest.mark.asyncio
async def test_an_entry_is_stamped_at_insert_and_not_at_construction() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock, cache_seconds=10.0)
    clock.now = 500.0
    assert await authenticator.authenticate(TEST_KEY) is not None
    clock.now = 509.0
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert len(store.find_calls) == 1


@pytest.mark.asyncio
async def test_with_a_cache_time_of_zero_every_request_reads_the_store() -> None:
    clock = FakeClock()
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock, cache_seconds=0)
    for _ in range(3):
        assert await authenticator.authenticate(TEST_KEY) is not None
    assert store.find_calls == [TEST_KEY_ID] * 3


@pytest.mark.asyncio
async def test_a_cache_time_of_zero_makes_a_revocation_visible_at_once() -> None:
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, cache_seconds=0)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.revoke(TEST_KEY)
    assert await authenticator.authenticate(TEST_KEY) is None


@pytest.mark.asyncio
async def test_a_missing_id_is_never_cached_so_a_new_key_works_at_once() -> None:
    store = FakeKeyStore()
    authenticator = make_authenticator(store)
    assert await authenticator.authenticate(TEST_KEY) is None
    assert await authenticator.authenticate(TEST_KEY) is None
    assert store.find_calls == [TEST_KEY_ID, TEST_KEY_ID]
    store.add(TEST_KEY)
    assert await authenticator.authenticate(TEST_KEY) == Principal(TEST_KEY_ID, TEST_TENANT)


@pytest.mark.asyncio
async def test_a_revocation_is_not_seen_while_the_row_is_cached() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.revoke(TEST_KEY)
    clock.now = 109.9
    assert await authenticator.authenticate(TEST_KEY) is not None


@pytest.mark.asyncio
async def test_a_revocation_is_seen_once_the_cached_row_has_expired() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.revoke(TEST_KEY)
    clock.now = 110.0
    assert await authenticator.authenticate(TEST_KEY) is None


@pytest.mark.asyncio
async def test_a_revocation_is_seen_at_once_by_a_key_that_was_never_cached() -> None:
    store = FakeKeyStore()
    store.add(TEST_KEY, revoked=True)
    assert await make_authenticator(store).authenticate(TEST_KEY) is None


@pytest.mark.asyncio
async def test_a_removed_row_is_not_found_after_the_cached_one_expires() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.remove(TEST_KEY)
    clock.now = 110.0
    assert await authenticator.authenticate(TEST_KEY) is None


@pytest.mark.asyncio
async def test_each_key_id_has_its_own_cache_entry() -> None:
    store = FakeKeyStore(make_key(1), make_key(2))
    authenticator = make_authenticator(store)
    assert await authenticator.authenticate(make_key(1)) is not None
    assert await authenticator.authenticate(make_key(2)) is not None
    assert await authenticator.authenticate(make_key(1)) is not None
    assert await authenticator.authenticate(make_key(2)) is not None
    assert store.find_calls == [key_id(make_key(1)), key_id(make_key(2))]


@pytest.mark.asyncio
async def test_a_full_cache_drops_the_entry_inserted_first() -> None:
    store = FakeKeyStore(make_key(1), make_key(2), make_key(3))
    authenticator = make_authenticator(store, max_entries=2)
    for n in (1, 2, 3):
        assert await authenticator.authenticate(make_key(n)) is not None
    assert len(store.find_calls) == 3
    # 2 and 3 are cached, 1 was dropped when 3 went in.
    assert await authenticator.authenticate(make_key(2)) is not None
    assert await authenticator.authenticate(make_key(3)) is not None
    assert len(store.find_calls) == 3
    assert await authenticator.authenticate(make_key(1)) is not None
    assert store.find_calls[3:] == [key_id(make_key(1))]


@pytest.mark.asyncio
async def test_a_full_cache_drops_the_oldest_inserted_and_not_the_least_recently_used() -> None:
    store = FakeKeyStore(make_key(1), make_key(2), make_key(3))
    authenticator = make_authenticator(store, max_entries=2)
    assert await authenticator.authenticate(make_key(1)) is not None
    assert await authenticator.authenticate(make_key(2)) is not None
    # A hit on 1 does not make it newer.
    assert await authenticator.authenticate(make_key(1)) is not None
    assert await authenticator.authenticate(make_key(3)) is not None
    assert len(store.find_calls) == 3
    # 1 was the oldest inserted, so it is the one gone.
    assert await authenticator.authenticate(make_key(2)) is not None
    assert len(store.find_calls) == 3
    assert await authenticator.authenticate(make_key(1)) is not None
    assert len(store.find_calls) == 4


@pytest.mark.asyncio
async def test_the_cache_never_holds_more_than_max_entries() -> None:
    store = FakeKeyStore(*[make_key(n) for n in range(1, 6)])
    authenticator = make_authenticator(store, max_entries=3)
    for n in range(1, 6):
        assert await authenticator.authenticate(make_key(n)) is not None
    assert len(store.find_calls) == 5
    # 3, 4 and 5 are in; 1 and 2 are out.
    for n in (3, 4, 5):
        assert await authenticator.authenticate(make_key(n)) is not None
    assert len(store.find_calls) == 5
    for n in (1, 2):
        assert await authenticator.authenticate(make_key(n)) is not None
    assert len(store.find_calls) == 7


@pytest.mark.asyncio
async def test_unknown_ids_never_fill_the_cache() -> None:
    store = FakeKeyStore(make_key(1), make_key(2))
    authenticator = make_authenticator(store, max_entries=2)
    assert await authenticator.authenticate(make_key(1)) is not None
    assert await authenticator.authenticate(make_key(2)) is not None
    for n in range(100, 110):
        assert await authenticator.authenticate(make_key(n)) is None
    # Both real keys are still cached: the made-up ids pushed nothing out.
    before = len(store.find_calls)
    assert await authenticator.authenticate(make_key(1)) is not None
    assert await authenticator.authenticate(make_key(2)) is not None
    assert len(store.find_calls) == before


@pytest.mark.asyncio
async def test_two_concurrent_requests_for_an_uncached_id_both_read_the_store() -> None:
    # There is no single flight (D-090).
    store = FakeKeyStore(TEST_KEY)
    store.hang()
    authenticator = make_authenticator(store)
    first = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    second = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    await until(lambda: len(store.find_calls) == 2, "both lookups to reach the store")
    store.release()
    assert await first == Principal(TEST_KEY_ID, TEST_TENANT)
    assert await second == Principal(TEST_KEY_ID, TEST_TENANT)
    assert store.find_calls == [TEST_KEY_ID, TEST_KEY_ID]


# Authenticator: Postgres down or slow (D-091)


async def _unavailable(authenticator: Any, key: str = TEST_KEY) -> AuthUnavailable:
    with pytest.raises(AuthUnavailable) as info:
        await authenticator.authenticate(key)
    error: AuthUnavailable = info.value
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["OSError", "ConnectionRefusedError", "InterfaceError"])
async def test_a_store_that_cannot_connect_is_auth_unavailable_store_error(
    error_type: str,
) -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable(error_type)
    error = await _unavailable(make_authenticator(store))
    assert error.reason == "store_error"
    assert error.error_type == error_type


@pytest.mark.asyncio
async def test_a_store_error_for_an_unknown_id_is_the_same_auth_unavailable() -> None:
    store = FakeKeyStore()
    store.unavailable("OSError")
    error = await _unavailable(make_authenticator(store), make_key(3))
    assert (error.reason, error.error_type) == ("store_error", "OSError")


@pytest.mark.asyncio
async def test_a_store_bug_propagates_unchanged() -> None:
    bug = RuntimeError("store-bug-marker")
    store = FakeKeyStore(TEST_KEY)
    store.fail_with(bug)
    with pytest.raises(RuntimeError) as info:
        await make_authenticator(store).authenticate(TEST_KEY)
    assert info.value is bug


@pytest.mark.asyncio
async def test_a_store_bug_that_is_a_value_error_is_not_turned_into_auth_unavailable() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.fail_with(ValueError("x"))
    with pytest.raises(ValueError, match="x"):
        await make_authenticator(store).authenticate(TEST_KEY)


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_cached_and_the_next_one_recovers() -> None:
    store = FakeKeyStore(TEST_KEY)
    store.unavailable()
    authenticator = make_authenticator(store)
    await _unavailable(authenticator)
    store.fail_with(None)
    assert await authenticator.authenticate(TEST_KEY) == Principal(TEST_KEY_ID, TEST_TENANT)
    assert store.find_calls == [TEST_KEY_ID, TEST_KEY_ID]


@pytest.mark.asyncio
async def test_a_lookup_that_hangs_is_cut_off_at_the_lookup_timeout_and_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)
    store = FakeKeyStore(TEST_KEY)
    store.hang()
    authenticator = make_authenticator(store, clock=clock, lookup_timeout=2.0)
    task = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    await until(lambda: store.find_calls == [TEST_KEY_ID], "the lookup to start", task=task)
    start = clock.now
    # A tick short of the limit it is still waiting.
    await pass_time(clock, 1.75)
    assert not task.done()
    assert store.cancelled == 0
    for _ in range(8):
        if task.done():
            break
        await pass_time(clock, 0.25)
    await until(task.done, "the lookup timeout to fire")
    assert at_about(clock.now - start, 2.0), clock.now - start
    with pytest.raises(AuthUnavailable) as info:
        task.result()
    assert info.value.reason == "timeout"
    assert info.value.error_type == "TimeoutError"
    assert store.cancelled == 1


@pytest.mark.asyncio
async def test_the_lookup_timeout_is_the_one_given(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)
    store = FakeKeyStore(TEST_KEY)
    store.hang()
    authenticator = make_authenticator(store, clock=clock, lookup_timeout=5.0)
    task = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    await until(lambda: store.find_calls == [TEST_KEY_ID], "the lookup to start", task=task)
    start = clock.now
    await pass_time(clock, 4.75)
    assert not task.done()
    for _ in range(8):
        if task.done():
            break
        await pass_time(clock, 0.25)
    await until(task.done, "the lookup timeout to fire")
    assert at_about(clock.now - start, 5.0), clock.now - start
    assert isinstance(task.exception(), AuthUnavailable)


@pytest.mark.asyncio
async def test_a_slow_lookup_that_answers_in_time_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)
    store = FakeKeyStore(TEST_KEY)
    store.hang()
    authenticator = make_authenticator(store, clock=clock, lookup_timeout=2.0)
    task = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    await until(lambda: store.find_calls == [TEST_KEY_ID], "the lookup to start", task=task)
    await pass_time(clock, 1.5)
    assert not task.done()
    store.release()
    await until(task.done, "the answer to be used")
    assert task.result() == Principal(TEST_KEY_ID, TEST_TENANT)
    assert store.cancelled == 0


@pytest.mark.asyncio
async def test_a_row_still_cached_is_served_while_the_store_is_down() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.unavailable()
    clock.now = 109.0
    assert await authenticator.authenticate(TEST_KEY) == Principal(TEST_KEY_ID, TEST_TENANT)
    assert len(store.find_calls) == 1


@pytest.mark.asyncio
async def test_an_expired_row_is_not_served_while_the_store_is_down() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.unavailable("OSError")
    clock.now = 110.0
    error = await _unavailable(authenticator)
    assert (error.reason, error.error_type) == ("store_error", "OSError")


@pytest.mark.asyncio
async def test_an_expired_row_is_dropped_and_not_served_again_after_a_failed_lookup() -> None:
    clock = FakeClock(100.0)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.unavailable()
    clock.now = 110.0
    await _unavailable(authenticator)
    # Even if time stood still, the old row is gone: the next request must ask again.
    clock.now = 110.0
    await _unavailable(authenticator)
    assert len(store.find_calls) == 3


@pytest.mark.asyncio
async def test_a_row_cached_with_a_zero_cache_time_is_not_served_while_the_store_is_down() -> None:
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, cache_seconds=0)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.unavailable()
    await _unavailable(authenticator)


@pytest.mark.asyncio
async def test_an_expired_row_with_a_hanging_store_is_auth_unavailable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(100.0)
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store, clock=clock, lookup_timeout=1.0)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.hang()
    clock.now = 111.0
    task = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    await until(lambda: len(store.find_calls) == 2, "the second lookup to start", task=task)
    for _ in range(12):
        if task.done():
            break
        await pass_time(clock, 0.25)
    await until(task.done, "the lookup timeout to fire")
    with pytest.raises(AuthUnavailable) as info:
        task.result()
    assert info.value.reason == "timeout"


@pytest.mark.asyncio
async def test_a_cached_row_answers_without_waiting_on_a_hanging_store() -> None:
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store)
    assert await authenticator.authenticate(TEST_KEY) is not None
    store.hang()
    task = asyncio.ensure_future(authenticator.authenticate(TEST_KEY))
    await until(task.done, "a cached key to be answered with no lookup")
    assert task.result() == Principal(TEST_KEY_ID, TEST_TENANT)
    assert len(store.find_calls) == 1


# Authenticator: closing


@pytest.mark.asyncio
async def test_aclose_closes_the_store_once() -> None:
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store)
    await authenticator.aclose()
    assert store.closed == 1


@pytest.mark.asyncio
async def test_a_fresh_authenticator_has_not_closed_its_store() -> None:
    store = FakeKeyStore(TEST_KEY)
    authenticator = make_authenticator(store)
    assert await authenticator.authenticate(TEST_KEY) is not None
    assert store.closed == 0
