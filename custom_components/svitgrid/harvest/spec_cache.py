"""Version-gated register-spec cache (mirrors preset_refresh)."""

from __future__ import annotations

from ..preset_refresh import should_merge  # reuse numeric/str version compare


async def load_spec(fetch, model_id: str, cached: dict | None) -> tuple[dict | None, bool]:
    """Fetch the spec; return (spec, changed). Fail-open: keep `cached` on
    error/None. `fetch` is an async callable (model_id) -> dict | None."""
    try:
        fetched = await fetch(model_id)
    except Exception:  # noqa: BLE001  fail-open
        return cached, False
    if not fetched:
        return cached, False
    cached_version = cached.get("version") if cached else None
    if cached is None or should_merge(fetched.get("version"), cached_version):
        return fetched, True
    return cached, False
