"""Tests for the `status` and `reason` fields on every ProviderError class (D-054).

Each class takes `status` and `reason` as keyword arguments, both defaulting to
None, and stores them as attributes. The retryable flags of D-014 do not change,
and ProviderRateLimited keeps its `retry_after_seconds`.
"""

import pytest

from llmgateway.providers.base import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

SUBCLASSES: list[tuple[type[ProviderError], bool]] = [
    (ProviderTimeout, True),
    (ProviderRateLimited, True),
    (ProviderUnavailable, True),
    (ProviderBadRequest, False),
    (ProviderAuthError, False),
]
IDS = [cls.__name__ for cls, _ in SUBCLASSES]


@pytest.mark.parametrize(("cls", "retryable"), SUBCLASSES, ids=IDS)
def test_status_and_reason_default_to_none(cls: type[ProviderError], retryable: bool) -> None:
    exc = cls("fixed message")
    assert exc.status is None
    assert exc.reason is None
    assert exc.retryable is retryable
    assert exc.message == "fixed message"


@pytest.mark.parametrize(("cls", "retryable"), SUBCLASSES, ids=IDS)
def test_status_and_reason_keywords_are_stored(cls: type[ProviderError], retryable: bool) -> None:
    exc = cls("fixed message", status=418, reason="http_status")
    assert exc.status == 418
    assert exc.reason == "http_status"
    assert exc.retryable is retryable
    assert exc.message == "fixed message"
    assert str(exc) == "fixed message"


@pytest.mark.parametrize(("cls", "retryable"), SUBCLASSES, ids=IDS)
def test_status_alone_and_reason_alone(cls: type[ProviderError], retryable: bool) -> None:
    only_status = cls("m", status=503)
    assert (only_status.status, only_status.reason) == (503, None)
    only_reason = cls("m", reason="timeout")
    assert (only_reason.status, only_reason.reason) == (None, "timeout")


def test_base_class_takes_status_and_reason_with_retryable() -> None:
    exc = ProviderError("m", retryable=True, status=200, reason="malformed_body")
    assert (exc.retryable, exc.status, exc.reason) == (True, 200, "malformed_body")
    default = ProviderError("m")
    assert (default.retryable, default.status, default.reason) == (False, None, None)


def test_rate_limited_keeps_retry_after_alongside_status_and_reason() -> None:
    exc = ProviderRateLimited("m", retry_after_seconds=1.5, status=429, reason="rate_limited")
    assert exc.retry_after_seconds == 1.5
    assert (exc.status, exc.reason, exc.retryable) == (429, "rate_limited", True)
    assert ProviderRateLimited("m").retry_after_seconds is None


def test_status_and_reason_do_not_change_the_message() -> None:
    # The message is what errors.py and logs must never pass on; status and reason
    # are separate fields, not appended to it.
    exc = ProviderUnavailable("fixed message", status=503, reason="http_status")
    assert "503" not in str(exc)
    assert "http_status" not in str(exc)
