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

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing deploy: tracked working tree changes are present." >&2
  git status --short
  exit 1
fi

git fetch origin
git checkout main
git pull --ff-only origin main

HEAD_SUBJECT="$(git log -1 --pretty=%s)"
if [[ "$HEAD_SUBJECT" =~ ^Deploy:\ cache-bust\ assets\ for\ ([0-9a-fA-F]{7,40})$ ]]; then
  ASSET_VERSION="${BASH_REMATCH[1]}"
  echo "Current HEAD is already a deploy cache-bust commit for ${ASSET_VERSION}."
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
    echo "Skipping git push because --no-push was supplied."
  fi
else
  echo "Asset version already stamped for ${ASSET_VERSION}; no deploy commit needed."
fi

if [[ "$RESTART" -eq 1 ]]; then
  launchctl kickstart -k "gui/$(id -u)/${SERVICE_LABEL}"
else
  echo "Skipping launchctl restart because --no-restart was supplied."
fi

if [[ "$VERIFY" -eq 1 ]]; then
  echo "Waiting for local health at ${LOCAL_HEALTH_URL}"
  LOCAL_OK=0
  for attempt in {1..15}; do
    if curl -fsS "$LOCAL_HEALTH_URL" >/dev/null 2>&1; then
      echo "Local health check passed on attempt ${attempt}."
      LOCAL_OK=1
      break
    fi
    if [[ "$attempt" -lt 15 ]]; then
      echo "Local health not ready yet (attempt ${attempt}/15); retrying in 2s..."
      sleep 2
    fi
  done
  if [[ "$LOCAL_OK" -ne 1 ]]; then
    echo "Deploy verification failed: local backend did not pass health within 30s." >&2
    echo "Checked URL: ${LOCAL_HEALTH_URL}" >&2
    echo "Check launchd status and logs under data/server.log / data/server.err.log." >&2
    exit 1
  fi

  ASSET_URL="${PUBLIC_URL%/}/styles.css?v=${ASSET_VERSION}"
  echo "Verifying fresh CSS at ${ASSET_URL}"
  PUBLIC_OK=0
  for attempt in {1..3}; do
    if curl -fsSL "$ASSET_URL" | grep -q "asset-version: ${ASSET_VERSION}"; then
      echo "Public freshness check passed on attempt ${attempt}."
      PUBLIC_OK=1
      break
    fi
    if [[ "$attempt" -lt 3 ]]; then
      echo "Public freshness check failed (attempt ${attempt}/3); retrying in 5s..."
      sleep 5
    fi
  done
  if [[ "$PUBLIC_OK" -ne 1 ]]; then
    echo "Deploy verification failed: public CSS does not contain asset-version: ${ASSET_VERSION} after 3 attempts." >&2
    echo "Likely culprit: Cloudflare reconnect lag or stale Cloudflare cache." >&2
    echo "Checked URL: ${ASSET_URL}" >&2
    exit 1
  fi
  echo "Deploy verification passed for asset-version ${ASSET_VERSION}."
else
  echo "Skipping live verification because --no-verify was supplied."
fi
