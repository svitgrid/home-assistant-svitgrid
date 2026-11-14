"""Held priors and revert deadlines for the battery charge current cap.

``set_battery_charge_current`` writes Deye "Max A Charge" (register 108 on a
3-phase LV unit, 210 on a 1-phase one). The register survives an inverter
restart, so a cap whose restore never arrives would stop the battery charging
for good. The guard keeps, per inverter, the value the inverter held before the
first cap (the held prior) and the deadline after which the revert loop writes
it back unless a renewal moved it.

Mirrors the edge firmware's ``bcc_guard`` table and ``bcc_guard_board_tick``
(zigbee_poller.c) and the phone harvester's ``BatteryChargeCurrentPriorStore``:

- A prior is recorded before the write and never replaced while one is held:
  a later set reads the cap itself back.
- A deadline is kept only beside a prior.
- The prior is cleared only once a restore's readback confirmed the value, or
  the inverter already held it.

Persisted in SQLite with the same open, operate, close pattern as
``island_event_store.py``, so a Home Assistant restart in the middle of a cap
keeps what is needed to undo it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SET_COMMAND = "set_battery_charge_current"
RESTORE_COMMAND = "restore_battery_charge_current"
COMMANDS = frozenset({SET_COMMAND, RESTORE_COMMAND})

# The largest value the Max A Charge register holds, in amps. 0 is a legal
# prior: an owner may have set the pack not to charge.
PRIOR_MAX_A = 350

# How often the revert loop looks for a passed deadline.
_TICK_INTERVAL_S = 60

_INITIALIZED: set[str] = set()


def valid_prior(value: object) -> int | None:
    """*value* as a prior, or None: an integer 0-350, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= PRIOR_MAX_A else None


class BatteryChargeCurrentError(RuntimeError):
    """A battery charge current command that failed.

    ``str(err)`` is the bare code (``prior_read_failed``, ``no_prior``,
    ``write_failed``, ``verify_read_failed``, ``verify_mismatch``,
    ``bad_payload``), the one the board and the phone ACK. ``ack`` is the
    failed ACK's result: ``error`` plus each of prev/target/verify that was
    known, under the ACK keys every host uses.
    """

    def __init__(self, code: str, ack: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.ack = {"error": code, **(ack or {})}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if db_path not in _INITIALIZED:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS charge_cap_guard (
                inverter_id  TEXT PRIMARY KEY,
                prev_a       INTEGER NOT NULL,
                revert_at_ms INTEGER
            );
            """
        )
        conn.commit()
        _INITIALIZED.add(db_path)
    return conn


class ChargeCapGuard:
    """The held battery charge current priors, keyed by inverter id.

    ``db_path`` is the SQLite file. Pass *hass* to run the I/O on Home
    Assistant's executor pool; without it the loop's default executor runs it,
    as in ``IslandEventStore``.
    """

    def __init__(self, db_path: str, hass: HomeAssistant | None = None) -> None:
        self._db_path = db_path
        self._hass = hass

    # ── sync core ─────────────────────────────────────────────────────────────

    def _prior_sync(self, inverter_id: str) -> int | None:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT prev_a FROM charge_cap_guard WHERE inverter_id = ?", (inverter_id,)
            ).fetchone()
            return int(row["prev_a"]) if row else None
        finally:
            conn.close()

    def _record_prior_sync(self, inverter_id: str, prev_a: int) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO charge_cap_guard (inverter_id, prev_a) VALUES (?, ?)",
                (inverter_id, prev_a),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_deadline_sync(self, inverter_id: str, revert_at_ms: int) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "UPDATE charge_cap_guard SET revert_at_ms = ? WHERE inverter_id = ?",
                (revert_at_ms, inverter_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _due_sync(self, now_ms: int) -> list[tuple[str, int]]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT inverter_id, prev_a FROM charge_cap_guard "
                "WHERE revert_at_ms IS NOT NULL AND revert_at_ms <= ? "
                "ORDER BY revert_at_ms ASC",
                (now_ms,),
            ).fetchall()
            return [(r["inverter_id"], int(r["prev_a"])) for r in rows]
        finally:
            conn.close()

    def _clear_sync(self, inverter_id: str) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute("DELETE FROM charge_cap_guard WHERE inverter_id = ?", (inverter_id,))
            conn.commit()
        finally:
            conn.close()

    def _forget_except_sync(self, keep: list[str]) -> list[str]:
        conn = _connect(self._db_path)
        try:
            held = [
                r["inverter_id"] for r in conn.execute("SELECT inverter_id FROM charge_cap_guard")
            ]
            gone = [i for i in held if i not in keep]
            conn.executemany(
                "DELETE FROM charge_cap_guard WHERE inverter_id = ?", [(i,) for i in gone]
            )
            conn.commit()
            return gone
        finally:
            conn.close()

    async def _run(self, fn, *args):
        if self._hass is not None:
            return await self._hass.async_add_executor_job(fn, *args)
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # ── async API ─────────────────────────────────────────────────────────────

    async def prior(self, inverter_id: str) -> int | None:
        """The held prior for *inverter_id*, or None."""
        return await self._run(self._prior_sync, inverter_id)

    async def record_prior(self, inverter_id: str, prev_a: int) -> None:
        """Hold *prev_a* for *inverter_id*, keeping a prior already held.

        Raises ValueError for anything but an integer 0-350.
        """
        if valid_prior(prev_a) is None:
            raise ValueError(f"not a battery charge current prior: {prev_a!r}")
        await self._run(self._record_prior_sync, inverter_id, prev_a)

    async def set_deadline(self, inverter_id: str, revert_at_ms: int) -> None:
        """Arm or move the revert deadline. No-op without a held prior."""
        await self._run(self._set_deadline_sync, inverter_id, int(revert_at_ms))

    async def due(self, now_ms: int) -> list[tuple[str, int]]:
        """``(inverter id, prior)`` for every deadline at or before *now_ms*."""
        return await self._run(self._due_sync, int(now_ms))

    async def clear(self, inverter_id: str) -> None:
        """Drop the held prior and its deadline."""
        await self._run(self._clear_sync, inverter_id)

    async def forget_except(self, inverter_ids) -> list[str]:
        """Drop the entries of inverters not in *inverter_ids*; return their ids.

        An inverter removed from the configuration has nothing left to write
        its prior back to, as the phone's prior store forgets an inverter
        that leaves the phone.
        """
        return await self._run(self._forget_except_sync, list(inverter_ids))


async def charge_cap_guard_tick(guard: ChargeCapGuard, executor_for, now_ms: int) -> None:
    """Write back every held prior whose deadline has passed.

    The revert runs as ``restore_battery_charge_current`` with the prior, on
    the inverter's own WriteExecutor, which clears the prior once its
    readback confirms the value. A failure keeps it, and the next tick
    retries. An inverter with no executor, or whose spec has no battery
    charge current recipe, has nothing to write to: its prior is dropped, as
    the board drops one for an inverter no longer configured.
    """
    for inverter_id, prev_a in await guard.due(now_ms):
        executor = executor_for(inverter_id)
        if executor is None:
            _LOGGER.error(
                "Battery charge current revert: inverter %s has no executor; dropping its "
                "held prior (%s A)",
                inverter_id,
                prev_a,
            )
            await guard.clear(inverter_id)
            continue
        try:
            await executor.dispatch(RESTORE_COMMAND, {"batteryChargeCurrentA": prev_a})
        except NotImplementedError:
            _LOGGER.error(
                "Battery charge current revert: inverter %s has no charge current recipe; "
                "dropping its held prior (%s A)",
                inverter_id,
                prev_a,
            )
            await guard.clear(inverter_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Battery charge current revert: %s back to %s A failed (%s); retrying next tick",
                inverter_id,
                prev_a,
                err,
            )
        else:
            _LOGGER.warning(
                "Battery charge current revert: inverter %s back to %s A (no restore by deadline)",
                inverter_id,
                prev_a,
            )


async def run_charge_cap_guard_loop(
    *, hass, guard: ChargeCapGuard, executor_for, interval_s: int = _TICK_INTERVAL_S
) -> None:
    """Run ``charge_cap_guard_tick`` every *interval_s* until Home Assistant stops.

    Runs in cloud mode and on an island alike: the deadline exists for the
    cloud's restore that never arrives.
    """
    import time

    while not hass.is_stopping:
        try:
            await charge_cap_guard_tick(guard, executor_for, int(time.time() * 1000))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Battery charge current revert tick failed; retrying next interval")
        await asyncio.sleep(interval_s)
