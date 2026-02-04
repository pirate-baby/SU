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

# Check that npx is available on the host
if ! command -v npx &>/dev/null; then
    echo "Error: npx not found. Install Node.js on the host to run Playwright MCP."
    exit 1
fi

echo "Starting Playwright MCP server on host (port 8931) in extension mode..."
# --extension      : connect to the existing browser via the Playwright MCP
#   Bridge extension instead of launching a new instance. This avoids profile
#   lock conflicts and about:blank issues with launchPersistentContext.
# --host 0.0.0.0   : accept connections from Docker containers
# --allowed-hosts *: disable the Host-header check so that requests arriving
#   with "Host: host.docker.internal:8931" (from inside Docker) are not rejected.
export PLAYWRIGHT_MCP_EXTENSION_TOKEN="oMa0YkhIfcFTnJLjvSo0Md8fHeck1sOo0ifO9ycE08o"
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
    echo "Shutting down Playwright MCP server (PID $PLAYWRIGHT_PID)..."
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
wait "$PLAYWRIGHT_PID"
