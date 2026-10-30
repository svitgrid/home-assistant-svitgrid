"""Contract tests that fail when a repository moves alone (edge island, Task 14,
svitgrid#1234).

The firmware, the API and this integration each hold half of the edge box's
contract. The monorepo writes it down once in
`packages/inverter_protocol/fixtures/` and `scripts/sync-edge-command-fixtures.sh`
vendors it here:

- `firmware_signed_commands.json`: the commands the box refuses without a
  phone signature (`command_auth.c`).
- `edge_paths.json`: every path the box calls, kept equal to a scan of the
  firmware sources by `signing-command-parity.test.ts`.
- `edge_pair_code.json`: the pairing code's header, pattern, refusals and
  lifetime, the island commands, and the `serverTime` strings the box's
  clock parser accepts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.svitgrid import edge_api, island_lan
from custom_components.svitgrid.const import PAIRING_CODE_ALPHABET, PAIRING_CODE_LENGTH
from custom_components.svitgrid.executors.edge_box import EDGE_SIGNED_COMMANDS
from custom_components.svitgrid.keystore import EDGE_PAIR_CODE_TTL

FIXTURES = Path(__file__).parent / "fixtures"

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_edge_signed_commands_match_firmware():
    assert set(_fixture("firmware_signed_commands.json")["commands"]) == EDGE_SIGNED_COMMANDS


def test_edge_paths_match_firmware_contract():
    """The edge views serve exactly the paths the box calls: a path the box
    calls with no view is a 404 storm and no 2xx for the box's watchdog, and a
    view the box never calls is surface nothing tests from the box's side."""
    served = set()
    for view_cls in edge_api.EDGE_VIEWS:
        methods = {m.upper() for m in _HTTP_METHODS if hasattr(view_cls, m)}
        for url in (view_cls.url, *view_cls.extra_urls):
            served |= {(url, method) for method in methods}
    expected = {(p["path"], p["method"]) for p in _fixture("edge_paths.json")["paths"]}
    assert served == expected


def test_pair_code_contract():
    contract = _fixture("edge_pair_code.json")
    assert contract["header"] == edge_api.PAIR_CODE_HEADER
    assert set(edge_api.PAIR_CODE_ERRORS) <= set(contract["errors"])
    assert EDGE_PAIR_CODE_TTL.total_seconds() == contract["ttlSeconds"]
    alphabet, length = re.fullmatch(r"\^\[(\w+)\]\{(\d+)\}\$", contract["pattern"]).groups()
    assert sorted(PAIRING_CODE_ALPHABET) == sorted(alphabet)
    assert int(length) == PAIRING_CODE_LENGTH
    assert contract["commands"]["clear"] == island_lan.EDGE_LEAVE_COMMAND
    assert contract["commands"]["set"] in EDGE_SIGNED_COMMANDS
    assert contract["commands"]["clear"] in EDGE_SIGNED_COMMANDS


def test_server_time_contract():
    """`serverTime` keeps the shape of the first string the box accepts
    (milliseconds and `Z`), so the integration's format cannot drift into one
    the box's clock parser refuses."""
    examples = _fixture("edge_pair_code.json")["serverTimeExamples"]
    shape = re.sub(r"\d", r"\\d", re.escape(examples["accepted"][0]))
    assert re.fullmatch(shape, edge_api._server_time())
    for refused in examples["refused"]:
        assert not re.fullmatch(shape, refused)
