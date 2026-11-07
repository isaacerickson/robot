#!/bin/bash
# Launches the robot at login, waits for Wi-Fi, restarts it if it crashes,
# and checks for updates (at boot and every 5 minutes) when ~/robot is linked
# to an online repo. Updates are pulled and the robot restarts on its own.
export PYTHONUNBUFFERED=1
cd "$HOME/robot"
[ -f robot.env ] && set -a && . ./robot.env && set +a
source venv/bin/activate

# Wait until https://api.x.ai answers. Do not ICMP-ping it: that host
# accepts HTTPS and drops ping, which used to burn this whole loop
# (about two minutes) on every launch. Any HTTP status counts. The bare
# URL returns 421, and curl -f would treat that as failure.
for _ in $(seq 1 12); do                       # about a minute if still offline
  code=$(curl -sS --max-time 3 -o /dev/null -w "%{http_code}" https://api.x.ai 2>/dev/null || true)
  case "$code" in
    ""|000) sleep 2 ;;
    *) break ;;
  esac
done

update() {
  [ -d .git ] || return 1
  before=$(git rev-parse HEAD)
  git pull --ff-only -q >> robot.log 2>&1 || return 1
  [ "$before" != "$(git rev-parse HEAD)" ]
}
update && echo "$(date) updated at boot" >> robot.log

(sleep 60; bash upload_log.sh) &

while true; do
  python3 -u robot_brain.py >> robot.log 2>&1 &
  PID=$!
  while kill -0 $PID 2>/dev/null; do
    sleep 300
    bash upload_log.sh &
    if update; then
      echo "$(date) new version found - restarting robot" >> robot.log
      kill $PID; wait $PID 2>/dev/null
      break
    fi
  done
  sleep 3
done
