# Proton Bridge sidecar — headless IMAP/SMTP gateway for ProtonMail.
#
# Runs protonmail-bridge --noninteractive after one-time interactive login.
# Auth state is persisted in named volumes so rebuilds don't lose credentials.
#
# The pre-built .deb binary only listens on 127.0.0.1. Since other containers
# on the Docker network need to reach IMAP/SMTP, we use socat to forward
# <container-ip>:{1143,1025} → 127.0.0.1:{1143,1025} inside the container.
# This is the same trick the old host setup used, but now it's all local
# to the container — no cross-network hops, no host firewall issues.
#
# First-time setup (after `docker compose up -d`):
#   docker compose exec proton-bridge /setup.sh
#   (follow prompts to log in, then Ctrl-C / exit)
#
# Verify:
#   docker compose exec proton-bridge /check.sh

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    pass \
    socat \
    ca-certificates \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated user (bridge writes to $HOME)
RUN useradd -m -s /bin/bash -u 1100 proton

# Download and install latest Proton Bridge .deb from GitHub releases.
# The install target is resolved dynamically so the image always gets the
# latest release without hardcoding a version.
RUN BRIDGE_DEB_URL=$(curl -fsSL "https://api.github.com/repos/ProtonMail/proton-bridge/releases/latest" \
        | python3 -c "import json,sys; r=json.load(sys.stdin); print(next(a['browser_download_url'] for a in r['assets'] if 'amd64' in a['name'] and a['name'].endswith('.deb')))") \
    && curl -fsSL "$BRIDGE_DEB_URL" -o /tmp/protonmail-bridge.deb \
    && apt-get update && apt-get install -y /tmp/protonmail-bridge.deb \
    && rm -f /tmp/protonmail-bridge.deb \
    && rm -rf /var/lib/apt/lists/*

# Copy helper scripts
COPY setup.sh /setup.sh
COPY check.sh /check.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /setup.sh /check.sh /entrypoint.sh

USER proton
WORKDIR /home/proton

# Pre-create GPG key for bridge keyring (passphrase-free, headless-safe)
RUN gpg --batch --passphrase '' --quick-gen-key 'ProtonMailBridge' default default never

# Initialize pass (password store) with the GPG key — bridge uses this on Linux
RUN GPGID=$(gpg --list-keys --with-colons 'ProtonMailBridge' | awk -F: '/^pub/{print $5}') \
    && pass init "$GPGID"

EXPOSE 1143 1025

# Healthcheck — verify bridge IMAP is accepting connections (on localhost)
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD bash -c '(echo QUIT | timeout 3 bash -c "cat > /dev/tcp/127.0.0.1/1143") 2>/dev/null'

ENTRYPOINT ["/entrypoint.sh"]
