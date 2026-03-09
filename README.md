# Tesla Charge Alarm

A macOS menu bar app that monitors your Tesla's charging state and sends loud Pushover emergency alarms to your iPhone when:

- The battery reaches your target percentage (default 80%)
- A set time has elapsed since charging started (default 4 hours)

Designed for use with public charge points — alerts you to stop charging or move your car without having to watch the Tesla app.

## How it works

- Shows live battery % and charging state in the macOS menu bar (⚡ 73%)
- Polls your Tesla every 5 minutes via the Tesla API
- Sends a **Pushover Emergency** alarm (siren sound, repeats every 30 seconds until acknowledged) when a trigger fires
- Resets automatically when a new charging session begins — so it won't spam you if the car stays plugged in at 80% all night
- Persists state to disk so triggers aren't missed or double-fired after a crash/restart

## Prerequisites

- macOS
- Python 3.9+
- A Tesla account
- [Pushover](https://pushover.net) account + the Pushover iOS app ($5 one-time)

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/yourname/TeslaChargeAlarm.git
cd TeslaChargeAlarm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Pushover

1. Create a free account at [pushover.net](https://pushover.net)
2. Note your **User Key** on the dashboard
3. Create an application at [pushover.net/apps/build](https://pushover.net/apps/build) — name it anything (e.g. "Tesla Alarm")
4. Note the **API Token** for the new app
5. Install the **Pushover app** on your iPhone and log in
6. In iPhone Settings → Pushover → Notifications → enable **Critical Alerts** (required for sound to play when phone is on silent)

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your details:

```env
TESLA_EMAIL=you@example.com
PUSHOVER_TOKEN=your_pushover_app_token   # from pushover.net/apps
PUSHOVER_USER=your_pushover_user_key     # from pushover.net dashboard

POLL_INTERVAL=300    # seconds between checks (default 5 min)
TARGET_PERCENT=80    # alarm when battery hits this %
TIMER_MINUTES=240    # alarm after this many minutes of charging (240 = 4 hours)
```

### 4. Run

```bash
source .venv/bin/activate
python charge_alarm.py
```

On first run, a browser will open for Tesla OAuth login. After that, the token is cached in `.tesla_token.json` and no login is needed on subsequent runs.

You should see a menu bar icon (⚡ —) appear in the top right of your screen.

## Menu bar

Click the icon to see:

| Item | Description |
|---|---|
| Status | Charging / Complete / Stopped / Disconnected |
| Battery | Current % and charge limit |
| Timer | Time remaining until the 4-hour alarm |
| Updated | Time of last poll and ETA to full |

Icons:
- ⚡ Charging
- ✅ Complete
- ⏹ Stopped (charging halted from app)
- 🔌 Disconnected

## Run automatically at login (launchd)

Create the launchd service file:

```bash
cat > ~/Library/LaunchAgents/com.user.tesla-charge-alarm.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.tesla-charge-alarm</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(pwd)/.venv/bin/python</string>
        <string>$(pwd)/charge_alarm.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(pwd)</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$(pwd)/alarm.log</string>
    <key>StandardErrorPath</key>
    <string>$(pwd)/alarm.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.user.tesla-charge-alarm.plist
```

### Useful launchd commands

```bash
# Check it's running (should show a PID)
launchctl list | grep tesla

# Watch the log
tail -f alarm.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.user.tesla-charge-alarm.plist

# Restart after changes
launchctl unload ~/Library/LaunchAgents/com.user.tesla-charge-alarm.plist
launchctl load ~/Library/LaunchAgents/com.user.tesla-charge-alarm.plist
```

## Files

| File | Description |
|---|---|
| `charge_alarm.py` | Main script |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |
| `.env` | Your secrets (git-ignored) |
| `.tesla_token.json` | Cached Tesla OAuth token (git-ignored) |
| `.charge_state.json` | Session state for crash recovery (git-ignored) |
| `alarm.log` | Log output when running via launchd (git-ignored) |
