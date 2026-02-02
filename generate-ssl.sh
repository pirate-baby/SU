#!/bin/bash

# Generate self-signed SSL certificates for nginx
# These are suitable for internal use over Tailscale

set -e

SSL_DIR="./nginx/ssl"
CERT_FILE="$SSL_DIR/cert.pem"
KEY_FILE="$SSL_DIR/key.pem"

# Check if certificates already exist
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "SSL certificates already exist in $SSL_DIR"
    echo "To regenerate, delete the existing certificates first:"
    echo "  rm $SSL_DIR/*.pem"
    exit 0
fi

# Create SSL directory if it doesn't exist
mkdir -p "$SSL_DIR"

echo "Generating self-signed SSL certificate..."
echo "This certificate is valid for 365 days and suitable for internal use."

# Generate certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -subj "/C=US/ST=State/L=City/O=Organization/CN=claude-executor" \
    -addext "subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:*.tail-scale.ts.net"

# Set proper permissions
chmod 600 "$KEY_FILE"
chmod 644 "$CERT_FILE"

echo ""
echo "✓ SSL certificates generated successfully!"
echo "  Certificate: $CERT_FILE"
echo "  Private Key: $KEY_FILE"
echo ""
echo "Note: This is a self-signed certificate. Browsers will show a warning."
echo "This is expected and safe for internal use over Tailscale."
echo ""
echo "You can now start the services with: docker compose up -d"
