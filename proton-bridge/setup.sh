#!/bin/bash
# One-time interactive login for Proton Bridge.
#
# Usage:
#   docker compose exec proton-bridge /setup.sh
#
# This drops you into the Bridge CLI where you can:
#   > login            (enter ProtonMail credentials + 2FA if enabled)
#   > list             (verify the account appears)
#   > change mode combined   (if you want all addresses in one IMAP login)
#   > info             (shows the Bridge mailbox password for .env)
#   > exit
#
# After exiting, restart the container so it picks up the new account:
#   docker compose restart proton-bridge

set -e

echo "============================================"
echo "  Proton Bridge — Interactive Setup"
echo "============================================"
echo ""
echo "Commands you'll need:"
echo "  login    — Log in with your ProtonMail credentials"
echo "  list     — Verify your account is connected"
echo "  info     — Show the Bridge mailbox password (for PROTONMAIL_PASSWORD in .env)"
echo "  exit     — Done (then restart the container)"
echo ""

# Kill ALL running bridge processes (started by entrypoint via gosu)
echo "Stopping running bridge instance..."
pkill -9 -f protonmail-bridge 2>/dev/null || true
sleep 2

# Remove stale lock file — bridge doesn't clean up after SIGKILL
find /home/proton -name '*.lock' -path '*protonmail*' -delete 2>/dev/null || true
find /home/proton -name '.lock' -path '*bridge*' -delete 2>/dev/null || true
# Common lock file locations for bridge v3
rm -f /home/proton/.cache/protonmail/bridge-v3/bridge.lock 2>/dev/null
rm -f /home/proton/.config/protonmail/bridge-v3/bridge.lock 2>/dev/null
rm -f /home/proton/.local/share/protonmail/bridge-v3/bridge.lock 2>/dev/null

echo "Starting Bridge CLI..."
echo ""

# Run as proton user (container runs as root, bridge state is owned by proton)
# After exiting, the container will need a restart to bring the daemon back.
exec gosu proton /usr/bin/protonmail-bridge --cli
