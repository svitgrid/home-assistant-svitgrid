"""Tells an adopted edge box its desired set of battery packs (plan Task 45).

On an edge island Home Assistant keeps each box's desired roster
(`bms_packs.py`) and sends it as the cloud does
(`services/bms-roster/bms-roster-delivery.ts`): the whole set with its epoch
in one unsigned `bms_sync` `{householdId, generation, n, packs}`, on every
change and whenever a box reports a `rosterEpoch` behind it. At most one is
pending per box; a newer set supersedes a pending one. The same epoch is not
re-sent within `SYNC_RESEND_AFTER_MS`, so a box whose reports keep drifting is
not flooded.

A box Home Assistant never changed has no roster here and is sent nothing: it
keeps the set the cloud gave it.

Only a board whose S3 and H2 both run `bms_sync` is sent it
(`supportsBmsSync`, svitgrid D-2157). Older firmware acks nothing and its set
never changes. The cloud drives such a board with one `bms_adopt` or
`bms_forget` at a time; Home Assistant has no such path, so a pack change for
it is refused with `BOARD_FIRMWARE_TOO_OLD` (`bms_lan.py`).
"""

from __future__ import annotations

import logging
import re
import time

from .bms_packs import new_roster_generation

BMS_SYNC_COMMAND = "bms_sync"
# `SYNC_RESEND_AFTER_MS` (`bms-roster-model.ts`).
SYNC_RESEND_AFTER_MS = 10 * 60_000
# `ROSTER_COMMAND_TTL_MS` (`routes/edge-devices.ts`): a roster change must
# outlive a box that is merely offline. Nothing else re-drives a box that
# stopped reporting, so `bms_sync` gets it too.
ROSTER_COMMAND_TTL_S = 24 * 3600
# `MAX_PACKS_PER_BOARD`.
MAX_PACKS_PER_BOX = 5

# `BMS_SYNC_MIN_S3_VERSION` and `BMS_SYNC_MIN_H2_VERSION`: the first releases
# that handle `bms_sync`. Never lower them below the release that does.
BMS_SYNC_MIN_S3_VERSION = "2.149.0"
BMS_SYNC_MIN_H2_VERSION = "0.19.0"
# The 409 code for a pack change on a board below them.
BOARD_FIRMWARE_TOO_OLD = "BOARD_FIRMWARE_TOO_OLD"

_LOGGER = logging.getLogger(__name__)

# `SEMVER_RE` (`utils/semver.ts`): `x.y.z`, optionally followed by one label
# (`-beta.1`, `+build.7`, `.dev0`, `rc1`).
_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z][0-9A-Za-z.+-]*|\.?[A-Za-z][0-9A-Za-z.+-]*)?$",
    re.ASCII,
)


def parse_semver(value) -> tuple[int, int, int] | None:
    """`parseSemver`: the `x.y.z` core, ignoring a label and a leading `v`;
    None for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    m = _SEMVER_RE.match(text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def supports_bms_sync(board) -> bool:
    """`supportsBmsSync`: the board's S3 AND its H2 handle `bms_sync`. A new
    S3 on an old H2 is common (onboarding does not update the H2), and an
    unknown or unparseable version is old too."""
    if not isinstance(board, dict):
        return False
    s3 = parse_semver(board.get("version"))
    h2 = parse_semver(board.get("h2FirmwareVersion"))
    return (
        s3 is not None
        and h2 is not None
        and s3 >= parse_semver(BMS_SYNC_MIN_S3_VERSION)
        and h2 >= parse_semver(BMS_SYNC_MIN_H2_VERSION)
    )


async def async_board(store, box_id: str) -> dict:
    """The firmware versions the box last reported in its telemetry
    (`edge_api._stamp_contact`), or an empty dict, which reads as old."""
    if store is None:
        return {}
    return (await store.get_edge_box_state(box_id)) or {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def compare_epoch(reported, roster: dict) -> str:
    """`compareRosterEpoch`: `equal`, `behind` (no epoch, another generation
    or a lower n) or `ahead` (a higher n in the same generation)."""
    if not isinstance(reported, dict) or reported.get("generation") != roster["generation"]:
        return "behind"
    n = reported.get("n")
    if n == roster["n"]:
        return "equal"
    return "ahead" if isinstance(n, int) and n > roster["n"] else "behind"


def sync_payload(household_id: str | None, roster: dict) -> dict:
    """`buildBmsSyncPayload`: the set ordered by adoption, without labels
    (the H2's label field is 24 bytes and cuts Cyrillic)."""
    packs = sorted(
        roster["packs"],
        key=lambda p: (
            p["adoptedAt"] if p.get("adoptedAt") is not None else float("inf"),
            p["packId"],
        ),
    )
    out = []
    for p in packs:
        entry: dict = {"packId": p["packId"]}
        if p.get("addrType") is not None:
            entry["addrType"] = p["addrType"]
        entry["driver"] = p.get("driver")
        if p.get("driverSource") == "owner" and p.get("driver"):
            entry["driverSource"] = "owner"
        entry["adoptedAt"] = p.get("adoptedAt")
        out.append(entry)
    return {
        "householdId": household_id,
        "generation": roster["generation"],
        "n": roster["n"],
        "packs": out,
    }


async def async_deliver(
    packs, queue, household_id: str | None, box_id: str, *, board, force: bool = False
) -> str | None:
    """Queue the box's set as `bms_sync`; returns the command id, or None when
    the box has no roster here, its *board* firmware does not handle
    `bms_sync`, or it was sent this epoch less than `SYNC_RESEND_AFTER_MS`
    ago (unless `force`)."""
    roster = await packs.async_roster(box_id)
    if roster is None:
        return None
    if not supports_bms_sync(board):
        _LOGGER.info("Edge box %s: firmware too old for bms_sync, roster not sent", box_id)
        return None
    now = _now_ms()
    last = roster.get("lastSync")
    if (
        not force
        and last
        and (last["generation"], last["n"]) == (roster["generation"], roster["n"])
        and now - last["atMs"] < SYNC_RESEND_AFTER_MS
    ):
        return None
    await queue.async_supersede(box_id, BMS_SYNC_COMMAND)
    command_id = await queue.async_enqueue(
        box_id,
        BMS_SYNC_COMMAND,
        sync_payload(household_id, roster),
        signing=None,
        inverter_id=None,
        source="bms",
        ttl_s=ROSTER_COMMAND_TTL_S,
    )
    await packs.async_update_roster(
        box_id,
        lastSync={
            "generation": roster["generation"],
            "n": roster["n"],
            "atMs": now,
            "commandId": command_id,
        },
    )
    return command_id


async def async_converge(
    packs, queue, household_id: str | None, box_id: str, reported_epoch, *, board
) -> str | None:
    """Re-drive a box from its report: a box behind its roster is sent the
    set again; one ahead of it (Home Assistant went backwards, a restore) is
    sent a new generation, because the H2 refuses a lower n forever."""
    roster = await packs.async_roster(box_id)
    if roster is None or not supports_bms_sync(board):
        return None
    position = compare_epoch(reported_epoch, roster)
    if position == "equal":
        return None
    if position == "ahead":
        await packs.async_update_roster(box_id, generation=new_roster_generation(), n=1)
        return await async_deliver(packs, queue, household_id, box_id, board=board, force=True)
    return await async_deliver(packs, queue, household_id, box_id, board=board)
