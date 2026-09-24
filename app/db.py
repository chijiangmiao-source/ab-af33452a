"""PostgreSQL connectivity and schema management."""

from __future__ import annotations

import logging
import time

import psycopg
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id        TEXT PRIMARY KEY,
        current_revision BIGINT NOT NULL DEFAULT 0,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions (
        device_id           TEXT NOT NULL REFERENCES devices(device_id),
        revision            BIGINT NOT NULL,
        operation_id        TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        interval_start      BIGINT NOT NULL,
        interval_end        BIGINT NOT NULL,
        content             JSONB NOT NULL,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (device_id, revision),
        UNIQUE (device_id, operation_id)
    )
    """,
    """
    -- 半开区间 [batch_start, batch_end)；[from_rev, to_rev) 为该段生效的修订生命周期，
    -- to_rev IS NULL 表示当前仍生效。历史修订通过生命周期区间可完整复算。
    CREATE TABLE IF NOT EXISTS segments (
        device_id   TEXT NOT NULL REFERENCES devices(device_id),
        batch_start BIGINT NOT NULL,
        batch_end   BIGINT NOT NULL,
        content     JSONB NOT NULL,
        from_rev    BIGINT NOT NULL,
        to_rev      BIGINT,
        PRIMARY KEY (device_id, batch_start, from_rev),
        CHECK (batch_start < batch_end)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS segments_effective_idx
        ON segments (device_id, batch_start)
        WHERE to_rev IS NULL
    """,
]


def wait_for_database(dsn: str, attempts: int = 60, delay: float = 1.0) -> None:
    """Block until PostgreSQL accepts connections (readiness gate)."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            logger.info("database is ready")
            return
        except Exception as exc:  # noqa: BLE001 - report and retry any failure
            last_exc = exc
            logger.info("database not ready (attempt %s/%s): %s", attempt, attempts, exc)
            time.sleep(delay)
    raise RuntimeError("database did not become ready in time") from last_exc


def create_pool(dsn: str) -> ConnectionPool:
    return ConnectionPool(conninfo=dsn, min_size=1, max_size=10, kwargs={"connect_timeout": 5})


def ensure_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
    logger.info("database schema ensured")
