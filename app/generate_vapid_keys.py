"""
Generate VAPID key pair for Web Push notifications.

Usage:
    python -m app.generate_vapid_keys

Outputs KEY=VALUE lines suitable for appending directly to .env.
With --inject flag, appends them to .env automatically (skipping any
keys already present).
"""
import argparse
import os
import re

from py_vapid import Vapid


def main():
    parser = argparse.ArgumentParser(description="Generate VAPID keys for Web Push")
    parser.add_argument(
        "--inject",
        metavar="ENV_FILE",
        nargs="?",
        const=".env",
        help="Append keys to ENV_FILE (default .env), skipping existing keys",
    )
    args = parser.parse_args()

    vapid = Vapid()
    vapid.generate_keys()

    # py_vapid stores keys as raw bytes — we need URL-safe base64 strings
    # that pywebpush / the browser Push API expect.
    private_key = vapid.private_pem().decode() if isinstance(vapid.private_pem(), bytes) else vapid.private_pem()
    # For pywebpush, the private key can be passed as the raw PEM string,
    # but it must be a single line in .env.  Easier: use the base64url
    # encoding that the library also accepts.
    import base64
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    priv_raw = vapid._private_key.private_numbers().private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_raw).rstrip(b"=").decode()

    pub_raw = vapid._private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()

    lines = {
        "VAPID_PRIVATE_KEY": priv_b64,
        "VAPID_PUBLIC_KEY": pub_b64,
        "VAPID_CLAIMS_EMAIL": "mailto:su@localhost",
    }

    if args.inject:
        env_path = args.inject
        existing = ""
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                existing = f.read()

        added = []
        with open(env_path, "a") as f:
            for key, value in lines.items():
                # Skip if already present (commented or uncommented)
                if re.search(rf"^\s*{key}\s*=", existing, re.MULTILINE):
                    print(f"  {key} already in {env_path}, skipping")
                    continue
                f.write(f"{key}={value}\n")
                added.append(key)

        if added:
            print(f"Added to {env_path}: {', '.join(added)}")
        else:
            print(f"All VAPID keys already present in {env_path}")
    else:
        print("# Add these to your .env file:\n")
        for key, value in lines.items():
            print(f"{key}={value}")


if __name__ == "__main__":
    main()
