"""
Structured logging module for SU.

All application logging flows through this module. Logs are emitted as
machine-readable JSON lines (compatible with Claude session entry format)
and simultaneously persisted to the SQLite ``logs`` table for efficient
querying via the log viewer UI.

Usage::

    from app.logger import get_logger

    log = get_logger(__name__)
    log.info("ws.connect", session_id=sid, remote=addr)
    log.error("sdk.send_failed", session_id=sid, error=str(e))
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone

from app.tz import LOCAL_TZ
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Persistent writer — batches INSERT statements to avoid per-log I/O
# ---------------------------------------------------------------------------
_log_queue: asyncio.Queue[dict[str, Any]] | None = None
_writer_task: asyncio.Task | None = None


async def _flush_queue() -> None:
    """Background coroutine: drains _log_queue and bulk-inserts into SQLite."""
    from app.database import get_db

    assert _log_queue is not None
    while True:
        batch: list[dict[str, Any]] = []
        # Block on first item, then drain up to 64 more without blocking
        try:
            first = await asyncio.wait_for(_log_queue.get(), timeout=2.0)
            batch.append(first)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break

        for _ in range(64):
            try:
                batch.append(_log_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        try:
            async with get_db() as db:
                await db.executemany(
                    """INSERT INTO logs
                       (timestamp, level, event, module, session_id, data)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            entry["timestamp"],
                            entry["level"],
                            entry["event"],
                            entry["module"],
                            entry.get("session_id"),
                            json.dumps(
                                {
                                    k: v
                                    for k, v in entry.items()
                                    if k
                                    not in (
                                        "timestamp",
                                        "level",
                                        "event",
                                        "module",
                                        "session_id",
                                    )
                                }
                            ),
                        )
                        for entry in batch
                    ],
                )
                await db.commit()
        except Exception:
            # Last resort — don't let DB errors kill the writer
            sys.stderr.write(
                f"[logger] failed to persist {len(batch)} log entries\n"
            )


async def start_log_writer() -> None:
    """Call once at app startup (inside the lifespan context)."""
    global _log_queue, _writer_task
    _log_queue = asyncio.Queue(maxsize=4096)
    _writer_task = asyncio.create_task(_flush_queue(), name="log-writer")


async def stop_log_writer() -> None:
    """Call once at app shutdown."""
    global _writer_task
    if _writer_task:
        _writer_task.cancel()
        try:
            await _writer_task
        except asyncio.CancelledError:
            pass
        _writer_task = None


# ---------------------------------------------------------------------------
# JSON formatter for stdout
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emit each record as a single JSON line on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=LOCAL_TZ
            ).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "module": record.name,
        }
        # Merge structured kwargs attached by StructuredLogger
        extra = getattr(record, "_structured", None)
        if extra:
            entry.update(extra)
        if record.exc_info and record.exc_info[1]:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


# Install once at module-import time so every logger gets JSON output.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logging.root.handlers = [_handler]
logging.root.setLevel(logging.DEBUG)

# Quiet noisy third-party loggers
for _name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpcore", "httpx", "websockets"):
    logging.getLogger(_name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Public API: structured logger
# ---------------------------------------------------------------------------

class StructuredLogger:
    """Thin wrapper around stdlib logging that attaches structured data."""

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    # -- convenience levels ---------------------------------------------------

    def debug(self, event: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, event, kw)

    def info(self, event: str, **kw: Any) -> None:
        self._emit(logging.INFO, event, kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._emit(logging.WARNING, event, kw)

    def error(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, kw, exc_info=True)

    # -- internal -------------------------------------------------------------

    def _emit(
        self,
        level: int,
        event: str,
        kw: dict[str, Any],
        exc_info: bool = False,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return

        record = self._logger.makeRecord(
            name=self._logger.name,
            level=level,
            fn="",
            lno=0,
            msg=event,
            args=(),
            exc_info=sys.exc_info() if exc_info else None,
        )
        record._structured = kw  # type: ignore[attr-defined]
        self._logger.handle(record)

        # Enqueue for DB persistence (best-effort, non-blocking)
        if _log_queue is not None:
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=LOCAL_TZ
                ).isoformat(),
                "level": logging.getLevelName(level).lower(),
                "event": event,
                "module": self._logger.name,
                **kw,
            }
            if exc_info:
                entry["traceback"] = "".join(
                    traceback.format_exception(*sys.exc_info())
                )
            try:
                _log_queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # drop rather than block


def get_logger(name: str) -> StructuredLogger:
    """Return a structured logger for the given module name."""
    return StructuredLogger(name)
