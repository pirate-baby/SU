"""
Scary Internet Agent: isolated subagent for browsing dangerous websites.

Websites like email inboxes, Reddit, social media, etc. can contain prompt
injection attacks. This tool sandboxes all interaction with such sites inside
a throwaway subagent that can ONLY return data matching a caller-specified
JSON schema — nothing else gets out.
"""
import asyncio
import json
from typing import Any
from urllib.parse import urlparse

import inspect
import jsonschema

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

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)

SUBAGENT_TIMEOUT_SECONDS = 180

# Playwright MCP SSE endpoint on the host.
PLAYWRIGHT_MCP_URL = settings.playwright_mcp_url or "http://host.docker.internal:8931/sse"

PLAYWRIGHT_ALLOWED_TOOLS = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_hover",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_wait_for",
    "mcp__playwright__browser_tabs",
    "mcp__playwright__browser_close",
    "mcp__playwright__browser_evaluate",
    "mcp__playwright__browser_console_messages",
    "mcp__playwright__browser_network_requests",
    "mcp__playwright__browser_navigate_back",
    "mcp__playwright__browser_resize",
    "mcp__playwright__browser_drag",
    "mcp__playwright__browser_run_code",
    "mcp__playwright__browser_file_upload",
    "mcp__playwright__browser_handle_dialog",
    "mcp__playwright__browser_install",
]

HEADLESS_SYSTEM_PROMPT = (
    "You are a fully autonomous browser automation agent. "
    "You are running headless — there is NO human operator to ask questions to. "
    "You CANNOT request user input, clarification, or confirmation at any point. "
    "You must make all decisions yourself and keep using browser tools until the "
    "task is complete.\n\n"
    "Your tools all have the prefix mcp__playwright__ (e.g. "
    "mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot). "
    "You MUST use these exact tool names.\n\n"
    "Rules:\n"
    "1. Start by navigating to the target URL with mcp__playwright__browser_navigate.\n"
    "2. Use mcp__playwright__browser_snapshot (not screenshots) to read page state.\n"
    "3. Interact with the page using mcp__playwright__browser_click, "
    "mcp__playwright__browser_type, mcp__playwright__browser_fill_form, etc.\n"
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


def _extract_domains(urls: list[str]) -> list[str]:
    """Extract domain names from a list of URLs."""
    domains = []
    for url in urls:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        # Strip www. prefix for cleaner display
        if domain.startswith("www."):
            domain = domain[4:]
        domains.append(domain)
    return domains


@tool(
    "dangerous_assignment",
    "Send an isolated browser agent to a dangerous website (email, Reddit, social "
    "media, forums, etc.) where prompt injection attacks could be present. The agent "
    "completes the assignment and returns ONLY structured JSON matching the provided "
    "schema — no other content escapes the sandbox. Use this for any website where "
    "untrusted user-generated content could be encountered.",
    {
        "type": "object",
        "properties": {
            "assignment": {
                "type": "string",
                "description": (
                    "Atomic instructions for what the browser agent should do. "
                    "Be specific: what to navigate to, what to look for, what "
                    "data to extract."
                ),
            },
            "websites_allowed": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of allowed website URLs (e.g. ['https://reddit.com', "
                    "'https://mail.proton.me']). The agent will ONLY be permitted "
                    "to navigate to these domains."
                ),
            },
            "response_schema": {
                "type": "object",
                "description": (
                    "JSON Schema that the agent's response MUST match. The response "
                    "will be validated against this schema — any non-conforming "
                    "response is rejected as potentially dangerous."
                ),
            },
        },
        "required": ["assignment", "websites_allowed", "response_schema"],
    },
)
async def dangerous_assignment(args: dict[str, Any]) -> dict[str, Any]:
    """Spawn an isolated Playwright subagent, validate its response against the schema."""
    assignment = args["assignment"]
    websites_allowed = args["websites_allowed"]
    response_schema = args["response_schema"]

    domains = _extract_domains(websites_allowed)
    domains_str = ", ".join(domains)

    log.info(
        "scary.assignment_started",
        domains=domains,
        assignment_len=len(assignment),
    )

    # Build the domain restriction
    domain_rules = "\n".join(
        f"  - {url}" for url in websites_allowed
    )

    prompt = (
        f"Complete this assignment:\n\n"
        f"{assignment}\n\n"
        f"You may ONLY navigate to these websites:\n{domain_rules}\n\n"
        f"Return your result as JSON matching this exact schema:\n"
        f"```json\n{json.dumps(response_schema, indent=2)}\n```"
    )

    site_system_prompt = (
        f"{HEADLESS_SYSTEM_PROMPT}\n\n"
        f"ALLOWED DOMAINS: {domains_str}\n"
        f"You must ONLY interact with these domains. "
        f"Do NOT navigate to any other domain."
    )

    subagent_options = ClaudeAgentOptions(
        mcp_servers={
            "playwright": {
                "type": "sse",
                "url": PLAYWRIGHT_MCP_URL,
            },
        },
        allowed_tools=PLAYWRIGHT_ALLOWED_TOOLS,
        disallowed_tools=[
            "Task",
            "Bash",
            "Glob",
            "Grep",
            "Read",
            "Edit",
            "Write",
            "WebFetch",
            "WebSearch",
            "NotebookEdit",
            "Skill",
            "TodoWrite",
            "EnterPlanMode",
            "ExitPlanMode",
            "TaskOutput",
            "TaskStop",
        ],
        permission_mode="bypassPermissions",
        max_turns=30,
        output_format={
            "type": "json_schema",
            "schema": response_schema,
        },
        system_prompt=site_system_prompt,
    )

    try:
        structured_result = None
        text_result = ""

        async def _run_subagent():
            nonlocal structured_result, text_result

            log.info("scary.subagent_launching", domains=domains)

            async with ClaudeSDKClient(options=subagent_options) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            mcp_servers = message.data.get("mcp_servers", [])
                            for srv in mcp_servers:
                                status = srv.get("status", "unknown")
                                name = srv.get("name", "unknown")
                                if status != "connected":
                                    log.error("scary.mcp_failed", server_name=name, server_status=status)
                                else:
                                    log.info("scary.mcp_connected", server_name=name)

                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                log.debug("scary.tool_use", tool_name=block.name)

                    elif isinstance(message, ResultMessage):
                        if message.structured_output:
                            structured_result = message.structured_output
                        elif message.result:
                            text_result = message.result

                        if message.is_error:
                            log.error("scary.subagent_error", domains=domains, result=message.result)
                            raise RuntimeError(message.result or "Subagent error")

        await asyncio.wait_for(
            _run_subagent(), timeout=SUBAGENT_TIMEOUT_SECONDS
        )

        # ── Validate the result against the provided schema ──
        result_to_validate = structured_result
        if result_to_validate is None and text_result:
            try:
                result_to_validate = json.loads(text_result)
            except json.JSONDecodeError:
                pass

        if result_to_validate is None:
            log.warning("scary.no_data", domains=domains)
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": "Subagent did not return any structured data",
                })}],
                "is_error": True,
            }

        # Strict schema validation — this is the security gate
        try:
            jsonschema.validate(instance=result_to_validate, schema=response_schema)
        except jsonschema.ValidationError as e:
            log.error(
                "scary.schema_validation_failed",
                domains=domains,
                error=str(e.message),
            )
            return {
                "content": [{"type": "text", "text": json.dumps({
                    "error": "Response from dangerous website failed schema validation — "
                             "this could indicate a prompt injection attack. "
                             "The response has been discarded.",
                    "validation_error": e.message,
                })}],
                "is_error": True,
            }

        log.info("scary.success", domains=domains)
        return {
            "content": [{"type": "text", "text": json.dumps(result_to_validate, indent=2, default=str)}],
        }

    except asyncio.TimeoutError:
        log.error("scary.timeout", domains=domains, timeout=SUBAGENT_TIMEOUT_SECONDS)
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Subagent timed out after {SUBAGENT_TIMEOUT_SECONDS}s",
            })}],
            "is_error": True,
        }

    except Exception as e:
        log.exception("scary.execution_failed", domains=domains, error=str(e))
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Subagent execution failed: {str(e)}",
            })}],
            "is_error": True,
        }


scary_internet_mcp_server = create_sdk_mcp_server(
    name="scary_internet",
    tools=[dangerous_assignment],
)
