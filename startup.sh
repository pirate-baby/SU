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

# Auto-detect Claude binary location
if [ -n "$CLAUDE_BINARY_PATH" ]; then
    echo "Using CLAUDE_BINARY_PATH from environment: $CLAUDE_BINARY_PATH"
elif command -v claude &> /dev/null; then
    CLAUDE_BINARY_PATH=$(which claude)
    echo "Found Claude at: $CLAUDE_BINARY_PATH"
else
    echo "Error: Claude binary not found"
    echo "Please install Claude Code CLI first:"
    echo "  https://docs.anthropic.com/en/docs/claude-code"
    echo ""
    echo "Or set CLAUDE_BINARY_PATH environment variable to the path of your Claude binary"
    exit 1
fi

# Export for docker-compose
export CLAUDE_BINARY_PATH

# Check if user is authenticated with Claude
echo "Checking Claude authentication..."
if ! "$CLAUDE_BINARY_PATH" --version &> /dev/null; then
    echo "Warning: Unable to verify Claude CLI. Continuing anyway..."
fi

# Use local development configuration (HTTP only, no SSL)
echo "Starting services with local development configuration (HTTP only)..."
docker-compose -f docker-compose.yml -f docker-compose.local.yml up --build -d

echo ""
echo "✓ Services started successfully!"
echo ""
echo "Access the chat at: http://localhost"
echo ""
echo "To view logs: docker-compose logs -f"
echo "To stop: docker-compose down"
