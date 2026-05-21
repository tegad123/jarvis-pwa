#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRON_LOG="${CRON_LOG:-${ROOT}/data/health-cron.log}"

mkdir -p "$(dirname "$CRON_LOG")"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

crontab -l 2>/dev/null \
  | grep -vF "${ROOT}/scripts/cloudflared-health.sh" \
  | grep -vF "${ROOT}/scripts/health-check.sh" \
  > "$TMP" || true

cat >> "$TMP" <<EOF
* * * * * cd ${ROOT} && ./scripts/cloudflared-health.sh >> ${CRON_LOG} 2>&1
*/2 * * * * cd ${ROOT} && ./scripts/health-check.sh >> ${CRON_LOG} 2>&1
EOF

crontab "$TMP"
echo "[cron] Installed Jarvis health checks:"
echo "  every 1 min: cloudflared public tunnel health"
echo "  every 2 min: combined PWA + cloudflared health"
echo "  log: ${CRON_LOG}"
