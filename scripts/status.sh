#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_URL="${APP_URL:-https://app.34jarvis.uk}"
LEGACY_URL="${LEGACY_URL:-https://jarvis.34jarvis.uk}"
PWA_HEALTH_URL="${PWA_HEALTH_URL:-http://localhost:8765/api/health}"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:18789}"
PWA_PLIST="${PWA_PLIST:-${HOME}/Library/LaunchAgents/com.34dev.jarvis-pwa.plist}"
CLOUDFLARED_PLIST="${CLOUDFLARED_PLIST:-/Library/LaunchDaemons/com.cloudflare.cloudflared.plist}"
FAILURES=0

line() {
  printf '%-32s %s\n' "$1" "$2"
}

fail() {
  line "$1" "❌ $2"
  if [[ -n "${3:-}" ]]; then
    printf '  remediation: %s\n' "$3"
  fi
  FAILURES=$((FAILURES + 1))
}

ok() {
  line "$1" "✅ $2"
}

warn() {
  line "$1" "⚠️  $2"
}

pid_for_port() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

pid_for_pattern() {
  local pattern="$1"
  pgrep -f "$pattern" 2>/dev/null | head -1 || true
}

uptime_for_pid() {
  local pid="$1"
  ps -p "$pid" -o etime= 2>/dev/null | xargs || echo "unknown"
}

lstart_for_pid() {
  local pid="$1"
  ps -p "$pid" -o lstart= 2>/dev/null | xargs || echo "unknown"
}

http_status_time() {
  local url="$1"
  curl -L -sS -o /dev/null -w "%{http_code} %{time_total}" --max-time 10 "$url" 2>/dev/null || echo "000 0"
}

check_public_url() {
  local label="$1"
  local url="$2"
  local result status seconds ms
  result="$(http_status_time "$url")"
  status="${result%% *}"
  seconds="${result##* }"
  ms="$(python3 - "$seconds" <<'PY'
import sys
try:
    print(int(float(sys.argv[1]) * 1000))
except Exception:
    print(0)
PY
)"
  if [[ "$status" == "200" ]]; then
    ok "$label" "200 (${ms}ms)"
  else
    fail "$label" "${status:-curl failed}" "If local backend is healthy, restart cloudflared: sudo launchctl kickstart -k system/com.cloudflare.cloudflared"
  fi
}

count_recent_errors() {
  python3 - "$ROOT" <<'PY'
from datetime import datetime, timedelta
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
files = [
    root / "data" / "server.err.log",
    root / "data" / "server.log",
    root / "data" / "cloudflared-health.log",
    root / "data" / "jarvis-pwa-health.log",
    Path("/var/log/cloudflared.err.log"),
    Path("/var/log/cloudflared.log"),
    Path("/Library/Logs/com.cloudflare.cloudflared.err.log"),
    Path("/Library/Logs/com.cloudflare.cloudflared.out.log"),
]
cutoff = datetime.now() - timedelta(hours=1)
pattern = re.compile(r"(error|exception|traceback|failed|unauthorized| 5\d\d )", re.I)
count = 0
for path in files:
    try:
        if not path.exists() or datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            continue
        for line in path.read_text(errors="ignore").splitlines()[-1000:]:
            if pattern.search(line):
                count += 1
    except Exception:
        pass
print(count)
PY
}

cloudflared_log_files() {
  printf '%s\n' \
    /var/log/cloudflared.log \
    /var/log/cloudflared.err.log \
    /Library/Logs/com.cloudflare.cloudflared.out.log \
    /Library/Logs/com.cloudflare.cloudflared.err.log
}

cloudflared_connections() {
  local count
  count="$(
    while IFS= read -r file; do
      [[ -r "$file" ]] && grep -hEi "registered tunnel connection|connection.*registered|connected to" "$file" || true
    done < <(cloudflared_log_files) | tail -20 | wc -l | xargs
  )"
  echo "${count:-0}"
}

cloudflared_last_reconnect() {
  local line
  line="$(
    while IFS= read -r file; do
      [[ -r "$file" ]] && grep -hEi "registered tunnel connection|connection.*registered|connected to|reconnect" "$file" || true
    done < <(cloudflared_log_files) | tail -1
  )"
  [[ -n "$line" ]] && echo "$line" | cut -c1-120 || echo "unknown"
}

plist_value() {
  local plist="$1"
  local key="$2"
  /usr/libexec/PlistBuddy -c "Print :${key}" "$plist" 2>/dev/null | tr '\n' ' ' | xargs || true
}

echo "[STATUS] Mac Mini Health Check - $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "─────────────────────────────────────────"

pwa_status="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$PWA_HEALTH_URL" 2>/dev/null || true)"
pwa_pid="$(pid_for_port 8765)"
if [[ "$pwa_status" == "200" && -n "$pwa_pid" ]]; then
  ok "PWA Backend (port 8765):" "Running (PID ${pwa_pid}, uptime $(uptime_for_pid "$pwa_pid"), last restart: $(lstart_for_pid "$pwa_pid"))"
else
  fail "PWA Backend (port 8765):" "Down or unhealthy (status ${pwa_status:-curl failed})" "launchctl kickstart -k gui/$(id -u)/com.34dev.jarvis-pwa; tail -80 ${ROOT}/data/server.err.log"
fi

gateway_pid="$(pid_for_port 18789)"
if [[ -n "$gateway_pid" ]]; then
  ok "OpenClaw Gateway (18789):" "Running (PID ${gateway_pid}, uptime $(uptime_for_pid "$gateway_pid"))"
else
  gateway_pid="$(pid_for_pattern 'openclaw-gateway')"
  if [[ -n "$gateway_pid" ]]; then
    ok "OpenClaw Gateway (18789):" "Process alive (PID ${gateway_pid}, uptime $(uptime_for_pid "$gateway_pid"))"
  else
    fail "OpenClaw Gateway (18789):" "Not listening" "Restart OpenClaw gateway before testing Jarvis chat/voice relay."
  fi
fi

cloudflared_pid="$(pid_for_pattern '[c]loudflared.*tunnel run')"
if [[ -n "$cloudflared_pid" ]]; then
  ok "Cloudflared Tunnel:" "Connected/probed (PID ${cloudflared_pid}, uptime $(uptime_for_pid "$cloudflared_pid"), recent connection log entries: $(cloudflared_connections), last reconnect: $(cloudflared_last_reconnect))"
else
  fail "Cloudflared Tunnel:" "No cloudflared tunnel process" "sudo launchctl kickstart -k system/com.cloudflare.cloudflared"
fi

check_public_url "Public URL (app.34jarvis.uk):" "$APP_URL"
check_public_url "Public URL (jarvis.34jarvis.uk):" "$LEGACY_URL"

disk_line="$(df -k / | awk 'NR==2 { avail_gb=int($4/1024/1024); free=100-$5; printf "%s%% free (%sGB available)", free, avail_gb }')"
disk_free="$(df -k / | awk 'NR==2 { print 100-$5 }')"
if [[ "$disk_free" -ge 10 ]]; then
  ok "Disk space:" "$disk_line"
else
  fail "Disk space:" "$disk_line" "Free disk space: delete old logs or prune data/audio_cache after backup."
fi

last_deploy="$(git -C "$ROOT" log -1 --pretty='%h (%cr)' 2>/dev/null || echo unknown)"
ok "Last deploy:" "$last_deploy"

recent_errors="$(count_recent_errors)"
if [[ "$recent_errors" == "0" ]]; then
  ok "Recent errors (last 1hr):" "0"
else
  fail "Recent errors (last 1hr):" "$recent_errors" "Inspect recent logs: tail -120 ${ROOT}/data/server.err.log; tail -120 /var/log/cloudflared.err.log"
fi

pwa_keepalive="$(plist_value "$PWA_PLIST" KeepAlive)"
pwa_throttle="$(plist_value "$PWA_PLIST" ThrottleInterval)"
cloud_keepalive="$(plist_value "$CLOUDFLARED_PLIST" KeepAlive)"
cloud_throttle="$(plist_value "$CLOUDFLARED_PLIST" ThrottleInterval)"
if [[ "$pwa_keepalive" == "true" && -n "$pwa_throttle" ]]; then
  ok "PWA launchd recovery:" "KeepAlive=${pwa_keepalive}, ThrottleInterval=${pwa_throttle}"
else
  fail "PWA launchd recovery:" "plist drift" "Run ./scripts/harden-launchd-plists.sh"
fi
if [[ "$cloud_keepalive" == "true" && "$cloud_throttle" == "10" ]]; then
  ok "Cloudflared launchd recovery:" "KeepAlive=${cloud_keepalive}, ThrottleInterval=${cloud_throttle}"
else
  fail "Cloudflared launchd recovery:" "plist drift (KeepAlive=${cloud_keepalive:-missing}, ThrottleInterval=${cloud_throttle:-missing})" "Run ./scripts/harden-launchd-plists.sh, then reload cloudflared launch daemon."
fi

exit "$FAILURES"
