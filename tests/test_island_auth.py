"""Tests for island_auth.py — HA-session-or-X-Island-Key request authorization.

TDD: tests written before implementation, expected to FAIL until
island_auth.py exists with the correct logic.

The HA-auth marker used here is KEY_AUTHENTICATED from
homeassistant.helpers.http (value = "ha_authenticated"), confirmed against
the installed HA package. A request is authenticated if
request["ha_authenticated"] is truthy.
"""
from __future__ import annotations

import hmac as _hmac
from unittest.mock import patch

from custom_components.svitgrid.island_auth import (
    island_key_present_and_valid,
    island_request_authorized,
)

# ---------------------------------------------------------------------------
# Tiny fake request object matching what HA passes to aiohttp views
# ---------------------------------------------------------------------------

_KEY_AUTHENTICATED = "ha_authenticated"  # homeassistant.helpers.http.KEY_AUTHENTICATED


class _FakeHeaders(dict):
    """Case-insensitive header dict, same semantics as aiohttp CIMultiDictProxy."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeRequest:
    """Minimal aiohttp-style request mock."""

    def __init__(self, *, authenticated: bool = False, island_key_header: str | None = None):
        self._data: dict = {_KEY_AUTHENTICATED: authenticated}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]

    def get(self, key, default=None):  # noqa: D105
        return self._data.get(key, default)


# ---------------------------------------------------------------------------
# island_key_present_and_valid
# ---------------------------------------------------------------------------


def test_key_valid_with_correct_header():
    """Correct X-Island-Key header + matching island_key → True."""
    req = _FakeRequest(island_key_header="secret-abc")
    assert island_key_present_and_valid(req, "secret-abc") is True


def test_key_valid_wrong_header_value():
    """Wrong X-Island-Key header value → False."""
    req = _FakeRequest(island_key_header="wrong")
    assert island_key_present_and_valid(req, "secret-abc") is False


def test_key_valid_missing_header():
    """No X-Island-Key header → False."""
    req = _FakeRequest()
    assert island_key_present_and_valid(req, "secret-abc") is False


def test_key_valid_island_key_none():
    """island_key=None → False even with correct header."""
    req = _FakeRequest(island_key_header="anything")
    assert island_key_present_and_valid(req, None) is False


def test_key_valid_island_key_empty_string():
    """island_key='' → False (falsy guard)."""
    req = _FakeRequest(island_key_header="")
    assert island_key_present_and_valid(req, "") is False


def test_key_valid_one_char_difference():
    """One-character difference in header → False (constant-time compare still rejects)."""
    req = _FakeRequest(island_key_header="secret-abX")
    assert island_key_present_and_valid(req, "secret-abc") is False


def test_key_valid_uses_hmac_compare_digest():
    """island_key_present_and_valid must use hmac.compare_digest internally.

    We monkeypatch hmac.compare_digest to capture calls and verify it is invoked
    with the correct byte arguments.
    """
    calls = []
    real_compare = _hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    req = _FakeRequest(island_key_header="mykey")
    with patch("custom_components.svitgrid.island_auth.hmac.compare_digest", side_effect=_spy):
        island_key_present_and_valid(req, "mykey")

    assert len(calls) == 1, "hmac.compare_digest must be called exactly once"
    # Both sides must be comparable types (str or bytes); equal keys must pass.
    a, b = calls[0]
    assert real_compare(a, b) is True


# ---------------------------------------------------------------------------
# island_request_authorized
# ---------------------------------------------------------------------------


def test_authorized_authenticated_session_no_key():
    """Authenticated HA session + no island key header + island_key=None → True."""
    req = _FakeRequest(authenticated=True)
    assert island_request_authorized(req, None) is True


def test_authorized_authenticated_session_ignores_bad_island_key():
    """Authenticated HA session → True regardless of island key state."""
    req = _FakeRequest(authenticated=True, island_key_header="wrong")
    assert island_request_authorized(req, "correct") is True


def test_authorized_valid_island_key_not_authed_session():
    """Non-session request with correct X-Island-Key → True."""
    req = _FakeRequest(authenticated=False, island_key_header="correct-key")
    assert island_request_authorized(req, "correct-key") is True


def test_authorized_also_key_valid_for_authed_plus_correct_key():
    """Authenticated session + correct key → both helpers return True."""
    req = _FakeRequest(authenticated=True, island_key_header="correct-key")
    assert island_request_authorized(req, "correct-key") is True
    assert island_key_present_and_valid(req, "correct-key") is True


def test_not_authorized_no_session_wrong_key():
    """Non-session + wrong X-Island-Key → False."""
    req = _FakeRequest(authenticated=False, island_key_header="wrong")
    assert island_request_authorized(req, "correct") is False


def test_not_authorized_no_session_no_key():
    """Non-session + no X-Island-Key header → False."""
    req = _FakeRequest(authenticated=False)
    assert island_request_authorized(req, "correct") is False


def test_not_authorized_no_session_island_key_none():
    """Non-session + island_key=None → False (key path disabled)."""
    req = _FakeRequest(authenticated=False, island_key_header="anything")
    assert island_request_authorized(req, None) is False


def test_authorized_session_only_when_island_key_none():
    """island_key=None disables key path; session-only still authorizes read endpoint."""
    req_authed = _FakeRequest(authenticated=True)
    req_not_authed = _FakeRequest(authenticated=False)
    assert island_request_authorized(req_authed, None) is True
    assert island_request_authorized(req_not_authed, None) is False


# ---------------------------------------------------------------------------
# MINOR 2: non-ASCII X-Island-Key header must not crash (TypeError) (RED phase)
# ---------------------------------------------------------------------------


def test_key_valid_non_ascii_header_does_not_raise():
    """Non-ASCII X-Island-Key header value → returns False, never raises TypeError/ValueError."""

    class _NonASCIIHeaders:
        def get(self, key, default=None):  # noqa: D102
            if key.lower() == "x-island-key":
                # Return a string with non-ASCII characters
                return "café-secret"
            return default

    class _NonASCIIRequest:
        headers = _NonASCIIHeaders()

        def get(self, key, default=None):  # noqa: D102
            return default

    req = _NonASCIIRequest()
    # Must not raise; must return False (key mismatch or TypeError caught)
    result = island_key_present_and_valid(req, "cafe-secret")
    assert result is False
