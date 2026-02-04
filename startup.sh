#!/bin/bash

set -e

echo "Starting Claude Chat Service..."

# Detect OS
OS=$(uname -s)
CHROME_USER_DATA_DIR=""

case "$OS" in
    Darwin)
        echo "Detected macOS"
        CHROME_USER_DATA_DIR="$HOME/Library/Application Support/Google/Chrome"
        ;;
    Linux)
        echo "Detected Linux"
        CHROME_USER_DATA_DIR="$HOME/.config/google-chrome"
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            if [[ "$ID" == "ubuntu" ]] || [[ "$ID_LIKE" == *"ubuntu"* ]] || [[ "$ID_LIKE" == *"debian"* ]]; then
                echo "Running on Ubuntu/Debian"
            fi
        fi
        ;;
    *)
        echo "Unsupported OS: $OS"
        exit 1
        ;;
esac

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

echo "Starting Playwright MCP server on host (port 8931)..."
# --host 0.0.0.0  : accept connections from Docker containers
# --allowed-hosts *: disable the Host-header check so that requests arriving
#   with "Host: host.docker.internal:8931" (from inside Docker) are not rejected.
#   Without this, the server only accepts requests whose Host header matches
#   "localhost" exactly, which fails for Docker's host.docker.internal alias.
# --config         : passes launch args to Chrome, specifically disabling
#   DevToolsDebuggingRestrictions (Chrome 136+) which otherwise causes
#   launchPersistentContext to open about:blank instead of the actual page.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
npx -y @playwright/mcp@latest \
    --browser chrome \
    --user-data-dir "$CHROME_USER_DATA_DIR" \
    --host 0.0.0.0 \
    --allowed-hosts '*' \
    --port 8931 \
    --config "$SCRIPT_DIR/playwright-mcp-config.json" &
PLAYWRIGHT_PID=$!

# Wait briefly and verify the process is still running
sleep 2
if ! kill -0 "$PLAYWRIGHT_PID" 2>/dev/null; then
    echo "Error: Playwright MCP server failed to start."
    echo "Check that Chrome is installed and the user data directory exists:"
    echo "  $CHROME_USER_DATA_DIR"
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
