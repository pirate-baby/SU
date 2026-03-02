#!/bin/bash
# Entrypoint for the Proton Bridge sidecar container.
#
# Runs as root to fix volume permissions, then drops to the proton user.
#
# The pre-built bridge binary only listens on 127.0.0.1:{1143,1025}.
# Other containers on the Docker network connect to this container's
# network IP, so we need to expose those ports externally via socat.
#
# Strategy:
#   1. Fix ownership on volume-mounted directories (may be root from first run)
#   2. Start bridge as proton user in the background (binds 127.0.0.1:1143/1025)
#   3. Wait for bridge ports to open
#   4. Start socat forwarders on the container's non-loopback IP
#      (eth0 IP:port → 127.0.0.1:port) so there's no port conflict
#   5. Wait on the bridge process — if it exits, the container exits.

set -e

# Fix volume ownership — on first run Docker may create these as root
chown -R proton:proton \
    /home/proton/.config \
    /home/proton/.gnupg \
    /home/proton/.local \
    /home/proton/.cache \
    /home/proton/.password-store \
    2>/dev/null || true

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
