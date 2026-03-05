"""
Deep Learning mode: autonomous document ingestion and knowledge base refinement.

Named after how humans "deeply learn" material through study — reading, connecting,
consolidating, and reviewing. When triggered, this module:

1. INGEST — chunks uploaded documents and writes knowledge to basic-memory
2. CROSS-REFERENCE — links newly-created notes to existing knowledge
3. CONSOLIDATE — merges fragmented notes into well-organized canonical entries
4. AUDIT — scans for contradictions, duplicates, stale info
5. REFINE — fixes issues flagged during audit

The controller loop (`run_deep_learning`) orchestrates these phases sequentially,
spawning short-lived pydantic-ai agents for each step.
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import aiosqlite

from app.config import settings
from app.daemon_registry import (
    DaemonCategory,
    DaemonInfo,
    RunStatus,
    daemon_registry,
)
from app.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Daemon registration
# ---------------------------------------------------------------------------
daemon_registry.register(DaemonInfo(
    name="deep_learning",
    display_name="Deep Learning",
    category=DaemonCategory.MEMORY,
    description="Document ingestion and knowledge base refinement",
))

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
# Run ID → True means "please cancel at next checkpoint"
_cancel_flags: dict[str, bool] = {}

# Broadcast function injected by main.py at startup
_broadcast_fn: Optional[Callable[[dict[str, Any]], Coroutine]] = None


def set_broadcast_fn(fn: Callable[[dict[str, Any]], Coroutine]) -> None:
    """Set the WebSocket broadcast function. Called once from main.py."""
    global _broadcast_fn
    _broadcast_fn = fn


# ---------------------------------------------------------------------------
# Database helpers (self-contained — not in shared repositories.py)
# ---------------------------------------------------------------------------
def _db_path() -> str:
    return os.environ.get("DATABASE_PATH", "/data/sessions.db")


async def _exec(sql: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(sql, params)
        await db.commit()


async def _fetch_one(sql: str, params: tuple = ()) -> Optional[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# -- Document repo --

class DocRepo:
    @staticmethod
    async def create(filename: str, file_path: str, file_size: int) -> str:
        doc_id = str(uuid.uuid4())
        await _exec(
            "INSERT INTO deep_learning_documents (id, filename, file_path, file_size) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, filename, file_path, file_size),
        )
        return doc_id

    @staticmethod
    async def list(status: Optional[str] = None) -> list[dict]:
        if status:
            return await _fetch_all(
                "SELECT * FROM deep_learning_documents WHERE status = ? "
                "ORDER BY created_at DESC", (status,),
            )
        return await _fetch_all(
            "SELECT * FROM deep_learning_documents ORDER BY created_at DESC"
        )

    @staticmethod
    async def update(doc_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [doc_id]
        await _exec(f"UPDATE deep_learning_documents SET {sets} WHERE id = ?", tuple(vals))


# -- Run repo --

class RunRepo:
    @staticmethod
    async def create(total_documents: int = 0) -> str:
        run_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        await _exec(
            "INSERT INTO deep_learning_runs "
            "(id, status, total_documents, created_at) VALUES (?, 'pending', ?, ?)",
            (run_id, total_documents, now),
        )
        return run_id

    @staticmethod
    async def get(run_id: str) -> Optional[dict]:
        return await _fetch_one(
            "SELECT * FROM deep_learning_runs WHERE id = ?", (run_id,),
        )

    @staticmethod
    async def list_all() -> list[dict]:
        return await _fetch_all(
            "SELECT * FROM deep_learning_runs ORDER BY created_at DESC"
        )

    @staticmethod
    async def update(run_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [run_id]
        await _exec(f"UPDATE deep_learning_runs SET {sets} WHERE id = ?", tuple(vals))

    @staticmethod
    async def increment(run_id: str, field: str, amount: int = 1) -> None:
        await _exec(
            f"UPDATE deep_learning_runs SET {field} = COALESCE({field}, 0) + ? WHERE id = ?",
            (amount, run_id),
        )


# ---------------------------------------------------------------------------
# Document chunking
# ---------------------------------------------------------------------------

def _chunk_document(file_path: str, max_chars: int = 6000, overlap_chars: int = 500) -> list[str]:
    """Split a document into chunks at paragraph/heading boundaries."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    if not text.strip():
        return []

    if len(text) <= max_chars:
        return [text]

    # Try to split at markdown headings first
    heading_pattern = re.compile(r"^#{1,3} ", re.MULTILINE)
    sections = heading_pattern.split(text)
    heading_matches = heading_pattern.findall(text)

    # Reconstruct sections with their headings
    if len(sections) > 1 and heading_matches:
        rebuilt: list[str] = []
        if sections[0].strip():
            rebuilt.append(sections[0].strip())
        for i, heading in enumerate(heading_matches):
            section_text = heading + sections[i + 1]
            rebuilt.append(section_text.strip())
        sections = rebuilt
    else:
        # Fall back to paragraph splitting
        sections = text.split("\n\n")

    chunks: list[str] = []
    current_chunk = ""

    for section in sections:
        if not section.strip():
            continue

        # If adding this section would exceed max, flush current chunk
        if len(current_chunk) + len(section) + 2 > max_chars and current_chunk.strip():
            chunks.append(current_chunk.strip())
            # Keep tail for overlap context
            tail_paras = current_chunk.split("\n\n")
            overlap = "\n\n".join(tail_paras[-2:]) if len(tail_paras) > 1 else ""
            current_chunk = overlap + "\n\n" if overlap and len(overlap) <= overlap_chars else ""

        current_chunk += section + "\n\n"

        # If a single section is larger than max_chars, force-flush it
        if len(current_chunk) > max_chars:
            chunks.append(current_chunk.strip())
            current_chunk = ""

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks if chunks else [text]


# ---------------------------------------------------------------------------
# Phase system prompts and tool configs
# ---------------------------------------------------------------------------

BASIC_MEMORY_TOOLS = [
    "mcp__basic_memory__write_note",
    "mcp__basic_memory__edit_note",
    "mcp__basic_memory__search_notes",
    "mcp__basic_memory__read_note",
]

BASIC_MEMORY_TOOLS_EXTENDED = BASIC_MEMORY_TOOLS + [
    "mcp__basic_memory__recent_activity",
    "mcp__basic_memory__build_context",
    "mcp__basic_memory__delete_note",
]


def _ingest_system_prompt() -> str:
    return (
        "You are a knowledge ingestion system for SU. You will receive a section of a "
        "document that the user wants you to deeply learn. Your job:\n\n"
        "1. Identify every piece of meaningful knowledge: facts, preferences, decisions, "
        "relationships between concepts, timelines, opinions, goals, technical details, "
        "personal history, habits, and context.\n"
        "2. Search basic-memory for existing notes on these topics.\n"
        "3. If a related note exists, use edit_note to APPEND new observations — "
        "do NOT duplicate what already exists.\n"
        "4. If no related note exists, use write_note to create one.\n\n"
        "ORGANIZATION:\n"
        "- 'people/' — information about people, relationships, preferences, communication style\n"
        "- 'projects/' — project context, goals, technical decisions, status\n"
        "- 'decisions/' — conclusions reached, choices made, rationale\n"
        "- 'knowledge/' — technical knowledge, domain expertise, patterns\n"
        "- 'history/' — historical events, timelines, personal history, backstory\n\n"
        "NOTE FORMAT — use observation syntax:\n"
        "  - [fact] The company was founded in 2019 #timeline #company\n"
        "  - [decision] Chose React over Vue for the frontend #tech #architecture\n"
        "  - [relationship] Jake reports to Maria in the org chart #people #org\n"
        "  - [preference] Prefers async communication over meetings #workflow\n"
        "  - [goal] Wants to ship v2 by Q3 #timeline #product\n"
        "  - [context] Has been working remotely since 2020 #personal #work\n\n"
        "Use relations to link related concepts:\n"
        "  - part_of [[Project Alpha]]\n"
        "  - relates_to [[Q3 Planning]]\n\n"
        "Be thorough. Extract EVERYTHING worth remembering. This is a deliberate "
        "learning session, not casual chat — better to over-extract than miss something.\n\n"
        "You are running headless. Do not ask for clarification."
    )


def _cross_reference_system_prompt() -> str:
    return (
        "You are a knowledge cross-referencing system for SU. New documents have just "
        "been ingested. Your job is to review the recently-created notes and find "
        "connections to existing knowledge.\n\n"
        "For each note listed, search for related notes across the knowledge base and "
        "add bidirectional relations where appropriate.\n\n"
        "Types of relations to look for:\n"
        "- part_of: A is a component/aspect of B\n"
        "- relates_to: A and B are about related topics\n"
        "- extends: A adds detail to what B describes\n"
        "- contradicts: A says something different from B (flag this!)\n"
        "- supersedes: A contains newer information than B\n\n"
        "When you find a connection, use edit_note on BOTH notes to add the relation. "
        "If you find a potential contradiction, add a [contradiction] observation to "
        "both notes describing the discrepancy.\n\n"
        "Be thorough but precise. Only create links that are genuinely meaningful.\n\n"
        "You are running headless. Do not ask for clarification."
    )


def _consolidate_system_prompt() -> str:
    return (
        "You are a knowledge consolidation system for SU. The knowledge base may "
        "contain many fragmented notes on the same topics — scattered observations "
        "from different documents and conversations that should be unified.\n\n"
        "Your job:\n"
        "1. Read the notes listed below.\n"
        "2. Identify clusters: notes that cover the same person, project, topic, or concept.\n"
        "3. For each cluster, choose or create ONE canonical note that comprehensively "
        "covers the topic.\n"
        "4. Merge all unique observations from fragment notes into the canonical note "
        "using edit_note. Deduplicate — same idea in different words should appear once.\n"
        "5. On merged fragment notes, add: [archived] merged into [[Canonical Note Title]]\n\n"
        "PRINCIPLES:\n"
        "- One authoritative note per topic/entity is the goal\n"
        "- Preserve ALL unique information — consolidation is about structure, not deletion\n"
        "- Keep the best-organized note as the canonical one; merge others into it\n"
        "- If two notes are genuinely about different aspects of the same topic, "
        "link them with relations rather than merging\n"
        "- Improve tag coverage during consolidation\n\n"
        "You are running headless. Do not ask for clarification."
    )


def _audit_system_prompt() -> str:
    return (
        "You are a knowledge base auditor for SU. Review the batch of notes below "
        "and identify problems:\n\n"
        "1. DUPLICATES: Two or more notes covering the same topic. Note which ones "
        "should be merged and which is the canonical version.\n"
        "2. CONTRADICTIONS: Conflicting facts across different notes. Note the specific "
        "discrepancy and which note is likely more current.\n"
        "3. STALE INFO: Information that appears outdated (past deadlines, completed "
        "projects described as ongoing, people in roles they've left).\n"
        "4. POOR ORGANIZATION: Notes in the wrong folder, missing tags, vague titles, "
        "observations that lack context.\n\n"
        "For each issue found, add a [needs-review] observation to the affected note(s) "
        "with a clear description of the problem and what should be done.\n\n"
        "Do NOT fix anything yet — just identify and tag issues. The refinement phase "
        "will handle fixes.\n\n"
        "You are running headless. Do not ask for clarification."
    )


def _refine_system_prompt() -> str:
    return (
        "You are a knowledge base refinement system for SU. The audit phase identified "
        "issues that need fixing. For each issue:\n\n"
        "DUPLICATES:\n"
        "- Read both notes fully\n"
        "- Merge all unique observations into the canonical note using edit_note\n"
        "- Add [archived] [duplicate-of [[Canonical Note Title]]] to the duplicate\n\n"
        "CONTRADICTIONS:\n"
        "- Research which fact is correct using other notes as context\n"
        "- Update the incorrect note with the correct information\n"
        "- Add a [resolved] observation explaining the correction\n\n"
        "STALE INFO:\n"
        "- Update dates, statuses, and facts to current information where possible\n"
        "- If you don't know the current state, add [needs-verification] instead of guessing\n\n"
        "ORGANIZATION:\n"
        "- Add missing tags, improve observation clarity\n"
        "- Add relations to connect orphaned notes\n\n"
        "After fixing each issue, remove the #needs-review tag from the resolved observation.\n\n"
        "Be careful and precise. Do not lose information during merges.\n\n"
        "You are running headless. Do not ask for clarification."
    )


# ---------------------------------------------------------------------------
# Agent spawning helper
# ---------------------------------------------------------------------------

async def _spawn_agent(
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
    phase_name: str,
    max_turns: int = 30,
    timeout: int = 300,
) -> dict[str, int]:
    """Spawn a short-lived pydantic-ai agent and return write/edit counts."""
    from app.agents import build_deep_learning_agent

    stats = {"writes": 0, "edits": 0, "errors": 0}

    try:
        agent = build_deep_learning_agent(system_prompt=system_prompt)
        result = await asyncio.wait_for(
            agent.run(user_prompt),
            timeout=timeout,
        )

        # Count tool calls from the messages
        for msg in result.all_messages():
            for part in msg.parts:
                tool_name = getattr(part, 'tool_name', None)
                if tool_name:
                    if "write_note" in tool_name:
                        stats["writes"] += 1
                    elif "edit_note" in tool_name:
                        stats["edits"] += 1

    except asyncio.TimeoutError:
        log.warning(f"deep_learning.{phase_name}_timeout", timeout=timeout)
        stats["errors"] += 1
    except asyncio.CancelledError:
        log.warning(f"deep_learning.{phase_name}_cancelled")
        stats["errors"] += 1
    except Exception:
        log.exception(f"deep_learning.{phase_name}_agent_failed")
        stats["errors"] += 1

    return stats


# ---------------------------------------------------------------------------
# Progress broadcasting
# ---------------------------------------------------------------------------

async def _broadcast_progress(run_id: str) -> None:
    """Push current run state to all connected WebSocket clients."""
    if not _broadcast_fn:
        return
    run = await RunRepo.get(run_id)
    if not run:
        return
    try:
        await _broadcast_fn({
            "type": "deep_learning_progress",
            "run_id": run_id,
            "status": run.get("status", "unknown"),
            "phase": run.get("phase"),
            "current_step": run.get("current_step"),
            "progress": {
                "documents_total": run.get("total_documents", 0),
                "documents_processed": run.get("documents_processed", 0),
                "memories_written": run.get("memories_written", 0),
                "memories_updated": run.get("memories_updated", 0),
                "memories_deleted": run.get("memories_deleted", 0),
                "contradictions_found": run.get("contradictions_found", 0),
                "duplicates_merged": run.get("duplicates_merged", 0),
                "notes_consolidated": run.get("notes_consolidated", 0),
            },
        })
    except Exception:
        log.exception("deep_learning.broadcast_failed", run_id=run_id)


def _is_cancelled(run_id: str) -> bool:
    return _cancel_flags.get(run_id, False)


async def _update_run(run_id: str, **fields: Any) -> None:
    """Update run state and broadcast progress."""
    await RunRepo.update(run_id, **fields)
    await _broadcast_progress(run_id)


# ---------------------------------------------------------------------------
# Phase 1: INGEST
# ---------------------------------------------------------------------------

async def _run_ingest_phase(run_id: str) -> None:
    """Chunk and ingest all pending documents."""
    await _update_run(run_id, phase="ingest", current_step="Starting document ingestion")

    pending_docs = await DocRepo.list(status="pending")
    if not pending_docs:
        log.info("deep_learning.ingest_no_docs", run_id=run_id)
        return

    for doc_idx, doc in enumerate(pending_docs):
        if _is_cancelled(run_id):
            return

        file_path = doc["file_path"]
        filename = doc["filename"]
        log.info("deep_learning.ingest_doc_starting", run_id=run_id, filename=filename)

        try:
            chunks = _chunk_document(file_path)
        except Exception:
            log.exception("deep_learning.chunk_failed", filename=filename)
            await DocRepo.update(doc["id"], status="failed", error="Failed to read/chunk file")
            continue

        await DocRepo.update(doc["id"], status="processing", chunk_count=len(chunks))

        for chunk_idx, chunk in enumerate(chunks):
            if _is_cancelled(run_id):
                return

            step = f"Ingesting {filename} (chunk {chunk_idx + 1}/{len(chunks)})"
            await _update_run(run_id, current_step=step)

            prompt = (
                f"Document: \"{filename}\" (section {chunk_idx + 1} of {len(chunks)})\n\n"
                f"---\n{chunk}\n---\n\n"
                "Analyze this section and store all noteworthy information in the "
                "knowledge base. Follow your instructions."
            )

            run_id_daemon = await daemon_registry.start_run(
                "deep_learning", phase="ingest", document=filename,
                chunk=f"{chunk_idx + 1}/{len(chunks)}",
            )
            stats = await _spawn_agent(
                system_prompt=_ingest_system_prompt(),
                user_prompt=prompt,
                allowed_tools=BASIC_MEMORY_TOOLS,
                phase_name="ingest",
            )
            await daemon_registry.end_run(run_id_daemon, "deep_learning", RunStatus.COMPLETED)

            await RunRepo.increment(run_id, "memories_written", stats["writes"])
            await RunRepo.increment(run_id, "memories_updated", stats["edits"])
            await DocRepo.update(doc["id"], chunks_processed=chunk_idx + 1)
            await _broadcast_progress(run_id)

            # Yield to other tasks
            await asyncio.sleep(2)

        await DocRepo.update(
            doc["id"], status="ingested",
            processed_at=datetime.utcnow().isoformat(),
        )
        await RunRepo.increment(run_id, "documents_processed")
        log.info("deep_learning.ingest_doc_done", run_id=run_id, filename=filename)


# ---------------------------------------------------------------------------
# Phase 2: CROSS-REFERENCE
# ---------------------------------------------------------------------------

async def _run_cross_reference_phase(run_id: str) -> None:
    """Link newly-ingested notes to existing knowledge."""
    if _is_cancelled(run_id):
        return

    await _update_run(run_id, phase="cross_reference",
                      current_step="Cross-referencing new knowledge with existing memory")

    prompt = (
        "Review recently-created notes in the knowledge base (use recent_activity). "
        "For each new note, search for related existing notes and add bidirectional "
        "relation links. Flag any contradictions you discover. "
        "Follow your instructions."
    )

    run_id_daemon = await daemon_registry.start_run("deep_learning", phase="cross_reference")
    stats = await _spawn_agent(
        system_prompt=_cross_reference_system_prompt(),
        user_prompt=prompt,
        allowed_tools=BASIC_MEMORY_TOOLS_EXTENDED,
        phase_name="cross_reference",
        max_turns=40,
        timeout=360,
    )
    await daemon_registry.end_run(run_id_daemon, "deep_learning", RunStatus.COMPLETED)

    await RunRepo.increment(run_id, "memories_updated", stats["edits"])
    log.info("deep_learning.cross_reference_done", run_id=run_id,
             edits=stats["edits"])


# ---------------------------------------------------------------------------
# Phase 3: CONSOLIDATE
# ---------------------------------------------------------------------------

async def _run_consolidate_phase(run_id: str) -> None:
    """Merge fragmented notes into well-organized canonical entries."""
    if _is_cancelled(run_id):
        return

    await _update_run(run_id, phase="consolidate",
                      current_step="Consolidating fragmented knowledge")

    # Run multiple consolidation passes by topic area
    topic_areas = [
        ("people", "Search for all notes in the people/ folder. Consolidate fragmented "
                   "notes about the same person into one canonical note per person."),
        ("projects", "Search for all notes in the projects/ folder. Consolidate fragmented "
                     "notes about the same project into one canonical note per project."),
        ("knowledge", "Search for all notes in the knowledge/ and decisions/ folders. "
                      "Consolidate notes covering the same topic or decision."),
        ("general", "Search broadly across all folders for any remaining fragmented notes "
                    "that cover the same topic but haven't been consolidated yet. "
                    "Look for notes with similar titles or overlapping observations."),
    ]

    for area_name, area_prompt in topic_areas:
        if _is_cancelled(run_id):
            return

        await _update_run(run_id, current_step=f"Consolidating {area_name} notes")

        prompt = (
            f"{area_prompt}\n\n"
            "Follow your instructions for consolidation."
        )

        run_id_daemon = await daemon_registry.start_run(
            "deep_learning", phase="consolidate", area=area_name,
        )
        stats = await _spawn_agent(
            system_prompt=_consolidate_system_prompt(),
            user_prompt=prompt,
            allowed_tools=BASIC_MEMORY_TOOLS_EXTENDED,
            phase_name="consolidate",
            max_turns=40,
            timeout=360,
        )
        await daemon_registry.end_run(run_id_daemon, "deep_learning", RunStatus.COMPLETED)

        await RunRepo.increment(run_id, "memories_updated", stats["edits"])
        await RunRepo.increment(run_id, "notes_consolidated", stats["edits"])
        await _broadcast_progress(run_id)

        await asyncio.sleep(2)

    log.info("deep_learning.consolidate_done", run_id=run_id)


# ---------------------------------------------------------------------------
# Phase 4: AUDIT
# ---------------------------------------------------------------------------

async def _run_audit_phase(run_id: str) -> None:
    """Scan the entire knowledge base for problems."""
    if _is_cancelled(run_id):
        return

    await _update_run(run_id, phase="audit",
                      current_step="Auditing knowledge base for issues")

    # Audit in topic-area passes to keep each agent focused
    audit_areas = [
        ("people", "Search for all notes in the people/ folder and audit them."),
        ("projects", "Search for all notes in the projects/ folder and audit them."),
        ("knowledge", "Search for all notes in the knowledge/, decisions/, and history/ "
                      "folders and audit them."),
        ("cross-check", "Search broadly for any notes tagged #needs-review that were "
                        "missed, and do a final cross-check for contradictions between "
                        "different topic areas (e.g., a person note says one thing but "
                        "a project note says another)."),
    ]

    for area_name, area_prompt in audit_areas:
        if _is_cancelled(run_id):
            return

        await _update_run(run_id, current_step=f"Auditing {area_name}")

        prompt = (
            f"{area_prompt}\n\n"
            "Follow your instructions for auditing."
        )

        run_id_daemon = await daemon_registry.start_run(
            "deep_learning", phase="audit", area=area_name,
        )
        stats = await _spawn_agent(
            system_prompt=_audit_system_prompt(),
            user_prompt=prompt,
            allowed_tools=BASIC_MEMORY_TOOLS_EXTENDED,
            phase_name="audit",
            max_turns=30,
            timeout=300,
        )
        await daemon_registry.end_run(run_id_daemon, "deep_learning", RunStatus.COMPLETED)

        await RunRepo.increment(run_id, "memories_updated", stats["edits"])
        await _broadcast_progress(run_id)

        await asyncio.sleep(2)

    log.info("deep_learning.audit_done", run_id=run_id)


# ---------------------------------------------------------------------------
# Phase 5: REFINE
# ---------------------------------------------------------------------------

async def _run_refine_phase(run_id: str) -> None:
    """Fix all issues flagged during the audit phase."""
    if _is_cancelled(run_id):
        return

    await _update_run(run_id, phase="refine",
                      current_step="Refining knowledge base — fixing flagged issues")

    prompt = (
        "Search the knowledge base for any notes containing #needs-review observations. "
        "For each issue found, fix it according to your instructions. "
        "After fixing, remove the #needs-review tag from the resolved observation. "
        "Also search for notes marked [archived] and verify they were properly merged."
    )

    run_id_daemon = await daemon_registry.start_run("deep_learning", phase="refine")
    stats = await _spawn_agent(
        system_prompt=_refine_system_prompt(),
        user_prompt=prompt,
        allowed_tools=BASIC_MEMORY_TOOLS_EXTENDED,
        phase_name="refine",
        max_turns=40,
        timeout=360,
    )
    await daemon_registry.end_run(run_id_daemon, "deep_learning", RunStatus.COMPLETED)

    await RunRepo.increment(run_id, "memories_updated", stats["edits"])
    await RunRepo.increment(run_id, "duplicates_merged", stats["edits"])
    log.info("deep_learning.refine_done", run_id=run_id, edits=stats["edits"])


# ---------------------------------------------------------------------------
# Main controller loop
# ---------------------------------------------------------------------------

async def run_deep_learning(run_id: str, audit_only: bool = False) -> None:
    """Main controller loop for a deep learning run.

    Orchestrates all phases sequentially. Each phase spawns short-lived agents.
    """
    log.info("deep_learning.run_starting", run_id=run_id, audit_only=audit_only)
    await _update_run(run_id, status="running",
                      started_at=datetime.utcnow().isoformat())

    try:
        if not audit_only:
            # Phase 1: Ingest uploaded documents
            await _run_ingest_phase(run_id)
            if _is_cancelled(run_id):
                await _update_run(run_id, status="cancelled",
                                  current_step="Cancelled during ingest")
                return

            # Phase 2: Cross-reference new with existing
            await _run_cross_reference_phase(run_id)
            if _is_cancelled(run_id):
                await _update_run(run_id, status="cancelled",
                                  current_step="Cancelled during cross-reference")
                return

        # Phase 3: Consolidate fragmented notes
        await _run_consolidate_phase(run_id)
        if _is_cancelled(run_id):
            await _update_run(run_id, status="cancelled",
                              current_step="Cancelled during consolidation")
            return

        # Phase 4: Audit the knowledge base
        await _run_audit_phase(run_id)
        if _is_cancelled(run_id):
            await _update_run(run_id, status="cancelled",
                              current_step="Cancelled during audit")
            return

        # Phase 5: Refine — fix flagged issues
        await _run_refine_phase(run_id)

        await _update_run(
            run_id, status="completed",
            current_step="Deep learning complete",
            completed_at=datetime.utcnow().isoformat(),
        )
        log.info("deep_learning.run_completed", run_id=run_id)

    except Exception as exc:
        log.exception("deep_learning.run_failed", run_id=run_id)
        await _update_run(
            run_id, status="failed",
            current_step=f"Failed: {str(exc)[:200]}",
            error=str(exc)[:500],
        )
    finally:
        _cancel_flags.pop(run_id, None)


def cancel_run(run_id: str) -> None:
    """Request cancellation of a running deep learning session."""
    _cancel_flags[run_id] = True
    log.info("deep_learning.cancel_requested", run_id=run_id)


# ---------------------------------------------------------------------------
# Staging directory management
# ---------------------------------------------------------------------------

def ensure_staging_dir() -> Path:
    """Create and return the deep learning inbox directory."""
    inbox = Path(settings.deep_learning_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox
