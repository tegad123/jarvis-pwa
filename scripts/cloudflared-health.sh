#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL:-https://app.34jarvis.uk/api/health}"
STATE_DIR="${JARVIS_HEALTH_STATE_DIR:-/tmp/jarvis-health}"
LOG_FILE="${JARVIS_CLOUDFLARED_HEALTH_LOG:-${ROOT}/data/cloudflared-health.log}"
SERVICE_LABEL="${CLOUDFLARED_SERVICE_LABEL:-system/com.cloudflare.cloudflared}"
MAX_FAILURES="${MAX_FAILURES:-3}"
LOOP=0

if [[ "${1:-}" == "--loop" ]]; then
  LOOP=1
fi

mkdir -p "$STATE_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
FAIL_FILE="${STATE_DIR}/cloudflared.failures"

log() {
  printf '%s [cloudflared-health] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"
}

failure_count() {
  [[ -f "$FAIL_FILE" ]] && cat "$FAIL_FILE" || echo 0
}

set_failure_count() {
  printf '%s\n' "$1" > "$FAIL_FILE"
}

check_once() {
  local status
  status="$(curl -L -sS -o /dev/null -w "%{http_code}" --max-time 10 "$PUBLIC_HEALTH_URL" 2>/dev/null || true)"
  if [[ "$status" == "200" ]]; then
    set_failure_count 0
    log "OK ${PUBLIC_HEALTH_URL} returned 200."
    return 0
  fi

  local failures
  failures="$(( $(failure_count) + 1 ))"
  set_failure_count "$failures"
  log "FAIL ${PUBLIC_HEALTH_URL} returned ${status:-curl failed}; consecutive failures ${failures}/${MAX_FAILURES}."

  if [[ "$failures" -ge "$MAX_FAILURES" ]]; then
    log "Restarting cloudflared via sudo launchctl kickstart -k ${SERVICE_LABEL}."
    if sudo launchctl kickstart -k "$SERVICE_LABEL"; then
      set_failure_count 0
      log "Restart command completed."
    else
      log "Restart command failed. Run manually: sudo launchctl kickstart -k ${SERVICE_LABEL}"
      return 1
    fi
  fi
  return 1
}

if [[ "$LOOP" -eq 1 ]]; then
  while true; do
    check_once || true
    sleep 60
  done
else
  check_once
fi
