# Plan: Replace Claude Agent SDK with pydantic-ai

## Goal
Replace the `claude-agent-sdk` (which spawns ~350MB `claude` subprocesses) with
`pydantic-ai` — a lightweight agent framework that handles agentic loops, MCP
connections, streaming, and tool dispatch natively. Uses the existing OAuth
subscription token for auth. Eliminates subprocess overhead, the process-slot
semaphore system, and ~650 LOC of custom infrastructure we'd otherwise have to
hand-roll.

## Why pydantic-ai over hand-rolling

The previous version of this plan proposed ~850 LOC of custom code:
- `agent_client.py` (~500 LOC) — manual tool loop, message management, MCP dispatch
- `mcp_client.py` (~150 LOC) — custom stdio/SSE MCP client
- `tool_registry.py` (~200 LOC) — manual tool schema definitions + dispatch dict

pydantic-ai provides all of this out of the box:
- `agent.run_stream()` handles the agentic tool loop (tool_call → execute → feed → repeat)
- `MCPServerStdio` / `MCPServerSSE` handle MCP connections and lifecycle
- `@agent.tool` auto-generates tool schemas from function signatures
- `output_type=` enforces structured output with automatic retry
- `message_history=` manages multi-turn conversation state
- `FallbackModel` supports graceful model failover

Additionally, pydantic-ai gives us a **model provider abstraction** — we can swap
from Anthropic to any OpenAI-compatible endpoint (Ollama, llama.cpp, vLLM) by
changing one line, with zero changes to agent logic, tools, or MCP connections.

## Architecture Change

```
BEFORE:                                    AFTER:
FastAPI                                    FastAPI
  └─ ClaudeSDKClient                         └─ pydantic-ai Agent
       └─ spawns `claude` subprocess              └─ httpx call to api.anthropic.com
            (~350MB RSS each)                          (~0MB marginal overhead)
            └─ manages MCP internally                  └─ MCPServerStdio / MCPServerSSE
            └─ manages tool loop internally            └─ agent.run_stream() auto-loops
            └─ manages conversation state              └─ message_history= parameter
```

## Key Design Decisions

### 1. Auth
Pass a pre-built `anthropic.AsyncAnthropic` client with `auth_token`:

```python
import anthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

client = anthropic.AsyncAnthropic(auth_token=settings.claude_code_oauth_token)
model = AnthropicModel('claude-sonnet-4-6', provider=AnthropicProvider(anthropic_client=client))
```

Same `Authorization: Bearer` header, same token from `CLAUDE_CODE_OAUTH_TOKEN`.

### 2. MCP Strategy

**External MCP servers** use pydantic-ai's built-in MCP client:

```python
from pydantic_ai.mcp import MCPServerStdio, MCPServerSSE

# ProtonMail — stdio subprocess
protonmail = MCPServerStdio(
    'protonmail-mcp-server',
    env={
        'PROTONMAIL_USERNAME': settings.protonmail_username,
        'PROTONMAIL_PASSWORD': settings.protonmail_password,
        ...
    },
)

# Playwright — SSE
playwright = MCPServerSSE(settings.playwright_mcp_url)

# basic-memory — stdio subprocess
basic_memory = MCPServerStdio('uvx', args=['basic-memory', 'mcp'])
```

**In-process tools** (life_manager, su_notes, telegram, etc.) use `@agent.tool`:

```python
@chat_agent.tool
async def create_task(ctx: RunContext, title: str, due_date: str | None = None) -> str:
    """Create a new task in the planner."""
    result = await life_manager.create_task(title=title, due_date=due_date)
    return json.dumps(result)
```

No custom `mcp_client.py` needed. No custom `tool_registry.py` needed.

### 3. Streaming
Use `run_stream_events()` which yields exactly the event types our WebSocket
handler needs:

```python
from pydantic_ai import PartDeltaEvent, TextPartDelta, FunctionToolCallEvent, FunctionToolResultEvent

async for event in agent.run_stream_events(user_message, message_history=history):
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        yield {"type": "text", "content": event.delta.content_delta}
    elif isinstance(event, FunctionToolCallEvent):
        yield {"type": "tool_use", "id": event.part.tool_call_id,
               "name": event.part.tool_name, "input": event.part.args}
    elif isinstance(event, FunctionToolResultEvent):
        yield {"type": "tool_result", "tool_use_id": event.tool_call_id,
               "content": event.result.content, "is_error": event.result.is_error}
```

### 4. Conversation State
pydantic-ai manages message history natively:

```python
# First message
result1 = await agent.run(user_message)

# Subsequent messages — pass history
result2 = await agent.run(next_message, message_history=result1.all_messages())

# Persist to DB
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_core import to_jsonable_python
serialized = to_jsonable_python(result.all_messages())  # store in SQLite
restored = ModelMessagesTypeAdapter.validate_python(from_db)  # restore
```

### 5. Multiple Agent Instances
Each use case gets its own `Agent` with appropriate tools and prompts:

```python
# Main chat agent — full tool suite
chat_agent = Agent(
    model,
    instructions=build_chat_system_prompt(),
    tools=[dangerous_assignment, send_telegram, create_task, ...],
    toolsets=[protonmail, playwright, basic_memory],
)

# Scary internet — Playwright only, structured output
scary_agent = Agent(
    model,
    instructions=SCARY_INTERNET_PROMPT,
    output_type=StructuredDict(caller_schema),  # enforced JSON schema
    toolsets=[playwright],
)

# Subconscious — basic-memory only
subconscious_agent = Agent(
    model,
    instructions=SUBCONSCIOUS_PROMPT,
    toolsets=[basic_memory],
)

# REM — basic-memory only
rem_agent = Agent(model, instructions=REM_PROMPT, toolsets=[basic_memory])

# Scheduler daemons — minimal tools per job
email_scanner_agent = Agent(model, instructions=EMAIL_SCANNER_PROMPT, toolsets=[protonmail])
```

### 6. Model Portability
Swap to any provider without changing agent code:

```python
# Anthropic (current)
model = AnthropicModel('claude-sonnet-4-6', provider=AnthropicProvider(anthropic_client=client))

# Local Ollama
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
model = OpenAIChatModel('llama3:70b', provider=OllamaProvider(base_url='http://localhost:11434/v1'))

# llama.cpp
from pydantic_ai.providers.openai import OpenAIProvider
model = OpenAIChatModel('local', provider=OpenAIProvider(base_url='http://localhost:8080/v1', api_key='none'))

# Fallback chain
from pydantic_ai.models.fallback import FallbackModel
model = FallbackModel(anthropic_model, local_model)  # try Claude, fall back to local
```

---

## Files to Change

### New Files

#### `app/agents.py` — Agent definitions (~200 LOC)
Central module that creates and configures all agent instances.

```python
"""Agent definitions for all SU agent modes."""
import anthropic
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.mcp import MCPServerStdio, MCPServerSSE

from app.config import settings
from app.prompts import build_system_prompt

# --- Model ---
_anthropic_client = anthropic.AsyncAnthropic(auth_token=settings.claude_code_oauth_token)
model = AnthropicModel(
    settings.anthropic_model,
    provider=AnthropicProvider(anthropic_client=_anthropic_client),
)

# --- MCP Servers (shared across agents that need them) ---
playwright_mcp = MCPServerSSE(settings.playwright_mcp_url) if settings.playwright_mcp_url else None
basic_memory_mcp = MCPServerStdio('uvx', args=['basic-memory', 'mcp'])
protonmail_mcp = MCPServerStdio(
    'protonmail-mcp-server',
    env={...},
) if settings.protonmail_username else None

# --- Chat Agent ---
chat_agent = Agent(
    model,
    instructions=build_system_prompt("chat"),
    toolsets=[s for s in [playwright_mcp, basic_memory_mcp, protonmail_mcp] if s],
)

# Register in-process tools on chat_agent
@chat_agent.tool
async def dangerous_assignment(ctx: RunContext, assignment: str, websites_allowed: list[str], response_schema: dict) -> str:
    """Send isolated browser agent to a dangerous website..."""
    ...

@chat_agent.tool
async def create_task(ctx: RunContext, title: str, ...) -> str: ...
@chat_agent.tool
async def send_telegram_message(ctx: RunContext, content: str) -> str: ...
# ... etc for all in-process tools

# --- Scary Internet Agent ---
def make_scary_agent(response_schema: dict) -> Agent:
    """Create a sandboxed browser agent for a specific assignment."""
    return Agent(
        model,
        instructions=SCARY_INTERNET_PROMPT,
        output_type=StructuredDict(response_schema),
        toolsets=[playwright_mcp],
    )

# --- Memory Agents ---
subconscious_agent = Agent(model, instructions=SUBCONSCIOUS_PROMPT, toolsets=[basic_memory_mcp])
rem_agent = Agent(model, instructions=REM_PROMPT, toolsets=[basic_memory_mcp])

# --- Scheduler Agents ---
email_scanner_agent = Agent(model, instructions=EMAIL_SCANNER_PROMPT, toolsets=[protonmail_mcp] if protonmail_mcp else [])
# ... etc
```

### Modified Files

#### `app/claude_client.py` → **DELETE** (replaced by `app/agents.py`)

#### `app/process_limiter.py` → **DELETE**
No subprocesses = no semaphore. pydantic-ai agents are just Python objects making
HTTP calls.

#### `app/agent_registry.py` → **SIMPLIFY**
- Remove all semaphore/slot management
- Keep session tracking, idle cleanup, per-session locking
- `get_or_create_agent()` returns a session wrapper holding message history
- No `connect()/disconnect()` lifecycle — agents are module-level singletons
- History: store `result.all_messages()` serialized to DB, restore with
  `ModelMessagesTypeAdapter.validate_python()`

```python
class SessionState:
    """Tracks conversation state for one chat session."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.message_history: list = []  # pydantic-ai message objects

    async def send_message(self, user_message: str) -> AsyncGenerator[dict, None]:
        async for event in chat_agent.run_stream_events(
            user_message, message_history=self.message_history
        ):
            # yield events to WebSocket...
            ...
        # Update history from result
        self.message_history = result.all_messages()
```

#### `app/scary_internet_agent.py` → **SIMPLIFY**
- `dangerous_assignment()` becomes a `@chat_agent.tool` on the chat agent
- Internally creates a `make_scary_agent(response_schema)` and calls
  `await agent.run(prompt)` — pydantic-ai handles the full multi-turn
  Playwright loop (10-30+ tool calls) automatically
- `output_type=StructuredDict(response_schema)` replaces manual
  `jsonschema.validate()` — pydantic-ai enforces the schema and retries
  on validation failure
- Same timeout via `asyncio.wait_for()`

#### `app/subconscious_agent.py` → **SIMPLIFY**
- Replace `ClaudeSDKClient` + process slot with `await subconscious_agent.run(...)`
- basic-memory MCP connection managed by pydantic-ai

#### `app/rem_agent.py` → **SIMPLIFY**
- Replace `ClaudeSDKClient` + process slot with `await rem_agent.run(...)`

#### `app/scheduler.py` → **SIMPLIFY**
- All daemon jobs: replace `ClaudeSDKClient` + `claude_process_slot()` with
  `await email_scanner_agent.run(prompt)` etc.
- Remove all `async with claude_process_slot(timeout=...)` wrappers
- Daemon logic, intervals, and observability stay identical

#### `app/main.py` → **MINOR CHANGES**
- WebSocket handler uses `SessionState.send_message()` instead of
  `ClaudeChat.send_message()`
- Stream event format stays identical (text/tool_use/tool_result)

#### `app/config.py` → **MINOR**
- Keep `claude_code_oauth_token`
- Add `anthropic_model: str = "claude-sonnet-4-6"`
- Add `model_provider: str = "anthropic"` (for future local model support)

#### `pyproject.toml` → **UPDATE DEPS**
- Remove: `claude-agent-sdk`
- Add: `pydantic-ai[anthropic]`
- Keep: `mcp` (pydantic-ai uses it internally, but it's already a dep)

#### `Dockerfile` → **SIMPLIFY**
- Remove Node.js installation (was needed for claude-agent-sdk subprocess)
- Remove `claude` binary setup
- Smaller image, faster builds

#### MCP server files → **REFACTOR to plain functions**
These files keep their handler logic but lose the `@tool()` decorator and
`create_sdk_mcp_server()` wrapper. The functions are registered directly on
agents via `@agent.tool` in `app/agents.py`:
- `app/life_manager.py` — export handler functions, remove MCP server creation
- `app/su_notes_manager.py` — same
- `app/telegram_messenger.py` — same
- `app/unsubscribe_manager.py` — same
- `app/restart_tool.py` — same

### Files NOT Needed (vs previous plan)
- ~~`app/agent_client.py`~~ — pydantic-ai `Agent` replaces this entirely
- ~~`app/mcp_client.py`~~ — pydantic-ai `MCPServerStdio`/`MCPServerSSE` replaces this
- ~~`app/tool_registry.py`~~ — pydantic-ai `@agent.tool` replaces this

---

## What Stays Exactly The Same

- All prompt files (`app/prompts/*.md`) — no changes
- Database layer — no changes
- WebSocket chat protocol — event format is identical
- Telegram integration — no changes
- Voice mode (ElevenLabs) — no changes
- Frontend — no changes
- Docker Compose structure — no changes (just lighter)
- Daemon scheduling logic — intervals, conditions, observability all stay
- Memory architecture — Subconscious/REM patterns stay, just lighter execution

## Migration Order

1. **`app/agents.py`** — Define model, MCP servers, and all agent instances
2. **`app/agent_registry.py`** — Simplify to `SessionState` with message history
3. **MCP server files** — Refactor to plain functions, register on agents
4. **`app/scary_internet_agent.py`** — Port to `make_scary_agent()` + `agent.run()`
5. **`app/subconscious_agent.py`** + **`app/rem_agent.py`** — Port to agent.run()
6. **`app/scheduler.py`** — Port all daemon jobs to agent.run()
7. **`app/main.py`** — Wire up SessionState, streaming events
8. **`pyproject.toml`** + **`Dockerfile`** — Update deps, remove Node.js
9. **Delete** `app/claude_client.py`, `app/process_limiter.py`
10. **Test** end-to-end

## Resource Impact

| Metric | Before (Claude SDK) | After (pydantic-ai) |
|--------|--------------------|--------------------|
| Per-agent memory | ~350 MB (subprocess) | ~0 MB (HTTP call) |
| Max concurrent agents | 3 (semaphore) | Unlimited (API rate limits only) |
| Container memory limit | 3 GB | Could drop to 512 MB-1 GB |
| Node.js required | Yes (SDK dep) | No |
| Docker image size | ~1.2 GB | ~400-600 MB |
| Agent startup time | 2-5s (subprocess spawn) | <100ms (object creation) |
| Custom infra LOC | 0 (SDK handles it) | 0 (pydantic-ai handles it) |
| Model lock-in | Claude only | Any provider (Anthropic, OpenAI, Ollama, llama.cpp) |

## Risks / Open Questions

1. **OAuth token enforcement**: Anthropic has stated that using OAuth subscription tokens
   outside Claude Code violates ToS (as of Jan 2026). The `auth_token` parameter works
   technically, but there is enforcement risk. Mitigation: pydantic-ai's model abstraction
   means we can switch to API key billing OR a local model with zero code changes.

2. **Token refresh**: OAuth tokens expire. The Claude Code binary handles refresh
   automatically. We may need to implement token refresh logic (call the token endpoint
   with the refresh token). Need to check if the current token is long-lived or requires
   periodic refresh.

3. **Model access**: OAuth subscription tokens may restrict which models are available
   or limit context window to 200k tokens (vs 1M with API keys). Need to verify.

4. **pydantic-ai MCP tool naming**: Need to verify that pydantic-ai uses the same
   `mcp__servername__toolname` convention as the Claude Agent SDK, since our system
   prompts reference these tool names (e.g., `mcp__playwright__browser_navigate`).
   If not, we update the prompts.

5. **pydantic-ai maturity**: At v1.65 with 15.2k stars and 214 releases, pydantic-ai
   is mature. But it's a dependency we don't control. Mitigation: the `anthropic` SDK
   is a transitive dep anyway, and pydantic-ai's API surface is small enough that we
   could replace it with hand-rolled code later if needed.

6. **Streaming event mapping**: Need to verify that `run_stream_events()` yields events
   in real-time (not buffered) and that the event types map cleanly to our WebSocket
   protocol. Build a small proof-of-concept first.
