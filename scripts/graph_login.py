#!/usr/bin/env python3
"""One-time interactive login to Microsoft Graph (device code flow).

Run this once (and again any time the refresh token stops working, e.g.
after a password change or if it hasn't been used in a while). Log in with
an account that has "Full Access" mailbox permission on the shared mailbox
configured as GRAPH_MAILBOX -- if you can already open that mailbox in your
own Outlook, you have this.

No web server or redirect URL is needed: this prints a short code, you
enter it at https://microsoft.com/devicelogin in any browser, and the
script picks up the result.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from app.config import settings
from app.email_client import GRAPH_SCOPES, REFRESH_TOKEN_FILE


def main() -> None:
    if not (settings.graph_tenant_id and settings.graph_client_id):
        print("Vul eerst GRAPH_TENANT_ID en GRAPH_CLIENT_ID in via .env")
        return

    devicecode_url = f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/devicecode"
    resp = requests.post(
        devicecode_url,
        data={"client_id": settings.graph_client_id, "scope": GRAPH_SCOPES},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Kon geen inlogcode aanvragen ({resp.status_code}): {resp.text}")
        return

    payload = resp.json()
    print()
    print(payload["message"])
    print()

    token_url = f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/token"
    interval = payload.get("interval", 5)
    deadline = time.time() + payload.get("expires_in", 900)

    while time.time() < deadline:
        time.sleep(interval)

        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": settings.graph_client_id,
            "device_code": payload["device_code"],
        }
        if settings.graph_client_secret:
            data["client_secret"] = settings.graph_client_secret

        token_resp = requests.post(token_url, data=data, timeout=30)
        token_payload = token_resp.json()

        if token_resp.status_code == 200:
            os.makedirs(os.path.dirname(REFRESH_TOKEN_FILE), exist_ok=True)
            with open(REFRESH_TOKEN_FILE, "w") as fh:
                fh.write(token_payload["refresh_token"])
            print(f"Gelukt! Ingelogd en refresh-token opgeslagen in {REFRESH_TOKEN_FILE}")
            return

        error = token_payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue

        print("Inloggen mislukt:", token_payload.get("error_description", token_payload))
        return

    print("Tijd verlopen voordat er werd ingelogd -- probeer het opnieuw.")


if __name__ == "__main__":
    main()
