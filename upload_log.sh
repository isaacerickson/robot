#!/bin/bash
# Upload a status snapshot to the "logs" branch only. Never touches main.
# Errors are appended to robot.log. This script always exits 0 so it cannot
# take down start_robot.sh.
# GH_LOG_TOKEN is never written to git config, a file we keep, or the log.
set +x
set +e

cd "$HOME/robot" 2>/dev/null || exit 0

if [ -f robot.env ]; then
  # shellcheck disable=SC1091
  . ./robot.env
fi

if [ -z "${GH_LOG_TOKEN:-}" ]; then
  exit 0
fi

# One upload at a time. The lock is released when this process exits.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$HOME/.robot_logs.lock"
  flock -n 9 || exit 0
fi

LOG="$HOME/robot/robot.log"
WORKDIR="$HOME/.robot_logs"

redact_stream() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line//${GH_LOG_TOKEN}/[REDACTED]}
    printf '%s\n' "$line" | sed -E 's#x-access-token:[^@[:space:]]+#x-access-token:[REDACTED]#g'
  done
}

scrub_workdir() {
  rm -f "$WORKDIR/.git/FETCH_HEAD" "$WORKDIR/.git/ORIG_HEAD" "$WORKDIR/.git/COMMIT_EDITMSG"
  rm -rf "$WORKDIR/.git/logs"
  local cfg="$WORKDIR/.git/config"
  if [ -f "$cfg" ] && grep -F -q -e "$GH_LOG_TOKEN" -e "x-access-token" "$cfg"; then
    grep -F -v -e "$GH_LOG_TOKEN" -e "x-access-token" "$cfg" > "$cfg.scrub" && mv "$cfg.scrub" "$cfg"
  fi
}

upload_status() {
  local auth_url="https://x-access-token:${GH_LOG_TOKEN}@github.com/isaacerickson/robot.git"
  local status tmp branch
  export GIT_TERMINAL_PROMPT=0
  export GIT_EDITOR=true

  mkdir -p "$WORKDIR"
  if [ ! -d "$WORKDIR/.git" ]; then
    # Init a scratch repo instead of cloning with the token URL. A clone would
    # store that URL, token included, in .git/config.
    local ls_out ls_status=0
    git init -q -b logs "$WORKDIR" || return 0
    if command -v timeout >/dev/null 2>&1; then
      ls_out=$(timeout 30 git -C "$WORKDIR" \
        -c credential.helper= \
        -c core.logAllRefUpdates=false \
        ls-remote -q --heads "$auth_url" refs/heads/logs 2>&1)
      ls_status=$?
    else
      ls_out=$(git -C "$WORKDIR" \
        -c credential.helper= \
        -c core.logAllRefUpdates=false \
        ls-remote -q --heads "$auth_url" refs/heads/logs 2>&1)
      ls_status=$?
    fi
    if [ "$ls_status" -ne 0 ]; then
      printf '%s\n' "$ls_out" >&2
    elif printf '%s\n' "$ls_out" | grep -q 'refs/heads/logs$'; then
      git -C "$WORKDIR" \
        -c credential.helper= \
        -c core.logAllRefUpdates=false \
        fetch -q --depth 1 "$auth_url" '+refs/heads/logs:refs/remotes/origin/logs' || true
      scrub_workdir
      if git -C "$WORKDIR" rev-parse --verify --quiet refs/remotes/origin/logs >/dev/null; then
        git -C "$WORKDIR" -c core.logAllRefUpdates=false \
          checkout -q -B logs refs/remotes/origin/logs || return 0
      fi
    fi
  fi
  scrub_workdir

  branch=$(git -C "$WORKDIR" symbolic-ref --short HEAD 2>/dev/null || echo "")
  if [ "$branch" != "logs" ]; then
    if git -C "$WORKDIR" rev-parse --verify --quiet refs/heads/logs >/dev/null; then
      git -C "$WORKDIR" checkout -q -f logs || return 0
    else
      git -C "$WORKDIR" checkout -q --orphan logs || return 0
    fi
  fi

  status="$WORKDIR/status.txt"
  {
    echo "Date: $(date)"
    echo "Host: $(hostname 2>/dev/null || echo unknown)"
    echo "Commit: $(git -C "$HOME/robot" log -1 --oneline 2>/dev/null || echo unknown)"
    echo "Uptime: $(uptime 2>/dev/null || echo unknown)"
    if command -v vcgencmd >/dev/null 2>&1; then
      echo "Temp: $(vcgencmd measure_temp 2>/dev/null || echo unavailable)"
    else
      echo "Temp: vcgencmd not available"
    fi
    echo "Video devices:"
    ls /dev/video* 2>/dev/null || echo "(none)"
    echo "robot_brain processes:"
    pgrep -af robot_brain || echo "(not running)"
    echo "--- robot.log (last 300 lines, filtered) ---"
    if [ -f "$LOG" ]; then
      tail -n 300 "$LOG" | grep -Ev 'ALSA|jack|Jack|Cannot connect to server|JackShm' || true
    else
      echo "(no robot.log yet)"
    fi
  } > "$status"

  tmp=$(mktemp)
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line//${GH_LOG_TOKEN}/[REDACTED]}
    printf '%s\n' "$line"
  done < "$status" | sed -E 's#x-access-token:[^@[:space:]]+#x-access-token:[REDACTED]#g' > "$tmp"
  mv "$tmp" "$status"

  git -C "$WORKDIR" add -- status.txt || return 0
  if git -C "$WORKDIR" rev-parse --verify --quiet HEAD >/dev/null; then
    git -C "$WORKDIR" \
      -c user.name='robot-pi' \
      -c user.email='robot@localhost' \
      -c credential.helper= \
      -c core.logAllRefUpdates=false \
      commit --amend -q -m "Robot status" --reset-author || return 0
  else
    git -C "$WORKDIR" \
      -c user.name='robot-pi' \
      -c user.email='robot@localhost' \
      -c credential.helper= \
      -c core.logAllRefUpdates=false \
      commit -q -m "Robot status" || return 0
  fi

  branch=$(git -C "$WORKDIR" symbolic-ref --short HEAD 2>/dev/null || echo "")
  if [ "$branch" != "logs" ]; then
    echo "upload_log: refusing to push from branch ${branch:-unknown}" >&2
    return 0
  fi

  if command -v timeout >/dev/null 2>&1; then
    timeout 90 git -C "$WORKDIR" \
      -c credential.helper= \
      -c core.logAllRefUpdates=false \
      push --force -q "$auth_url" HEAD:refs/heads/logs || true
  else
    git -C "$WORKDIR" \
      -c credential.helper= \
      -c core.logAllRefUpdates=false \
      push --force -q "$auth_url" HEAD:refs/heads/logs || true
  fi
  scrub_workdir
}

{
  upload_status
} 2>&1 | redact_stream >> "$LOG" || true

exit 0
