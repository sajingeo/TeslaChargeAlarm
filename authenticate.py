#!/usr/bin/env python3
"""
One-time Tesla Fleet API OAuth setup.

Opens a browser to log in to your Tesla account and approve the app, then
writes .fleet_token.json for charge_alarm.py. Requires TESLA_CLIENT_ID and
TESLA_CLIENT_SECRET in .env.

The redirect URI below must be listed in your app's "Allowed Redirect URIs"
at developer.tesla.com (your app → edit). Add exactly:

    http://localhost:8085/callback

Then run:  .venv/bin/python authenticate.py
"""

import json
import os
import secrets
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv

_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, ".env"))

CLIENT_ID     = os.getenv("TESLA_CLIENT_ID")
CLIENT_SECRET = os.getenv("TESLA_CLIENT_SECRET")
FLEET_BASE    = os.getenv("TESLA_FLEET_BASE", "https://fleet-api.prd.na.vn.cloud.tesla.com")

# Must exactly match an entry in the app's Allowed Redirect URIs. If your app
# has a non-localhost URI registered, set TESLA_REDIRECT_URI in .env to it and
# this script will let you paste the redirect URL manually instead.
REDIRECT_URI = os.getenv("TESLA_REDIRECT_URI", "http://localhost:8085/callback")
AUTH_URL     = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL    = "https://auth.tesla.com/oauth2/v3/token"
SCOPES       = "openid offline_access vehicle_device_data"
TOKEN_CACHE  = os.path.join(_DIR, ".fleet_token.json")

if not CLIENT_ID or not CLIENT_SECRET:
    sys.exit("Set TESLA_CLIENT_ID and TESLA_CLIENT_SECRET in .env first.")

state = secrets.token_urlsafe(16)
auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = parse_qs(urlparse(self.path).query)
        if urlparse(self.path).path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        if query.get("state", [None])[0] != state:
            body = "State mismatch — close this tab and rerun the script."
        elif "code" in query:
            auth_code = query["code"][0]
            body = "Authorized! You can close this tab and return to the terminal."
        else:
            body = f"Tesla returned an error: {query.get('error', ['unknown'])[0]}"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<h2>{body}</h2>".encode())

    def log_message(self, *args):
        pass


def main():
    login_url = f"{AUTH_URL}?" + urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPES,
        "state":         state,
    })

    global auth_code
    print("Opening Tesla login in your browser…")
    print(f"If it doesn't open, visit:\n\n{login_url}\n")
    webbrowser.open(login_url)

    if urlparse(REDIRECT_URI).hostname == "localhost":
        server = HTTPServer(("localhost", urlparse(REDIRECT_URI).port or 80), CallbackHandler)
        print("Waiting for you to log in and approve access…")
        while auth_code is None:
            server.handle_request()
    else:
        # Non-localhost redirect: the browser will land on that URI (the page
        # itself may 404 — that's fine). Copy the full URL from the address bar.
        pasted = input("After approving, paste the full redirected URL here:\n> ").strip()
        query = parse_qs(urlparse(pasted).query)
        if query.get("state", [None])[0] != state:
            sys.exit("State mismatch — rerun the script and use the fresh login URL.")
        if "code" not in query:
            sys.exit(f"No code in that URL (error: {query.get('error', ['unknown'])[0]})")
        auth_code = query["code"][0]

    print("Exchanging authorization code for tokens…")
    r = requests.post(TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          auth_code,
        "redirect_uri":  REDIRECT_URI,
        "audience":      FLEET_BASE,
    }, timeout=15)
    if not r.ok:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text}")
    data = r.json()

    tokens = {
        "access_token":  data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at":    (datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 120)).isoformat(),
    }
    tmp = TOKEN_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f)
    os.replace(tmp, TOKEN_CACHE)
    print(f"\nDone — tokens saved to {TOKEN_CACHE}")
    print("charge_alarm.py will refresh them automatically from now on.")
    print("You do NOT need to fill in TESLA_REFRESH_TOKEN in .env.")


if __name__ == "__main__":
    main()
