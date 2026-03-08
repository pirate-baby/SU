#!/bin/bash
# Entrypoint for the claude-executor container.
#
# Starts basic-memory as a persistent MCP sidecar (streamable-http),
# waits for it to be ready, then execs uvicorn as PID 1.
#
# basic-memory is pre-installed via `uv tool install basic-memory` in the
# Dockerfile. Running it once as a sidecar avoids the 5-10s cold-start
# penalty that occurred when every agent call spawned a new subprocess.

set -e

BASIC_MEMORY_PORT="${BASIC_MEMORY_PORT:-8765}"
BASIC_MEMORY_LOG=/tmp/basic-memory-mcp.log

# Start basic-memory MCP sidecar with a watchdog (auto-restart on crash)
echo "Starting basic-memory MCP server (streamable-http, port $BASIC_MEMORY_PORT)..."
(
    while true; do
        /home/appuser/.local/bin/basic-memory mcp \
            --transport streamable-http \
            --host 127.0.0.1 \
            --port "$BASIC_MEMORY_PORT" \
            --path /mcp \
            >> "$BASIC_MEMORY_LOG" 2>&1
        echo "$(date): basic-memory exited (status $?), restarting in 3s..." >> "$BASIC_MEMORY_LOG"
        sleep 3
    done
) &

# Wait for basic-memory to be ready (up to 30s)
echo "Waiting for basic-memory to be ready..."
for i in $(seq 1 30); do
    # streamable-http endpoint returns 405 on GET, which is still proof of liveness
    if curl -sf -o /dev/null -w '' "http://127.0.0.1:$BASIC_MEMORY_PORT/mcp" 2>/dev/null || \
       curl -sf -o /dev/null -w '%{http_code}' "http://127.0.0.1:$BASIC_MEMORY_PORT/mcp" 2>/dev/null | grep -q '405'; then
        echo "basic-memory ready after ${i}s"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "WARNING: basic-memory did not start within 30s — app will start anyway"
        echo "  Check logs: cat $BASIC_MEMORY_LOG"
        cat "$BASIC_MEMORY_LOG" 2>/dev/null | tail -20 || true
    fi
    sleep 1
done

# Hand off to the main application (exec = PID 1, receives Docker signals)
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
