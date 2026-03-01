"""
Daemon process registry: tracks background process state for the daemon index.

Every background process (scheduler jobs, memory agents, system tasks) registers
itself here at startup.  Each invocation is recorded as a "run" with start/end
times, status, and optional metadata.  In-memory state provides real-time
"currently running" info; the ``daemon_runs`` SQLite table provides persistent
history.
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.logger import get_logger
from app.tz import now_iso

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------

class DaemonCategory(str, Enum):
    SCHEDULER = "scheduler"
    MEMORY = "memory"
    SYSTEM = "system"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DaemonInfo:
    """Static metadata about a registered daemon."""
    name: str
    display_name: str
    category: DaemonCategory
    interval_seconds: Optional[int] = None   # None for event-driven daemons
    condition: Optional[str] = None           # e.g. "Once/day, 6-9am UTC"
    description: str = ""


@dataclass
class RunRecord:
    """A single run of a daemon."""
    id: str
    daemon_name: str
    started_at: str                           # ISO-8601
    ended_at: Optional[str] = None
    status: RunStatus = RunStatus.RUNNING
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _insert_run(record: RunRecord) -> None:
    from app.database import get_db
    async with get_db() as db:
        await db.execute(
            """INSERT INTO daemon_runs (id, daemon_name, started_at, status, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (record.id, record.daemon_name, record.started_at,
             record.status.value, json.dumps(record.metadata) if record.metadata else None),
        )
        await db.commit()


async def _update_run(record: RunRecord) -> None:
    from app.database import get_db
    duration_ms = None
    if record.started_at and record.ended_at:
        start = datetime.fromisoformat(record.started_at)
        end = datetime.fromisoformat(record.ended_at)
        duration_ms = int((end - start).total_seconds() * 1000)
    meta_json = json.dumps(record.metadata) if record.metadata else None
    async with get_db() as db:
        await db.execute(
            """UPDATE daemon_runs
               SET ended_at = ?, status = ?, error = ?, duration_ms = ?, metadata = ?
               WHERE id = ?""",
            (record.ended_at, record.status.value, record.error, duration_ms,
             meta_json, record.id),
        )
        await db.commit()


async def cleanup_stale_runs() -> None:
    """Mark any runs still 'running' in the DB as failed (process restart)."""
    from app.database import get_db
    now = now_iso()
    async with get_db() as db:
        await db.execute(
            """UPDATE daemon_runs
               SET status = 'failed', error = 'process_restart', ended_at = ?
               WHERE status = 'running'""",
            (now,),
        )
        await db.commit()


async def get_last_completed_run(daemon_name: str) -> Optional[dict]:
    from app.database import get_db
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM daemon_runs
               WHERE daemon_name = ? AND status != 'running'
               ORDER BY started_at DESC LIMIT 1""",
            (daemon_name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_runs(daemon_name: str, limit: int = 50, offset: int = 0) -> list[dict]:
    from app.database import get_db
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM daemon_runs
               WHERE daemon_name = ?
               ORDER BY started_at DESC
               LIMIT ? OFFSET ?""",
            (daemon_name, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class DaemonRegistry:
    """Singleton tracking daemon registration and run state."""

    def __init__(self):
        self._daemons: dict[str, DaemonInfo] = {}
        # daemon_name -> {run_id: RunRecord}  (supports concurrent runs)
        self._current_runs: dict[str, dict[str, RunRecord]] = {}

    def register(self, info: DaemonInfo) -> None:
        self._daemons[info.name] = info
        log.info("daemon_registry.registered", daemon=info.name, category=info.category.value)

    def list_daemons(self) -> list[DaemonInfo]:
        return list(self._daemons.values())

    def get_daemon(self, name: str) -> Optional[DaemonInfo]:
        return self._daemons.get(name)

    def get_current_runs(self, name: str) -> list[RunRecord]:
        return list(self._current_runs.get(name, {}).values())

    def is_running(self, name: str) -> bool:
        return bool(self._current_runs.get(name))

    async def start_run(self, daemon_name: str, **metadata: Any) -> str:
        """Record that a daemon run has started.  Returns run_id."""
        run_id = str(uuid.uuid4())
        now = now_iso()
        record = RunRecord(
            id=run_id,
            daemon_name=daemon_name,
            started_at=now,
            metadata=metadata,
        )
        self._current_runs.setdefault(daemon_name, {})[run_id] = record
        try:
            await _insert_run(record)
        except Exception:
            log.exception("daemon_registry.insert_failed", daemon=daemon_name, run_id=run_id)
        return run_id

    async def end_run(
        self,
        run_id: str,
        daemon_name: str,
        status: RunStatus = RunStatus.COMPLETED,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Record that a daemon run has ended."""
        now = now_iso()
        runs = self._current_runs.get(daemon_name, {})
        record = runs.pop(run_id, None)
        if not runs:
            self._current_runs.pop(daemon_name, None)

        if record:
            record.ended_at = now
            record.status = status
            record.error = error
            if metadata:
                record.metadata.update(metadata)
            try:
                await _update_run(record)
            except Exception:
                log.exception("daemon_registry.update_failed", daemon=daemon_name, run_id=run_id)
        else:
            # Record wasn't tracked in memory (maybe a restart) — update DB directly
            try:
                from app.database import get_db
                async with get_db() as db:
                    await db.execute(
                        """UPDATE daemon_runs
                           SET ended_at = ?, status = ?, error = ?
                           WHERE id = ?""",
                        (now, status.value, error, run_id),
                    )
                    await db.commit()
            except Exception:
                log.exception("daemon_registry.fallback_update_failed", run_id=run_id)


# Module-level singleton
daemon_registry = DaemonRegistry()
