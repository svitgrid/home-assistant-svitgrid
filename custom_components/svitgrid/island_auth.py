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
import time

from homeassistant.helpers.http import KEY_AUTHENTICATED

from .const import DOMAIN

# When the app last made an authorized call to the add-on, on the monotonic
# clock. Kept beside `hass.data[DOMAIN]`, not in it: several readers iterate
# that dict's values as entry states. An idle edge box polls fast only while
# the app is in use (`edge_api._poll_interval_ms`, svitgrid D-2117).
APP_SEEN_AT_KEY = f"{DOMAIN}_app_seen_at"


def _monotonic() -> float:
    return time.monotonic()


def _note_app_call(request: object) -> None:
    """Record an authorized app call. Best-effort: a request without the
    app's `hass` (a test fake) records nothing."""
    try:
        data = request.app["hass"].data
    except (AttributeError, KeyError, TypeError):
        return
    if isinstance(data, dict):
        data[APP_SEEN_AT_KEY] = _monotonic()


def app_seen_within(hass: object, seconds: float) -> bool:
    """True when the app made an authorized call in the last `seconds`."""
    data = getattr(hass, "data", None)
    seen = data.get(APP_SEEN_AT_KEY) if isinstance(data, dict) else None
    return isinstance(seen, float) and _monotonic() - seen <= seconds


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
    if matched:
        _note_app_call(request)
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
        _note_app_call(request)
        return True
    return island_key_present_and_valid(request, island_keys)
