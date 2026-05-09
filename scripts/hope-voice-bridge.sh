#!/usr/bin/env bash
# hope-voice-bridge.sh — simple process manager for the voice-out consumer.
#
# Usage:
#   hope-voice-bridge.sh start   # background via nohup, writes PID + logs
#   hope-voice-bridge.sh stop    # SIGTERM the tracked PID
#   hope-voice-bridge.sh status  # report liveness
#   hope-voice-bridge.sh restart
#   hope-voice-bridge.sh run     # foreground (for debugging, launchd)
#
# Use this script as the "lightweight alternative" to the launchd plist.
# For auto-start on login, load deploy/launchd/com.hope.voice-bridge.plist
# with `launchctl load -w ...`.
set -euo pipefail

HOPE_ROOT="/Users/joelc/Documents/Github/Hope"
LOG_DIR="${HOPE_ROOT}/logs"
PID_FILE="${HOPE_ROOT}/.hope-io/tts-consumer.pid"
LOG_FILE="${LOG_DIR}/voice-bridge.log"
ERR_FILE="${LOG_DIR}/voice-bridge.err"

mkdir -p "$LOG_DIR" "$(dirname "$PID_FILE")"

PY="${HOPE_ROOT}/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

export PYTHONPATH="${HOPE_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

cmd_run() {
  exec "$PY" -m voice_bridge.tts_consumer "$@"
}

cmd_start() {
  if cmd_status >/dev/null 2>&1; then
    echo "hope-voice-bridge: already running (PID $(cat "$PID_FILE"))"
    return 0
  fi
  nohup "$PY" -m voice_bridge.tts_consumer \
    >>"$LOG_FILE" 2>>"$ERR_FILE" &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  disown "$pid" 2>/dev/null || true
  sleep 0.3
  if kill -0 "$pid" 2>/dev/null; then
    echo "hope-voice-bridge: started (PID $pid)"
  else
    echo "hope-voice-bridge: failed to start; tail $ERR_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

cmd_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "hope-voice-bridge: no pid file at $PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    # Wait up to 5s for graceful exit
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      sleep 0.5
      kill -0 "$pid" 2>/dev/null || break
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "hope-voice-bridge: stopped (was PID $pid)"
  else
    echo "hope-voice-bridge: stale pid file; process $pid not running"
  fi
  rm -f "$PID_FILE"
}

cmd_status() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "hope-voice-bridge: running (PID $pid)"
      return 0
    fi
    echo "hope-voice-bridge: stale pid file ($pid not running)"
    return 1
  fi
  echo "hope-voice-bridge: not running"
  return 1
}

cmd_restart() {
  cmd_stop || true
  cmd_start
}

case "${1:-status}" in
  run) shift; cmd_run "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  restart) cmd_restart ;;
  *)
    echo "Usage: $0 {run|start|stop|status|restart}" >&2
    exit 2
    ;;
esac
