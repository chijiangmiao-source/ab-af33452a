"""Transactional publish/query logic.

Concurrency model
-----------------
Every publish serialises on the device's row (``SELECT ... FOR UPDATE``),
so for a given device exactly one transaction can advance the revision at a
time.  A concurrent publisher that presented the same ``seen_revision``
observes the bumped ``current_revision`` after the lock wait and fails with
``STALE_REVISION`` — only one of them can win.

Idempotency
-----------
``(device_id, operation_id)`` is unique.  Replaying the same operation with
the same parameters returns the stored first result without creating a new
revision; reusing the operation id with different parameters fails with
``OPERATION_CONFLICT``.

History
-------
Segment rows are immutable.  A publish closes superseded rows
(``to_rev = new_revision``) and inserts residual/new rows
(``from_rev = new_revision``); untouched rows keep spanning.  The effective
map at any revision ``r`` is therefore always recomputable as the rows with
``from_rev <= r AND (to_rev IS NULL OR to_rev > r)``.

Atomicity
---------
All writes of a publish happen inside one transaction; any failure rolls
the whole thing back, so a failed publish can never leave a partial split.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional, Tuple

from psycopg.types.json import Jsonb

from .domain import Segment, apply_publish, canonical_json


class ApiError(Exception):
    """Stable, machine-readable API error."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _fingerprint(
    device_id: str,
    operation_id: str,
    seen_revision: int,
    start: int,
    end: int,
    content: Any,
) -> str:
    material = canonical_json(
        {
            "device_id": device_id,
            "operation_id": operation_id,
            "seen_revision": seen_revision,
            "interval": [start, end],
            "content": content,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _payload(
    device_id: str,
    operation_id: str,
    revision: int,
    start: int,
    end: int,
    content: Any,
) -> Dict[str, Any]:
    return {
        "device_id": device_id,
        "operation_id": operation_id,
        "revision": revision,
        "interval": {"start": start, "end": end},
        "content": content,
    }


def publish(
    conn,
    device_id: str,
    operation_id: str,
    seen_revision: int,
    start: int,
    end: int,
    content: Any,
) -> Tuple[Dict[str, Any], bool]:
    """Publish a calibration interval.

    Returns ``(payload, replayed)``: ``replayed`` is True when the operation
    had already succeeded before and the stored first result is returned.
    """
    if start >= end:
        raise ApiError(400, "INVALID_INTERVAL", "interval start must be < end")

    fingerprint = _fingerprint(device_id, operation_id, seen_revision, start, end, content)

    with conn.transaction():
        # Create the device row if needed, then serialise on it.
        conn.execute(
            "INSERT INTO devices (device_id) VALUES (%s) ON CONFLICT (device_id) DO NOTHING",
            (device_id,),
        )
        row = conn.execute(
            "SELECT current_revision FROM devices WHERE device_id = %s FOR UPDATE",
            (device_id,),
        ).fetchone()
        current_revision = row[0]

        # Idempotency is checked before the revision check so that a delayed
        # retry of an already-applied publish replays instead of failing.
        existing = conn.execute(
            """
            SELECT revision, request_fingerprint, interval_start, interval_end, content
            FROM revisions
            WHERE device_id = %s AND operation_id = %s
            """,
            (device_id, operation_id),
        ).fetchone()
        if existing is not None:
            if existing[1] != fingerprint:
                raise ApiError(
                    409,
                    "OPERATION_CONFLICT",
                    "operation_id was already used with different parameters",
                )
            return (
                _payload(device_id, operation_id, existing[0], existing[2], existing[3], existing[4]),
                True,
            )

        if seen_revision != current_revision:
            raise ApiError(
                409,
                "STALE_REVISION",
                f"seen_revision {seen_revision} does not match current revision {current_revision}",
            )

        new_revision = current_revision + 1

        rows = conn.execute(
            """
            SELECT batch_start, batch_end, content
            FROM segments
            WHERE device_id = %s AND to_rev IS NULL
            ORDER BY batch_start
            """,
            (device_id,),
        ).fetchall()
        current_segments = [Segment(r[0], r[1], r[2]) for r in rows]
        new_segments = apply_publish(current_segments, start, end, content)

        old_keys = {s.key() for s in current_segments}
        new_keys = {s.key() for s in new_segments}

        # Close superseded segments; untouched ones keep spanning revisions.
        for seg in current_segments:
            if seg.key() not in new_keys:
                conn.execute(
                    """
                    UPDATE segments SET to_rev = %s
                    WHERE device_id = %s AND batch_start = %s AND to_rev IS NULL
                    """,
                    (new_revision, device_id, seg.start),
                )
        # Insert residuals / the new interval / merged segments.
        for seg in new_segments:
            if seg.key() not in old_keys:
                conn.execute(
                    """
                    INSERT INTO segments (device_id, batch_start, batch_end, content, from_rev)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (device_id, seg.start, seg.end, Jsonb(seg.content), new_revision),
                )

        conn.execute(
            """
            INSERT INTO revisions
                (device_id, revision, operation_id, request_fingerprint,
                 interval_start, interval_end, content)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (device_id, new_revision, operation_id, fingerprint, start, end, Jsonb(content)),
        )
        conn.execute(
            "UPDATE devices SET current_revision = %s WHERE device_id = %s",
            (new_revision, device_id),
        )

    return _payload(device_id, operation_id, new_revision, start, end, content), False


def get_device(conn, device_id: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT current_revision FROM devices WHERE device_id = %s",
        (device_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device {device_id!r}")
    return {"device_id": device_id, "current_revision": row[0]}


def get_effective(
    conn, device_id: str, batch: int, revision: Optional[int]
) -> Dict[str, Any]:
    """Return the uniquely effective segment for ``batch`` at ``revision``.

    ``revision=None`` resolves to the device's current revision.
    """
    row = conn.execute(
        "SELECT current_revision FROM devices WHERE device_id = %s",
        (device_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device {device_id!r}")
    current_revision = row[0]

    effective_revision = current_revision if revision is None else revision
    if effective_revision < 0 or effective_revision > current_revision:
        raise ApiError(
            404,
            "REVISION_NOT_FOUND",
            f"revision {effective_revision} does not exist for device {device_id!r}",
        )

    seg = conn.execute(
        """
        SELECT batch_start, batch_end, content
        FROM segments
        WHERE device_id = %s
          AND batch_start <= %s AND batch_end > %s
          AND from_rev <= %s AND (to_rev IS NULL OR to_rev > %s)
        """,
        (device_id, batch, batch, effective_revision, effective_revision),
    ).fetchone()
    if seg is None:
        raise ApiError(
            404,
            "BATCH_NOT_COVERED",
            f"batch {batch} is not covered at revision {effective_revision}",
        )

    return {
        "device_id": device_id,
        "revision": effective_revision,
        "batch": batch,
        "interval": {"start": seg[0], "end": seg[1]},
        "content": seg[2],
    }
