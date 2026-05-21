#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://localhost:8765/api/health}"
STATE_DIR="${JARVIS_HEALTH_STATE_DIR:-/tmp/jarvis-health}"
LOG_FILE="${JARVIS_PWA_HEALTH_LOG:-${ROOT}/data/jarvis-pwa-health.log}"
SERVICE_LABEL="${PWA_SERVICE_LABEL:-gui/$(id -u)/com.34dev.jarvis-pwa}"
MAX_FAILURES="${MAX_FAILURES:-3}"
LOOP=0

if [[ "${1:-}" == "--loop" ]]; then
  LOOP=1
fi

mkdir -p "$STATE_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
FAIL_FILE="${STATE_DIR}/jarvis-pwa.failures"

log() {
  printf '%s [jarvis-pwa-health] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"
}

failure_count() {
  [[ -f "$FAIL_FILE" ]] && cat "$FAIL_FILE" || echo 0
}

set_failure_count() {
  printf '%s\n' "$1" > "$FAIL_FILE"
}

check_once() {
  local status
  status="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "$LOCAL_HEALTH_URL" 2>/dev/null || true)"
  if [[ "$status" == "200" ]]; then
    set_failure_count 0
    log "OK ${LOCAL_HEALTH_URL} returned 200."
    return 0
  fi

  local failures
  failures="$(( $(failure_count) + 1 ))"
  set_failure_count "$failures"
  log "FAIL ${LOCAL_HEALTH_URL} returned ${status:-curl failed}; consecutive failures ${failures}/${MAX_FAILURES}."

  if [[ "$failures" -ge "$MAX_FAILURES" ]]; then
    log "Restarting Jarvis PWA via launchctl kickstart -k ${SERVICE_LABEL}."
    if launchctl kickstart -k "$SERVICE_LABEL"; then
      set_failure_count 0
      log "Restart command completed."
    else
      log "Restart command failed. Run manually: launchctl kickstart -k ${SERVICE_LABEL}"
      return 1
    fi
  fi
  return 1
}

if [[ "$LOOP" -eq 1 ]]; then
  while true; do
    check_once || true
    sleep 120
  done
else
  check_once
fi
