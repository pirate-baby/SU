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
echo "Starting Bridge CLI..."
echo ""

exec /usr/bin/protonmail-bridge --cli
