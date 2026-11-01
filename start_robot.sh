#!/bin/bash
# Launches the robot at login, waits for Wi-Fi, restarts it if it crashes,
# and checks for updates (at boot and every 5 minutes) when ~/robot is linked
# to an online repo. Updates are pulled and the robot restarts on its own.
cd "$HOME/robot"
[ -f robot.env ] && set -a && . ./robot.env && set +a
source venv/bin/activate

for i in $(seq 1 30); do                       # wait up to 60 s for internet
  ping -c1 -W2 api.x.ai >/dev/null 2>&1 && break
  sleep 2
done

update() {
  [ -d .git ] || return 1
  before=$(git rev-parse HEAD)
  git pull --ff-only -q >> robot.log 2>&1 || return 1
  [ "$before" != "$(git rev-parse HEAD)" ]
}
update && echo "$(date) updated at boot" >> robot.log

while true; do
  python3 robot_brain.py >> robot.log 2>&1 &
  PID=$!
  while kill -0 $PID 2>/dev/null; do
    sleep 300
    if update; then
      echo "$(date) new version found - restarting robot" >> robot.log
      kill $PID; wait $PID 2>/dev/null
      break
    fi
  done
  sleep 3
done
