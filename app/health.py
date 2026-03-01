"""
Health metrics collection for the SU system.

Gathers database, process, memory, disk, daemon, log pipeline, and
connection metrics into a single snapshot.  Used by the /api/health/detailed
endpoint and the periodic snapshot writer.
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.database import DATABASE_PATH, get_db
from app.logger import get_logger, _log_queue
from app.process_limiter import get_slot_status

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (used for traffic-light status)
# ---------------------------------------------------------------------------
THRESHOLDS = {
    "db_size_mb": {"warn": 100, "crit": 500},
    "table_rows": {"warn": 100_000, "crit": 500_000},
    "disk_usage_pct": {"warn": 80, "crit": 95},
    "log_queue_pct": {"warn": 50, "crit": 90},
    "process_slots_used_pct": {"warn": 66, "crit": 100},
}


def _status(value: float, key: str) -> str:
    """Return 'green', 'yellow', or 'red' based on thresholds."""
    t = THRESHOLDS.get(key)
    if not t:
        return "green"
    if value >= t["crit"]:
        return "red"
    if value >= t["warn"]:
        return "yellow"
    return "green"


# ---------------------------------------------------------------------------
# Database metrics
# ---------------------------------------------------------------------------

TABLE_NAMES = [
    "sessions", "messages", "logs", "tasks", "events",
    "interjections", "su_notes", "daemon_runs", "push_subscriptions",
    "telegram_users", "deep_learning_documents", "deep_learning_runs",
    "health_snapshots",
]


async def _db_metrics() -> dict[str, Any]:
    """Collect database file size, per-table row counts, and write latency."""
    db_size = 0
    wal_size = 0
    try:
        db_size = os.path.getsize(DATABASE_PATH)
    except OSError:
        pass
    wal_path = str(DATABASE_PATH) + "-wal"
    try:
        wal_size = os.path.getsize(wal_path)
    except OSError:
        pass

    db_size_mb = round(db_size / (1024 * 1024), 2)

    tables: dict[str, int] = {}
    oldest_rows: dict[str, str | None] = {}

    async with get_db() as db:
        for table in TABLE_NAMES:
            try:
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cursor.fetchone()
                tables[table] = row[0] if row else 0
            except Exception:
                tables[table] = -1  # table doesn't exist yet

            # Oldest row timestamp for high-growth tables
            if table in ("logs", "daemon_runs", "messages", "interjections", "su_notes"):
                ts_col = "timestamp" if table == "logs" else ("started_at" if table == "daemon_runs" else "created_at")
                try:
                    cursor = await db.execute(f"SELECT MIN({ts_col}) FROM {table}")
                    row = await cursor.fetchone()
                    oldest_rows[table] = row[0] if row and row[0] else None
                except Exception:
                    oldest_rows[table] = None

        # Write latency test
        t0 = time.monotonic()
        try:
            await db.execute(
                "INSERT INTO health_snapshots (timestamp, data) VALUES (?, ?)",
                (datetime.now(timezone.utc).isoformat(), '{"probe": true}'),
            )
            await db.commit()
            write_latency_ms = round((time.monotonic() - t0) * 1000, 1)
            # Clean up probe row
            await db.execute(
                "DELETE FROM health_snapshots WHERE data = '{\"probe\": true}'"
            )
            await db.commit()
        except Exception:
            write_latency_ms = -1

        # Integrity check (lightweight — just first page)
        try:
            cursor = await db.execute("PRAGMA integrity_check(1)")
            row = await cursor.fetchone()
            integrity = row[0] if row else "unknown"
        except Exception:
            integrity = "error"

        # Journal mode
        try:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            journal_mode = row[0] if row else "unknown"
        except Exception:
            journal_mode = "unknown"

    # Find the worst table for status
    max_rows = max(tables.values()) if tables else 0

    return {
        "status": _status(db_size_mb, "db_size_mb"),
        "file_size_bytes": db_size,
        "file_size_mb": db_size_mb,
        "wal_size_bytes": wal_size,
        "journal_mode": journal_mode,
        "integrity": integrity,
        "write_latency_ms": write_latency_ms,
        "tables": tables,
        "oldest_rows": oldest_rows,
        "worst_table_status": _status(max_rows, "table_rows"),
    }


# ---------------------------------------------------------------------------
# Process / memory metrics
# ---------------------------------------------------------------------------

def _process_metrics() -> dict[str, Any]:
    """Collect current process memory and child process info."""
    from app.agent_registry import _agents, _active_ws, _last_activity
    from app.scheduler import _reminded_event_ids
    from app.memory_manager import _session_counters, _pending_tasks, _rem_checkpoints, _checkpoint_tasks

    # RSS of current process (Python)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # maxrss is in bytes on macOS, KB on Linux
    rss_bytes = usage.ru_maxrss
    if os.uname().sysname == "Linux":
        rss_bytes *= 1024  # KB -> bytes

    # GC stats
    gc_counts = gc.get_count()

    # In-memory structure sizes
    in_memory = {
        "agent_registry": len(_agents),
        "active_websockets": len(_active_ws),
        "agent_last_activity": len(_last_activity),
        "reminded_event_ids": len(_reminded_event_ids),
        "session_counters": len(_session_counters),
        "pending_memory_tasks": len(_pending_tasks),
        "rem_checkpoints": len(_rem_checkpoints),
        "checkpoint_tasks": len(_checkpoint_tasks),
    }

    # Process limiter
    slots = get_slot_status()
    slot_pct = (slots["used_slots"] / slots["max_slots"] * 100) if slots["max_slots"] > 0 else 0

    return {
        "status": _status(slot_pct, "process_slots_used_pct"),
        "rss_bytes": rss_bytes,
        "rss_mb": round(rss_bytes / (1024 * 1024), 1),
        "gc_counts": {"gen0": gc_counts[0], "gen1": gc_counts[1], "gen2": gc_counts[2]},
        "process_limiter": slots,
        "in_memory_structures": in_memory,
    }


# ---------------------------------------------------------------------------
# Disk / volume metrics
# ---------------------------------------------------------------------------

def _disk_metrics() -> dict[str, Any]:
    """Collect disk usage for key paths."""
    import shutil

    volumes: dict[str, Any] = {}

    for name, path in [
        ("data", "/data"),
        ("basic_memory", "/home/appuser/basic-memory"),
        ("root", "/"),
    ]:
        try:
            usage = shutil.disk_usage(path)
            pct = round(usage.used / usage.total * 100, 1) if usage.total > 0 else 0
            volumes[name] = {
                "path": path,
                "total_mb": round(usage.total / (1024 * 1024), 1),
                "used_mb": round(usage.used / (1024 * 1024), 1),
                "free_mb": round(usage.free / (1024 * 1024), 1),
                "used_pct": pct,
                "status": _status(pct, "disk_usage_pct"),
            }
        except OSError:
            volumes[name] = {"path": path, "error": "not accessible"}

    # basic-memory file count
    bm_path = Path("/home/appuser/basic-memory")
    bm_file_count = 0
    bm_total_bytes = 0
    if bm_path.exists():
        for f in bm_path.rglob("*.md"):
            bm_file_count += 1
            try:
                bm_total_bytes += f.stat().st_size
            except OSError:
                pass

    volumes["basic_memory_files"] = {
        "count": bm_file_count,
        "total_mb": round(bm_total_bytes / (1024 * 1024), 2),
    }

    # Worst status across volumes
    worst = "green"
    for v in volumes.values():
        if isinstance(v, dict) and v.get("status") == "red":
            worst = "red"
            break
        if isinstance(v, dict) and v.get("status") == "yellow" and worst == "green":
            worst = "yellow"

    return {
        "status": worst,
        "volumes": volumes,
    }


# ---------------------------------------------------------------------------
# Daemon health metrics
# ---------------------------------------------------------------------------

async def _daemon_metrics() -> dict[str, Any]:
    """Collect per-daemon health stats from daemon_runs table."""
    from app.daemon_registry import daemon_registry

    now = datetime.now(timezone.utc)
    since_24h = (now - timedelta(hours=24)).isoformat()

    daemons: dict[str, Any] = {}

    async with get_db() as db:
        for info in daemon_registry.list_daemons():
            name = info.name

            # Last successful run
            cursor = await db.execute(
                "SELECT MAX(ended_at) FROM daemon_runs WHERE daemon_name = ? AND status = 'completed'",
                (name,),
            )
            row = await cursor.fetchone()
            last_success = row[0] if row and row[0] else None

            # Counts in last 24h
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM daemon_runs WHERE daemon_name = ? AND started_at >= ? GROUP BY status",
                (name, since_24h),
            )
            status_counts: dict[str, int] = {}
            for row in await cursor.fetchall():
                status_counts[row[0]] = row[1]

            total_24h = sum(status_counts.values())
            failed_24h = status_counts.get("failed", 0)
            failure_rate = round(failed_24h / total_24h * 100, 1) if total_24h > 0 else 0

            # Average duration (last 24h, completed only)
            cursor = await db.execute(
                "SELECT AVG(duration_ms) FROM daemon_runs WHERE daemon_name = ? AND status = 'completed' AND started_at >= ?",
                (name, since_24h),
            )
            row = await cursor.fetchone()
            avg_duration_ms = round(row[0], 1) if row and row[0] else None

            # Stuck runs
            cursor = await db.execute(
                "SELECT COUNT(*) FROM daemon_runs WHERE daemon_name = ? AND status = 'running'",
                (name,),
            )
            row = await cursor.fetchone()
            stuck_count = row[0] if row else 0

            # Status determination
            daemon_status = "green"
            if stuck_count > 0:
                daemon_status = "red"
            elif failure_rate > 50:
                daemon_status = "red"
            elif failure_rate > 20:
                daemon_status = "yellow"

            daemons[name] = {
                "display_name": info.display_name,
                "status": daemon_status,
                "last_success": last_success,
                "runs_24h": total_24h,
                "completed_24h": status_counts.get("completed", 0),
                "failed_24h": failed_24h,
                "failure_rate_pct": failure_rate,
                "avg_duration_ms": avg_duration_ms,
                "stuck_runs": stuck_count,
            }

    # Worst daemon status
    worst = "green"
    for d in daemons.values():
        if d["status"] == "red":
            worst = "red"
            break
        if d["status"] == "yellow" and worst == "green":
            worst = "yellow"

    return {
        "status": worst,
        "daemons": daemons,
    }


# ---------------------------------------------------------------------------
# Log pipeline metrics
# ---------------------------------------------------------------------------

async def _log_pipeline_metrics() -> dict[str, Any]:
    """Collect log queue and volume metrics."""
    queue_size = _log_queue.qsize() if _log_queue else 0
    queue_max = _log_queue.maxsize if _log_queue else 4096
    queue_pct = round(queue_size / queue_max * 100, 1) if queue_max > 0 else 0

    # Log volume (last hour)
    now = datetime.now(timezone.utc)
    since_1h = (now - timedelta(hours=1)).isoformat()

    level_counts: dict[str, int] = {}
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT level, COUNT(*) FROM logs WHERE timestamp >= ? GROUP BY level",
            (since_1h,),
        )
        for row in await cursor.fetchall():
            level_counts[row[0]] = row[1]

    total_1h = sum(level_counts.values())

    return {
        "status": _status(queue_pct, "log_queue_pct"),
        "queue_size": queue_size,
        "queue_max": queue_max,
        "queue_pct": queue_pct,
        "logs_last_hour": total_1h,
        "logs_by_level_last_hour": level_counts,
    }


# ---------------------------------------------------------------------------
# Connection metrics
# ---------------------------------------------------------------------------

def _connection_metrics() -> dict[str, Any]:
    """Collect WebSocket connection info."""
    # Import here to avoid circular dependency
    from app.main import _active_connections

    ws_count = len(_active_connections)
    session_ids = list(_active_connections.keys())

    return {
        "status": "green",
        "websocket_connections": ws_count,
        "connected_sessions": session_ids,
    }


# ---------------------------------------------------------------------------
# Aggregate health snapshot
# ---------------------------------------------------------------------------

async def collect_health_snapshot() -> dict[str, Any]:
    """Collect all metrics into a single snapshot dict."""
    t0 = time.monotonic()

    # Run independent collectors in parallel
    db_task = asyncio.create_task(_db_metrics())
    daemon_task = asyncio.create_task(_daemon_metrics())
    log_task = asyncio.create_task(_log_pipeline_metrics())

    db = await db_task
    process = _process_metrics()
    disk = _disk_metrics()
    daemons = await daemon_task
    log_pipeline = await log_task
    connections = _connection_metrics()

    # Overall status: worst of all sections
    all_statuses = [
        db["status"], db["worst_table_status"],
        process["status"], disk["status"],
        daemons["status"], log_pipeline["status"],
    ]
    if "red" in all_statuses:
        overall = "red"
    elif "yellow" in all_statuses:
        overall = "yellow"
    else:
        overall = "green"

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    return {
        "status": overall,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "collection_time_ms": elapsed_ms,
        "database": db,
        "process": process,
        "disk": disk,
        "daemons": daemons,
        "log_pipeline": log_pipeline,
        "connections": connections,
    }


# ---------------------------------------------------------------------------
# Snapshot persistence (for historical trends)
# ---------------------------------------------------------------------------

async def save_health_snapshot(snapshot: dict[str, Any]) -> None:
    """Persist a health snapshot to the health_snapshots table."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO health_snapshots (timestamp, data) VALUES (?, ?)",
            (snapshot["collected_at"], json.dumps(snapshot)),
        )
        await db.commit()


async def get_health_history(hours: int = 24, limit: int = 288) -> list[dict[str, Any]]:
    """Return recent health snapshots for trend display.

    Default: last 24 hours, up to 288 entries (one per 5 minutes).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT timestamp, data FROM health_snapshots WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT ?",
            (since, limit),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            try:
                data = json.loads(row[1])
                data["_ts"] = row[0]
                result.append(data)
            except (json.JSONDecodeError, TypeError):
                pass
        return result


# ---------------------------------------------------------------------------
# Data retention / cleanup
# ---------------------------------------------------------------------------

async def run_retention_cleanup() -> dict[str, int]:
    """Delete old data from high-growth tables.

    Returns a dict of {table: rows_deleted}.
    """
    now = datetime.now(timezone.utc)
    deleted: dict[str, int] = {}

    async with get_db() as db:
        # Logs: keep 7 days
        cutoff = (now - timedelta(days=7)).isoformat()
        cursor = await db.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
        deleted["logs"] = cursor.rowcount
        await db.commit()

        # Daemon runs: keep 14 days
        cutoff = (now - timedelta(days=14)).isoformat()
        cursor = await db.execute("DELETE FROM daemon_runs WHERE started_at < ?", (cutoff,))
        deleted["daemon_runs"] = cursor.rowcount
        await db.commit()

        # Ended sessions + their messages: keep 30 days
        cutoff = (now - timedelta(days=30)).isoformat()
        # First get the session IDs to delete
        cursor = await db.execute(
            "SELECT id FROM sessions WHERE status = 'ended' AND last_activity < ?",
            (cutoff,),
        )
        old_sessions = [row[0] for row in await cursor.fetchall()]
        if old_sessions:
            placeholders = ",".join("?" for _ in old_sessions)
            cursor = await db.execute(
                f"DELETE FROM messages WHERE session_id IN ({placeholders})",
                old_sessions,
            )
            deleted["messages"] = cursor.rowcount
            cursor = await db.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})",
                old_sessions,
            )
            deleted["sessions"] = cursor.rowcount
            await db.commit()
        else:
            deleted["messages"] = 0
            deleted["sessions"] = 0

        # Delivered/dismissed interjections: keep 30 days
        cutoff = (now - timedelta(days=30)).isoformat()
        cursor = await db.execute(
            "DELETE FROM interjections WHERE status IN ('delivered', 'dismissed') AND created_at < ?",
            (cutoff,),
        )
        deleted["interjections"] = cursor.rowcount
        await db.commit()

        # Completed SU notes: keep 30 days
        cursor = await db.execute(
            "DELETE FROM su_notes WHERE status = 'done' AND completed_at < ?",
            (cutoff,),
        )
        deleted["su_notes"] = cursor.rowcount
        await db.commit()

        # Health snapshots: keep 7 days
        cutoff = (now - timedelta(days=7)).isoformat()
        cursor = await db.execute("DELETE FROM health_snapshots WHERE timestamp < ?", (cutoff,))
        deleted["health_snapshots"] = cursor.rowcount
        await db.commit()

        # VACUUM to reclaim space (only if we actually deleted something)
        total_deleted = sum(deleted.values())
        if total_deleted > 100:
            try:
                await db.execute("VACUUM")
            except Exception:
                pass  # VACUUM can fail under concurrent access, that's OK

    log.info("health.retention_cleanup", **deleted, total=sum(deleted.values()))
    return deleted
