#!/usr/bin/env python3
"""
Tesla Charge Alarm — mac-agents compatible
Checks Tesla charging state once and exits. Designed to be run every 5 minutes
by mac-agents (github.com/yourname/mac-agents).

Uses the Tesla Fleet API (the legacy Owner API was shut down mid-2026).
Requires a registered developer app: set TESLA_CLIENT_ID and, for the first
run, TESLA_REFRESH_TOKEN in .env. Tokens are cached in .fleet_token.json and
rotated automatically afterwards.

State is persisted to .charge_state.json between runs so session tracking
and notification flags survive across invocations.
"""

import sys
import json
import os
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

TESLA_CLIENT_ID = os.getenv("TESLA_CLIENT_ID")
if not TESLA_CLIENT_ID:
    sys.exit("TESLA_CLIENT_ID is empty — fill in your Fleet API credentials in .env")
TESLA_CLIENT_SECRET = os.getenv("TESLA_CLIENT_SECRET")  # only confidential apps need this
TESLA_REFRESH_TOKEN = os.getenv("TESLA_REFRESH_TOKEN")  # bootstrap only
PUSHOVER_TOKEN      = os.environ["PUSHOVER_TOKEN"]
PUSHOVER_USER       = os.environ["PUSHOVER_USER"]

TARGET_PERCENT = int(os.getenv("TARGET_PERCENT", "80"))
TIMER_MINUTES  = int(os.getenv("TIMER_MINUTES", "240"))

FLEET_BASE = os.getenv(
    "TESLA_FLEET_BASE", "https://fleet-api.prd.na.vn.cloud.tesla.com"
)
AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"

# ─────────────────────────────────────────────────────────────────────────────

_DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(_DIR, ".charge_state.json")
TOKEN_CACHE = os.path.join(_DIR, ".fleet_token.json")

# Alert at most once per day about the monitor itself being broken
ERROR_ALERT_INTERVAL = timedelta(hours=24)


def ts():
    return datetime.now().strftime("%H:%M:%S")


def save_state(notified, last_state, session_start, last_error_alert=None):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "notified":         notified,
            "last_state":       last_state,
            "session_start":    session_start.isoformat() if session_start else None,
            "last_error_alert": last_error_alert.isoformat() if last_error_alert else None,
        }, f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {
            "notified":         data.get("notified", {"complete": False, "target": False, "timer": False}),
            "last_state":       data.get("last_state"),
            "session_start":    datetime.fromisoformat(data["session_start"]) if data.get("session_start") else None,
            "last_error_alert": datetime.fromisoformat(data["last_error_alert"]) if data.get("last_error_alert") else None,
        }
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return {
            "notified":         {"complete": False, "target": False, "timer": False},
            "last_state":       None,
            "session_start":    None,
            "last_error_alert": None,
        }


def send_pushover(title, body, emergency=True):
    payload = {
        "token":   PUSHOVER_TOKEN,
        "user":    PUSHOVER_USER,
        "title":   title,
        "message": body,
        "sound":   "siren",
    }
    if emergency:
        payload.update({"priority": 2, "retry": 30, "expire": 3600})
    else:
        payload["priority"] = 0

    r = requests.post(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        timeout=10,
    )
    if not r.ok:
        print(f"Pushover error {r.status_code}: {r.text}", file=sys.stderr)
        return False
    print(f"Alarm sent: {title}")
    return True


# ─── FLEET API ────────────────────────────────────────────────────────────────

def _save_tokens(tokens):
    # Fleet refresh tokens are single-use; losing one mid-write means a manual
    # re-auth, so write atomically.
    tmp = TOKEN_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f)
    os.replace(tmp, TOKEN_CACHE)


def _refresh_tokens(refresh_token):
    payload = {
        "grant_type":    "refresh_token",
        "client_id":     TESLA_CLIENT_ID,
        "refresh_token": refresh_token,
    }
    if TESLA_CLIENT_SECRET:
        payload["client_secret"] = TESLA_CLIENT_SECRET
    r = requests.post(AUTH_URL, data=payload, timeout=15)
    if r.status_code in (400, 401):
        raise RuntimeError(
            f"Token refresh rejected ({r.status_code}): {r.text}\n"
            "The refresh token is invalid or was already used. Redo the OAuth "
            "flow and put the new token in .env as TESLA_REFRESH_TOKEN, then "
            f"delete {TOKEN_CACHE}."
        )
    r.raise_for_status()
    data = r.json()
    tokens = {
        "access_token":  data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_at":    (datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 120)).isoformat(),
    }
    _save_tokens(tokens)
    return tokens


def get_access_token():
    try:
        with open(TOKEN_CACHE) as f:
            tokens = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        if not TESLA_REFRESH_TOKEN:
            raise RuntimeError(
                f"No token cache at {TOKEN_CACHE} and TESLA_REFRESH_TOKEN is "
                "not set in .env — cannot authenticate."
            )
        print(f"{ts()} Bootstrapping token cache from TESLA_REFRESH_TOKEN")
        return _refresh_tokens(TESLA_REFRESH_TOKEN)["access_token"]

    if datetime.now() >= datetime.fromisoformat(tokens["expires_at"]):
        tokens = _refresh_tokens(tokens["refresh_token"])
    return tokens["access_token"]


def fleet_get(path, access_token, **kwargs):
    r = requests.get(
        f"{FLEET_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
        **kwargs,
    )
    return r


def get_charge_state(access_token):
    """Returns charge info dict, or None if the vehicle is asleep/offline."""
    r = fleet_get("/api/1/vehicles", access_token)
    r.raise_for_status()
    vehicles = r.json()["response"]
    if not vehicles:
        print("No vehicles found.", file=sys.stderr)
        sys.exit(1)
    vehicle = vehicles[0]

    # Don't wake the car: a sleeping car isn't charging, and each wake costs
    # battery and billable API calls.
    if vehicle.get("state") != "online":
        print(f"{ts()} Vehicle {vehicle.get('state', 'unknown')} — not charging, skipping.")
        return None

    r = fleet_get(
        f"/api/1/vehicles/{vehicle['id']}/vehicle_data",
        access_token,
        params={"endpoints": "charge_state"},
    )
    if r.status_code == 408:  # went to sleep between the two calls
        print(f"{ts()} Vehicle asleep — skipping.")
        return None
    r.raise_for_status()
    cs = r.json()["response"]["charge_state"]
    return {
        "state":   cs["charging_state"],
        "level":   cs["battery_level"],
        "limit":   cs["charge_limit_soc"],
        "eta_min": cs["minutes_to_full_charge"],
    }


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def check_once(state):
    notified      = state["notified"]
    last_state    = state["last_state"]
    session_start = state["session_start"]
    timer_end     = (
        session_start + timedelta(minutes=TIMER_MINUTES)
        if session_start and TIMER_MINUTES else None
    )

    access_token = get_access_token()
    cs = get_charge_state(access_token)
    if cs is None:
        save_state(notified, last_state, session_start, state["last_error_alert"])
        return

    eta = f"{cs['eta_min']}m to full" if cs["eta_min"] else "—"
    print(f"{cs['state']} | {cs['level']}% (limit {cs['limit']}%) | {eta}")

    # ── New charging session ──────────────────────────────────────────────────
    if cs["state"] == "Charging" and last_state != "Charging":
        notified      = {"complete": False, "target": False, "timer": False}
        session_start = datetime.now()
        if TIMER_MINUTES:
            timer_end = session_start + timedelta(minutes=TIMER_MINUTES)
            print(f"New session — timer set for {timer_end.strftime('%H:%M')}")

    # ── Timer ─────────────────────────────────────────────────────────────────
    if timer_end and not notified["timer"] and datetime.now() >= timer_end:
        if send_pushover(
            f"Tesla — {TIMER_MINUTES // 60}hr Timer",
            f"Your {TIMER_MINUTES}-minute charge timer has elapsed!",
        ):
            notified["timer"] = True

    # ── Charging complete ─────────────────────────────────────────────────────
    if cs["state"] == "Complete" and not notified["complete"]:
        if send_pushover(
            "Tesla Fully Charged",
            f"Battery at {cs['level']}% — charging complete!",
        ):
            notified["complete"] = True

    # ── Target % reached ──────────────────────────────────────────────────────
    if (
        TARGET_PERCENT
        and not notified["target"]
        and cs["state"] == "Charging"
        and cs["level"] >= TARGET_PERCENT
    ):
        if send_pushover(
            f"Tesla at {TARGET_PERCENT}%",
            f"Battery reached your target of {TARGET_PERCENT}%.",
        ):
            notified["target"] = True

    # ── Unplugged ─────────────────────────────────────────────────────────────
    if last_state and last_state != "Disconnected" and cs["state"] == "Disconnected":
        send_pushover(
            "Tesla Unplugged",
            "Your Tesla has been disconnected from the charger.",
            emergency=False,
        )

    save_state(notified, cs["state"], session_start, state["last_error_alert"])


def main():
    state = load_state()
    try:
        check_once(state)
    except Exception as e:
        # The June 2026 Owner API shutdown went unnoticed for weeks because the
        # monitor never reported its own failures — alert (quietly) when broken.
        print(f"{ts()} Check failed: {e}", file=sys.stderr)
        last_alert = state["last_error_alert"]
        if not last_alert or datetime.now() - last_alert >= ERROR_ALERT_INTERVAL:
            if send_pushover(
                "Tesla Charge Alarm broken",
                f"Monitoring is failing and alarms will NOT fire: {e}",
                emergency=False,
            ):
                save_state(
                    state["notified"], state["last_state"],
                    state["session_start"], datetime.now(),
                )
        sys.exit(1)


if __name__ == "__main__":
    main()
