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


def island_key_present_and_valid(request: object, island_key: str | None) -> bool:
    """Return True iff the request carries a valid X-Island-Key header.

    Both conditions must hold:
    - ``island_key`` is truthy (not None, not empty string).
    - ``request.headers.get("X-Island-Key")`` equals ``island_key`` via
      ``hmac.compare_digest`` (constant-time, timing-attack-safe).
    """
    if not island_key:
        return False
    header_value: str | None = request.headers.get("X-Island-Key")
    if not header_value:
        return False
    try:
        return hmac.compare_digest(header_value, island_key)
    except (TypeError, ValueError):
        return False


def island_request_authorized(request: object, island_key: str | None) -> bool:
    """Return True iff the request is authorized for island read endpoints.

    A request is authorized if EITHER:
    - It is an authenticated HA session (``request[KEY_AUTHENTICATED]`` is
      truthy), OR
    - It carries a valid ``X-Island-Key`` header (see
      ``island_key_present_and_valid``).
    """
    if request.get(KEY_AUTHENTICATED, False):
        return True
    return island_key_present_and_valid(request, island_key)
