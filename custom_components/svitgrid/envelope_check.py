"""The signed-envelope check on a cloud command (svitgrid D-1939).

The admin signature covers ``signedEventData`` only, and the poller then runs
the UNSIGNED top-level ``command`` with its ``payload``. A cloud holding any
old signed envelope could therefore run any inverter command here. This check
asks the verified envelope three questions, as the S3 (``envelope_check.c``)
and the mobile harvester (``envelope_check.dart``) do:

- Is the command in its ``authorizedCommands``? An envelope with no such list
  but a string ``command`` (an owner's tap) authorizes exactly that command.
- Does its ``inverterId`` name the target, the command's
  ``payload.inverterId``?
- Is it still inside its ``validUntil``?

A ``planner_delegation`` envelope fails ``planner_delegation`` and nothing
else (svitgrid D-1714): the planner's delegation was removed on 2026-11-21,
and one valid delegation would otherwise pass any signed write on the
inverters it lists. Every level refuses it, ``off`` included, with the reason
``command_mismatch``. This check runs only after the signature verified
against a held trusted key.

What any other failure does depends on the enforcement level. ``off`` (the
poll's ``envelopeEnforce`` is false) runs the command and only reports the
failure in the ack's ``envelopeCheck``. ``switch`` (it is true) refuses on the board's
ENVELOPE_ENFORCE_MASK. ``always`` is the S3's always-enforce build; the add-on
has no such build, and the level is here so the shared cases pin all three.

The cloud's rules are ``envelopeFailures`` and ``envelopeVerdict`` in
``services/api/src/services/envelope-replay.ts``. Both are held to
``tests/fixtures/envelope_enforcement_cases.json``, vendored from
``packages/inverter_protocol/fixtures`` in the monorepo.
"""

from __future__ import annotations

import calendar
import re
from typing import Any

# Check names, in the board's fixed order.
CHECK_ORDER = (
    # D-1714: a removed planner delegation, refused at every level.
    "planner_delegation",
    "authorized_commands_missing",
    "command_not_authorized",
    "inverter_id_missing",
    "inverter_mismatch",
    "valid_until_missing",
    "envelope_no_clock",
    "envelope_expired",
)
_VALIDITY = frozenset({"valid_until_missing", "envelope_no_clock", "envelope_expired"})

# ENVELOPE_ENFORCE_OFF_MASK: refused even with the server's switch off (D-1714).
_ENFORCED_OFF = frozenset({"planner_delegation"})
# ENVELOPE_ENFORCE_MASK: what the server's switch refuses.
_ENFORCED_SWITCH = frozenset(
    {"planner_delegation", "command_not_authorized", "valid_until_missing", "envelope_no_clock"}
)
# ENVELOPE_ENFORCE_ALWAYS_MASK.
_ENFORCED_ALWAYS = frozenset(CHECK_ORDER)

LEVEL_OFF = "off"
LEVEL_SWITCH = "switch"
LEVEL_ALWAYS = "always"

ACTION_RUN = "run"
ACTION_REFUSE = "refuse"
ACTION_RETRY_LATER = "retry_later"

# What envelope_parse_iso8601_utc accepts: toIso8601String() and toISOString().
_ISO_UTC = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"(?:Z|([+-])(\d{2}):(\d{2}))$"
)


def parse_iso8601_utc(value: Any) -> int | None:
    """Whole UTC epoch seconds of ``value``, or None for anything the board
    would not parse. The fraction is dropped, as on the board."""
    if not isinstance(value, str):
        return None
    m = _ISO_UTC.match(value)
    if m is None:
        return None
    y, mo, d, h, mi, se = (int(g) for g in m.groups()[:6])
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h <= 23 and mi <= 59 and se <= 60):
        return None
    offset_s = 0
    if m.group(7):
        oh, om = int(m.group(8)), int(m.group(9))
        if oh > 23 or om > 59:
            return None
        offset_s = (-1 if m.group(7) == "-" else 1) * (oh * 3600 + om * 60)
    return calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)) - offset_s


def envelope_failures(
    signed_event_data: Any,
    command: str,
    target_inverter_id: str | None,
    now_epoch_s: int | None,
    *,
    clock_set: bool = True,
) -> list[str]:
    """The checks ``command`` fails, in :data:`CHECK_ORDER`; empty when all
    hold. An empty ``target_inverter_id`` skips the inverter check. A
    ``now_epoch_s`` of None skips the validity checks."""
    sed = signed_event_data if isinstance(signed_event_data, dict) else {}
    # D-1714: whatever a planner delegation lists, it authorizes nothing.
    if sed.get("type") == "planner_delegation":
        return ["planner_delegation"]
    failed: set[str] = set()

    authorized = sed.get("authorizedCommands")
    own = sed.get("command")
    if not isinstance(authorized, list) and isinstance(own, str):
        if own != command:
            failed.add("command_not_authorized")
    elif not isinstance(authorized, list):
        failed.add("authorized_commands_missing")
    elif command not in authorized:
        failed.add("command_not_authorized")

    one = sed.get("inverterId")
    if not (isinstance(one, str) and one != ""):
        failed.add("inverter_id_missing")
    elif target_inverter_id and one != target_inverter_id:
        failed.add("inverter_mismatch")

    if now_epoch_s is not None:
        until = parse_iso8601_utc(sed.get("validUntil"))
        if until is None:
            failed.add("valid_until_missing")
        elif not clock_set:
            failed.add("envelope_no_clock")
        elif now_epoch_s >= until:
            failed.add("envelope_expired")

    return [n for n in CHECK_ORDER if n in failed]


def envelope_verdict(
    failed: list[str], command: str, signed_event_data: Any, level: str
) -> tuple[str, str | None]:
    """``(action, reason)`` for a verified command that fails ``failed``.

    ``reason`` is the refusal's ack reason, and None unless the action is
    :data:`ACTION_REFUSE`. A restore runs without a clock (and, at
    ``always``, on an expired envelope), so the inverter is always put back.
    Any other level than ``switch`` and ``always`` is ``off``, which refuses
    only a planner delegation (D-1714). ``signed_event_data`` is unused and
    kept for the callers.
    """
    if level == LEVEL_ALWAYS:
        mask = _ENFORCED_ALWAYS
    elif level == LEVEL_SWITCH:
        mask = _ENFORCED_SWITCH
    else:
        mask = _ENFORCED_OFF
    enforced = [n for n in CHECK_ORDER if n in failed and n in mask]
    if command.startswith("restore_"):
        exempt = (
            {"envelope_no_clock", "envelope_expired"}
            if level == LEVEL_ALWAYS
            else {"envelope_no_clock"}
        )
        enforced = [n for n in enforced if n not in exempt]
    if not enforced:
        return ACTION_RUN, None
    if enforced == ["envelope_no_clock"]:
        return ACTION_RETRY_LATER, None
    first = enforced[0]
    if first == "planner_delegation":
        # A reason the app already explains (board_refusal_text.dart).
        return ACTION_REFUSE, "command_mismatch"
    return ACTION_REFUSE, first if first.startswith("envelope_") else f"envelope_{first}"


def envelope_shape(signed_event_data: Any) -> str:
    """The envelope's ``type``, or ``untyped``."""
    t = signed_event_data.get("type") if isinstance(signed_event_data, dict) else None
    return t if isinstance(t, str) and t else "untyped"


def envelope_check_report(
    failed: list[str],
    signed_event_data: Any,
    command: str,
    signing_key_id: str | None,
    now_epoch_s: int | None,
) -> dict[str, Any] | None:
    """The ack's ``envelopeCheck``, or None when nothing failed: names, shape
    and key id, never key material or payload values. A validity failure adds
    ``eventId`` (the envelope's scenarioId) and, when expired, ``expiredAgoS``.
    """
    if not failed:
        return None
    sed = signed_event_data if isinstance(signed_event_data, dict) else {}
    report: dict[str, Any] = {
        "failed": list(failed),
        "command": command,
        "shape": envelope_shape(sed),
        "signingKeyId": signing_key_id or "",
    }
    if any(f in _VALIDITY for f in failed):
        sid = sed.get("scenarioId")
        report["eventId"] = sid if isinstance(sid, str) else ""
        until = parse_iso8601_utc(sed.get("validUntil"))
        if "envelope_expired" in failed and until is not None and now_epoch_s is not None:
            report["expiredAgoS"] = now_epoch_s - until
    return report
