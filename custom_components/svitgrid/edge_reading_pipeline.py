"""The reading pipeline for an edge box's readings: what the cloud does to a
reading before it stores it.

A stub until the port of `reading-core` (edge island, Phase 1, Task 5) fills
it in: `process` returns the reading unchanged. The ingest views
(`edge_api.EdgeIngestBatchView`, `EdgeIngestReadingView`) already call it and
store `store_data`, or answer 422 with `rejected` when it is None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PipelineResult:
    """`store_data` is what the store keeps, or None when the cloud would
    refuse the reading, with its 422 reason in `rejected`."""

    store_data: dict | None
    rejected: str | None
    warnings: list[str] = field(default_factory=list)
    bucket_diff: dict = field(default_factory=dict)


def process(reading: dict, ctx, prev: dict | None, *, now: datetime) -> PipelineResult:
    """The stored form of `reading` from the inverter `ctx` describes (its
    adopt details), given the previous stored reading `prev`."""
    return PipelineResult(store_data=dict(reading), rejected=None)
