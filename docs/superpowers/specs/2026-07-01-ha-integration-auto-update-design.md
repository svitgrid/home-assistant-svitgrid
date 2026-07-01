# Svitgrid HA Integration — Auto-Update Design

**Date:** 2026-07-01
**Repo:** `home-assistant-svitgrid` (HACS custom integration, `custom_components/svitgrid/`, current version `0.10.1`)
**Status:** Approved design, ready for implementation plan

## Problem

The Svitgrid integration is a HACS custom integration. Updating it requires the user to
manually click "Update" in HACS and restart Home Assistant. This left two users stranded on
add-on versions `< 0.9.0` that lack the runtime cloud-endpoint switch (`set_cloud_endpoint`),
blocking their staging→prod migration. Any fix we ship cannot reach a version too old to
contain the receiver (chicken-and-egg).

**This design does NOT rescue those two stranded users** (they will be handled manually). It
makes *future* transitions apply automatically, so we never strand users below a floor again:
once a user is on a version that contains this auto-updater, subsequent releases install
themselves.

## Goal

From `0.10.1` onward, the integration keeps itself on the latest GitHub release automatically,
with a standard Home Assistant update UX and a per-install opt-out.

## Decisions (settled during brainstorming)

- **Mechanism:** HA-native `UpdateEntity`, self-contained (does not depend on each user's HACS
  auto-update configuration — that dependence is what caused the stranding).
- **Auto-restart:** Yes. Code changes only load on a full HA restart, so the updater triggers
  `homeassistant.restart` — **guarded** so it never fires while a command is mid-execution.
- **Download source:** GitHub release zip (`releases/latest`), public, no auth.
- **Rollout:** Always-latest. No server-side staging/channel gate, no min-required floor.

## Architecture

Four pieces, all in `home-assistant-svitgrid`. No monorepo (`services/api`) change is required
for the core feature; one optional telemetry addition is noted at the end.

### 1. `custom_components/svitgrid/updater.py` — pure update mechanics (testable core)

No Home Assistant imports. This is the riskiest logic (network + file mutation), isolated into
one HA-free module so it can be exercised with temp dirs and fake zips.

- `async fetch_latest_release(session) -> ReleaseInfo | None`
  - GETs `https://api.github.com/repos/svitgrid/home-assistant-svitgrid/releases/latest`.
  - Returns `ReleaseInfo(tag: str, version: str, zip_url: str)` (version = tag with any leading
    `v` stripped), or `None` on any non-200 / parse failure / rate-limit (fail-open).
- `async apply_update(session, zip_url, install_dir: Path) -> str`
  - Downloads the zip to a temp dir.
  - **Validates** it contains `custom_components/svitgrid/manifest.json` with a parseable
    `version`. If not, raises `UpdateValidationError` — the live `install_dir` is never touched.
  - Backs up the current `install_dir` to a sibling `svitgrid.bak` (single backup, overwritten
    each time).
  - Atomically swaps the new `custom_components/svitgrid/` contents into `install_dir`
    (write to `install_dir.new`, then replace).
  - Returns the newly-installed version string.
  - On any failure after backup, restores from backup and re-raises.

`install_dir` is derived from `Path(__file__).parent` by the caller (the running integration's
own directory).

### 2. `custom_components/svitgrid/update.py` — Home Assistant glue

- `SvitgridUpdateCoordinator(DataUpdateCoordinator)`
  - `_async_update_data` calls `fetch_latest_release()` every **12h** (`UPDATE_CHECK_INTERVAL_S`).
  - After each refresh, if `latest.version != installed_version` **and** auto-update is enabled
    **and** the restart guard is clear, it runs the install sequence:
    `apply_update()` → `hass.services.async_call("homeassistant", "restart")`.
  - **Restart guard:** defer if a command executed recently — checks
    `ActivityTracker.last_command_at` (defer when within `RESTART_GUARD_WINDOW_S`, 60s). If
    guarded, skip this tick; the next 12h refresh (or a manual install) retries. An install
    already in progress is also skipped (single-flight `in_progress` flag).
- `SvitgridUpdateEntity(UpdateEntity, CoordinatorEntity)`
  - `installed_version` from the running `manifest.json`.
  - `latest_version` from the coordinator.
  - Supports `UpdateEntityFeature.INSTALL` so users who disable auto-update still get a manual
    "Install" button; `async_install` runs the same sequence (ignoring the auto-update flag but
    keeping the restart guard).
  - `auto_update` property reflects the config option (True by default).
  - Appears in *Settings → Updates* and as entity `update.svitgrid`.
  - Reports `in_progress` while installing.

Registered by adding `"update"` to the existing
`async_forward_entry_setups(entry, ["sensor", "binary_sensor"])` (and the matching
`async_unload_platforms`) in `__init__.py`. The coordinator is created in `__init__.py` setup so
the entity and the auto-install loop share it.

### 3. `custom_components/svitgrid/config_flow.py` — one option

Add an **"Automatic updates"** boolean to `SvitgridOptionsFlow`, **default on**
(`CONF_AUTO_UPDATE`, stored in entry options). The coordinator and entity read it live.

### 4. `command_poller.py` — telemetry (small, high value)

Add `integrationVersion` (the running manifest version) alongside the existing
`executorVersion` in the command-poll payload. This lets the cloud *see* each install's version
so we can spot stragglers still below current and handle them manually. Backward-compatible
(the server ignores unknown fields today).

## Constants (`const.py`)

- `GITHUB_REPO = "svitgrid/home-assistant-svitgrid"`
- `UPDATE_CHECK_INTERVAL_S = 12 * 3600`
- `RESTART_GUARD_WINDOW_S = 60`
- `CONF_AUTO_UPDATE = "auto_update"`

## Error handling / safety

| Situation | Behavior |
|---|---|
| GitHub unreachable / rate-limited / non-200 | Log debug, skip tick, retry in 12h (fail-open) |
| Corrupt or invalid zip (no valid manifest) | Abort before swap; live dir untouched; entity stays on installed version |
| Failure mid-swap | Restore from `svitgrid.bak`; re-raise; entity error logged |
| Command mid-execution | Defer restart to next tick |
| Install already running | Skip (single-flight) |

**Out of scope (accepted risk):** post-restart auto-rollback of a *valid but runtime-broken*
release. One `svitgrid.bak` is kept for manual recovery. This follows from the always-latest,
no-floor decision.

## Testing (TDD)

`updater.py` (pure, no HA):
- Fake release zip in a temp dir → `apply_update` replaces files and creates `svitgrid.bak`.
- Corrupt/invalid zip (missing manifest) → raises `UpdateValidationError`, live dir byte-identical.
- Failure after backup → live dir restored from backup.
- GitHub `releases/latest` JSON fixture → `fetch_latest_release` returns correct tag/version/zip_url; non-200 → `None`.

`update.py` (HA, using the existing test harness in `tests/`):
- `installed < latest` + auto-on + idle → triggers `apply_update` + `homeassistant.restart`.
- Same but a command ran within `RESTART_GUARD_WINDOW_S` → defers (no restart).
- Auto-off → no install; entity reports update available with an Install button.
- Install already in progress → second tick is a no-op.

`command_poller.py`:
- Poll payload includes `integrationVersion` equal to the manifest version.

## Deliberately NOT included (YAGNI)

- No cloud version-info endpoint — GitHub `releases/latest` is the source of truth.
- No min-required floor / repair-issue nudge.
- No staged rollout or release channel.

## Optional follow-up (not in this scope)

Record `integrationVersion` server-side (small `services/api` command-poll handler change) so the
straggler view is queryable. Can ship as a separate monorepo change; this integration change is
self-contained without it.
