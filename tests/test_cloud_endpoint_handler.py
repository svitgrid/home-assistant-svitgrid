"""Allow-list validation for the runtime cloud-endpoint switch.
Mirrors services/api/src/config/cloud-endpoint.ts in the monorepo —
keep in lockstep."""

from __future__ import annotations

from custom_components.svitgrid.cloud_endpoint_handler import (
    ALLOWED_API_BASES,
    is_allowed_api_base,
)


def test_allow_list_is_exactly_staging_and_prod():
    """The allow-list must match the monorepo (services/api/src/config/
    cloud-endpoint.ts). Drift = silent migration failures."""
    assert set(ALLOWED_API_BASES) == {
        "https://api-staging.svitgrid.app",
        "https://api.svitgrid.app",
    }


def test_is_allowed_accepts_staging():
    assert is_allowed_api_base("https://api-staging.svitgrid.app") is True


def test_is_allowed_accepts_prod():
    assert is_allowed_api_base("https://api.svitgrid.app") is True


def test_is_allowed_rejects_arbitrary_https_host():
    assert is_allowed_api_base("https://evil.example.com") is False


def test_is_allowed_rejects_http_protocol():
    """http:// is rejected even for the allowed host — TLS is required."""
    assert is_allowed_api_base("http://api-staging.svitgrid.app") is False


def test_is_allowed_rejects_trailing_slash():
    """Exact match — trailing slash is a different string. The API mints
    URLs without trailing slashes; the integration stores them verbatim."""
    assert is_allowed_api_base("https://api-staging.svitgrid.app/") is False


def test_is_allowed_rejects_non_string():
    assert is_allowed_api_base(None) is False
    assert is_allowed_api_base(123) is False
    assert is_allowed_api_base({"url": "x"}) is False


def test_is_allowed_rejects_empty():
    assert is_allowed_api_base("") is False
