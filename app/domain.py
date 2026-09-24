"""Pure interval split/merge logic for versioned calibration segments.

A device's effective calibration map is a set of non-overlapping, half-open
``[start, end)`` batch segments, each carrying a JSON content payload.

Publishing a new interval:
  * replaces the overlapping part of the previous (current) revision,
  * keeps the necessary left/right residual pieces of partially overlapped
    segments,
  * merges adjacent segments whose content is structurally identical.

This module is deliberately free of any I/O so the transformation can be
unit-tested in isolation and reused by the transactional service layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple


@dataclass(frozen=True)
class Segment:
    """A half-open batch interval ``[start, end)`` carrying ``content``."""

    start: int
    end: int
    content: Any

    def key(self) -> Tuple[int, int, str]:
        """Identity used when diffing two revisions of the segment map."""
        return (self.start, self.end, canonical_json(self.content))


def canonical_json(value: Any) -> str:
    """Deterministic JSON rendering used for equality and fingerprinting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def apply_publish(
    current: Iterable[Segment], start: int, end: int, content: Any
) -> List[Segment]:
    """Return the new effective segment list after publishing ``[start, end)``.

    ``current`` must be the sorted, non-overlapping, already-merged effective
    segment list of the previous revision.  The result is sorted and merged.
    """
    if start >= end:
        raise ValueError("interval start must be < end")

    out: List[Segment] = []
    for seg in current:
        if seg.end <= start or seg.start >= end:
            out.append(seg)  # disjoint: untouched by the new interval
            continue
        # Overlap: the covered middle is replaced; keep the residuals.
        if seg.start < start:
            out.append(Segment(seg.start, start, seg.content))
        if seg.end > end:
            out.append(Segment(end, seg.end, seg.content))
    out.append(Segment(start, end, content))
    out.sort(key=lambda s: (s.start, s.end))
    return merge_adjacent(out)


def merge_adjacent(segments: Iterable[Segment]) -> List[Segment]:
    """Merge neighbouring segments that touch and carry identical content."""
    merged: List[Segment] = []
    for seg in segments:
        if merged and merged[-1].end == seg.start and merged[-1].content == seg.content:
            merged[-1] = Segment(merged[-1].start, seg.end, seg.content)
        else:
            merged.append(seg)
    return merged
