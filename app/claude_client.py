"""
Claude SDK client wrapper for chat functionality.

All standard Claude Code tools are enabled. The agent uses subagents (Task tool)
to delegate complex work and keep the main conversation context clean. Playwright
browsing is available directly via MCP, and dangerous websites are accessed through
the scary_internet sandboxed subagent.
"""
import os
import re
from pathlib import Path
from typing import Any, AsyncGenerator, Optional
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    TextBlock,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)
from app.config import settings
from app.logger import get_logger
from app.tz import now as local_now
from app.scary_internet_agent import scary_internet_mcp_server
from app.life_manager import life_manager_mcp_server
from app.su_notes_manager import su_notes_mcp_server
from app.telegram_messenger import telegram_messenger_mcp_server

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).parent / "prompts"
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_VARS_RE = re.compile(r"\{(\w+)\}")


def _load_prompt(filename: str, all_vars: dict[str, str]) -> str:
    """Load a prompt markdown file, validate declared vars, and interpolate.

    Each file declares expected variables in YAML frontmatter:
        ---
        vars: [user, current_time]
        ---
    Only the declared variables are substituted. Files declaring no vars
    skip interpolation entirely, so literal braces in content are safe.
    """
    raw = (PROMPTS_DIR / filename).read_text()

    # Parse frontmatter
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"{filename}: missing YAML frontmatter")
    body = raw[m.end():]

    # Extract declared vars list
    fm = m.group(1)
    declared: list[str] = []
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("vars:"):
            inner = line.split(":", 1)[1].strip().strip("[]")
            if inner:
                declared = [v.strip() for v in inner.split(",")]
            break

    if not declared:
        return body

    # Validate: every declared var must be provided
    missing = set(declared) - set(all_vars)
    if missing:
        raise ValueError(f"{filename}: missing vars {missing}")

    # Validate: every {placeholder} in body must be declared
    used = set(_VARS_RE.findall(body))
    undeclared = used - set(declared)
    if undeclared:
        raise ValueError(f"{filename}: undeclared vars {undeclared} in body")

    # Substitute only declared vars via regex so literal braces are untouched
    result = body
    for var in declared:
        result = result.replace("{" + var + "}", all_vars[var])
    return result


def _build_system_prompt() -> str:
    """Build the system prompt by assembling markdown files from app/prompts/."""
    fmt = dict(
        su=settings.su_name,
        user=settings.user_name,
        current_time=local_now().strftime("%A, %B %-d, %Y %I:%M %p %Z"),
    )

    parts = [
        _load_prompt("01-identity.md", fmt),
        _load_prompt("02-personality.md", fmt),
        _load_prompt("03-tools.md", fmt),
    ]

    if settings.telegram_bot_token:
        parts.append(_load_prompt("04-telegram.md", fmt))

    if settings.protonmail_username and settings.protonmail_password:
        parts.append(_load_prompt("05-email.md", fmt))

    # 06-browsing always included; the Playwright paragraph is conditional
    browsing = _load_prompt("06-browsing.md", fmt)
    if not settings.playwright_mcp_url:
        # Strip the Direct Playwright paragraph
        lines = browsing.splitlines(keepends=True)
        filtered: list[str] = []
        skip = False
        for line in lines:
            if line.startswith("**Direct Playwright**"):
                skip = True
                continue
            if skip and line.strip() == "":
                skip = False
                continue
            if skip:
                continue
            filtered.append(line)
        browsing = "".join(filtered)
    parts.append(browsing)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ClaudeChat client
# ---------------------------------------------------------------------------

class ClaudeChat:
    """Wrapper for Claude SDK client.

    The SDK client must be kept alive for the duration of a session so that
    conversation history is maintained automatically across query() calls.

    All tools are enabled by default. The agent uses subagents via the Task
    tool to keep the main conversation context clean.
    """

    def __init__(self, oauth_token: Optional[str] = None):
        if oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        self.options = self._build_options()
        self._client: Optional[ClaudeSDKClient] = None

    def _build_options(self) -> ClaudeAgentOptions:
        """Build agent options with all tools enabled."""
        mcp_servers = {
            "scary_internet": scary_internet_mcp_server,
            "life_manager": life_manager_mcp_server,
            "su_notes_manager": su_notes_mcp_server,
        }

        if settings.telegram_bot_token:
            mcp_servers["telegram_messenger"] = telegram_messenger_mcp_server

        if settings.playwright_mcp_url:
            mcp_servers["playwright"] = {
                "type": "sse",
                "url": settings.playwright_mcp_url,
            }

        if settings.protonmail_username and settings.protonmail_password:
            mcp_servers["protonmail"] = {
                "type": "stdio",
                "command": "protonmail-mcp-server",
                "args": [],
                "env": {
                    "PROTONMAIL_USERNAME": settings.protonmail_username,
                    "PROTONMAIL_PASSWORD": settings.protonmail_password,
                    "PROTONMAIL_SMTP_HOST": settings.protonmail_smtp_host,
                    "PROTONMAIL_SMTP_PORT": str(settings.protonmail_smtp_port),
                    "PROTONMAIL_IMAP_HOST": settings.protonmail_imap_host,
                    "PROTONMAIL_IMAP_PORT": str(settings.protonmail_imap_port),
                },
            }

        if settings.self_iteration_mode:
            from app.restart_tool import restart_mcp_server
            mcp_servers["restart"] = restart_mcp_server

        return ClaudeAgentOptions(
            mcp_servers=mcp_servers,
            disallowed_tools=[
                "NotebookEdit",
            ],
            permission_mode="bypassPermissions",
            max_turns=50,
            system_prompt=_build_system_prompt(),
        )

    async def connect(self):
        """Open and connect the SDK client. Must be called before send_message."""
        log.info("sdk.connecting")
        self._client = ClaudeSDKClient(options=self.options)
        await self._client.connect()
        log.info("sdk.connected")

    async def disconnect(self):
        """Disconnect the SDK client."""
        if self._client:
            log.info("sdk.disconnecting")
            await self._client.disconnect()
            self._client = None
            log.info("sdk.disconnected")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def send_message(
        self,
        message: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield structured events: text chunks, tool calls, and tool results."""
        if not self._client:
            raise RuntimeError("ClaudeChat client is not connected. Call connect() or use as async context manager.")

        try:
            await self._client.query(message)

            async for msg in self._client.receive_response():
                msg_type = type(msg).__name__
                log.debug("sdk.message", msg_type=msg_type)

                if isinstance(msg, SystemMessage):
                    log.info(
                        "sdk.system_message",
                        subtype=msg.subtype,
                        data=str(msg.data)[:300],
                    )
                    if msg.subtype == "init":
                        mcp_servers = msg.data.get("mcp_servers", [])
                        for srv in mcp_servers:
                            name = srv.get("name", "unknown")
                            status = srv.get("status", "unknown")
                            if status != "connected":
                                log.error(
                                    "sdk.mcp_connect_failed",
                                    server_name=name,
                                    server_status=status,
                                )
                            else:
                                log.info("sdk.mcp_connected", server_name=name)

                elif isinstance(msg, UserMessage):
                    content = msg.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                log.info(
                                    "sdk.tool_result",
                                    tool_use_id=block.tool_use_id,
                                    is_error=block.is_error,
                                )
                                yield {
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                    "content": block.content,
                                    "is_error": block.is_error or False,
                                }
                            else:
                                log.debug(
                                    "sdk.user_block",
                                    block_type=type(block).__name__,
                                )
                    else:
                        log.debug("sdk.user_content", content=str(content)[:200])

                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        log.debug("sdk.assistant_block", block_type=type(block).__name__)
                        if isinstance(block, TextBlock):
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
                            log.info(
                                "sdk.tool_use",
                                tool_name=block.name,
                                tool_id=block.id,
                            )
                            yield {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        elif isinstance(block, ToolResultBlock):
                            yield {
                                "type": "tool_result",
                                "tool_use_id": block.tool_use_id,
                                "content": block.content,
                                "is_error": block.is_error or False,
                            }

                elif isinstance(msg, ResultMessage):
                    log.info(
                        "sdk.result",
                        subtype=getattr(msg, "subtype", None),
                        is_error=msg.is_error,
                        result=(msg.result or "")[:500],
                    )
                    if msg.is_error:
                        yield {
                            "type": "error",
                            "content": msg.result or "Unknown error",
                        }

        except Exception as e:
            log.exception("sdk.send_error", error=str(e))
            yield {"type": "error", "content": f"Error communicating with Claude: {str(e)}"}
