#!/bin/bash
# backup.sh — Sync persistent data to/from S3.
#
# Usage:
#   bash backup.sh push          # Upload local data/ to S3
#   bash backup.sh pull          # Download S3 data to local data/
#   bash backup.sh push --force  # Push even if local looks empty (for cleanup)
#
# Requires:
#   - AWS CLI configured (aws configure, or IAM role on EC2)
#   - S3_BACKUP_BUCKET set in .env or environment
#
# Data layout in S3:
#   s3://$S3_BACKUP_BUCKET/
#     sessions/sessions.db        (SQLite database + WAL)
#     basic-memory/               (knowledge base files)
#     proton-bridge/              (bridge auth state)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"

# Load S3_BACKUP_BUCKET from .env if not in environment
if [ -z "${S3_BACKUP_BUCKET:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    S3_BACKUP_BUCKET=$(grep -E '^S3_BACKUP_BUCKET=' "$SCRIPT_DIR/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'" || true)
fi

if [ -z "${S3_BACKUP_BUCKET:-}" ]; then
    echo "Error: S3_BACKUP_BUCKET not set."
    echo "  Set it in .env or export it:  export S3_BACKUP_BUCKET=my-su-backups"
    exit 1
fi

if ! command -v aws &>/dev/null; then
    echo "Error: AWS CLI not found. Install with: pip install awscli"
    exit 1
fi

S3_PATH="s3://${S3_BACKUP_BUCKET}"
ACTION="${1:-}"
FORCE="${2:-}"

case "$ACTION" in
    push)
        # Safety check: don't push empty data over a good backup
        if [ ! -f "$DATA_DIR/sessions/sessions.db" ] && [ "$FORCE" != "--force" ]; then
            echo "Warning: $DATA_DIR/sessions/sessions.db not found."
            echo "  This looks like a fresh install — refusing to overwrite S3 backup with empty data."
            echo "  Use 'bash backup.sh push --force' to override."
            exit 1
        fi

        echo "Pushing data/ to $S3_PATH ..."

        # For SQLite, checkpoint WAL into main DB before copying to ensure consistency.
        # This is safe even while the app is running (WAL mode supports concurrent reads).
        if [ -f "$DATA_DIR/sessions/sessions.db" ]; then
            echo "  Checkpointing SQLite WAL..."
            sqlite3 "$DATA_DIR/sessions/sessions.db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
        fi

        aws s3 sync "$DATA_DIR/" "$S3_PATH/" \
            --delete \
            --exclude "proton-bridge/cache/*" \
            --exclude "*.db-wal" \
            --exclude "*.db-shm"

        echo "Done. Backup pushed to $S3_PATH"
        ;;

    pull)
        echo "Pulling $S3_PATH to data/ ..."

        # Create data directories (Docker bind mounts need these to exist)
        mkdir -p "$DATA_DIR/sessions" \
                 "$DATA_DIR/basic-memory" \
                 "$DATA_DIR/proton-bridge/config" \
                 "$DATA_DIR/proton-bridge/gnupg" \
                 "$DATA_DIR/proton-bridge/data" \
                 "$DATA_DIR/proton-bridge/cache" \
                 "$DATA_DIR/proton-bridge/pass"

        aws s3 sync "$S3_PATH/" "$DATA_DIR/" \
            --exclude "proton-bridge/cache/*"

        # Fix ownership for container user (UID 501 = appuser in Dockerfile)
        # Only needed on Linux (Docker Desktop on macOS handles this transparently)
        if [ "$(uname)" = "Linux" ]; then
            echo "  Fixing ownership for container UID 501..."
            sudo chown -R 501:501 "$DATA_DIR/sessions" "$DATA_DIR/basic-memory" 2>/dev/null || true
        fi

        echo "Done. Data restored to $DATA_DIR/"
        echo "  Start services with: bash startup.sh"
        ;;

    *)
        echo "Usage: bash backup.sh <push|pull>"
        echo ""
        echo "  push    Upload local data/ to S3 (skips if DB missing; use --force to override)"
        echo "  pull    Download S3 data to local data/"
        exit 1
        ;;
esac
