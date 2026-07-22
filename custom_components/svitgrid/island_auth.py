"""Island-mode request authorization helpers.

A LAN request is authorized for READ endpoints if it either:
  - Comes from an authenticated HA session (``request[KEY_AUTHENTICATED]`` is
    truthy), OR
  - Carries a valid ``X-Island-Key`` header matching the stored island key via
    constant-time comparison.

The CONTROL endpoint uses only ``island_key_present_and_valid`` (key path
only, no session bypass).

HA-auth attribute used: ``KEY_AUTHENTICATED`` from
``homeassistant.helpers.http`` (value = ``"ha_authenticated"``).  Verified
against the installed HA package at
``.venv/lib/python3.14/site-packages/homeassistant/helpers/http.py`` line 34:
``KEY_AUTHENTICATED: Final = "ha_authenticated"``.  The same symbol is
re-exported by ``homeassistant.components.http.const``.  HA's own middleware
sets ``request[KEY_AUTHENTICATED] = True`` after successful auth check (see
``homeassistant/helpers/http.py`` line 58).
"""

from __future__ import annotations

import hmac

from homeassistant.helpers.http import KEY_AUTHENTICATED


def island_key_present_and_valid(request: object, island_keys: list[str] | str | None) -> bool:
    """Return True iff the request carries an X-Island-Key header matching ANY
    registered key.

    `island_keys` accepts either the multi-device list (the current scheme) or
    a bare string / None (the pre-multi-device scheme), so call sites can
    migrate independently.

    Every candidate is compared with `hmac.compare_digest` (constant-time).
    We deliberately compare against ALL candidates rather than short-circuiting
    on the first match — the loop's cost is bounded by the number of paired
    devices, and not short-circuiting keeps the timing profile flat with
    respect to *which* device is calling.
    """
    if island_keys is None:
        return False
    candidates = [island_keys] if isinstance(island_keys, str) else list(island_keys)
    candidates = [c for c in candidates if c]
    if not candidates:
        return False

    header_value: str | None = request.headers.get("X-Island-Key")
    if not header_value:
        return False

    matched = False
    for candidate in candidates:
        try:
            if hmac.compare_digest(header_value, candidate):
                matched = True
        except (TypeError, ValueError):
            continue
    return matched


def island_request_authorized(request: object, island_keys: list[str] | str | None) -> bool:
    """Return True iff the request is authorized for island read endpoints.

    A request is authorized if EITHER:
    - It is an authenticated HA session (``request[KEY_AUTHENTICATED]`` is
      truthy), OR
    - It carries a valid ``X-Island-Key`` header (see
      ``island_key_present_and_valid``).
    """
    if request.get(KEY_AUTHENTICATED, False):
        return True
    return island_key_present_and_valid(request, island_keys)
