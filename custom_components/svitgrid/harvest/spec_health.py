"""Turn an unexecutable register spec into something a support chat can find.

`RegisterSpec.validate()` has existed since the decoder was written and was
never called from production code — only from tests. The consequence was that
all three of these failed IDENTICALLY, and silently:

  * the spec carries a builtin this add-on does not implement,
  * the spec document is malformed,
  * the spec 404s / 500s and is never fetched at all,

each producing an inverter that pairs successfully, shows as configured in the
mobile app, and then reports nothing at all, indefinitely, behind a
`logger.debug` line. That is what this module exists to prevent. It is
deliberately the LAST line of defence: the golden-vector contract test is the
first, and catching a gap here means the corpus already outran the decoder.

Surfacing follows the add-on's existing convention — the ActivityTracker,
whose `diagnostics_line()` is rendered by `sensor.svitgrid_diagnostics`.
"""

from __future__ import annotations

import logging

from .register_spec import RegisterSpec

_LOGGER = logging.getLogger(__name__)

# Consecutive harvest ticks with no spec before we stop calling it a startup
# race. The loop's cadence is >= 10 s, so this is seconds-to-minutes — long
# enough not to cry wolf while the cache fills in, short enough that a user
# reporting "it's been dark all morning" finds it already logged.
SPEC_UNAVAILABLE_TICKS = 3


def build_spec(spec_dict: dict | None, *, model_id: str, activity=None) -> RegisterSpec | None:
    """Parse and validate a spec document. Return None — loudly — if unusable.

    Refusing an invalid spec rather than installing it is deliberate. An
    unknown builtin makes `decoder._apply_builtin` raise on EVERY tick; the
    harvest loop's catch-all swallows that, so installing it buys nothing and
    costs a traceback a minute. Both roads lead to no readings; only this one
    says why.
    """
    if spec_dict is None:
        detail = (
            "register spec unavailable — GET /api/v1/register-specs/"
            f"{model_id} returned no document (404/500?). This model may not be "
            "seeded in the cloud yet."
        )
        _LOGGER.error("harvest: %s", detail)
        _record(activity, model_id, detail)
        return None

    try:
        spec = RegisterSpec.from_dict(spec_dict)
    except Exception as exc:  # noqa: BLE001 — any malformed document
        detail = f"register spec for {model_id} could not be parsed: {exc!r}"
        _LOGGER.error("harvest: %s", detail)
        _record(activity, model_id, detail)
        return None

    problems = spec.validate()
    if problems:
        detail = f"register spec for {model_id} is not executable: {'; '.join(problems)}"
        _LOGGER.error(
            "harvest: %s. This add-on version cannot decode this model — update the "
            "Svitgrid add-on.",
            detail,
        )
        _record(activity, model_id, detail)
        return None

    _clear(activity)
    return spec


def report_spec_unavailable(
    *,
    model_id: str,
    inverter_id: str,
    consecutive: int,
    activity=None,
) -> None:
    """Escalate a harvest tick skipped because no spec is loaded.

    Quiet below SPEC_UNAVAILABLE_TICKS (a spec arriving a tick late at startup
    is normal), then loud EXACTLY ONCE at the threshold — an every-tick warning
    for a permanently-missing spec is just a slower kind of noise.
    """
    if consecutive < SPEC_UNAVAILABLE_TICKS:
        _LOGGER.debug(
            "harvest %s: spec not ready yet (tick %d), skipping", inverter_id, consecutive
        )
        return
    if consecutive > SPEC_UNAVAILABLE_TICKS:
        return  # already reported; stay quiet until it clears

    detail = (
        f"no register spec for model {model_id!r} after {consecutive} polls — "
        f"inverter {inverter_id} is publishing NOTHING. Check that the model is "
        f"seeded at /api/v1/register-specs/{model_id}."
    )
    _LOGGER.warning("harvest: %s", detail)
    _record(activity, model_id, detail)


def _record(activity, model_id: str, detail: str) -> None:
    recorder = getattr(activity, "record_spec_problem", None)
    if recorder is not None:
        recorder(model_id=model_id, detail=detail)


def _clear(activity) -> None:
    clearer = getattr(activity, "clear_spec_problem", None)
    if clearer is not None:
        clearer()
