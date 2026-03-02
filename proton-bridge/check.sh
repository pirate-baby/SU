#!/bin/bash
# Sanity-check that Proton Bridge is running and IMAP is accessible.
#
# Usage (from inside the container or via exec):
#   docker compose exec proton-bridge /check.sh
#
# With email listing (pass creds via env or .env):
#   docker compose exec -e PROTONMAIL_USERNAME=you@proton.me \
#       -e PROTONMAIL_PASSWORD=bridge-mailbox-password proton-bridge /check.sh --emails

set -e

IMAP_HOST="localhost"
IMAP_PORT="1143"
SMTP_HOST="localhost"
SMTP_PORT="1025"

echo "=== Proton Bridge Sanity Check ==="
echo ""

# 1. Check if bridge process is running
echo "1. Bridge process:"
if pgrep -f 'protonmail-bridge' > /dev/null 2>&1; then
    echo "   OK — protonmail-bridge is running (PID $(pgrep -f 'protonmail-bridge' | head -1))"
else
    echo "   FAIL — protonmail-bridge process not found"
    exit 1
fi

# 2. Check IMAP port (bridge on localhost)
echo ""
echo "2. IMAP port ($IMAP_HOST:$IMAP_PORT):"
if (echo QUIT | timeout 3 bash -c "cat > /dev/tcp/$IMAP_HOST/$IMAP_PORT" 2>/dev/null); then
    echo "   OK — IMAP port is accepting connections"
else
    echo "   FAIL — cannot connect to IMAP on $IMAP_HOST:$IMAP_PORT"
    echo "   Bridge may still be starting up. Wait 30s and retry."
    exit 1
fi

# 3. Check SMTP port (bridge on localhost)
echo ""
echo "3. SMTP port ($SMTP_HOST:$SMTP_PORT):"
if (echo QUIT | timeout 3 bash -c "cat > /dev/tcp/$SMTP_HOST/$SMTP_PORT" 2>/dev/null); then
    echo "   OK — SMTP port is accepting connections"
else
    echo "   FAIL — cannot connect to SMTP on $SMTP_HOST:$SMTP_PORT"
    exit 1
fi

# 4. Check socat forwarding (network-accessible ports)
echo ""
CONTAINER_IP=$(hostname -i 2>/dev/null | awk '{print $1}' || echo "")
if [ -n "$CONTAINER_IP" ] && [ "$CONTAINER_IP" != "127.0.0.1" ]; then
    echo "4. socat forwarding ($CONTAINER_IP → localhost):"
    SOCAT_OK=true
    if (echo QUIT | timeout 3 bash -c "cat > /dev/tcp/$CONTAINER_IP/$IMAP_PORT" 2>/dev/null); then
        echo "   OK — IMAP forwarding working on $CONTAINER_IP:$IMAP_PORT"
    else
        echo "   FAIL — IMAP not reachable on $CONTAINER_IP:$IMAP_PORT"
        echo "   Other containers will not be able to connect."
        SOCAT_OK=false
    fi
    if (echo QUIT | timeout 3 bash -c "cat > /dev/tcp/$CONTAINER_IP/$SMTP_PORT" 2>/dev/null); then
        echo "   OK — SMTP forwarding working on $CONTAINER_IP:$SMTP_PORT"
    else
        echo "   FAIL — SMTP not reachable on $CONTAINER_IP:$SMTP_PORT"
        SOCAT_OK=false
    fi
    if [ "$SOCAT_OK" = false ]; then
        echo ""
        echo "   socat forwarding failed. Check that entrypoint.sh started socat."
        exit 1
    fi
else
    echo "4. socat forwarding: SKIP (could not determine container IP)"
fi

# 5. Optional: Test IMAP login and list emails
STEP=5
if [ "$1" = "--emails" ]; then
    echo ""
    echo "$STEP. IMAP login and email listing:"

    if [ -z "$PROTONMAIL_USERNAME" ] || [ -z "$PROTONMAIL_PASSWORD" ]; then
        echo "   SKIP — PROTONMAIL_USERNAME and PROTONMAIL_PASSWORD not set"
        echo "   Pass them with: docker compose exec -e PROTONMAIL_USERNAME=... -e PROTONMAIL_PASSWORD=... proton-bridge /check.sh --emails"
        exit 0
    fi

    # Use Python for IMAP since it's available in the container
    python3 - "$IMAP_HOST" "$IMAP_PORT" "$PROTONMAIL_USERNAME" "$PROTONMAIL_PASSWORD" <<'PYEOF'
import imaplib
import ssl
import sys
import email
from email.header import decode_header

host, port, username, password = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

try:
    # Proton Bridge may use implicit TLS (IMAP4_SSL) or STARTTLS.
    # Try SSL first, fall back to STARTTLS.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    except ssl.SSLError:
        imap = imaplib.IMAP4(host, port)
        imap.starttls(ssl_context=ctx)
    typ, data = imap.login(username, password)
    print(f"   Login: {typ} — {data[0].decode()}")

    # List mailboxes
    typ, mailboxes = imap.list()
    print(f"   Mailboxes: {len(mailboxes)} found")
    for mb in mailboxes[:10]:
        print(f"     {mb.decode()}")
    if len(mailboxes) > 10:
        print(f"     ... and {len(mailboxes) - 10} more")

    # Select INBOX and show recent emails
    typ, data = imap.select("INBOX", readonly=True)
    msg_count = int(data[0].decode())
    print(f"\n   INBOX: {msg_count} messages")

    if msg_count > 0:
        # Fetch last 5 message headers
        start = max(1, msg_count - 4)
        typ, msg_data = imap.fetch(f"{start}:{msg_count}", "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        print(f"\n   Last {min(5, msg_count)} emails:")
        for i, response_part in enumerate(msg_data):
            if isinstance(response_part, tuple):
                header_data = response_part[1].decode(errors="replace")
                msg = email.message_from_string(header_data)

                subject = msg.get("Subject", "(no subject)")
                # Decode encoded headers
                decoded_parts = decode_header(subject)
                subject = "".join(
                    part.decode(enc or "utf-8") if isinstance(part, bytes) else part
                    for part, enc in decoded_parts
                )

                sender = msg.get("From", "(unknown)")
                date = msg.get("Date", "(unknown)")
                print(f"     [{i+1}] {date}")
                print(f"         From: {sender}")
                print(f"         Subject: {subject}")
                print()

    imap.logout()
    print("   OK — IMAP login, mailbox listing, and email fetch all working")

except Exception as e:
    print(f"   FAIL — {type(e).__name__}: {e}")
    sys.exit(1)
PYEOF

else
    echo ""
    echo "$STEP. Email listing: SKIPPED (pass --emails to test IMAP login)"
fi

echo ""
echo "=== All checks passed ==="
