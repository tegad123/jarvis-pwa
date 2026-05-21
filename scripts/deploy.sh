#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_URL="${PUBLIC_URL:-https://app.34jarvis.uk}"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://localhost:8765/api/health}"
SERVICE_LABEL="${SERVICE_LABEL:-com.34dev.jarvis-pwa}"
PUSH=1
RESTART=1
VERIFY=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-push) PUSH=0 ;;
    --no-restart) RESTART=0 ;;
    --no-verify) VERIFY=0 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

cd "$ROOT"

log() {
  echo "[deploy] $*"
}

verify_auth_status_json() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data, dict) and "authenticated" in data else 1)
'
}

if ! git diff --quiet || ! git diff --cached --quiet; then
  log "Refusing deploy: tracked working tree changes are present." >&2
  git status --short
  exit 1
fi

git fetch origin
git checkout main
git pull --ff-only origin main

HEAD_SUBJECT="$(git log -1 --pretty=%s)"
if [[ "$HEAD_SUBJECT" =~ ^Deploy:\ cache-bust\ assets\ for\ ([0-9a-fA-F]{7,40})$ ]]; then
  ASSET_VERSION="${BASH_REMATCH[1]}"
  log "Current HEAD is already a deploy cache-bust commit for ${ASSET_VERSION}."
else
  ASSET_VERSION="$(git rev-parse --short HEAD)"
fi

./scripts/cache-bust.sh "$ASSET_VERSION"

if ! git diff --quiet -- frontend/index.html frontend/styles.css frontend/sw.js; then
  git add frontend/index.html frontend/styles.css frontend/sw.js
  git commit -m "Deploy: cache-bust assets for ${ASSET_VERSION}"
  if [[ "$PUSH" -eq 1 ]]; then
    git push origin main
  else
    log "Skipping git push because --no-push was supplied."
  fi
else
  log "Asset version already stamped for ${ASSET_VERSION}; no deploy commit needed."
fi

if [[ "$RESTART" -eq 1 ]]; then
  log "Restarting ${SERVICE_LABEL} with launchctl kickstart."
  launchctl kickstart -k "gui/$(id -u)/${SERVICE_LABEL}"
else
  log "Skipping launchctl restart because --no-restart was supplied."
fi

if [[ "$VERIFY" -eq 1 ]]; then
  log "Waiting for local health at ${LOCAL_HEALTH_URL}"
  LOCAL_OK=0
  for attempt in {1..15}; do
    LOCAL_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "$LOCAL_HEALTH_URL" || true)"
    if [[ "$LOCAL_STATUS" == "200" ]]; then
      log "local health attempt ${attempt}/15 - 200 OK"
      LOCAL_OK=1
      break
    fi
    log "local health attempt ${attempt}/15 - ${LOCAL_STATUS:-curl failed}"
    if [[ "$attempt" -lt 15 ]]; then
      log "local health retrying in 2s..."
      sleep 2
    fi
  done
  if [[ "$LOCAL_OK" -ne 1 ]]; then
    log "Deploy verification failed: local backend did not pass health within 30s." >&2
    log "Checked URL: ${LOCAL_HEALTH_URL}" >&2
    log "Check launchd status and logs under data/server.log / data/server.err.log." >&2
    exit 1
  fi

  ASSET_URL="${PUBLIC_URL%/}/styles.css?v=${ASSET_VERSION}"
  INDEX_URL="${PUBLIC_URL%/}/"
  AUTH_URL="${PUBLIC_URL%/}/chat/api/auth-status"
  log "Verifying public frontend/API through Cloudflare."
  PUBLIC_OK=0
  for attempt in {1..3}; do
    CSS_OK=0
    INDEX_OK=0
    AUTH_OK=0

    if curl -fsSL "$ASSET_URL" | grep -q "asset-version: ${ASSET_VERSION}"; then
      CSS_OK=1
      log "public css attempt ${attempt}/3 - 200 OK, asset-version ${ASSET_VERSION}"
    else
      log "public css attempt ${attempt}/3 - stale or unavailable"
    fi

    INDEX_BODY="$(mktemp)"
    INDEX_STATUS="$(curl -fsSL -w "%{http_code}" -o "$INDEX_BODY" "$INDEX_URL" 2>/dev/null || true)"
    if [[ "$INDEX_STATUS" == "200" ]] \
      && grep -q "styles.css?v=${ASSET_VERSION}" "$INDEX_BODY" \
      && grep -q "app.js?v=${ASSET_VERSION}" "$INDEX_BODY" \
      && grep -q "manifest.json?v=${ASSET_VERSION}" "$INDEX_BODY"; then
      INDEX_OK=1
      log "public index attempt ${attempt}/3 - 200 OK, asset URLs match ${ASSET_VERSION}"
    else
      log "public index attempt ${attempt}/3 - status ${INDEX_STATUS:-curl failed}, asset URLs not confirmed"
    fi
    rm -f "$INDEX_BODY"

    AUTH_BODY="$(mktemp)"
    AUTH_STATUS="$(curl -fsSL -w "%{http_code}" -o "$AUTH_BODY" "$AUTH_URL" 2>/dev/null || true)"
    if [[ "$AUTH_STATUS" == "200" ]] && verify_auth_status_json < "$AUTH_BODY"; then
      AUTH_OK=1
      log "public auth-status attempt ${attempt}/3 - 200 OK, valid JSON"
    else
      log "public auth-status attempt ${attempt}/3 - status ${AUTH_STATUS:-curl failed}, valid JSON not confirmed"
    fi
    rm -f "$AUTH_BODY"

    if [[ "$CSS_OK" -eq 1 && "$INDEX_OK" -eq 1 && "$AUTH_OK" -eq 1 ]]; then
      PUBLIC_OK=1
      break
    fi
    if [[ "$attempt" -lt 3 ]]; then
      log "public verification retrying in 5s..."
      sleep 5
    fi
  done
  if [[ "$PUBLIC_OK" -ne 1 ]]; then
    log "Deploy verification failed: public checks did not pass within 15s after local health." >&2
    log "Likely culprit: Cloudflare reconnect lag, stale Cloudflare cache, or tunnel outage." >&2
    log "Checked URLs: ${ASSET_URL}, ${INDEX_URL}, ${AUTH_URL}" >&2
    exit 1
  fi
  log "Deploy verification passed for asset-version ${ASSET_VERSION}."
else
  log "Skipping live verification because --no-verify was supplied."
fi
