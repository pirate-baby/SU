# SU - Claude Chat Service

FastAPI service that provides a web chat interface using the Claude Agent SDK.

## Quick Start

```bash
./startup.sh
```

Then access the chat at http://localhost

## Requirements

- Docker and Docker Compose
- Claude Code CLI installed and authenticated (`claude login`)
- Claude Pro or Max subscription

## Optional Configuration

Set `CLAUDE_OAUTH_TOKEN` environment variable to use an OAuth session token instead of `claude login`.

## Managing the Service

```bash
# View logs
docker compose logs -f

# Stop the service
docker compose down

# Restart
./startup.sh
```
