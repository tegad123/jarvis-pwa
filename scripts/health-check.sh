#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOOP=0

if [[ "${1:-}" == "--loop" ]]; then
  LOOP=1
fi

run_once() {
  "${ROOT}/scripts/jarvis-pwa-health.sh" || true
  "${ROOT}/scripts/cloudflared-health.sh" || true
}

if [[ "$LOOP" -eq 1 ]]; then
  while true; do
    run_once
    sleep 120
  done
else
  run_once
fi
