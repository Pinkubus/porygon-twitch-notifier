"""
authorize.py — one-time (or once-every-~30-days) local pairing helper.

Run this locally whenever you need a new Twitch refresh token: the first time
you set this repo up, and again whenever the notifier workflow starts failing
because the refresh token expired (Public-client refresh tokens expire 30
days after being issued, regardless of use).

Usage:
    pip install requests
    TWITCH_CLIENT_ID=xxxx python authorize.py

Then follow the printed URL/code, and copy the printed refresh_token into
this repo's TWITCH_REFRESH_TOKEN secret (Settings > Secrets and variables >
Actions > update TWITCH_REFRESH_TOKEN).
"""
import os
import sys
import time

import requests

_TWITCH_DEVICE_URL = "https://id.twitch.tv/oauth2/device"
_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def main():
    client_id = os.environ.get("TWITCH_CLIENT_ID", "")
    if not client_id:
        print("Set TWITCH_CLIENT_ID in your environment first.")
        return 1

    resp = requests.post(_TWITCH_DEVICE_URL, data={"client_id": client_id, "scopes": ""}, timeout=10)
    if resp.status_code != 200:
        print(f"Failed to start device flow: {resp.status_code} {resp.text[:200]}")
        return 1
    device = resp.json()

    print(f"\nGo to: {device['verification_uri']}")
    print(f"Enter code: {device['user_code']}\n")
    print("Waiting for you to authorize...")

    deadline = time.time() + device["expires_in"]
    while time.time() < deadline:
        time.sleep(device["interval"])
        resp = requests.post(_TWITCH_TOKEN_URL, data={
            "client_id": client_id,
            "scopes": "",
            "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=10)
        if resp.status_code == 200:
            token = resp.json()
            print("\nAuthorized! Copy this into the TWITCH_REFRESH_TOKEN secret:\n")
            print(token["refresh_token"])
            print()
            return 0
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if body.get("message") != "authorization_pending":
            print(f"Authorization failed: {resp.status_code} {resp.text[:200]}")
            return 1

    print("Device code expired before authorization. Run again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
