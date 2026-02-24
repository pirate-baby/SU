#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/su-startup.pids"
LOG_DIR="/tmp"

# ---------------------------------------------------------------------------
# stop subcommand — tears down all host-side processes and Docker containers
# ---------------------------------------------------------------------------
if [ "${1:-}" = "stop" ]; then
    echo "Stopping Claude Chat Service..."

    # Stop host-side processes by port
    for PORT in 8931 8932 3001; do
        if lsof -ti :$PORT >/dev/null 2>&1; then
            echo "  Stopping process on port $PORT..."
            lsof -ti :$PORT | xargs kill 2>/dev/null || true
        fi
    done

    # Stop Docker containers
    echo "  Stopping Docker containers..."
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" -f "$SCRIPT_DIR/docker-compose.local.yml" down 2>/dev/null || true

    rm -f "$PID_FILE"
    echo "All services stopped."
    exit 0
fi

echo "Starting Claude Chat Service (daemonized)..."

# Check for .claude directory (needed for authentication)
if [ ! -d "$HOME/.claude" ]; then
    echo "Warning: $HOME/.claude directory not found"
    echo "Make sure you've authenticated with Claude Code CLI on the host:"
    echo "  claude login"
    echo ""
    echo "Continuing anyway (the container uses the host's ~/.claude credentials)..."
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
    if [ -f "$SCRIPT_DIR/.env" ]; then
        PLAYWRIGHT_MCP_EXTENSION_TOKEN=$(grep -E '^PLAYWRIGHT_MCP_EXTENSION_TOKEN=' "$SCRIPT_DIR/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    fi
    if [ -z "$PLAYWRIGHT_MCP_EXTENSION_TOKEN" ]; then
        echo "Error: PLAYWRIGHT_MCP_EXTENSION_TOKEN is not set."
        echo "Set it in .env or export it before running this script."
        exit 1
    fi
fi
export PLAYWRIGHT_MCP_EXTENSION_TOKEN
PLAYWRIGHT_LOG="$LOG_DIR/playwright-mcp.log"
nohup npx -y @playwright/mcp@latest \
    --extension \
    --host 0.0.0.0 \
    --allowed-hosts '*' \
    --port 8931 > "$PLAYWRIGHT_LOG" 2>&1 &
PLAYWRIGHT_PID=$!

# Wait briefly and verify the process is still running
sleep 2
if ! kill -0 "$PLAYWRIGHT_PID" 2>/dev/null; then
    echo "Error: Playwright MCP server failed to start. Check $PLAYWRIGHT_LOG"
    echo "Make sure Chrome is running and the Playwright MCP Bridge extension is installed."
    exit 1
fi
echo "Playwright MCP server started (PID $PLAYWRIGHT_PID), logs at $PLAYWRIGHT_LOG"

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
# Vibe Kanban (runs on HOST so it can access git, Claude Code, etc.)
# ---------------------------------------------------------------------------
VK_PORT=3001
if lsof -ti :$VK_PORT >/dev/null 2>&1; then
    echo "Stopping existing Vibe Kanban on port $VK_PORT..."
    lsof -ti :$VK_PORT | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# Load CLAUDE_CODE_OAUTH_TOKEN from .env if not already set
if [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$SCRIPT_DIR/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'")
fi
export CLAUDE_CODE_OAUTH_TOKEN

VK_LOG="$LOG_DIR/vibe-kanban.log"
echo "Starting Vibe Kanban on host (port $VK_PORT)..."
nohup env HOST=0.0.0.0 PORT=$VK_PORT npx -y vibe-kanban > "$VK_LOG" 2>&1 &
VK_PID=$!

sleep 3
if ! kill -0 "$VK_PID" 2>/dev/null; then
    echo "Error: Vibe Kanban failed to start. Check $VK_LOG"
    exit 1
fi
echo "Vibe Kanban started (PID $VK_PID), logs at $VK_LOG"

# ---------------------------------------------------------------------------
# Restart server (runs on HOST so the container can trigger its own rebuild)
# ---------------------------------------------------------------------------
# Kill any existing restart server on port 8932
if lsof -ti :8932 >/dev/null 2>&1; then
    echo "Stopping existing restart server on port 8932..."
    lsof -ti :8932 | xargs kill -9 2>/dev/null || true
    sleep 1
fi

RESTART_LOG="$LOG_DIR/restart-server.log"
echo "Starting restart server on host (port 8932)..."
nohup env SU_REPO_DIR="$SCRIPT_DIR" python3 "$SCRIPT_DIR/restart_server.py" > "$RESTART_LOG" 2>&1 &
RESTART_PID=$!

sleep 1
if ! kill -0 "$RESTART_PID" 2>/dev/null; then
    echo "Error: Restart server failed to start. Check $RESTART_LOG"
    exit 1
fi
echo "Restart server started (PID $RESTART_PID), logs at $RESTART_LOG"

# Save PIDs for the stop command
cat > "$PID_FILE" <<EOF
PLAYWRIGHT_PID=$PLAYWRIGHT_PID
VK_PID=$VK_PID
RESTART_PID=$RESTART_PID
EOF

# ---------------------------------------------------------------------------
# Self-signed SSL certificates (for Tailscale / non-localhost access)
# ---------------------------------------------------------------------------
SSL_DIR="$SCRIPT_DIR/nginx/ssl"
if [ ! -f "$SSL_DIR/cert.pem" ] || [ ! -f "$SSL_DIR/key.pem" ]; then
    echo "Generating self-signed SSL certificates..."
    mkdir -p "$SSL_DIR"
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$SSL_DIR/key.pem" \
        -out "$SSL_DIR/cert.pem" \
        -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
        2>/dev/null
    echo "SSL certificates generated at $SSL_DIR/"
else
    echo "SSL certificates already exist at $SSL_DIR/"
fi

# ---------------------------------------------------------------------------
# Docker services
# ---------------------------------------------------------------------------

echo "Starting services with local development configuration..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" -f "$SCRIPT_DIR/docker-compose.local.yml" up --build -d

echo ""
echo "Services started successfully! (all processes daemonized)"
echo ""
echo "Access the chat at: http://localhost"
echo "Vibe Kanban running on: https://localhost:53187 (host process via nginx)"
echo "Playwright MCP server running on: http://localhost:8931/sse"
echo "Restart server running on: http://localhost:8932/health"
echo ""
echo "To enable self-iteration: set SELF_ITERATION_MODE=true in .env"
echo "To view logs:"
echo "  Docker:         docker compose logs -f"
echo "  Playwright MCP: tail -f $PLAYWRIGHT_LOG"
echo "  Vibe Kanban:    tail -f $VK_LOG"
echo "  Restart server: tail -f $RESTART_LOG"
echo ""
echo "To stop all services: ./startup.sh stop"
echo ""
echo "You can safely disconnect the terminal now."
