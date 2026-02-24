"""
Self-restart MCP tool: allows SU to restart itself after code changes.

Calls the host-side restart server which runs `docker compose up --build -d`
to rebuild and restart the container with the latest code from /src.
"""
import inspect

from mcp.server import Server as _McpServer

# Monkey-patch: mcp 0.9.x+ removed the `version` kwarg from Server.__init__,
# but claude-agent-sdk's create_sdk_mcp_server still passes it.
_orig_server_init = _McpServer.__init__
if "version" not in inspect.signature(_orig_server_init).parameters:
    def _patched_server_init(self, name, **kwargs):
        version = kwargs.pop("version", "1.0.0")
        _orig_server_init(self, name, **kwargs)
        self.version = version
    _McpServer.__init__ = _patched_server_init

from claude_agent_sdk import create_sdk_mcp_server, tool
import httpx

from app.logger import get_logger

log = get_logger(__name__)

RESTART_SERVER_URL = "http://host.docker.internal:8932/restart"


@tool(
    "restart_self",
    "Restart SU with the latest code from the main branch. "
    "Call this AFTER you have merged your changes to main and verified tests pass. "
    "WARNING: This will terminate the current process. The session will be "
    "preserved in the database and will resume automatically after restart.",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
async def restart_self(args: dict) -> dict:
    """Signal the host-side restart server to rebuild and restart the container."""
    log.info("restart.initiated")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(RESTART_SERVER_URL, timeout=10)
            resp.raise_for_status()
        log.info("restart.signalled")
        return {
            "content": [{
                "type": "text",
                "text": (
                    "Restart initiated successfully. The container is rebuilding "
                    "with the latest code. This process will terminate shortly. "
                    "The session will resume automatically in a new WebSocket "
                    "connection once the service is back up."
                ),
            }],
        }
    except Exception as e:
        log.exception("restart.failed", error=str(e))
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to initiate restart: {str(e)}",
            }],
            "is_error": True,
        }


restart_mcp_server = create_sdk_mcp_server(
    name="restart",
    tools=[restart_self],
)
