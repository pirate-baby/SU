"""
Agent definitions for all SU agent modes.

Central module that creates and configures all pydantic-ai Agent instances,
the shared model, and MCP server connections.
"""
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.mcp import MCPServerStdio, MCPServerSSE, MCPServerStreamableHTTP
from pydantic_ai.toolsets.abstract import AbstractToolset

from app.config import settings
from app.tz import now as local_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def _build_model() -> Model:
    """Build the LLM model for the configured provider."""
    provider = settings.llm_provider
    model_name = settings.llm_model

    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider_kwargs: dict[str, Any] = {}
        if settings.anthropic_api_key:
            provider_kwargs["api_key"] = settings.anthropic_api_key
        elif settings.claude_code_oauth_token:
            import anthropic
            client = anthropic.AsyncAnthropic(
                auth_token=settings.claude_code_oauth_token,
            )
            provider_kwargs["anthropic_client"] = client

        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(**provider_kwargs),
        )

    if provider == "together":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.together import TogetherProvider

        kw: dict[str, Any] = {}
        if settings.together_api_key:
            kw["api_key"] = settings.together_api_key

        return OpenAIChatModel(model_name, provider=TogetherProvider(**kw))

    if provider == "fireworks":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.fireworks import FireworksProvider

        kw = {}
        if settings.fireworks_api_key:
            kw["api_key"] = settings.fireworks_api_key

        return OpenAIChatModel(model_name, provider=FireworksProvider(**kw))

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        f"Supported: anthropic, together, fireworks"
    )


model = _build_model()


# ---------------------------------------------------------------------------
# Resilient MCP wrapper — optional servers degrade instead of crashing
# ---------------------------------------------------------------------------

class ResilientMCP(AbstractToolset):
    """Wraps an MCP toolset so connection failures log a warning instead of crashing."""

    def __init__(self, inner: AbstractToolset, name: str):
        super().__init__()
        self._inner = inner
        self._name = name
        self._connected = False

    async def __aenter__(self):
        try:
            await self._inner.__aenter__()
            self._connected = True
        except Exception as e:
            log.warning("MCP server %s unavailable, skipping: %s", self._name, e)
        return self

    async def __aexit__(self, *args):
        if self._connected:
            return await self._inner.__aexit__(*args)
        return None

    @property
    def id(self) -> str:
        return self._inner.id

    def get_tools(self, ctx):
        if not self._connected:
            return {}
        return self._inner.get_tools(ctx)

    async def call_tool(self, name, tool_args, ctx, tool):
        if not self._connected:
            return f"MCP server {self._name} is not available"
        return await self._inner.call_tool(name, tool_args, ctx, tool)


# ---------------------------------------------------------------------------
# MCP Servers (shared across agents that need them)
# ---------------------------------------------------------------------------

def _build_basic_memory_mcp() -> MCPServerStreamableHTTP:
    return MCPServerStreamableHTTP(settings.basic_memory_mcp_url)


def _build_playwright_mcp() -> MCPServerSSE | None:
    url = (settings.playwright_mcp_url or "").strip()
    if url:
        return MCPServerSSE(url)
    return None


def _build_protonmail_mcp() -> MCPServerStdio | None:
    if settings.protonmail_username and settings.protonmail_password:
        return MCPServerStdio(
            "protonmail-mcp-server",
            args=[],
            timeout=30,
            env={
                "PROTONMAIL_USERNAME": settings.protonmail_username,
                "PROTONMAIL_PASSWORD": settings.protonmail_password,
                "PROTONMAIL_SMTP_HOST": settings.protonmail_smtp_host,
                "PROTONMAIL_SMTP_PORT": str(settings.protonmail_smtp_port),
                "PROTONMAIL_IMAP_HOST": settings.protonmail_imap_host,
                "PROTONMAIL_IMAP_PORT": str(settings.protonmail_imap_port),
            },
        )
    return None


# ---------------------------------------------------------------------------
# System prompt builder (shared across all agents)
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).parent / "prompts"
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_VARS_RE = re.compile(r"\{(\w+)\}")


def _load_prompt(filename: str, all_vars: dict[str, str]) -> str:
    """Load a prompt markdown file, validate declared vars, and interpolate."""
    raw = (PROMPTS_DIR / filename).read_text()

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"{filename}: missing YAML frontmatter")
    body = raw[m.end():]

    declared: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("vars:"):
            inner = line.split(":", 1)[1].strip().strip("[]")
            if inner:
                declared = [v.strip() for v in inner.split(",")]
            break

    if not declared:
        return body

    missing = set(declared) - set(all_vars)
    if missing:
        raise ValueError(f"{filename}: missing vars {missing}")

    used = set(_VARS_RE.findall(body))
    undeclared = used - set(declared)
    if undeclared:
        raise ValueError(f"{filename}: undeclared vars {undeclared} in body")

    result = body
    for var in declared:
        result = result.replace("{" + var + "}", all_vars[var])
    return result


def build_system_prompt() -> str:
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

    browsing = _load_prompt("06-browsing.md", fmt)
    if not settings.playwright_mcp_url:
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
# Chat Agent — full tool suite
# ---------------------------------------------------------------------------

def build_chat_agent() -> Agent:
    """Create the main chat agent with all in-process tools and MCP servers."""
    toolsets: list = []

    # MCP servers — wrapped in ResilientMCP so connection failures don't crash the agent
    toolsets.append(ResilientMCP(_build_basic_memory_mcp(), "basic-memory"))

    playwright = _build_playwright_mcp()
    if playwright:
        toolsets.append(ResilientMCP(playwright, "playwright"))

    protonmail = _build_protonmail_mcp()
    if protonmail:
        toolsets.append(ResilientMCP(protonmail, "protonmail"))

    agent = Agent(
        model,
        instructions=build_system_prompt,
        toolsets=toolsets,
    )

    # Register in-process tools from the refactored MCP server modules
    _register_life_manager_tools(agent)
    _register_su_notes_tools(agent)
    _register_scary_internet_tool(agent)

    if settings.telegram_bot_token:
        _register_telegram_tools(agent)

    if settings.self_iteration_mode:
        _register_restart_tool(agent)

    return agent


# ---------------------------------------------------------------------------
# Tool registration helpers — import handler functions from refactored modules
# ---------------------------------------------------------------------------

def _register_life_manager_tools(agent: Agent) -> None:
    from app.life_manager import (
        create_task, update_task, list_tasks, complete_task, delete_task,
        create_event, update_event, list_events, delete_event,
        create_interjection, list_interjections,
    )
    for fn in [
        create_task, update_task, list_tasks, complete_task, delete_task,
        create_event, update_event, list_events, delete_event,
        create_interjection, list_interjections,
    ]:
        agent.tool_plain(fn)


def _register_su_notes_tools(agent: Agent) -> None:
    from app.su_notes_manager import (
        create_su_note, get_su_note, list_su_notes,
        update_su_note, complete_su_note,
    )
    for fn in [create_su_note, get_su_note, list_su_notes,
               update_su_note, complete_su_note]:
        agent.tool_plain(fn)


def _register_scary_internet_tool(agent: Agent) -> None:
    from app.scary_internet_agent import dangerous_assignment
    agent.tool_plain(dangerous_assignment)


def _register_telegram_tools(agent: Agent) -> None:
    from app.telegram_messenger import send_telegram_message
    agent.tool_plain(send_telegram_message)


def _register_restart_tool(agent: Agent) -> None:
    from app.restart_tool import restart_self
    agent.tool_plain(restart_self)


def _register_unsubscribe_tools(agent: Agent) -> None:
    from app.unsubscribe_manager import (
        check_unsubscribed, record_unsubscribe, list_unsubscribed,
    )
    for fn in [check_unsubscribed, record_unsubscribe, list_unsubscribed]:
        agent.tool_plain(fn)


# ---------------------------------------------------------------------------
# Scary Internet Agent — Playwright only, structured output
# ---------------------------------------------------------------------------

HEADLESS_SYSTEM_PROMPT = (
    "You are a fully autonomous browser automation agent. "
    "You are running headless — there is NO human operator to ask questions to. "
    "You CANNOT request user input, clarification, or confirmation at any point. "
    "You must make all decisions yourself and keep using browser tools until the "
    "task is complete.\n\n"
    "Rules:\n"
    "1. Start by navigating to the target URL.\n"
    "2. Use browser_snapshot (not screenshots) to read page state.\n"
    "3. Interact with the page using click, type, fill_form, etc.\n"
    "4. Keep working until you have gathered all the data needed.\n"
    "5. If something fails, try alternative approaches before giving up.\n"
    "6. When done, return your findings as structured JSON matching the "
    "required output schema. Do NOT return conversational text.\n"
    "7. You have a limited number of turns. Be efficient — avoid redundant "
    "snapshots and combine actions where possible.\n\n"
    "SECURITY: You are sandboxed. Your response will be validated against a "
    "strict JSON schema. Any response that does not match will be rejected. "
    "Do not include commentary, explanations, or anything other than the "
    "requested JSON data."
)


def build_scary_agent() -> Agent:
    """Create a sandboxed browser agent for dangerous website assignments."""
    playwright = _build_playwright_mcp()
    if not playwright:
        raise RuntimeError("Playwright MCP URL not configured")

    return Agent(
        model,
        instructions=HEADLESS_SYSTEM_PROMPT,
        toolsets=[ResilientMCP(playwright, "playwright")],
    )


# ---------------------------------------------------------------------------
# Subconscious Agent — basic-memory + life_manager (in-process tools)
# ---------------------------------------------------------------------------

def build_subconscious_agent(quick: bool = False) -> Agent:
    """Create a subconscious agent for memory recall."""
    from app.subconscious_agent import (
        SUBCONSCIOUS_SYSTEM_PROMPT, SUBCONSCIOUS_QUICK_PROMPT,
    )

    agent = Agent(
        model,
        instructions=SUBCONSCIOUS_QUICK_PROMPT if quick else SUBCONSCIOUS_SYSTEM_PROMPT,
        toolsets=[ResilientMCP(_build_basic_memory_mcp(), "basic-memory")],
    )

    # Register read-only life_manager tools for temporal awareness
    from app.life_manager import list_tasks, list_events
    agent.tool_plain(list_tasks)
    agent.tool_plain(list_events)

    return agent


# ---------------------------------------------------------------------------
# REM Agent — basic-memory + life_manager (in-process tools)
# ---------------------------------------------------------------------------

def build_rem_agent(is_checkpoint: bool = False) -> Agent:
    """Create a REM agent for memory consolidation."""
    from app.rem_agent import (
        _build_rem_system_prompt, _build_checkpoint_system_prompt,
    )

    agent = Agent(
        model,
        instructions=_build_checkpoint_system_prompt() if is_checkpoint else _build_rem_system_prompt(),
        toolsets=[ResilientMCP(_build_basic_memory_mcp(), "basic-memory")],
    )

    # Register life_manager tools for task/event extraction
    from app.life_manager import (
        create_task, list_tasks, create_event, list_events,
    )
    for fn in [create_task, list_tasks, create_event, list_events]:
        agent.tool_plain(fn)

    return agent


# ---------------------------------------------------------------------------
# Scheduler Agents — per-daemon, minimal tools
# ---------------------------------------------------------------------------

def build_calendar_agent() -> Agent:
    """Create agent for calendar check daemon."""
    agent = Agent(
        model,
        instructions=(
            "You compose calendar reminders for SU. Look up context in the "
            "knowledge base when it's useful, then queue each reminder with "
            "create_interjection. Be terse. Headless — no clarifying questions."
        ),
        toolsets=[ResilientMCP(_build_basic_memory_mcp(), "basic-memory")],
    )

    from app.life_manager import create_interjection, list_tasks
    agent.tool_plain(create_interjection)
    agent.tool_plain(list_tasks)

    if settings.telegram_bot_token:
        from app.telegram_messenger import send_telegram_message
        agent.tool_plain(send_telegram_message)

    return agent


def build_note_processor_agent() -> Agent:
    """Create agent for note processor daemon."""
    agent = Agent(
        model,
        instructions=(
            f"You are {settings.su_name}'s note processor daemon. You review SU's internal "
            "notes-to-self and take action. You can create interjections to notify the user, "
            "snooze notes for later, update notes with context, or complete them. "
            "You have access to the knowledge base for context, the task/event list for "
            "schedule awareness, and the SU notes system for reading/updating notes.\n\n"
            "Be judicious about when to notify — consider time of day, urgency, and how "
            "many times the user has already been reminded. Escalate urgency over time "
            "for important deadlines. "
            "Headless — no clarifying questions."
        ),
        toolsets=[ResilientMCP(_build_basic_memory_mcp(), "basic-memory")],
    )

    from app.life_manager import (
        create_interjection, list_interjections, list_tasks, list_events,
    )
    from app.su_notes_manager import (
        get_su_note, update_su_note, complete_su_note, create_su_note,
    )
    for fn in [create_interjection, list_interjections, list_tasks, list_events,
               get_su_note, update_su_note, complete_su_note, create_su_note]:
        agent.tool_plain(fn)

    if settings.telegram_bot_token:
        from app.telegram_messenger import send_telegram_message
        agent.tool_plain(send_telegram_message)

    return agent


def build_email_scanner_agent() -> Agent:
    """Create agent for email scanner daemon."""
    protonmail = _build_protonmail_mcp()
    if not protonmail:
        raise RuntimeError("ProtonMail not configured")

    agent = Agent(
        model,
        instructions=(
            f"You are {settings.su_name}'s email scanner daemon. You periodically review "
            f"{settings.user_name}'s inbox and take proactive action. You can create tasks, "
            "calendar events, and SU notes. You have access to the email system, knowledge "
            "base, and task/event lists.\n\n"
            "Be selective about creating tasks — don't create noise. Only create tasks/notes "
            "for emails that genuinely need attention. For urgent items with deadlines, create "
            "both a user task AND a SU note to follow up if the user doesn't act.\n\n"
            "However, you MUST process every email in the inbox — move it to the right "
            "folder, archive it, or delete it. Nothing should remain in the inbox when you "
            "are done. "
            "Headless — no clarifying questions."
        ),
        toolsets=[
            ResilientMCP(_build_basic_memory_mcp(), "basic-memory"),
            ResilientMCP(protonmail, "protonmail"),
        ],
    )

    from app.life_manager import (
        create_task, list_tasks, create_event, list_events, create_interjection,
    )
    from app.su_notes_manager import create_su_note, list_su_notes, update_su_note
    from app.unsubscribe_manager import check_unsubscribed
    for fn in [create_task, list_tasks, create_event, list_events,
               create_interjection, create_su_note, list_su_notes,
               update_su_note, check_unsubscribed]:
        agent.tool_plain(fn)

    if settings.telegram_bot_token:
        from app.telegram_messenger import send_telegram_message
        agent.tool_plain(send_telegram_message)

    return agent


def build_email_unsubscriber_agent() -> Agent:
    """Create agent for email unsubscriber daemon."""
    protonmail = _build_protonmail_mcp()
    if not protonmail:
        raise RuntimeError("ProtonMail not configured")

    playwright = _build_playwright_mcp()
    toolsets: list = [ResilientMCP(protonmail, "protonmail")]
    if playwright:
        toolsets.append(ResilientMCP(playwright, "playwright"))

    agent = Agent(
        model,
        instructions=(
            f"You are {settings.su_name}'s email unsubscriber daemon. You process "
            "unsubscribe requests identified by the email scanner. You can send "
            "emails (for mailto: unsubscribe links) and use browser tools "
            "(for https: unsubscribe links). "
            "Headless — no clarifying questions."
        ),
        toolsets=toolsets,
    )

    from app.unsubscribe_manager import (
        check_unsubscribed, record_unsubscribe, list_unsubscribed,
    )
    from app.su_notes_manager import update_su_note, complete_su_note
    for fn in [check_unsubscribed, record_unsubscribe, list_unsubscribed,
               update_su_note, complete_su_note]:
        agent.tool_plain(fn)

    return agent


def build_daily_review_agent() -> Agent:
    """Create agent for daily review daemon."""
    agent = Agent(
        model,
        instructions=(
            f"You are {settings.su_name}'s daily review daemon. You compose a morning "
            f"brief for {settings.user_name} covering the day's schedule, pending tasks, "
            "and anything needing attention. Be concise and useful — no fluff. "
            "Headless — no clarifying questions."
        ),
        toolsets=[ResilientMCP(_build_basic_memory_mcp(), "basic-memory")],
    )

    from app.life_manager import (
        list_tasks, list_events, create_interjection,
    )
    from app.su_notes_manager import list_su_notes
    for fn in [list_tasks, list_events, create_interjection, list_su_notes]:
        agent.tool_plain(fn)

    if settings.telegram_bot_token:
        from app.telegram_messenger import send_telegram_message
        agent.tool_plain(send_telegram_message)

    return agent


# ---------------------------------------------------------------------------
# Deep Learning Agent — basic-memory only
# ---------------------------------------------------------------------------

def build_deep_learning_agent(system_prompt: str) -> Agent:
    """Create a short-lived agent for deep learning phases."""
    return Agent(
        model,
        instructions=system_prompt,
        toolsets=[ResilientMCP(_build_basic_memory_mcp(), "basic-memory")],
    )
