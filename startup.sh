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

    # Stop Vibe Kanban systemd scope (and all child Claude Code processes)
    if systemctl --user is-active vibe-kanban.scope &>/dev/null 2>&1; then
        echo "  Stopping vibe-kanban.scope (and all Claude Code children)..."
        systemctl --user stop vibe-kanban.scope 2>/dev/null || true
    fi

    # Stop host-side processes by port (fallback for anything not in the scope)
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
# Playwright MCP server
# ---------------------------------------------------------------------------
# If PLAYWRIGHT_MCP_URL points to a remote machine (e.g. a laptop on the same
# Tailscale network), skip starting a local server entirely. This is the
# recommended setup: run Playwright MCP on a machine with a real desktop and
# browser, set PLAYWRIGHT_MCP_URL in .env on the EC2, done.
#
# To run Playwright on a remote machine (e.g. your laptop):
#   npx -y @playwright/mcp@latest --browser chrome --host 0.0.0.0 \
#       --allowed-hosts '*' --port 8931
# Then set in .env on the EC2:
#   PLAYWRIGHT_MCP_URL=http://<tailscale-ip>:8931/sse

# Load PLAYWRIGHT_MCP_URL from .env if not already set
if [ -z "$PLAYWRIGHT_MCP_URL" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    PLAYWRIGHT_MCP_URL=$(grep -E '^PLAYWRIGHT_MCP_URL=' "$SCRIPT_DIR/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'")
fi

# Check if PLAYWRIGHT_MCP_URL points to a remote host (not localhost / host.docker.internal)
PLAYWRIGHT_IS_REMOTE=false
if [ -n "$PLAYWRIGHT_MCP_URL" ]; then
    case "$PLAYWRIGHT_MCP_URL" in
        *host.docker.internal*|*localhost*|*127.0.0.1*) ;;
        *) PLAYWRIGHT_IS_REMOTE=true ;;
    esac
fi

if [ "$PLAYWRIGHT_IS_REMOTE" = true ]; then
    echo "Playwright MCP server is remote: $PLAYWRIGHT_MCP_URL"
    echo "  Skipping local Playwright MCP startup."
    # Quick connectivity check
    PLAYWRIGHT_REMOTE_HOST=$(echo "$PLAYWRIGHT_MCP_URL" | sed -E 's|https?://([^:/]+).*|\1|')
    if ping -c 1 -W 2 "$PLAYWRIGHT_REMOTE_HOST" >/dev/null 2>&1; then
        echo "  Remote host $PLAYWRIGHT_REMOTE_HOST is reachable."
    else
        echo "  Warning: Remote host $PLAYWRIGHT_REMOTE_HOST is not reachable."
        echo "  Make sure the Playwright MCP server is running on the remote machine."
    fi
    PLAYWRIGHT_PID=""
else
    # --- Local Playwright MCP server ---

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
        exit 1
    fi

    # On DCV/headless EC2 the shell may not have DISPLAY set even though Xorg
    # is running on :0 — export it so Chrome can open a window.
    export DISPLAY="${DISPLAY:-:0}"

    PLAYWRIGHT_LOG="$LOG_DIR/playwright-mcp.log"
    PLAYWRIGHT_USER_DATA_DIR="$HOME/.playwright-mcp-profile"
    mkdir -p "$PLAYWRIGHT_USER_DATA_DIR"

    echo "Starting Playwright MCP server on host (port 8931) in browser mode (DISPLAY=$DISPLAY)..."
    nohup npx -y @playwright/mcp@latest \
        --browser chrome \
        --user-data-dir "$PLAYWRIGHT_USER_DATA_DIR" \
        --host 0.0.0.0 \
        --allowed-hosts '*' \
        --port 8931 > "$PLAYWRIGHT_LOG" 2>&1 &
    PLAYWRIGHT_PID=$!

    sleep 2
    if ! kill -0 "$PLAYWRIGHT_PID" 2>/dev/null; then
        echo "Error: Playwright MCP server failed to start. Check $PLAYWRIGHT_LOG"
        exit 1
    fi
    echo "Playwright MCP server started (PID $PLAYWRIGHT_PID), logs at $PLAYWRIGHT_LOG"
fi

# ---------------------------------------------------------------------------
# ~/Repos directory — isolated from the SU container
# ---------------------------------------------------------------------------
# SU's Docker container runs as UID 501 (appuser). ~/Repos is owned by the
# host user (ubuntu) so that Vibe Kanban and git can operate on repos inside
# it without sudo. Docker isolation is enforced by the absence of any volume
# mount to this directory — the container simply has no path to reach it.
REPOS_DIR="$HOME/Repos"
if [ ! -d "$REPOS_DIR" ]; then
    echo "Creating $REPOS_DIR (owned by host user)..."
    mkdir -p "$REPOS_DIR"
    chmod 700 "$REPOS_DIR"
else
    # Ensure permissions are correct on every start
    chown "$(id -u):$(id -g)" "$REPOS_DIR"
    chmod 700 "$REPOS_DIR"
fi
echo "~/Repos directory secured (owner: $(id -un), mode: 700)"

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

# Build VK_ALLOWED_ORIGINS from all machine IPs so Vibe Kanban accepts
# requests proxied through nginx (Tailscale, LAN, localhost, etc.)
if [ -z "$VK_ALLOWED_ORIGINS" ]; then
    VK_ORIGINS="https://localhost:53187,http://localhost:53187"
    # Collect non-loopback IPv4 addresses
    for ip in $(hostname -I 2>/dev/null || ifconfig 2>/dev/null | grep 'inet ' | awk '{print $2}' | grep -v '^127\.'); do
        ip=$(echo "$ip" | tr -d '[:space:]')
        [ -n "$ip" ] && VK_ORIGINS="$VK_ORIGINS,https://$ip:53187,http://$ip:53187"
    done
    # Include Tailscale MagicDNS FQDN so VK accepts requests via the .ts.net hostname
    TS_FQDN_VK=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
    if [ -n "$TS_FQDN_VK" ]; then
        VK_ORIGINS="$VK_ORIGINS,https://$TS_FQDN_VK:53187,https://$TS_FQDN_VK"
    fi
    VK_ALLOWED_ORIGINS="$VK_ORIGINS"
fi
export VK_ALLOWED_ORIGINS
echo "Vibe Kanban allowed origins: $VK_ALLOWED_ORIGINS"

VK_LOG="$LOG_DIR/vibe-kanban.log"
echo "Starting Vibe Kanban on host (port $VK_PORT)..."

# Run VK inside a systemd transient scope backed by vibe-kanban.slice so that
# the kernel's OOM killer targets this cgroup (VK + all Claude Code children)
# before touching Docker, Tailscale, or the host OS.
#
# vibe-kanban.slice caps the entire tree at 2.5 GB hard / 2 GB soft.
# systemd --user is available on Ubuntu 22.04+ desktop sessions.
#
# If systemd --user is not available (e.g. headless CI), fall back to nohup.
if systemctl --user status &>/dev/null 2>&1; then
    # Reload slice definition in case it changed
    systemctl --user daemon-reload

    # Remove any stale scope from a previous run
    systemctl --user stop vibe-kanban.scope 2>/dev/null || true

    # systemd-run launches in a stripped environment — PATH won't include nvm.
    # Resolve npx to its absolute path now (in the current nvm-aware shell) so
    # the service finds the right Node binary.
    NPX_BIN="$(command -v npx)"

    systemd-run --user \
        --unit=vibe-kanban \
        --slice=vibe-kanban.slice \
        --same-dir \
        --collect \
        -E PATH="$PATH" \
        -E HOME="$HOME" \
        -E HOST=0.0.0.0 \
        -E PORT=$VK_PORT \
        -E VK_ALLOWED_ORIGINS="$VK_ALLOWED_ORIGINS" \
        -E CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
        --property=StandardOutput=append:$VK_LOG \
        --property=StandardError=append:$VK_LOG \
        "$NPX_BIN" -y vibe-kanban
    VK_PID=$(systemctl --user show vibe-kanban.scope --property=MainPID --value 2>/dev/null || echo "0")
else
    echo "Warning: systemd --user not available, falling back to nohup (no memory cap)"
    nohup env HOST=0.0.0.0 PORT=$VK_PORT VK_ALLOWED_ORIGINS="$VK_ALLOWED_ORIGINS" \
        CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
        npx -y vibe-kanban > "$VK_LOG" 2>&1 &
    VK_PID=$!
fi

for i in $(seq 1 15); do
    sleep 1
    if lsof -ti :$VK_PORT >/dev/null 2>&1; then
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "Error: Vibe Kanban failed to start on port $VK_PORT after 15s. Check $VK_LOG"
        exit 1
    fi
done
echo "Vibe Kanban started (PID $VK_PID, slice: vibe-kanban.slice), logs at $VK_LOG"

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
# Proton Bridge (systemd service — managed separately, just check status here)
# ---------------------------------------------------------------------------
# Proton Bridge runs as a systemd service (User=proton) installed by provision.sh.
# It exposes IMAP on localhost:1143 so the Docker container can read ProtonMail.
# The service must be set up manually once with: sudo -u proton protonmail-bridge -c
# If the service is not running, IMAP email features will be unavailable.
if systemctl is-active proton-bridge &>/dev/null; then
    echo "Proton Bridge service is running (IMAP on localhost:1143)"
    # Check port-forwarding services (needed for Docker container access)
    FORWARD_OK=true
    for SVC in proton-bridge-imap-forward proton-bridge-smtp-forward; do
        if ! systemctl is-active "$SVC" &>/dev/null; then
            echo "  Warning: $SVC is not running — Docker container cannot reach Proton Bridge."
            echo "    To fix: sudo systemctl enable --now $SVC"
            FORWARD_OK=false
        fi
    done
    if [ "$FORWARD_OK" = true ]; then
        echo "  Port forwarding active (Docker can reach IMAP:1143, SMTP:1025 via host.docker.internal)"
    fi
elif pgrep -f 'protonmail-bridge' &>/dev/null; then
    echo "Proton Bridge is running outside systemd (IMAP on localhost:1143)"
    echo "  Consider using: sudo systemctl start proton-bridge"
    echo "  Warning: Docker container may not reach Bridge without port forwarding."
    echo "  See provision.sh for socat forwarding setup."
elif systemctl list-unit-files proton-bridge.service 2>/dev/null | grep -q proton-bridge; then
    echo "Warning: Proton Bridge service is installed but not running."
    echo "  To start: sudo systemctl start proton-bridge"
    echo "  (Email reading/searching will be unavailable until Bridge is running)"
else
    echo "Proton Bridge not installed — email reading/searching unavailable."
    echo "  To set up: run provision.sh, then log in with: sudo -u proton protonmail-bridge -c"
fi

# ---------------------------------------------------------------------------
# SSL certificates — prefer Tailscale HTTPS (trusted), fall back to self-signed
# ---------------------------------------------------------------------------
SSL_DIR="$SCRIPT_DIR/nginx/ssl"
mkdir -p "$SSL_DIR"

TS_CERT_OBTAINED=false

# Try to get a real Let's Encrypt certificate via Tailscale.
# `tailscale cert` issues a trusted cert for the machine's MagicDNS FQDN
# (e.g. myhost.tailnet-name.ts.net). This makes service workers, push
# notifications, and other APIs that require trusted HTTPS work without
# any manual browser/OS trust steps.
if command -v tailscale &>/dev/null; then
    TS_FQDN=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
    if [ -n "$TS_FQDN" ]; then
        echo "Requesting Tailscale HTTPS certificate for ${TS_FQDN}..."
        # tailscale cert may need sudo if the current user is not a Tailscale operator
        TS_CERT_CMD="tailscale cert"
        if [ "$(id -u)" -ne 0 ]; then
            TS_CERT_CMD="sudo tailscale cert"
        fi
        if $TS_CERT_CMD \
            --cert-file "$SSL_DIR/cert.pem" \
            --key-file "$SSL_DIR/key.pem" \
            "$TS_FQDN" 2>/dev/null; then
            TS_CERT_OBTAINED=true
            echo "Tailscale HTTPS certificate obtained for ${TS_FQDN}"
        else
            echo "Warning: tailscale cert failed — is HTTPS enabled in your Tailscale admin console?"
            echo "  Enable it at: https://login.tailscale.com/admin/dns → HTTPS Certificates"
        fi
    fi
fi

# Fall back to self-signed certificate if Tailscale cert wasn't obtained
if [ "$TS_CERT_OBTAINED" = false ]; then
    if [ ! -f "$SSL_DIR/cert.pem" ] || [ ! -f "$SSL_DIR/key.pem" ]; then
        echo "Generating self-signed SSL certificates..."

        # Build SAN list — always include localhost and loopback, plus any
        # Tailscale IP so service workers work when accessed remotely.
        SAN="DNS:localhost,IP:127.0.0.1"
        TS_IP=$(tailscale ip -4 2>/dev/null || true)
        if [ -n "$TS_IP" ]; then
            SAN="${SAN},IP:${TS_IP}"
            echo "Including Tailscale IP ${TS_IP} in certificate SAN"
        fi

        openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
            -keyout "$SSL_DIR/key.pem" \
            -out "$SSL_DIR/cert.pem" \
            -subj "/CN=localhost" \
            -addext "subjectAltName=${SAN}" \
            2>/dev/null
        echo "Self-signed SSL certificates generated at $SSL_DIR/"
        echo "Note: Service workers require a trusted certificate. Access via Tailscale"
        echo "  HTTPS (enable in admin console) or manually trust the cert in your browser."
    else
        echo "SSL certificates already exist at $SSL_DIR/"
    fi
fi


# ---------------------------------------------------------------------------
# Docker services
# ---------------------------------------------------------------------------

echo "Starting services with local development configuration..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" -f "$SCRIPT_DIR/docker-compose.local.yml" up --build -d

echo ""
echo "Services started successfully! (all processes daemonized)"
echo ""
if [ "$TS_CERT_OBTAINED" = true ] && [ -n "$TS_FQDN" ]; then
    echo "Access the chat at: https://${TS_FQDN} (trusted Tailscale HTTPS)"
    echo "Vibe Kanban running on: https://${TS_FQDN}:53187 (host process via nginx)"
else
    echo "Access the chat at: https://localhost"
    echo "Vibe Kanban running on: https://localhost:53187 (host process via nginx)"
fi
echo "Playwright MCP server running on: http://localhost:8931/sse"
echo "Restart server running on: http://localhost:8932/health"
echo "Proton Bridge (IMAP): localhost:1143 (systemd service — see above)"
echo ""
echo "To enable self-iteration: set SELF_ITERATION_MODE=true in .env"
echo "To view logs:"
echo "  Docker:         docker compose logs -f"
echo "  Playwright MCP: tail -f $PLAYWRIGHT_LOG"
echo "  Vibe Kanban:    tail -f $VK_LOG"
echo "  Restart server: tail -f $RESTART_LOG"
echo "  Proton Bridge:  sudo journalctl -u proton-bridge -f"
echo ""
echo "To stop all services: ./startup.sh stop"
echo ""
echo "You can safely disconnect the terminal now."
