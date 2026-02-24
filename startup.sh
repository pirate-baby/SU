#!/bin/bash

set -e

echo "Starting Claude Chat Service..."

# Check for .claude directory (needed for authentication)
if [ ! -d "$HOME/.claude" ]; then
    echo "Warning: $HOME/.claude directory not found"
    echo "Make sure you've authenticated with Claude Code CLI:"
    echo "  claude login"
    echo ""
    echo "Continuing anyway (you can set CLAUDE_CODE_OAUTH_TOKEN environment variable instead)..."
fi

# ---------------------------------------------------------------------------
# Playwright MCP server (runs on the HOST so it can access Chrome + profile)
# ---------------------------------------------------------------------------
# Kill any existing Playwright MCP server on port 8931
if lsof -ti :8931 >/dev/null 2>&1; then
    echo "Stopping existing Playwright MCP server on port 8931..."
    lsof -ti :8931 | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# Ensure Node.js / npm / npx are available on the host
if ! command -v npx &>/dev/null; then
    echo "npx not found – installing Node.js..."
    if [ "$(id -u)" -eq 0 ]; then
        apt-get update && apt-get install -y nodejs npm
    else
        sudo apt-get update && sudo apt-get install -y nodejs npm
    fi
    # Verify installation succeeded
    if ! command -v npx &>/dev/null; then
        echo "Error: Failed to install Node.js/npm. Please install manually and retry."
        exit 1
    fi
fi

# Check Node.js version (Playwright MCP requires Node.js 18+)
NODE_VERSION=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1)
if [ -z "$NODE_VERSION" ] || [ "$NODE_VERSION" -lt 18 ]; then
    echo "Error: Node.js 18 or higher is required for Playwright MCP."
    echo "Current version: $(node -v 2>/dev/null || echo 'not found')"
    echo ""
    echo "To upgrade Node.js on Ubuntu:"
    echo "  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
    echo "  sudo apt-get install -y nodejs"
    echo ""
    echo "Or use nvm:"
    echo "  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash"
    echo "  source ~/.bashrc"
    echo "  nvm install 20"
    exit 1
fi

echo "Starting Playwright MCP server on host (port 8931) in extension mode..."
# --extension      : connect to the existing browser via the Playwright MCP
#   Bridge extension instead of launching a new instance. This avoids profile
#   lock conflicts and about:blank issues with launchPersistentContext.
# --host 0.0.0.0   : accept connections from Docker containers
# --allowed-hosts *: disable the Host-header check so that requests arriving
#   with "Host: host.docker.internal:8931" (from inside Docker) are not rejected.
# Load PLAYWRIGHT_MCP_EXTENSION_TOKEN from .env if not already set
if [ -z "$PLAYWRIGHT_MCP_EXTENSION_TOKEN" ]; then
    if [ -f .env ]; then
        PLAYWRIGHT_MCP_EXTENSION_TOKEN=$(grep -E '^PLAYWRIGHT_MCP_EXTENSION_TOKEN=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    fi
    if [ -z "$PLAYWRIGHT_MCP_EXTENSION_TOKEN" ]; then
        echo "Error: PLAYWRIGHT_MCP_EXTENSION_TOKEN is not set."
        echo "Set it in .env or export it before running this script."
        exit 1
    fi
fi
export PLAYWRIGHT_MCP_EXTENSION_TOKEN
npx -y @playwright/mcp@latest \
    --extension \
    --host 0.0.0.0 \
    --allowed-hosts '*' \
    --port 8931 &
PLAYWRIGHT_PID=$!

# Wait briefly and verify the process is still running
sleep 2
if ! kill -0 "$PLAYWRIGHT_PID" 2>/dev/null; then
    echo "Error: Playwright MCP server failed to start."
    echo "Check that Chrome is running and the Playwright MCP Bridge extension is installed."
    exit 1
fi
echo "Playwright MCP server started (PID $PLAYWRIGHT_PID)"

# ---------------------------------------------------------------------------
# ~/Repos directory — isolated from the SU container
# ---------------------------------------------------------------------------
# SU's Docker container runs as UID 501 (appuser). We create ~/Repos owned
# by root with mode 0700 so the container cannot read/write other repos even
# if a volume mount is accidentally added.
REPOS_DIR="$HOME/Repos"
if [ ! -d "$REPOS_DIR" ]; then
    echo "Creating $REPOS_DIR (restricted to host user only)..."
    sudo mkdir -p "$REPOS_DIR"
    sudo chown root:root "$REPOS_DIR"
    sudo chmod 700 "$REPOS_DIR"
else
    # Ensure permissions are correct on every start
    sudo chown root:root "$REPOS_DIR"
    sudo chmod 700 "$REPOS_DIR"
fi
echo "~/Repos directory secured (owner: root, mode: 700)"

# ---------------------------------------------------------------------------
# Vibe Kanban (runs on HOST, port 53187)
# ---------------------------------------------------------------------------
VIBE_KANBAN_PORT=53187
if lsof -ti :${VIBE_KANBAN_PORT} >/dev/null 2>&1; then
    echo "Stopping existing Vibe Kanban on port ${VIBE_KANBAN_PORT}..."
    lsof -ti :${VIBE_KANBAN_PORT} | xargs kill -9 2>/dev/null || true
    sleep 1
fi

echo "Starting Vibe Kanban on host (port ${VIBE_KANBAN_PORT})..."
# Bind to 0.0.0.0 so it's reachable from Docker containers and external clients
HOST=0.0.0.0 PORT=${VIBE_KANBAN_PORT} npx -y vibe-kanban &
VIBE_KANBAN_PID=$!

sleep 2
if ! kill -0 "$VIBE_KANBAN_PID" 2>/dev/null; then
    echo "Warning: Vibe Kanban failed to start. Continuing without it."
else
    echo "Vibe Kanban started (PID $VIBE_KANBAN_PID)"
fi

# ---------------------------------------------------------------------------
# Restart server (runs on HOST so the container can trigger its own rebuild)
# ---------------------------------------------------------------------------
# Kill any existing restart server on port 8932
if lsof -ti :8932 >/dev/null 2>&1; then
    echo "Stopping existing restart server on port 8932..."
    lsof -ti :8932 | xargs kill -9 2>/dev/null || true
    sleep 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Starting restart server on host (port 8932)..."
SU_REPO_DIR="$SCRIPT_DIR" python3 "$SCRIPT_DIR/restart_server.py" &
RESTART_PID=$!

sleep 1
if ! kill -0 "$RESTART_PID" 2>/dev/null; then
    echo "Error: Restart server failed to start."
    exit 1
fi
echo "Restart server started (PID $RESTART_PID)"

# Ensure background servers are stopped when this script exits
cleanup() {
    echo ""
    echo "Shutting down background servers..."
    # Stop Playwright MCP
    if lsof -ti :8931 >/dev/null 2>&1; then
        lsof -ti :8931 | xargs kill 2>/dev/null || true
    fi
    kill "$PLAYWRIGHT_PID" 2>/dev/null || true
    wait "$PLAYWRIGHT_PID" 2>/dev/null || true
    # Stop Vibe Kanban
    if lsof -ti :${VIBE_KANBAN_PORT} >/dev/null 2>&1; then
        lsof -ti :${VIBE_KANBAN_PORT} | xargs kill 2>/dev/null || true
    fi
    kill "$VIBE_KANBAN_PID" 2>/dev/null || true
    wait "$VIBE_KANBAN_PID" 2>/dev/null || true
    # Stop restart server
    if lsof -ti :8932 >/dev/null 2>&1; then
        lsof -ti :8932 | xargs kill 2>/dev/null || true
    fi
    kill "$RESTART_PID" 2>/dev/null || true
    wait "$RESTART_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Docker services
# ---------------------------------------------------------------------------

# Use local development configuration (HTTP only, no SSL)
echo "Starting services with local development configuration (HTTP only)..."
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d

echo ""
echo "Services started successfully!"
echo ""
echo "Access the chat at: http://localhost"
echo "Vibe Kanban running on: http://localhost:${VIBE_KANBAN_PORT} (proxied at /kanban/)"
echo "Playwright MCP server running on: http://localhost:8931/sse"
echo "Restart server running on: http://localhost:8932/health"
echo ""
echo "To enable self-iteration: set SELF_ITERATION_MODE=true in .env"
echo "To view logs: docker compose logs -f"
echo "To stop: Ctrl-C  (stops host servers; then 'docker compose down' for containers)"
echo ""

# Keep the script alive so the Playwright MCP background process isn't killed.
# The EXIT trap will clean it up when this script is interrupted (Ctrl-C / SIGTERM).
# Note: We can't use `wait "$PLAYWRIGHT_PID"` because npx may fork and exit quickly
# on some systems (e.g., Ubuntu), causing wait to return immediately.
# Instead, we poll to check if something is still listening on port 8931.
while lsof -ti :8931 >/dev/null 2>&1; do
    sleep 5
done
echo "Playwright MCP server is no longer running on port 8931."
