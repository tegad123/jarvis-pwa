#!/usr/bin/env bash
set -euo pipefail

PWA_PLIST="${PWA_PLIST:-${HOME}/Library/LaunchAgents/com.34dev.jarvis-pwa.plist}"
CLOUDFLARED_PLIST="${CLOUDFLARED_PLIST:-/Library/LaunchDaemons/com.cloudflare.cloudflared.plist}"
PLISTBUDDY="/usr/libexec/PlistBuddy"

set_bool() {
  local plist="$1"
  local key="$2"
  local value="$3"
  "$PLISTBUDDY" -c "Delete :${key}" "$plist" >/dev/null 2>&1 || true
  "$PLISTBUDDY" -c "Add :${key} bool ${value}" "$plist"
}

set_integer() {
  local plist="$1"
  local key="$2"
  local value="$3"
  "$PLISTBUDDY" -c "Delete :${key}" "$plist" >/dev/null 2>&1 || true
  "$PLISTBUDDY" -c "Add :${key} integer ${value}" "$plist"
}

set_string() {
  local plist="$1"
  local key="$2"
  local value="$3"
  "$PLISTBUDDY" -c "Delete :${key}" "$plist" >/dev/null 2>&1 || true
  "$PLISTBUDDY" -c "Add :${key} string ${value}" "$plist"
}

echo "[launchd] Hardening Jarvis PWA plist at ${PWA_PLIST}"
set_bool "$PWA_PLIST" KeepAlive true
set_bool "$PWA_PLIST" RunAtLoad true
set_integer "$PWA_PLIST" ThrottleInterval 5
set_string "$PWA_PLIST" StandardOutPath "${HOME}/jarvis-pwa/data/server.log"
set_string "$PWA_PLIST" StandardErrorPath "${HOME}/jarvis-pwa/data/server.err.log"

echo "[launchd] Hardening cloudflared plist at ${CLOUDFLARED_PLIST}"
echo "[launchd] This requires sudo because the plist is root-owned."
sudo "$PLISTBUDDY" -c "Delete :KeepAlive" "$CLOUDFLARED_PLIST" >/dev/null 2>&1 || true
sudo "$PLISTBUDDY" -c "Add :KeepAlive bool true" "$CLOUDFLARED_PLIST"
sudo "$PLISTBUDDY" -c "Delete :RunAtLoad" "$CLOUDFLARED_PLIST" >/dev/null 2>&1 || true
sudo "$PLISTBUDDY" -c "Add :RunAtLoad bool true" "$CLOUDFLARED_PLIST"
sudo "$PLISTBUDDY" -c "Delete :ThrottleInterval" "$CLOUDFLARED_PLIST" >/dev/null 2>&1 || true
sudo "$PLISTBUDDY" -c "Add :ThrottleInterval integer 10" "$CLOUDFLARED_PLIST"
sudo "$PLISTBUDDY" -c "Delete :StandardOutPath" "$CLOUDFLARED_PLIST" >/dev/null 2>&1 || true
sudo "$PLISTBUDDY" -c "Add :StandardOutPath string /var/log/cloudflared.log" "$CLOUDFLARED_PLIST"
sudo "$PLISTBUDDY" -c "Delete :StandardErrorPath" "$CLOUDFLARED_PLIST" >/dev/null 2>&1 || true
sudo "$PLISTBUDDY" -c "Add :StandardErrorPath string /var/log/cloudflared.err.log" "$CLOUDFLARED_PLIST"

echo "[launchd] Validating plists."
plutil -lint "$PWA_PLIST"
sudo plutil -lint "$CLOUDFLARED_PLIST"

echo "[launchd] Done. Reload manually during a maintenance window:"
echo "  launchctl unload ${PWA_PLIST} && launchctl load ${PWA_PLIST}"
echo "  sudo launchctl bootout system/com.cloudflare.cloudflared || true"
echo "  sudo launchctl bootstrap system ${CLOUDFLARED_PLIST}"
