"""
Website subagent: SDK MCP tool that spawns a Playwright-equipped Claude subagent
to interact with pre-registered websites and return structured data.
"""
import asyncio
import json
from typing import Any

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

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from app.logger import get_logger
from app.website_models import WEBSITE_REGISTRY

log = get_logger(__name__)

# Maximum seconds to wait for the subagent to finish browsing.
SUBAGENT_TIMEOUT_SECONDS = 120

# Module-level queue for streaming subagent progress to the frontend.
subagent_event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()


def _build_playwright_mcp_config() -> dict:
    """Connect to Playwright MCP running as an SSE server on the host."""
    return {
        "type": "sse",
        "url": "http://host.docker.internal:8931/sse",
    }


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
    "snapshots and combine actions where possible."
)


@tool(
    "browse_website",
    "Browse a registered website using an autonomous browser-equipped subagent. "
    "The subagent navigates to the website, executes the given instructions, "
    "and returns structured data. Available websites: "
    + ", ".join(WEBSITE_REGISTRY.keys()),
    {
        "type": "object",
        "properties": {
            "website": {
                "type": "string",
                "description": "Name of the registered website (e.g., 'email', 'airbnb')",
                "enum": list(WEBSITE_REGISTRY.keys()),
            },
            "instructions": {
                "type": "string",
                "description": (
                    "Specific instructions for what to do on the website. "
                    "Appended to the website's default instructions."
                ),
            },
        },
        "required": ["website"],
    },
)
async def browse_website(args: dict[str, Any]) -> dict[str, Any]:
    """Validate website, spawn Playwright subagent, return structured result."""
    website_name = args["website"].lower()
    user_instructions = args.get("instructions", "")

    log.info("website.tool_called", website=website_name, has_instructions=bool(user_instructions))

    config = WEBSITE_REGISTRY.get(website_name)
    if config is None:
        log.warning("website.unknown", website=website_name, available=list(WEBSITE_REGISTRY.keys()))
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Unknown website: {website_name}",
                    "available": list(WEBSITE_REGISTRY.keys()),
                }),
            }],
            "is_error": True,
        }

    response_schema = config.response_model.model_json_schema()

    prompt = (
        f"Navigate to {config.url} and complete the following task.\n\n"
        f"Website context: {config.instructions}\n\n"
    )
    if user_instructions:
        prompt += f"Additional instructions: {user_instructions}\n\n"
    prompt += (
        f"Return your result as JSON matching this schema:\n"
        f"```json\n{json.dumps(response_schema, indent=2)}\n```"
    )

    site_system_prompt = (
        f"{HEADLESS_SYSTEM_PROMPT}\n\n"
        f"Target website: {config.url}\n"
        f"You must ONLY interact with {config.url}. "
        f"Do not navigate to any other domain."
    )

    subagent_options = ClaudeAgentOptions(
        mcp_servers={
            "playwright": _build_playwright_mcp_config(),
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

            def _emit(event: dict[str, Any]):
                log.debug("website.subagent_emit", event_type=event.get("type"))
                subagent_event_queue.put_nowait(event)

            _emit({"type": "subagent_status", "message": f"Launching browser for {config.url}"})
            log.info("website.subagent_launching", website=website_name, url=config.url)

            async with ClaudeSDKClient(options=subagent_options) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    msg_type = type(message).__name__
                    log.debug("website.subagent_message", msg_type=msg_type)

                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            mcp_servers = message.data.get("mcp_servers", [])
                            for srv in mcp_servers:
                                status = srv.get("status", "unknown")
                                name = srv.get("name", "unknown")
                                if status != "connected":
                                    log.error("website.mcp_failed", server_name=name, server_status=status)
                                    _emit({"type": "subagent_status", "message": f"MCP server {name} failed to connect"})
                                else:
                                    log.info("website.mcp_connected", server_name=name)
                            _emit({"type": "subagent_status", "message": "Subagent connected"})

                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                log.debug("website.subagent_tool", tool_name=block.name)
                                _emit({
                                    "type": "subagent_tool",
                                    "name": block.name,
                                    "input": block.input,
                                })
                            elif isinstance(block, TextBlock):
                                _emit({
                                    "type": "subagent_text",
                                    "content": block.text[:500],
                                })

                    elif isinstance(message, ResultMessage):
                        if message.structured_output:
                            structured_result = message.structured_output
                        elif message.result:
                            text_result = message.result

                        if message.is_error:
                            log.error("website.subagent_error", website=website_name, result=message.result)
                            _emit({"type": "subagent_status", "message": f"Error: {message.result or 'Unknown'}"})
                            raise RuntimeError(
                                message.result or "Subagent error"
                            )
                        _emit({"type": "subagent_status", "message": "Done"})

        await asyncio.wait_for(
            _run_subagent(), timeout=SUBAGENT_TIMEOUT_SECONDS
        )

        if structured_result:
            validated = config.response_model.model_validate(structured_result)
            log.info("website.success", website=website_name, result_type="structured")
            return {
                "content": [{
                    "type": "text",
                    "text": validated.model_dump_json(indent=2),
                }],
            }

        # Fallback: try to parse text result as JSON
        if text_result:
            try:
                parsed = json.loads(text_result)
                validated = config.response_model.model_validate(parsed)
                log.info("website.success", website=website_name, result_type="text_parsed")
                return {
                    "content": [{
                        "type": "text",
                        "text": validated.model_dump_json(indent=2),
                    }],
                }
            except (json.JSONDecodeError, Exception) as e:
                log.warning("website.parse_failed", website=website_name, error=str(e))

        log.warning("website.no_structured_data", website=website_name)
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Subagent did not return structured data",
                    "raw_response": text_result[:2000] if text_result else "No response",
                }),
            }],
            "is_error": True,
        }

    except asyncio.TimeoutError:
        log.error(
            "website.timeout",
            website=website_name,
            timeout_seconds=SUBAGENT_TIMEOUT_SECONDS,
        )
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Subagent timed out after {SUBAGENT_TIMEOUT_SECONDS}s",
                    "raw_response": text_result[:2000] if text_result else "No response yet",
                }),
            }],
            "is_error": True,
        }

    except Exception as e:
        log.exception("website.execution_failed", website=website_name, error=str(e))
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": f"Subagent execution failed: {str(e)}",
                }),
            }],
            "is_error": True,
        }


website_mcp_server = create_sdk_mcp_server(
    name="website",
    tools=[browse_website],
)
