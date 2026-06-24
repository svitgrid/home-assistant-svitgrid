"""Pure aggregation for raw->hourly->daily roll-ups (Sub-project 1)."""
from __future__ import annotations

from typing import Any

from .const import DAILY_COUNTER_FIELDS, INSTANTANEOUS_FIELDS, PEAK_FIELDS


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    payloads = [r["payload"] for r in rows]
    avgs: dict[str, float] = {}
    for f in INSTANTANEOUS_FIELDS:
        vals = [p[f] for p in payloads if isinstance(p.get(f), (int, float))]
        if vals:
            avgs[f] = sum(vals) / len(vals)
    peaks: dict[str, float] = {}
    for f in PEAK_FIELDS:
        vals = [p[f] for p in payloads if isinstance(p.get(f), (int, float))]
        if vals:
            peaks[f] = max(vals)
    energy: dict[str, float] = {}
    for f in DAILY_COUNTER_FIELDS:
        vals = [p[f] for p in payloads if isinstance(p.get(f), (int, float))]
        if vals:
            energy[f] = max(vals)
    return {"sample_count": len(payloads), "avgs": avgs, "peaks": peaks, "energy": energy}


def merge_hourly(hourly: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(h["sample_count"] for h in hourly) or 1
    avgs: dict[str, float] = {}
    fields = {f for h in hourly for f in h["avgs"]}
    for f in fields:
        num = sum(h["avgs"][f] * h["sample_count"] for h in hourly if f in h["avgs"])
        den = sum(h["sample_count"] for h in hourly if f in h["avgs"]) or 1
        avgs[f] = num / den
    peaks: dict[str, float] = {}
    for f in {f for h in hourly for f in h["peaks"]}:
        peaks[f] = max(h["peaks"][f] for h in hourly if f in h["peaks"])
    energy: dict[str, float] = {}
    for f in {f for h in hourly for f in h["energy"]}:
        energy[f] = max(h["energy"][f] for h in hourly if f in h["energy"])
    return {"sample_count": sum(h["sample_count"] for h in hourly),
            "avgs": avgs, "peaks": peaks, "energy": energy}
