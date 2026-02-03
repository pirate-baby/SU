# SU - Claude Chat Service

FastAPI service that provides a web chat interface using the Claude Agent SDK.

## Quick Start

```bash
# 1. Authenticate with Claude Code (uses your Claude Max subscription)
claude login

# 2. Start the service
docker-compose up -d

# 3. Access the chat
open http://localhost
```

Optionally, set `CLAUDE_OAUTH_TOKEN` environment variable to use an OAuth session token instead of `claude login`.

## Features

- WebSocket chat with Claude AI
- Session persistence (SQLite)
- Message history
- Real-time streaming responses

## Requirements

- Docker and Docker Compose
- Claude Code CLI installed and authenticated (`claude login`)
- Claude Pro or Max subscription
- Claude binary at `/usr/local/bin/claude`
