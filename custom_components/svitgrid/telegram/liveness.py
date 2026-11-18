"""Board and inverter liveness for island Telegram alerts (svitgrid D-2149).

The cloud's offline sweep (offline-inverter-sweep.ts) skips a local-only
island, and the outage detector runs only on readings, so silence sent nothing.
This check runs on a timer instead, over what Home Assistant knows:

- A board whose `lastContactAt` is 15 minutes old sends `offline_board`, or
  `offline_outage` while the household is out, and `offline_board_recovered`
  when it reports again. The recovery is the island's own: the cloud sends none.
- An inverter with no accepted reading for 15 minutes sends `offline`. One on a
  silent board is left to the board's message, and one on a paused board sends
  nothing: a paused board uploads nothing by design.

Each episode sends once, then again every 24 hours while it lasts, the cloud's
`LIVENESS_REMINDER_COOLDOWN_MINUTES`. The 15 minutes never start before Home
Assistant did, so HA's own downtime is not blamed on the board. An inverter or
board that never reported sends nothing, as the cloud leaves a new one alone.

`state` holds only JSON values, so the runtime can persist it.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

OFFLINE_MS = 15 * 60 * 1000
REMINDER_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class LivenessAlert:
    """One liveness message: `subject_id` is the board or the inverter."""

    kind: str
    household_id: str
    subject_id: str
    event_at_ms: int
    minutes_ago: int | None


def _ms(value: Any) -> int | None:
    return value if type(value) is int else None


def _minutes(now_ms: int, since_ms: int) -> int:
    # Math.round, as the cloud's sweep computes `minutesAgo`.
    return int((now_ms - since_ms) / 60_000 + 0.5)


def _episode(slot: dict, silent: bool, now_ms: int) -> str | None:
    """Advance one subject's episode. Returns "sent", "back" or None."""
    if not silent:
        if slot.get("sentAtMs") is not None:
            slot.clear()
            slot["backAtMs"] = now_ms
            return "back"
        return None
    sent = _ms(slot.get("sentAtMs"))
    if sent is None or now_ms - sent >= REMINDER_MS:
        slot["sentAtMs"] = now_ms
        return "sent"
    return None


def evaluate_liveness(
    state: dict, households: list[dict], now_ms: int, started_ms: int
) -> tuple[dict, list[LivenessAlert]]:
    """One timer tick over every island household. Returns the new state and
    the alerts to send; `state` itself is left unchanged.

    Each household: `{householdId, gridOut, inverters: [{id, lastReadingMs,
    boardId}], boards: [{deviceId, lastContactMs, paused, inverterIds}]}`.
    """
    old = deepcopy(state.get("households")) if isinstance(state, dict) else None
    old = old if isinstance(old, dict) else {}
    kept: dict[str, dict] = {}
    alerts: list[LivenessAlert] = []

    for hh in households:
        household_id = hh["householdId"]
        prev = old.get(household_id) or {}
        prev_boards = prev.get("boards") if isinstance(prev.get("boards"), dict) else {}
        prev_invs = prev.get("inverters") if isinstance(prev.get("inverters"), dict) else {}
        boards: dict[str, dict] = {}
        invs: dict[str, dict] = {}
        quiet_boards: set[str] = set()
        board_back: dict[str, int] = {}

        for board in hh.get("boards") or []:
            device_id = board["deviceId"]
            slot = dict(prev_boards.get(device_id) or {})
            boards[device_id] = slot
            contact = _ms(board.get("lastContactMs"))
            if board.get("paused") is True:
                quiet_boards.add(device_id)
                continue
            if contact is None:
                continue
            since = max(contact, started_ms)
            silent = now_ms - since >= OFFLINE_MS
            if silent:
                quiet_boards.add(device_id)
            outcome = _episode(slot, silent, now_ms)
            if outcome == "sent":
                kind = "offline_outage" if hh.get("gridOut") else "offline_board"
                alerts.append(
                    LivenessAlert(kind, household_id, device_id, contact, _minutes(now_ms, contact))
                )
            elif outcome == "back":
                alerts.append(
                    LivenessAlert("offline_board_recovered", household_id, device_id, now_ms, None)
                )
            if _ms(slot.get("backAtMs")) is not None:
                board_back[device_id] = slot["backAtMs"]

        for inv in hh.get("inverters") or []:
            inverter_id = inv["id"]
            slot = dict(prev_invs.get(inverter_id) or {})
            invs[inverter_id] = slot
            last = _ms(inv.get("lastReadingMs"))
            board_id = inv.get("boardId")
            if last is None or board_id in quiet_boards:
                continue
            # A board that just came back gets 15 minutes to upload again.
            since = max(last, started_ms, board_back.get(board_id, 0))
            if _episode(slot, now_ms - since >= OFFLINE_MS, now_ms) == "sent":
                alerts.append(
                    LivenessAlert(
                        "offline", household_id, inverter_id, last, _minutes(now_ms, last)
                    )
                )
            slot.pop("backAtMs", None)

        kept[household_id] = {"boards": boards, "inverters": invs}

    return {"households": kept}, alerts
