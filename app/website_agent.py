"""
Website subagent: SDK MCP tool that spawns a Playwright-equipped Claude subagent
to interact with pre-registered websites and return structured data.
"""
import json
import logging
import platform
from pathlib import Path
from typing import Any

import inspect

from mcp.server import Server as _McpServer

# Monkey-patch: mcp 0.9.x+ removed the `version` kwarg from Server.__init__,
# but claude-agent-sdk's create_sdk_mcp_server still passes it.  We must
# preserve it as an instance attribute because the SDK's control-protocol
# handler reads `server.version` during MCP initialization.
_orig_server_init = _McpServer.__init__
if "version" not in inspect.signature(_orig_server_init).parameters:
    def _patched_server_init(self, name, **kwargs):
        version = kwargs.pop("version", "1.0.0")
        _orig_server_init(self, name, **kwargs)
        self.version = version
    _McpServer.__init__ = _patched_server_init

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from app.website_models import WEBSITE_REGISTRY

logger = logging.getLogger(__name__)


def _get_chrome_user_data_dir() -> str:
    """Return the default Chrome user data directory for the current platform."""
    system = platform.system()
    if system == "Darwin":
        return str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    elif system == "Linux":
        return str(Path.home() / ".config" / "google-chrome")
    elif system == "Windows":
        return str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def _build_playwright_mcp_config() -> dict:
    """Build the McpStdioServerConfig for Playwright MCP with Chrome user profile."""
    return {
        "type": "stdio",
        "command": "npx",
        "args": [
            "-y",
            "@playwright/mcp@latest",
            "--browser", "chrome",
            "--user-data-dir", _get_chrome_user_data_dir(),
        ],
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
    "Rules:\n"
    "1. Start by navigating to the target URL.\n"
    "2. Use browser_snapshot (not screenshots) to read page state.\n"
    "3. Interact with the page using click, type, fill_form, etc.\n"
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

    config = WEBSITE_REGISTRY.get(website_name)
    if config is None:
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

        async with ClaudeSDKClient(options=subagent_options) as client:
            await client.query(prompt)

            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    if message.structured_output:
                        structured_result = message.structured_output
                    elif message.result:
                        text_result = message.result

                    if message.is_error:
                        return {
                            "content": [{
                                "type": "text",
                                "text": json.dumps({
                                    "error": "Subagent encountered an error",
                                    "details": message.result or "Unknown error",
                                }),
                            }],
                            "is_error": True,
                        }

        if structured_result:
            validated = config.response_model.model_validate(structured_result)
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
                return {
                    "content": [{
                        "type": "text",
                        "text": validated.model_dump_json(indent=2),
                    }],
                }
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Failed to parse subagent text as JSON: %s", e)

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

    except Exception as e:
        logger.exception("Subagent execution failed")
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
