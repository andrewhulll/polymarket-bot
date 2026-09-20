# Always-on supervision

The headless capture process (`scripts/capture_live_rfqs.py`) is the only
writer of `data/live/rfq_capture.db`. The dashboard is a read-only viewer.
This doc covers keeping the writer alive: OS-level supervision
(launchd on macOS, systemd on Linux), the dead-man's heartbeat checker, and
what to do when the heartbeat goes stale.

## How the pieces fit

| Piece | What it does | Where |
|---|---|---|
| Capture process | Listens to the RFQ gateway, prices quotable RFQs, writes heartbeats to `live_engine_health` every poll | `scripts/capture_live_rfqs.py` |
| Process lock | `data/live/rfq_capture.lock` — held by exactly one capture process; released by the OS when the owner dies | `combo_mm/capture_process.py` |
| Supervisor | Restarts the capture when it exits (crash, kill, reboot) — even with the dashboard closed | `deploy/` units below |
| Dead-man's checker | Exits non-zero when the heartbeat is older than 10 min (or never written) | `scripts/check_heartbeat.py` |
| Dashboard banner | Engine tab warns when the heartbeat is stale | `dashboard/server.py`, `dashboard/static/js/engine.js` |

The dashboard does not own the capture process. The supervisor keeps capture
running whether or not a browser or dashboard server is open.

## Credentials

The units contain **no secrets**. The capture reads `POLYMARKET_API_KEY`,
`POLYMARKET_SECRET`, `POLYMARKET_PASSPHRASE`, `POLYMARKET_ADDRESS` from the
environment, falling back to the gitignored repo-root `.env` (see
`GatewayCredentials.from_env`). Create that file once:

```bash
cd ~/polymarket-bot   # your checkout
cat > .env <<'EOF'
POLYMARKET_API_KEY=...
POLYMARKET_SECRET=...
POLYMARKET_PASSPHRASE=...
POLYMARKET_ADDRESS=...
EOF
chmod 600 .env
```

`.env` is gitignored — it never leaves the machine. Log output
(`data/live/capture.log`, journald, `heartbeat-check.log`) carries only
counters and timestamps: no RFQ message bodies, no credentials.

## macOS (launchd)

The unit files use a `__REPO_DIR__` placeholder. Replace it with your
checkout path first:

```bash
cd ~/polymarket-bot/deploy
sed -i '' "s|__REPO_DIR__|$HOME/polymarket-bot|g" com.polymarket-bot.*.plist
```

Install:

```bash
cp com.polymarket-bot.capture.plist com.polymarket-bot.heartbeat-check.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.polymarket-bot.capture.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.polymarket-bot.heartbeat-check.plist
```

(`launchctl load` works on older macOS in place of `bootstrap`.)

Check status:

```bash
launchctl print gui/$(id -u)/com.polymarket-bot.capture | grep -E 'state|pid'
python3 scripts/check_heartbeat.py
tail -5 data/live/capture.log
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.polymarket-bot.capture
launchctl bootout gui/$(id -u)/com.polymarket-bot.heartbeat-check
rm ~/Library/LaunchAgents/com.polymarket-bot.capture.plist \
   ~/Library/LaunchAgents/com.polymarket-bot.heartbeat-check.plist
```

`KeepAlive` restarts the capture on crash/kill; `RunAtLoad` starts it at
login. If your Python dependencies live under a Homebrew or venv
interpreter, point the plist's `ProgramArguments` at that interpreter
instead of `/usr/bin/env python3`.

## Linux (systemd, user units)

```bash
cd ~/polymarket-bot/deploy
sed -i "s|__REPO_DIR__|$HOME/polymarket-bot|g" polymarket-bot-*.service polymarket-bot-*.timer
mkdir -p ~/.config/systemd/user
cp polymarket-bot-capture.service polymarket-bot-heartbeat-check.service \
   polymarket-bot-heartbeat-check.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now polymarket-bot-capture.service
systemctl --user enable --now polymarket-bot-heartbeat-check.timer
```

Check status:

```bash
systemctl --user status polymarket-bot-capture.service
systemctl --user list-timers polymarket-bot-heartbeat-check.timer
python3 scripts/check_heartbeat.py
journalctl --user -u polymarket-bot-capture.service -n 20
```

Uninstall:

```bash
systemctl --user disable --now polymarket-bot-capture.service
systemctl --user disable --now polymarket-bot-heartbeat-check.timer polymarket-bot-heartbeat-check.service
rm ~/.config/systemd/user/polymarket-bot-capture.service \
   ~/.config/systemd/user/polymarket-bot-heartbeat-check.service \
   ~/.config/systemd/user/polymarket-bot-heartbeat-check.timer
```

`Restart=always` + `RestartSec=10` brings the capture back after a crash or
kill. Enable lingering (`loginctl enable-linger $USER`) if the machine
should keep it running with no session open.

## What "stale" means

`scripts/check_heartbeat.py` (and the dashboard banner) treat the capture
as stale when `live_engine_health.heartbeat_at` is older than **10 minutes**
(the default; pass `--max-age-minutes N` to change it) or when no heartbeat
was ever written. Exit codes: `0` = fresh, `2` = stale or missing.

A stale heartbeat with a **dead process** is the supervisor's job — check
the service state above; it should already be restarting. A stale heartbeat
with a **live process** means the loop is wedged (e.g. stuck in the gateway
thread): restart the service and read the tail of the log. On a laptop,
sleep suspends the process — a stale heartbeat after waking is expected;
the supervisor restarts it on wake.

## Verifying end to end

1. Install per your platform above.
2. `python3 scripts/check_heartbeat.py` → `OK: last heartbeat …s ago`.
3. Kill the capture (`pkill -f capture_live_rfqs.py`) with the dashboard
   closed; within ~10 s the supervisor restarts it and the heartbeat goes
   fresh again — no dashboard involvement.
4. Open the dashboard: the Engine-status tab shows uptime and no stale
   banner. Stop the service and wait 10 min (or temporarily pass a tiny
   `--max-age-minutes` to the checker) to see the banner appear.
