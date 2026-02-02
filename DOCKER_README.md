# Claude Task Executor - Docker Setup

A FastAPI application running in Docker with nginx reverse proxy, accessible via HTTP/HTTPS on your local network or Tailscale.

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- Claude CLI installed at `/usr/local/bin/claude` (or set `CLAUDE_BINARY_PATH`)
- Claude configured and authenticated

### Setup

```bash
# 1. Generate SSL certificates
./generate-ssl.sh

# 2. Create workspace directory
mkdir -p claude-workspace

# 3. Build and start services
docker compose up -d

# 4. Check health
curl http://localhost/health
# or
curl -k https://localhost/health
```

### Access Points

- **HTTP**: `http://localhost` or `http://<tailscale-ip>`
- **HTTPS**: `https://localhost` or `https://<tailscale-ip>` (self-signed cert)
- **Health Check**: Available over HTTP at `/health`

The service automatically redirects HTTP to HTTPS except for the `/health` endpoint.

## Configuration

Create a `.env` file for custom settings:

```env
# Path to Claude binary on host
CLAUDE_BINARY_PATH=/usr/local/bin/claude

# Workspace directory (Mac: ./claude-workspace, Ubuntu: ~/claude-workspace)
CLAUDE_WORKDIR=./claude-workspace
```

## API Endpoints

### GET /
Basic health check.

### GET /health
Detailed health check including Claude availability (available over HTTP).

```bash
curl http://localhost/health
```

Response:
```json
{
  "status": "healthy",
  "claude_available": true,
  "claude_version": "1.0.0"
}
```

### POST /execute
Execute a task using Claude (requires HTTPS or HTTP with redirect).

```bash
curl -k -X POST https://localhost/execute \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Create a hello world function in Python",
    "timeout": 300
  }'
```

Response:
```json
{
  "success": true,
  "output": "... Claude output ..."
}
```

## Docker Commands

```bash
# Build
docker compose build

# Start
docker compose up -d

# Stop
docker compose down

# View logs
docker compose logs -f

# View specific service logs
docker compose logs -f nginx
docker compose logs -f claude-executor

# Restart
docker compose restart

# Rebuild
docker compose up -d --build
```

## SSL Certificates

The `generate-ssl.sh` script creates self-signed SSL certificates suitable for internal use:

```bash
./generate-ssl.sh
```

Certificates are valid for 365 days and stored in `nginx/ssl/`. Browsers will show a warning for self-signed certificates - this is expected and safe for internal use over Tailscale.

To regenerate certificates:
```bash
rm nginx/ssl/*.pem
./generate-ssl.sh
docker compose restart nginx
```

## Accessing via Tailscale

Once deployed on your EC2 instance or any machine with Tailscale:

1. **Get your Tailscale IP**:
   ```bash
   tailscale ip -4
   ```

2. **Access the service**:
   - HTTP: `http://<tailscale-ip>`
   - HTTPS: `https://<tailscale-ip>`

3. **From any device on your Tailscale network**:
   - Mac, iPhone, iPad, etc. can all access the service
   - Accept the self-signed certificate warning in your browser

## Platform-Specific Notes

### Mac
Claude is typically at `/usr/local/bin/claude`. The default configuration should work out of the box.

```bash
./generate-ssl.sh
mkdir -p claude-workspace
docker compose up -d
```

### Ubuntu EC2
If Claude is installed elsewhere, set the path:

```bash
export CLAUDE_BINARY_PATH=/path/to/claude
export CLAUDE_WORKDIR=~/claude-workspace

./generate-ssl.sh
mkdir -p ~/claude-workspace
docker compose up -d
```

Or use `.env`:
```env
CLAUDE_BINARY_PATH=/home/ubuntu/bin/claude
CLAUDE_WORKDIR=/home/ubuntu/claude-workspace
```

## Architecture

```
┌─────────────────────────────────────┐
│     Tailscale Network / LAN         │
│                                     │
│  HTTP (80) / HTTPS (443)           │
└────────────────┬────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────┐
│      nginx (Docker)                 │
│  - SSL termination                  │
│  - HTTP → HTTPS redirect            │
│  - Reverse proxy                    │
└────────────────┬────────────────────┘
                 │ port 8000
                 ▼
┌─────────────────────────────────────┐
│  claude-executor (Docker)           │
│  - FastAPI app                      │
│  - Executes Claude tasks            │
└────────────────┬────────────────────┘
                 │ volume mount
                 ▼
┌─────────────────────────────────────┐
│      Host System                    │
│  - /usr/local/bin/claude            │
│  - ~/.config/claude                 │
│  - ./claude-workspace               │
└─────────────────────────────────────┘
```

## Volume Mounts

The containers mount these items from the host:

1. **Claude Binary** (read-only): `/usr/local/bin/claude`
2. **Claude Config** (read-only): `~/.config/claude`
3. **Workspace** (read-write): `./claude-workspace`
4. **nginx Config** (read-only): `./nginx/nginx.conf`
5. **SSL Certificates** (read-only): `./nginx/ssl/`

## Development

### Local Development Without Docker

```bash
# Install UV
pip install uv

# Install dependencies (including dev dependencies)
uv pip install -r pyproject.toml
uv pip install -e ".[dev]"

# Run directly
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Testing

```bash
# Run tests with pytest
pytest

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/test_api.py

# Run with verbose output
pytest -v

# Test via nginx (HTTPS)
curl -k https://localhost/health

# Test via nginx (HTTP)
curl http://localhost/health
```

## Troubleshooting

### SSL Certificate Errors

If you see SSL errors:
```bash
# Regenerate certificates
rm nginx/ssl/*.pem
./generate-ssl.sh
docker compose restart nginx
```

### nginx Won't Start

Check if ports are in use:
```bash
lsof -i :80
lsof -i :443
```

On Mac, you may need to stop Apache or other services:
```bash
sudo apachectl stop
```

### Cannot Connect to Service

Check all containers are running:
```bash
docker compose ps
```

Check logs:
```bash
docker compose logs nginx
docker compose logs claude-executor
```

Verify nginx can reach the backend:
```bash
docker compose exec nginx ping claude-executor
```

### Claude Binary Not Found

Check Claude installation:
```bash
which claude
ls -l /usr/local/bin/claude
```

Update path in `.env` if needed.

## Security

- Claude binary and config mounted read-only
- Container runs as non-root user (appuser, UID 1000)
- SSL/TLS encryption for all traffic (except health endpoint)
- Modern TLS configuration (TLS 1.2+)
- Security headers enabled
- Self-signed certificates suitable for internal networks

## Production Deployment

### Ubuntu with Systemd

Create `/etc/systemd/system/claude-executor.service`:

```ini
[Unit]
Description=Claude Task Executor
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/ubuntu/SU
ExecStartPre=/home/ubuntu/SU/generate-ssl.sh
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=ubuntu

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable claude-executor
sudo systemctl start claude-executor
```

### Firewall Configuration

If using UFW on Ubuntu:
```bash
# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Or allow only from Tailscale interface
sudo ufw allow in on tailscale0 to any port 80
sudo ufw allow in on tailscale0 to any port 443
```

### Monitoring

View container stats:
```bash
docker stats
```

Monitor logs:
```bash
docker compose logs -f --tail=100
```
