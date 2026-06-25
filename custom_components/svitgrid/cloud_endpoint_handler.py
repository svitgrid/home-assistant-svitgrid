"""Runtime cloud-endpoint switch — URL allow-list validation + auth probe.

Mirrors `services/api/src/config/cloud-endpoint.ts` in the svitgrid
monorepo. Keep both sides in lockstep — drift means the integration
accepts a URL the server rejects (or vice versa), silently breaking the
migration handoff.

No HA imports — host-testable via plain pytest."""

from __future__ import annotations

import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)

ALLOWED_API_BASES: tuple[str, ...] = (
    "https://api-staging.svitgrid.app",
    "https://api.svitgrid.app",
)


def is_allowed_api_base(url: object) -> bool:
    """Exact-match allow-list. No protocol coercion, no trailing-slash
    normalization, no case-folding — the server mints these URLs verbatim
    and stores them verbatim. Anything different is rejected."""
    if not isinstance(url, str) or not url:
        return False
    return url in ALLOWED_API_BASES


async def probe_endpoint_auth(
    session: aiohttp.ClientSession,
    api_key: str,
    new_api_base: str,
) -> bool:
    """Pre-flight auth probe for a candidate cloud endpoint.

    Mirrors firmware D5's ce_apply_url two-step: GET /api/v3/me with our
    api_key. Returns True only on HTTP 200; any non-200 (401, 403, 404,
    network error) returns False so the caller can reject the migration.

    This prevents silent dead-in-water state when the new endpoint exists
    but doesn't have our api_key or our HA signing key registered in
    households/{id}/trustedDevices (e.g. incremental-sync missed a
    sub-collection)."""
    url = f"{new_api_base.rstrip('/')}/api/v3/me"
    try:
        async with session.get(url, headers={"x-api-key": api_key}) as resp:
            if resp.status == 200:
                _LOGGER.debug(
                    "set_cloud_endpoint probe OK: %s returned 200", url
                )
                return True
            _LOGGER.warning(
                "set_cloud_endpoint probe failed: %s returned HTTP %s",
                url, resp.status,
            )
            return False
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "set_cloud_endpoint probe failed: network error reaching %s", url,
            exc_info=True,
        )
        return False
