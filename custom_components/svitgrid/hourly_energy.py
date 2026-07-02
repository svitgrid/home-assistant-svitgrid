"""Pure per-hour import/export energy delta computation.

Turns cumulative daily energy counters (sampled per hour) into per-hour
energy deltas, bucketed to household-local date/hour. This is the risky
foundation of the island financial-settlements feature -- every downstream
tariff calculation depends on these deltas being correct, especially around
counter resets.

Reset-handling discipline mirrors the established pattern in the main
svitgrid repo (`services/api/src/services/derived-daily.ts`,
`firstPostResetIndex` / `counterDropIndices` / `counterNullDropIndices`):
a cumulative counter that resets mid-stream (a numeric drop, or a
meaningful value going to null/absent and coming back) must never be
diffed straight through -- that manufactures a bogus negative or inflated
delta. Instead the post-reset sample is treated as a fresh start, exactly
like the first sample of a new period. Here the "period" is the local day
(the counters reset at local midnight by design), and a mid-day drop is
treated the same way a post-reset bucket is treated in derived-daily.ts:
`max(0, value)` from zero, not `value - previous`.

No I/O; pure functions only.
"""

from __future__ import annotations


def _accumulate_track(rows: list[dict], cum_field: str, out_field: str) -> dict[tuple, float]:
    """Compute per-(local_date, hour) deltas for a single cumulative track.

    Returns a mapping of (local_date, hour) -> delta for hours where the
    input cumulative value was present. Hours where the input was None are
    absent from the result entirely (a gap), and do not advance the
    running "previous present cumulative" state.
    """
    result: dict[tuple, float] = {}
    prev_cum: float | None = None
    prev_date: str | None = None

    for row in rows:
        local_date = row["local_date"]
        hour = row["hour"]
        cum = row.get(cum_field)

        if local_date != prev_date:
            # New local day: the running previous always resets, regardless
            # of what the last hour of the previous day held.
            prev_cum = None
            prev_date = local_date

        if cum is None:
            # Gap hour: emits no delta and does not touch prev_cum.
            continue

        if prev_cum is None:
            # First present hour of this local day: baseline is 0 at
            # local midnight.
            delta = cum
        elif cum >= prev_cum:
            delta = cum - prev_cum
        else:
            # Mid-day drop: a counter reset/anomaly within the same local
            # day. Treat as a fresh post-reset start (same formula as the
            # first-present-hour-of-day case), never as a negative diff --
            # mirrors derived-daily.ts's firstPostResetIndex: everything
            # from the reset point on is measured from zero, not from the
            # stale pre-reset value.
            delta = cum

        result[(local_date, hour)] = max(0.0, delta)
        prev_cum = cum

    return result


def per_hour_deltas(hours: list[dict]) -> list[dict]:
    """Compute per-hour import/export energy deltas from cumulative counters.

    Args:
        hours: rows of {"local_date": "YYYY-MM-DD", "hour": 0..23,
            "import_cum": float|None, "export_cum": float|None} -- the
            cumulative daily counter's value at the END of that hour, or
            None if no data was reported that hour. Need not be
            pre-sorted.

    Returns:
        Rows of {"local_date", "hour", "importKwh", "exportKwh"}, one per
        (local_date, hour) present in the input (i.e. at least one of
        import_cum/export_cum was not None for that hour). import and
        export are computed as fully independent tracks: if only one of
        the two was present for a given hour, that hour's other field is
        0.0 (the row is still emitted, since the present field's delta is
        real) rather than the row being dropped.
    """
    sorted_rows = sorted(hours, key=lambda r: (r["local_date"], r["hour"]))

    import_deltas = _accumulate_track(sorted_rows, "import_cum", "importKwh")
    export_deltas = _accumulate_track(sorted_rows, "export_cum", "exportKwh")

    out: list[dict] = []
    for row in sorted_rows:
        key = (row["local_date"], row["hour"])
        has_import = key in import_deltas
        has_export = key in export_deltas
        if not has_import and not has_export:
            # Fully-gap hour for both tracks: no bucket at all.
            continue
        out.append(
            {
                "local_date": row["local_date"],
                "hour": row["hour"],
                "importKwh": import_deltas.get(key, 0.0),
                "exportKwh": export_deltas.get(key, 0.0),
            }
        )

    return out
