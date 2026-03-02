#!/bin/bash
# One-time interactive login for Proton Bridge.
#
# IMPORTANT: Run this via `docker compose run`, NOT `docker compose exec`.
# The daemon container holds a lock file, so we need a fresh container.
#
# Usage:
#   docker compose stop proton-bridge
#   docker compose run --rm proton-bridge /setup.sh
#   docker compose start proton-bridge
#
# This drops you into the Bridge CLI where you can:
#   > login            (enter ProtonMail credentials + 2FA if enabled)
#   > list             (verify the account appears)
#   > change mode combined   (if you want all addresses in one IMAP login)
#   > info             (shows the Bridge mailbox password for .env)
#   > exit

set -e

# Fix volume ownership (same as entrypoint)
chown -R proton:proton \
    /home/proton/.config \
    /home/proton/.gnupg \
    /home/proton/.local \
    /home/proton/.cache \
    /home/proton/.password-store \
    2>/dev/null || true

echo "============================================"
echo "  Proton Bridge — Interactive Setup"
echo "============================================"
echo ""
echo "Commands you'll need:"
echo "  login    — Log in with your ProtonMail credentials"
echo "  list     — Verify your account is connected"
echo "  info     — Show the Bridge mailbox password (for PROTONMAIL_PASSWORD in .env)"
echo "  exit     — Done"
echo ""
echo "After exiting, start the bridge:"
echo "  docker compose start proton-bridge"
echo ""
echo "Starting Bridge CLI..."
echo ""

exec gosu proton /usr/bin/protonmail-bridge --cli
