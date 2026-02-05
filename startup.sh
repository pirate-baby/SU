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

# Ensure the Playwright MCP server is stopped when this script exits
cleanup() {
    echo ""
    echo "Shutting down Playwright MCP server..."
    # Kill whatever process is listening on port 8931 (npx may have forked,
    # so the original $PLAYWRIGHT_PID may no longer be the actual server).
    if lsof -ti :8931 >/dev/null 2>&1; then
        lsof -ti :8931 | xargs kill 2>/dev/null || true
    fi
    # Also try the original PID in case lsof didn't find it
    kill "$PLAYWRIGHT_PID" 2>/dev/null || true
    wait "$PLAYWRIGHT_PID" 2>/dev/null || true
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
echo "Playwright MCP server running on: http://localhost:8931/sse"
echo ""
echo "To view logs: docker compose logs -f"
echo "To stop: Ctrl-C  (stops Playwright MCP; then 'docker compose down' for containers)"
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
