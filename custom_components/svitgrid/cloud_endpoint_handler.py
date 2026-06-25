"""Runtime cloud-endpoint switch — pure URL allow-list validation.

Mirrors `services/api/src/config/cloud-endpoint.ts` in the svitgrid
monorepo. Keep both sides in lockstep — drift means the integration
accepts a URL the server rejects (or vice versa), silently breaking the
migration handoff.

No HA imports — host-testable via plain pytest."""

from __future__ import annotations

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
