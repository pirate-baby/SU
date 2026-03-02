#!/bin/bash
# Entrypoint for the Proton Bridge sidecar container.
#
# Runs as root to fix volume permissions, then drops to the proton user.
#
# Modes:
#   (no args)   — Daemon mode: start bridge + socat forwarders
#   setup       — Interactive CLI for one-time login
#   check [..]. — Run the sanity-check script
#   *           — Run arbitrary command as proton user
#
# The pre-built bridge binary only listens on 127.0.0.1:{1143,1025}.
# Other containers on the Docker network connect to this container's
# network IP, so we need to expose those ports externally via socat.

set -e

# Fix volume ownership — on first run Docker may create these as root
chown -R proton:proton \
    /home/proton/.config \
    /home/proton/.gnupg \
    /home/proton/.local \
    /home/proton/.cache \
    /home/proton/.password-store \
    2>/dev/null || true

# --- Mode dispatch ---

case "${1:-}" in
    setup)
        # The bridge uses OS-level file locking. If the daemon container is
        # still running, it holds the lock and we can't start the CLI.
        # Remove stale lock files (from crashed containers) and try.
        find /home/proton -name '*.lock' -delete 2>/dev/null || true
        find /tmp -name '*proton*bridge*' -delete 2>/dev/null || true

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
        echo "  docker compose up -d proton-bridge"
        echo ""

        # Try to launch. If it fails with lock error, give a clear message.
        gosu proton /usr/bin/protonmail-bridge --cli
        EXIT_CODE=$?
        if [ "$EXIT_CODE" -ne 0 ]; then
            echo ""
            echo "============================================"
            echo "  Bridge CLI failed to start (exit $EXIT_CODE)."
            echo ""
            echo "  If you see 'lock file' errors, the daemon"
            echo "  container is still running. Stop it first:"
            echo ""
            echo "    docker compose stop proton-bridge"
            echo "    docker compose run --rm proton-bridge setup"
            echo "============================================"
        fi
        exit $EXIT_CODE
        ;;

    check)
        shift
        exec /check.sh "$@"
        ;;

    "")
        # Default: daemon mode (fall through below)
        ;;

    *)
        # Arbitrary command
        exec gosu proton "$@"
        ;;
esac

# --- Daemon mode ---

# Remove stale lock files from a previous crash
find /home/proton -name '*.lock' -delete 2>/dev/null || true
find /tmp -name '*proton*bridge*' -delete 2>/dev/null || true

# Start bridge as proton user in the background
gosu proton /usr/bin/protonmail-bridge --noninteractive --log-level info &
BRIDGE_PID=$!

# Wait for bridge to start listening (up to 60s)
echo "Waiting for bridge to open IMAP/SMTP ports..."
for i in $(seq 1 60); do
    if (echo QUIT | timeout 2 bash -c "cat > /dev/tcp/127.0.0.1/1143") 2>/dev/null; then
        echo "Bridge IMAP port ready after ${i}s"
        break
    fi
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "Bridge process exited unexpectedly"
        exit 1
    fi
    sleep 1
done

# Get the container's non-loopback IP for binding socat.
# This avoids conflicting with bridge's 127.0.0.1 binding.
CONTAINER_IP=$(hostname -i | awk '{print $1}')
echo "Container IP: $CONTAINER_IP"

# Start socat forwarders: container_ip:port → 127.0.0.1:port
socat TCP-LISTEN:1143,bind="$CONTAINER_IP",reuseaddr,fork TCP:127.0.0.1:1143 &
socat TCP-LISTEN:1025,bind="$CONTAINER_IP",reuseaddr,fork TCP:127.0.0.1:1025 &
echo "socat forwarders started ($CONTAINER_IP:{1143,1025} → 127.0.0.1:{1143,1025})"

# Wait on the bridge — if it dies, the container exits.
wait "$BRIDGE_PID"
