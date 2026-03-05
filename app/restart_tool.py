"""
Self-restart tool: allows SU to restart itself after code changes.
Exposed as a plain async function registered on pydantic-ai agents.
"""
import httpx

from app.logger import get_logger

log = get_logger(__name__)

RESTART_SERVER_URL = "http://host.docker.internal:8932/restart"


async def restart_self() -> str:
    """Restart SU with the latest code from the main branch.

    Call this AFTER you have merged your changes to main and verified tests pass.
    WARNING: This will terminate the current process. The session will be
    preserved in the database and will resume automatically after restart.
    """
    log.info("restart.initiated")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(RESTART_SERVER_URL, timeout=10)
            resp.raise_for_status()
        log.info("restart.signalled")
        return (
            "Restart initiated successfully. The container is rebuilding "
            "with the latest code. This process will terminate shortly. "
            "The session will resume automatically in a new WebSocket "
            "connection once the service is back up."
        )
    except Exception as e:
        log.exception("restart.failed", error=str(e))
        return f"Failed to initiate restart: {str(e)}"
