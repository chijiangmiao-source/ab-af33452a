"""HTTP API for the wafer calibration service.

The process only starts accepting requests after the database is reachable
and the schema is in place (lifespan gate), so publishes can only be
received once the service is healthy.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("calibration-service")

DEFAULT_DSN = "postgresql://calibration:calibration@localhost:5432/calibration"


class Interval(BaseModel):
    """Half-open batch interval [start, end)."""

    start: int
    end: int


class PublishRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=256)
    seen_revision: int = Field(ge=0)
    interval: Interval
    content: Any = Field(...)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    db.wait_for_database(dsn)  # readiness gate: no traffic before the DB is up
    pool = db.create_pool(dsn)
    db.ensure_schema(pool)
    app.state.pool = pool
    logger.info("calibration service ready")
    yield
    pool.close()


app = FastAPI(title="wafer-calibration-service", lifespan=lifespan)


@app.exception_handler(service.ApiError)
async def api_error_handler(request: Request, exc: service.ApiError):
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.exception_handler(psycopg.Error)
async def database_error_handler(request: Request, exc: psycopg.Error):
    logger.exception("database error while handling %s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "DATABASE_UNAVAILABLE", "message": "database is unavailable"}},
    )


@app.get("/health")
def health(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.post("/v1/devices/{device_id}/calibrations")
def publish_calibration(device_id: str, body: PublishRequest, request: Request):
    """Publish a calibration interval; returns 201, or 200 on idempotent replay."""
    with request.app.state.pool.connection() as conn:
        payload, replayed = service.publish(
            conn,
            device_id=device_id,
            operation_id=body.operation_id,
            seen_revision=body.seen_revision,
            start=body.interval.start,
            end=body.interval.end,
            content=body.content,
        )
    return JSONResponse(status_code=200 if replayed else 201, content=payload)


@app.get("/v1/devices/{device_id}")
def read_device(device_id: str, request: Request):
    with request.app.state.pool.connection() as conn:
        return service.get_device(conn, device_id)


@app.get("/v1/devices/{device_id}/calibrations/effective")
def read_effective(
    device_id: str,
    request: Request,
    batch: int,
    revision: Optional[int] = None,
):
    """The uniquely effective content, interval boundaries and revision."""
    with request.app.state.pool.connection() as conn:
        return service.get_effective(conn, device_id, batch, revision)
