#!/usr/bin/env python3
"""
Tesla Charge Alarm — mac-agents compatible
Checks Tesla charging state once and exits. Designed to be run every 5 minutes
by mac-agents (github.com/yourname/mac-agents).

State is persisted to .charge_state.json between runs so session tracking
and notification flags survive across invocations.
"""

import sys
import json
import os
import teslapy
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

TESLA_EMAIL    = os.environ["TESLA_EMAIL"]
PUSHOVER_TOKEN = os.environ["PUSHOVER_TOKEN"]
PUSHOVER_USER  = os.environ["PUSHOVER_USER"]

TARGET_PERCENT = int(os.getenv("TARGET_PERCENT", "80"))
TIMER_MINUTES  = int(os.getenv("TIMER_MINUTES", "240"))

# ─────────────────────────────────────────────────────────────────────────────

_DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(_DIR, ".charge_state.json")
TOKEN_CACHE = os.path.join(_DIR, ".tesla_token.json")


def ts():
    return datetime.now().strftime("%H:%M:%S")


def save_state(notified, last_state, session_start):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "notified":      notified,
            "last_state":    last_state,
            "session_start": session_start.isoformat() if session_start else None,
        }, f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {
            "notified":      data.get("notified", {"complete": False, "target": False, "timer": False}),
            "last_state":    data.get("last_state"),
            "session_start": datetime.fromisoformat(data["session_start"]) if data.get("session_start") else None,
        }
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {
            "notified":      {"complete": False, "target": False, "timer": False},
            "last_state":    None,
            "session_start": None,
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


def get_charge_state(vehicle):
    data = vehicle.get_vehicle_data()
    cs = data["charge_state"]
    return {
        "state":   cs["charging_state"],
        "level":   cs["battery_level"],
        "limit":   cs["charge_limit_soc"],
        "eta_min": cs["minutes_to_full_charge"],
    }


def main():
    # ── Load persisted session state ──────────────────────────────────────────
    state         = load_state()
    notified      = state["notified"]
    last_state    = state["last_state"]
    session_start = state["session_start"]
    timer_end     = (
        session_start + timedelta(minutes=TIMER_MINUTES)
        if session_start and TIMER_MINUTES else None
    )

    # ── Fetch charge state from Tesla ─────────────────────────────────────────
    with teslapy.Tesla(TESLA_EMAIL, cache_file=TOKEN_CACHE) as tesla:
        tesla.fetch_token()
        vehicles = tesla.vehicle_list()
        if not vehicles:
            print("No vehicles found.", file=sys.stderr)
            sys.exit(1)
        vehicle = vehicles[0]
        vehicle.sync_wake_up()
        cs = get_charge_state(vehicle)

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
        send_pushover(
            f"Tesla — {TIMER_MINUTES // 60}hr Timer",
            f"Your {TIMER_MINUTES}-minute charge timer has elapsed!",
        )
        notified["timer"] = True

    # ── Charging complete ─────────────────────────────────────────────────────
    if cs["state"] == "Complete" and not notified["complete"]:
        send_pushover(
            "Tesla Fully Charged",
            f"Battery at {cs['level']}% — charging complete!",
        )
        notified["complete"] = True

    # ── Target % reached ──────────────────────────────────────────────────────
    if (
        TARGET_PERCENT
        and not notified["target"]
        and cs["state"] == "Charging"
        and cs["level"] >= TARGET_PERCENT
    ):
        send_pushover(
            f"Tesla at {TARGET_PERCENT}%",
            f"Battery reached your target of {TARGET_PERCENT}%.",
        )
        notified["target"] = True

    # ── Unplugged ─────────────────────────────────────────────────────────────
    if last_state and last_state != "Disconnected" and cs["state"] == "Disconnected":
        send_pushover(
            "Tesla Unplugged",
            "Your Tesla has been disconnected from the charger.",
            emergency=False,
        )

    # ── Persist state for next run ────────────────────────────────────────────
    save_state(notified, cs["state"], session_start)


if __name__ == "__main__":
    main()
