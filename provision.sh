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

echo "=== 5/7  Proton Bridge (for ProtonMail IMAP/SMTP) ==="
# Install Proton Bridge as a headless systemd service.
# Version is resolved dynamically from the GitHub releases API so the script
# always installs the latest release without needing manual updates.
# After provisioning, the user must log in once interactively:
#   sudo -u proton /usr/bin/protonmail-bridge -c
#   > login       (enter ProtonMail credentials)
#   > list        (verify the account appears)
#   > exit
# Then start/enable the service: systemctl enable --now proton-bridge
BRIDGE_DEB="/tmp/protonmail-bridge.deb"
if ! command -v protonmail-bridge &>/dev/null; then
    echo "Fetching latest Proton Bridge release URL..."
    BRIDGE_DEB_URL=$(curl -fsSL "https://api.github.com/repos/ProtonMail/proton-bridge/releases/latest" \
        | python3 -c "import json,sys; r=json.load(sys.stdin); print(next(a['browser_download_url'] for a in r['assets'] if 'amd64' in a['name'] and a['name'].endswith('.deb')))")
    echo "Downloading Proton Bridge from: $BRIDGE_DEB_URL"
    curl -fsSL "$BRIDGE_DEB_URL" -o "$BRIDGE_DEB"
    # Install with apt to auto-resolve any dependencies
    apt-get install -y "$BRIDGE_DEB"
    rm -f "$BRIDGE_DEB"
    echo "Proton Bridge installed"
else
    echo "Proton Bridge already installed: $(protonmail-bridge --version 2>/dev/null || echo 'version unknown')"
fi

# Create a dedicated system user for the bridge daemon (no login shell)
if ! id proton &>/dev/null; then
    useradd -r -s /bin/false -m -d /home/proton proton
    echo "Created 'proton' system user"
else
    echo "'proton' user already exists"
fi

# Generate a passphrase-free GPG key for the bridge (needed for keychain/secret storage)
# Bridge uses the system keychain; on headless Ubuntu this must be pre-seeded.
if ! sudo -u proton gpg --list-keys 'ProtonMailBridge' &>/dev/null; then
    sudo -u proton gpg --batch --passphrase '' --quick-gen-key 'ProtonMailBridge' default default never
    echo "GPG key generated for proton user"
else
    echo "GPG key already exists for proton user"
fi

# Install systemd service unit for headless bridge operation
cat > /etc/systemd/system/proton-bridge.service <<'UNIT'
[Unit]
Description=Proton Mail Bridge (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=proton
ExecStart=/usr/bin/protonmail-bridge --noninteractive
Restart=on-failure
RestartSec=5
# Bridge listens on 127.0.0.1 by default (IMAP:1143, SMTP:1025).
# The Docker container reaches the host via host.docker.internal which
# resolves to the Docker bridge gateway — NOT 127.0.0.1. We use socat
# sidecars (below) to forward from 0.0.0.0 to Bridge's localhost ports.
Environment=HOME=/home/proton

[Install]
WantedBy=multi-user.target
UNIT

# Socat port-forwarding services — bridge Docker's host.docker.internal
# to Proton Bridge's localhost-only listeners.
# Docker containers reach the host via the bridge gateway IP (e.g. 172.17.0.1).
# Proton Bridge only binds to 127.0.0.1, so we forward 0.0.0.0:port → 127.0.0.1:port.
for PROTO_PORT in "imap:1143" "smtp:1025"; do
    PROTO="${PROTO_PORT%%:*}"
    PORT="${PROTO_PORT##*:}"
    cat > "/etc/systemd/system/proton-bridge-${PROTO}-forward.service" <<FWDUNIT
[Unit]
Description=Proton Bridge ${PROTO^^} port forward (0.0.0.0:${PORT} → 127.0.0.1:${PORT})
After=proton-bridge.service
Requires=proton-bridge.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:${PORT},bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:${PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
FWDUNIT
done

# Install socat if not present
if ! command -v socat &>/dev/null; then
    apt-get install -y socat
    echo "socat installed (needed for Proton Bridge port forwarding)"
fi

systemctl daemon-reload
echo "Proton Bridge systemd service installed (with IMAP/SMTP port forwarding for Docker)"
echo ""
echo "  IMPORTANT: Before starting the service, log in interactively as the proton user:"
echo "    sudo -u proton /usr/bin/protonmail-bridge -c"
echo "    > login      (follow prompts)"
echo "    > list       (verify account appears)"
echo "    > exit"
echo "  Then:"
echo "    sudo systemctl enable --now proton-bridge"
echo "    sudo systemctl enable --now proton-bridge-imap-forward"
echo "    sudo systemctl enable --now proton-bridge-smtp-forward"
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
echo "    2. Log in to Proton Bridge (if using ProtonMail):"
echo "         sudo -u proton /usr/bin/protonmail-bridge -c"
echo "         > login   > list   > exit"
echo "         sudo systemctl enable --now proton-bridge"
echo "         sudo systemctl enable --now proton-bridge-imap-forward"
echo "         sudo systemctl enable --now proton-bridge-smtp-forward"
echo "    3. cp .env.example .env && vim .env"
echo "    4. bash startup.sh"
echo "========================================="
