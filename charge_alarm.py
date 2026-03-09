#!/usr/bin/env python3
"""
Tesla Charge Alarm — Menu Bar Edition
Shows battery % in the macOS menu bar and sends Pushover emergency alarms.

SETUP:
  1. pip install -r requirements.txt
  2. Install Pushover from the iOS App Store
  3. python charge_alarm.py
  4. A browser will open for Tesla login (one-time, token is saved)
"""

import sys
import time
import json
import os
import threading
import teslapy
import requests
import rumps
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

TESLA_EMAIL    = os.environ["TESLA_EMAIL"]
PUSHOVER_TOKEN = os.environ["PUSHOVER_TOKEN"]
PUSHOVER_USER  = os.environ["PUSHOVER_USER"]

# How often to check (seconds). 300 = every 5 min. Don't go below 60.
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))

# ── Triggers (set to None to disable) ────────────────────────────────────────

# Notify when battery reaches this percentage while charging
TARGET_PERCENT = int(os.getenv("TARGET_PERCENT", "80"))

# Notify after this many minutes into a charging session
TIMER_MINUTES = int(os.getenv("TIMER_MINUTES", "240"))

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

    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            timeout=10,
        )
        if not r.ok:
            print(f"  [{ts()}] Pushover error {r.status_code}: {r.text}")
            return
        print(f"  [{ts()}] Alarm sent: {title}")
    except Exception as e:
        print(f"  [{ts()}] Warning — notification failed: {e}")


def get_charge_state(vehicle):
    data = vehicle.get_vehicle_data()
    cs = data["charge_state"]
    return {
        "state":   cs["charging_state"],
        "level":   cs["battery_level"],
        "limit":   cs["charge_limit_soc"],
        "eta_min": cs["minutes_to_full_charge"],
    }


# ─── Menu Bar App ─────────────────────────────────────────────────────────────

class TeslaAlarmApp(rumps.App):
    STATE_ICONS = {
        "Charging":     "⚡",
        "Complete":     "✅",
        "Stopped":      "⏹",
        "Disconnected": "🔌",
    }

    def __init__(self):
        super().__init__("⚡ —", quit_button=None)
        self.status_item  = rumps.MenuItem("Status: starting…")
        self.battery_item = rumps.MenuItem("Battery: —")
        self.timer_item   = rumps.MenuItem("Timer: —")
        self.updated_item = rumps.MenuItem("Updated: —")
        self.menu = [
            self.status_item,
            self.battery_item,
            self.timer_item,
            self.updated_item,
            None,
            rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application()),
        ]

    def update(self, state, level, limit, eta_min, timer_end):
        icon = self.STATE_ICONS.get(state, "🚗")
        self.title = f"{icon} {level}%"
        self.status_item.title  = f"Status: {state}"
        self.battery_item.title = f"Battery: {level}%  (limit {limit}%)"

        if timer_end:
            remaining = (timer_end - datetime.now()).total_seconds()
            if remaining > 0:
                h, m = divmod(int(remaining / 60), 60)
                self.timer_item.title = f"Timer: {h}h {m:02d}m remaining"
            else:
                self.timer_item.title = "Timer: elapsed"
        else:
            self.timer_item.title = "Timer: waiting for charge session"

        eta = f"  ({eta_min}m to full)" if eta_min else ""
        self.updated_item.title = f"Updated: {ts()}{eta}"

    def set_error(self, msg):
        self.title = "⚡ ?"
        self.status_item.title = f"Status: {msg}"


# ─── Polling loop (background thread) ─────────────────────────────────────────

def poll_loop(app):
    state         = load_state()
    notified      = state["notified"]
    last_state    = state["last_state"]
    session_start = state["session_start"]
    timer_end     = (
        session_start + timedelta(minutes=TIMER_MINUTES)
        if session_start and TIMER_MINUTES else None
    )

    if session_start:
        print(f"Resumed session from {session_start.strftime('%H:%M')} — notified={notified}")

    with teslapy.Tesla(TESLA_EMAIL, cache_file=TOKEN_CACHE) as tesla:
        tesla.fetch_token()

        vehicles = tesla.vehicle_list()
        if not vehicles:
            print("No vehicles found.")
            sys.exit(1)
        vehicle = vehicles[0]
        print(f"Monitoring: {vehicle['display_name']}")

        while True:
            try:
                vehicle.sync_wake_up()
                cs = get_charge_state(vehicle)

                eta = f"{cs['eta_min']} min left" if cs["eta_min"] else "done"
                print(f"[{ts()}]  {cs['state']:<13}  {cs['level']:3d}%  (limit {cs['limit']}%)  {eta}")

                app.update(cs["state"], cs["level"], cs["limit"], cs["eta_min"], timer_end)

                # ── New charging session ───────────────────────────────────
                if cs["state"] == "Charging" and last_state != "Charging":
                    notified      = {"complete": False, "target": False, "timer": False}
                    session_start = datetime.now()
                    if TIMER_MINUTES:
                        timer_end = session_start + timedelta(minutes=TIMER_MINUTES)
                        print(f"  New session — timer set for {timer_end.strftime('%H:%M')}")

                # ── Timer ─────────────────────────────────────────────────
                if timer_end and not notified["timer"] and datetime.now() >= timer_end:
                    send_pushover(
                        f"Tesla — {TIMER_MINUTES // 60}hr Timer",
                        f"Your {TIMER_MINUTES}-minute charge timer has elapsed!",
                    )
                    notified["timer"] = True

                # ── Charging complete ─────────────────────────────────────
                if cs["state"] == "Complete" and not notified["complete"]:
                    send_pushover(
                        "Tesla Fully Charged",
                        f"Battery at {cs['level']}% — charging complete!",
                    )
                    notified["complete"] = True

                # ── Target % reached ──────────────────────────────────────
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

                # ── Unplugged (informational) ─────────────────────────────
                if last_state and last_state != "Disconnected" and cs["state"] == "Disconnected":
                    send_pushover(
                        "Tesla Unplugged",
                        "Your Tesla has been disconnected from the charger.",
                        emergency=False,
                    )

                last_state = cs["state"]
                save_state(notified, last_state, session_start)

            except teslapy.VehicleError as e:
                print(f"[{ts()}]  Vehicle unavailable: {e}")
                app.set_error("unavailable")
            except Exception as e:
                print(f"[{ts()}]  Error: {e}")
                app.set_error("error")

            time.sleep(POLL_INTERVAL)


def main():
    app = TeslaAlarmApp()
    t = threading.Thread(target=poll_loop, args=(app,), daemon=True)
    t.start()
    app.run()


if __name__ == "__main__":
    main()
