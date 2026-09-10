"""GET /api/svitgrid/hello — the one view a phone may read before pairing.

The Svitgrid app sweeps the LAN for Home Assistant during onboarding. Finding
port 8123 says "a Home Assistant lives here" and nothing more: whether the
add-on is installed, what version it runs, and whether a pairing is waiting for
a code are all invisible, and every other view refuses an unpaired caller by
design.

That silence costs two things. The owner retypes a six-character code that the
add-on already knows, and the app cannot tell an add-on that can carry several
inverters from one that would accept the extra inverters and quietly poll only
the first.

So this view answers unauthenticated, and its whole contract is that it says
nothing an unpaired stranger on the LAN should not hear:

  * version and instance name — already on the login screen HA serves to the
    same caller on the same port.
  * the pairing code, and ONLY while a pairing is actually pending. The code
    is displayed on the Home Assistant screen to whoever is standing there;
    the window is minutes long and the code is single-use.
"""

from __future__ import annotations

import json

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridHelloView


class _FakeRequest:
    """An UNauthenticated request — no HA session, no island key."""

    def __init__(self, app):
        self.app = {"hass": app}
        self.query: dict = {}
        self._data: dict = {}
        self.headers: dict = {}

    def get(self, key, default=None):  # noqa: D102
        return self._data.get(key, default)

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_answers_without_any_authentication(hass):
    """The point of the view. Every other one 401s an unpaired caller."""
    resp = await SvitgridHelloView().get(_FakeRequest(hass))
    assert resp.status == 200
    assert _body(resp)["service"] == "svitgrid"


@pytest.mark.asyncio
async def test_reports_the_installed_version(hass):
    """Version detection is what lets the app avoid offering a second inverter
    to an add-on that would accept it and poll only the first."""
    body = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    # Read from the real manifest rather than asserting a literal, which would
    # need editing on every release and would be edited without thought.
    assert body["version"]
    assert body["version"][0].isdigit()


@pytest.mark.asyncio
async def test_declares_how_many_inverters_it_can_carry(hass):
    """A number the app reads rather than infers from the version string —
    version comparison is exactly the fragile step this replaces."""
    body = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    assert body["maxInverters"] >= 2


@pytest.mark.asyncio
async def test_no_pairing_pending_means_no_code(hass):
    """The default state. A code offered when none is pending would be a stale
    code, and a stale code sends the owner down a pairing that cannot finish."""
    body = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    assert body["pairingPending"] is False
    assert "code" not in body


@pytest.mark.asyncio
async def test_offers_the_code_while_a_pairing_is_pending(hass):
    hass.data.setdefault(DOMAIN, {})["pending_pairing"] = {"code": "K7M2QX"}
    body = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    assert body["pairingPending"] is True
    assert body["code"] == "K7M2QX"


@pytest.mark.asyncio
async def test_stops_offering_the_code_once_the_pairing_is_gone(hass):
    """The window closes the moment the flow clears its pending pairing —
    on claim, on cancel, and on expiry."""
    hass.data.setdefault(DOMAIN, {})["pending_pairing"] = {"code": "K7M2QX"}
    hass.data[DOMAIN]["pending_pairing"] = None
    body = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    assert body["pairingPending"] is False
    assert "code" not in body


@pytest.mark.asyncio
async def test_says_whether_the_add_on_is_already_paired(hass):
    """A paired add-on must not be offered as a fresh one to onboard against:
    the app shows it as the household's existing station instead."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    unpaired = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    assert unpaired["paired"] is False

    MockConfigEntry(domain=DOMAIN, data={"api_key": "k"}).add_to_hass(hass)
    paired = _body(await SvitgridHelloView().get(_FakeRequest(hass)))
    assert paired["paired"] is True


@pytest.mark.asyncio
async def test_never_leaks_a_secret(hass):
    """A blunt guard on the whole payload rather than on the fields we happen
    to have thought of: this view is readable by anyone on the network."""
    hass.data.setdefault(DOMAIN, {})["pending_pairing"] = {
        "code": "K7M2QX",
        # The flow's own state carries these alongside the code. None of them
        # may cross this boundary.
        "secret": "s" * 40,
        "api_key": "sk-live-nope",
        "private_key_pem": "-----BEGIN PRIVATE KEY-----",
    }
    raw = (await SvitgridHelloView().get(_FakeRequest(hass))).body.decode()
    assert "s" * 40 not in raw
    assert "sk-live-nope" not in raw
    assert "PRIVATE KEY" not in raw
