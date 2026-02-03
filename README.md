# SU - Claude Chat Service

FastAPI service that provides a web chat interface using the Claude Agent SDK.

## Quick Start

```bash
# Start the service
docker-compose up -d

# Access the chat
open http://localhost
```

## Features

- WebSocket chat with Claude AI
- Session persistence (SQLite)
- Message history
- Real-time streaming responses

## Requirements

- Docker and Docker Compose
- Claude binary mounted from host at `/usr/local/bin/claude`
- Claude config directory at `~/.config/claude`
