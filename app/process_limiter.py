"""
Global concurrency limiter for Claude SDK subprocesses.

Every ClaudeSDKClient instance forks a `claude` CLI process (~300-400 MB RSS).
On a constrained host (e.g. t3.medium with 4 GB) we must cap the number of
concurrent processes to avoid OOM kills that cascade into Docker and Tailscale
failures.

Usage — short-lived agents (subconscious, REM, calendar, website):

    async with claude_process_slot(timeout=120):
        async with ClaudeSDKClient(options=...) as client:
            ...
    # If the body exceeds *timeout* seconds the context manager cancels it,
    # disconnects the client, and releases the slot automatically.

Usage — long-lived session agents (managed by agent_registry):

    The registry calls _get_semaphore().acquire()/release() directly so
    that the slot lifetime spans many request/response cycles.
"""
import asyncio
import os

from app.logger import get_logger

log = get_logger(__name__)

# Max concurrent Claude CLI processes.  Override with env var.
MAX_CLAUDE_PROCESSES = int(os.environ.get("MAX_CLAUDE_PROCESSES", "3"))

# Default hard timeout for short-lived agents (seconds).
DEFAULT_TIMEOUT = int(os.environ.get("CLAUDE_PROCESS_TIMEOUT", "180"))

_semaphore: asyncio.Semaphore | None = None

# Track what's holding slots (best-effort, for the daemon index)
_slot_holders: list[str] = []


def get_slot_status() -> dict:
    """Return current process limiter state for the daemon index."""
    sem = _get_semaphore()
    return {
        "max_slots": MAX_CLAUDE_PROCESSES,
        "available_slots": sem._value,
        "used_slots": MAX_CLAUDE_PROCESSES - sem._value,
        "holders": list(_slot_holders),
    }


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy-init so the semaphore is created inside the running event loop."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CLAUDE_PROCESSES)
        log.info("process_limiter.init", max_processes=MAX_CLAUDE_PROCESSES)
    return _semaphore


class claude_process_slot:
    """Async context manager that reserves a slot for a Claude subprocess.

    Parameters
    ----------
    timeout : int | None
        Maximum seconds the body may run before being cancelled.
        ``None`` disables the deadline (used only for long-lived session
        agents whose lifetime is managed by the agent registry).
        Defaults to ``DEFAULT_TIMEOUT``.
    """

    def __init__(self, timeout: int | None = DEFAULT_TIMEOUT, name: str = "unknown"):
        self._timeout = timeout
        self._name = name
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        sem = _get_semaphore()
        log.debug(
            "process_limiter.waiting",
            available=sem._value,
            max=MAX_CLAUDE_PROCESSES,
        )
        await sem.acquire()
        _slot_holders.append(self._name)
        log.info(
            "process_limiter.acquired",
            available=sem._value,
            max=MAX_CLAUDE_PROCESSES,
            timeout=self._timeout,
            name=self._name,
        )
        if self._timeout is not None:
            # Arm a watchdog that cancels the current task after *timeout* seconds.
            self._task = asyncio.current_task()
            self._watchdog = asyncio.get_event_loop().call_later(
                self._timeout,
                self._cancel,
            )
        return self

    def _cancel(self):
        """Called by the event-loop timer when the deadline expires."""
        if self._task and not self._task.done():
            log.warning(
                "process_limiter.timeout",
                timeout=self._timeout,
                task=self._task.get_name(),
            )
            self._task.cancel()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Disarm the watchdog if it hasn't fired yet.
        if hasattr(self, "_watchdog"):
            self._watchdog.cancel()

        sem = _get_semaphore()
        sem.release()
        try:
            _slot_holders.remove(self._name)
        except ValueError:
            pass
        log.info(
            "process_limiter.released",
            available=sem._value,
            max=MAX_CLAUDE_PROCESSES,
            name=self._name,
        )
        # Suppress CancelledError raised by our own watchdog so the caller
        # sees a clean exit rather than an unhandled cancellation.
        if exc_type is asyncio.CancelledError:
            return True
        return False
