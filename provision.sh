#!/bin/bash
# provision.sh — Run once after EC2 user-data completes to finish setup.
#
# Prerequisites (handled by EC2 user-data):
#   - Ubuntu 22.04, system updated, ubuntu-desktop-minimal installed
#   - Tailscale installed (but NOT authenticated)
#   - NICE DCV, Chrome, Syncthing installed
#
# This script installs everything else needed to run SU and hardens the
# instance against the OOM issues we hit on t3.medium (4 GB RAM).
#
# Usage:
#   ssh ubuntu@<host>
#   git clone <repo-url> ~/SU && cd ~/SU
#   sudo bash provision.sh
#
# After this script finishes you still need to:
#   1. Run `sudo tailscale up` and authenticate
#   2. Create ~/SU/.env with secrets (see .env.example)
#   3. Run `bash startup.sh` to start services

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: run this script as root (sudo bash provision.sh)"
    exit 1
fi

UBUNTU_HOME="/home/ubuntu"

echo "=== 1/6 Swap file ==="
SWAP_SIZE="4G"
if [ ! -f /swapfile ]; then
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
    echo "Created ${SWAP_SIZE} swap file"
else
    echo "Swap file already exists, skipping"
fi

# Low swappiness — prefer keeping app pages in RAM, only swap under pressure
if ! grep -q "vm.swappiness=10" /etc/sysctl.conf; then
    echo "vm.swappiness=10" >> /etc/sysctl.conf
    sysctl vm.swappiness=10
    echo "Set vm.swappiness=10"
else
    echo "vm.swappiness already configured"
fi

echo "=== 2/6 Docker ==="
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker ubuntu
    systemctl enable docker
    echo "Docker installed"
else
    echo "Docker already installed: $(docker --version)"
fi

echo "=== 3/7 Playwright MCP Bridge Chrome extension (via enterprise policy) ==="
# The Playwright MCP server in --extension mode spawns Chrome and navigates to
# chrome-extension://mmlmfjhmonkocbjadbfplnigmagldckm/connect.html to initiate
# the relay connection. This fails with ERR_BLOCKED_BY_CLIENT if the extension
# isn't installed. Force-installing via Chrome enterprise policy ensures it's
# present in every Chrome instance on this machine automatically.
POLICY_DIR="/etc/opt/chrome/policies/managed"
POLICY_FILE="$POLICY_DIR/playwright-mcp.json"
mkdir -p "$POLICY_DIR"
cat > "$POLICY_FILE" <<'POLICY'
{
  "ExtensionInstallForcelist": [
    "mmlmfjhmonkocbjadbfplnigmagldckm;https://clients2.google.com/service/update2/crx"
  ]
}
POLICY
echo "Playwright MCP Bridge extension will auto-install in Chrome (policy: $POLICY_FILE)"

echo "=== 4/7 Node.js 20 (for Playwright MCP, Vibe Kanban) ==="
NODE_VERSION=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || echo "0")
if [ "$NODE_VERSION" -lt 18 ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    echo "Node.js installed: $(node -v)"
else
    echo "Node.js already sufficient: $(node -v)"
fi

echo "=== 5/7  Proton Bridge ==="
# Proton Bridge now runs as a Docker sidecar container (proton-bridge service
# in docker-compose.yml). No host-side installation needed.
#
# First-time setup after `docker compose up -d`:
#   docker compose stop proton-bridge
#   docker compose run --rm proton-bridge setup
#   docker compose up -d proton-bridge
#
# Verify connectivity:
#   docker compose exec proton-bridge /check.sh
#   docker compose exec proton-bridge /check.sh --emails  (with creds)
echo "Proton Bridge runs as a Docker sidecar — no host install needed."
echo "  After startup: docker compose run --rm proton-bridge setup"
echo ""

echo "=== 6/7  Git config ==="
su - ubuntu -c 'git config --global user.email "su@localhost"'
su - ubuntu -c 'git config --global user.name "SU"'
echo "Git configured for ubuntu user"

echo "=== 7/7  Permissions ==="
# Ensure ubuntu owns the repo
if [ -d "$UBUNTU_HOME/SU" ]; then
    chown -R ubuntu:ubuntu "$UBUNTU_HOME/SU"
fi

# Add ubuntu to docker group (may already be there)
usermod -aG docker ubuntu 2>/dev/null || true

echo ""
echo "========================================="
echo "  Provisioning complete."
echo ""
echo "  Next steps:"
echo "    1. sudo tailscale up"
echo "    2. cp .env.example .env && vim .env"
echo "    3. bash startup.sh"
echo "    4. Log in to Proton Bridge (if using ProtonMail):"
echo "         docker compose stop proton-bridge"
echo "         docker compose run --rm proton-bridge setup"
echo "         docker compose up -d proton-bridge"
echo "========================================="
