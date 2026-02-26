# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (curl for healthcheck, nodejs for claude runtime, git for self-iteration)
# Note: claude-agent-sdk bundles its own claude binary, but needs nodejs as runtime
RUN apt-get update && apt-get install -y \
    curl \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Copy UV from official image (no pip needed!)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install basic-memory MCP server
RUN uv tool install basic-memory

# Install protonmail-pro-mcp (email management via ProtonMail/Proton Bridge)
RUN git clone https://github.com/anyrxo/protonmail-pro-mcp /opt/protonmail-pro-mcp \
    && cd /opt/protonmail-pro-mcp \
    && npm install \
    && npm run build

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies with native UV (uses lock file for reproducibility)
RUN uv sync --frozen --no-dev

# Copy application code
COPY app/ ./app/

# Create a non-root user for security (match host UID for volume access)
RUN useradd -m -u 501 appuser && \
    chown -R appuser:appuser /app && \
    mkdir -p /home/appuser/basic-memory && \
    chown appuser:appuser /home/appuser/basic-memory && \
    mkdir -p /data && \
    chown appuser:appuser /data && \
    mkdir -p /src && \
    chown appuser:appuser /src

# Configure git identity for self-iteration commits
RUN git config --global user.email "su@localhost" && \
    git config --global user.name "SU"

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application using UV's virtual environment
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
