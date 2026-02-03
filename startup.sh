#!/bin/bash

set -e

echo "Starting Claude Chat Service..."

# Detect OS
OS=$(uname -s)

case "$OS" in
    Darwin)
        echo "Detected macOS"
        ;;
    Linux)
        echo "Detected Linux"
        # Check if running on Ubuntu/Debian
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            if [[ "$ID" == "ubuntu" ]] || [[ "$ID_LIKE" == *"ubuntu"* ]] || [[ "$ID_LIKE" == *"debian"* ]]; then
                echo "Running on Ubuntu/Debian"
            fi
        fi
        ;;
    *)
        echo "Unsupported OS: $OS"
        exit 1
        ;;
esac

# Check for .claude directory (needed for authentication)
if [ ! -d "$HOME/.claude" ]; then
    echo "Warning: $HOME/.claude directory not found"
    echo "Make sure you've authenticated with Claude Code CLI:"
    echo "  claude login"
    echo ""
    echo "Continuing anyway (you can set CLAUDE_OAUTH_TOKEN environment variable instead)..."
fi

# Use local development configuration (HTTP only, no SSL)
echo "Starting services with local development configuration (HTTP only)..."
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d

echo ""
echo "✓ Services started successfully!"
echo ""
echo "Access the chat at: http://localhost"
echo ""
echo "To view logs: docker compose logs -f"
echo "To stop: docker compose down"
