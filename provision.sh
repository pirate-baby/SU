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

echo "=== 1/5  Swap file ==="
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

echo "=== 2/5  Docker ==="
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker ubuntu
    systemctl enable docker
    echo "Docker installed"
else
    echo "Docker already installed: $(docker --version)"
fi

echo "=== 3/5  Node.js 20 (for Playwright MCP, Vibe Kanban) ==="
NODE_VERSION=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || echo "0")
if [ "$NODE_VERSION" -lt 18 ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    echo "Node.js installed: $(node -v)"
else
    echo "Node.js already sufficient: $(node -v)"
fi

echo "=== 4/5  Git config ==="
su - ubuntu -c 'git config --global user.email "su@localhost"'
su - ubuntu -c 'git config --global user.name "SU"'
echo "Git configured for ubuntu user"

echo "=== 5/5  Permissions ==="
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
echo "========================================="
