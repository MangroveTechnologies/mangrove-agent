"""Structured logging with correlation_id propagation.

JSON lines in non-local envs; pretty console in local. Every request gets
a correlation_id bound into a contextvar; subsequent log calls in that
request scope automatically carry it. A FastAPI middleware generates the
ID and echoes it back in the `X-Correlation-Id` response header.

Canonical event names (use verbatim in `.info()` / `.warning()` calls so
subagents + log-tailers can grep for them consistently):

    # lifecycle
    "app.startup", "app.shutdown", "db.migrated", "scheduler.started"

    # auth
    "auth.accepted", "auth.rejected"

    # wallet
    "wallet.created", "wallet.signed_tx"

    # strategy
    "strategy.created", "strategy.status_changed",
    "strategy.tick.started", "strategy.tick.completed", "strategy.tick.errored",
    "strategy.tick.order_intent.skipped"

    # order execution
    "order.executing", "order.live.signed", "order.live.broadcast",
    "order.live.confirmed", "order.paper.simulated", "order.errored"

    # scheduler
    "scheduler.job.registered", "scheduler.job.cancelled",
    "scheduler.job.fired", "scheduler.job.errored"

    # sdk
    "sdk.call.started", "sdk.call.completed", "sdk.call.errored"

Pass the event name as the first positional arg to the logger method;
attach any structured fields as kwargs:

    log.info("strategy.tick.completed", strategy_id=sid, order_count=3, duration_ms=42)
"""
from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from contextlib import contextmanager
from typing import Iterator

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

# Contextvar holding the correlation_id for the current async task / thread.
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def _add_correlation_id(_, __, event_dict: dict) -> dict:
    """structlog processor: inject the current correlation_id if one is bound."""
    cid = _correlation_id_var.get()
    if cid is not None and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict


def configure(env: str) -> None:
    """Configure structlog + stdlib logging for the app.

    - env == "local": ConsoleRenderer (human-readable, colored)
    - otherwise:      JSONRenderer (one JSON object per line)

    Call once at startup. Safe to call multiple times (idempotent).
    """
    # Wire stdlib logging to stderr at INFO so library logs flow through.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.INFO,
        force=True,
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        _add_correlation_id,
    ]

    if env == "local":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        # cache_logger_on_first_use=False so tests can reconfigure structlog
        # mid-process (e.g. to capture events into a buffer) without being
        # blocked by already-cached wrappers bound to the prior config.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a logger, optionally bound to a module name."""
    logger = structlog.get_logger()
    if name:
        logger = logger.bind(logger=name)
    return logger


@contextmanager
def with_correlation_id(cid: str) -> Iterator[str]:
    """Bind a correlation_id for the duration of a block.

        with with_correlation_id(some_id):
            log.info("event.name", ...)   # carries correlation_id automatically

    Used by the scheduler tick callback so every log line in that tick
    shares the same correlation_id.
    """
    token = _correlation_id_var.set(cid)
    try:
        yield cid
    finally:
        _correlation_id_var.reset(token)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that attaches a correlation_id to each request.

    - If the caller supplies `X-Correlation-Id`, use it.
    - Otherwise, generate a new UUID4.
    - Bind it into the logging contextvar for the request lifetime.
    - Echo it back as `X-Correlation-Id` on the response so clients can
      cross-reference against server logs.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        token = _correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            _correlation_id_var.reset(token)
        response.headers["X-Correlation-Id"] = cid
        return response
