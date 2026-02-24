"""
Generate VAPID key pair for Web Push notifications.

Usage:
    python -m app.generate_vapid_keys

Copy the output values into your .env file.
"""
from py_vapid import Vapid

def main():
    vapid = Vapid()
    vapid.generate_keys()

    print("Add these to your .env file:\n")
    print(f"VAPID_PRIVATE_KEY={vapid.private_pem().strip()}")
    print(f"VAPID_PUBLIC_KEY={vapid.public_key}")
    print("VAPID_CLAIMS_EMAIL=mailto:you@example.com")

if __name__ == "__main__":
    main()
