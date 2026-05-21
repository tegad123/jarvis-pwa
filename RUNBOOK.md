# Jarvis PWA Incident Runbook

Use this when the public Jarvis app is down, deploy verification fails,
or the Mini was restarted.

Start with:

```bash
cd ~/jarvis-pwa
./scripts/status.sh
```

`status.sh` exits nonzero if anything is unhealthy and prints a
remediation hint next to each failing line.

---

## Public URL Returning 502 or 530

Expected first command:

```bash
./scripts/status.sh
```

Interpretation:

- PWA backend green, public URL red: cloudflared or Cloudflare edge is
  the likely problem.
- PWA backend red, public URL red: backend is down or still booting.
- cloudflared process green but public URL red: tunnel process may be
  alive but disconnected.

Remediation:

```bash
sudo launchctl kickstart -k system/com.cloudflare.cloudflared
sleep 10
./scripts/status.sh
```

If still red:

```bash
tail -120 /var/log/cloudflared.err.log
tail -120 /var/log/cloudflared.log
```

If the logs still live at the older plist paths, check:

```bash
tail -120 /Library/Logs/com.cloudflare.cloudflared.err.log
tail -120 /Library/Logs/com.cloudflare.cloudflared.out.log
```

---

## Tunnel Disconnected

Expected behavior:

- `scripts/cloudflared-health.sh` records one failure per bad check.
- After 3 consecutive failures, it runs:
  ```bash
  sudo launchctl kickstart -k system/com.cloudflare.cloudflared
  ```
- The deploy script retries public checks 3 times after local backend
  health is confirmed.

Manual fallback:

```bash
sudo launchctl kickstart -k system/com.cloudflare.cloudflared
./scripts/cloudflared-health.sh
```

If sudo blocks non-interactive health checks, either configure
passwordless sudo for the exact `launchctl kickstart` command or run
`scripts/cloudflared-health.sh --loop` from a root-owned launchd job.

---

## Backend Crashed

Expected behavior:

- `~/Library/LaunchAgents/com.34dev.jarvis-pwa.plist` has
  `KeepAlive=true` and `RunAtLoad=true`.
- launchd restarts the backend after process exit.
- `scripts/jarvis-pwa-health.sh` restarts it after 3 consecutive failed
  local health checks.

Manual fallback:

```bash
launchctl kickstart -k gui/$(id -u)/com.34dev.jarvis-pwa
sleep 10
curl -fsS http://localhost:8765/api/health
tail -120 ~/jarvis-pwa/data/server.err.log
```

---

## Deploy Verification Fails

The deploy script now verifies in this order:

1. Local backend health: `http://localhost:8765/api/health`, every
   2 seconds for up to 30 seconds.
2. Public CSS freshness:
   `https://app.34jarvis.uk/styles.css?v=<sha>`.
3. Public index freshness: `/` contains the same `?v=<sha>` asset URLs.
4. Public API: `/chat/api/auth-status` returns 200 with valid JSON.

If it fails:

```bash
sleep 30
./scripts/deploy.sh
```

If it fails again:

```bash
./scripts/status.sh
```

Common causes:

- Local health red: backend crashed or is stuck during boot.
- Local health green, public checks red: cloudflared tunnel or Cloudflare
  edge issue.
- CSS red, index green: stale Cloudflare asset cache.
- Auth-status red: backend route/API issue or tunnel routing problem.

---

## Mini Rebooted

Expected behavior:

- PWA backend starts from `~/Library/LaunchAgents/com.34dev.jarvis-pwa.plist`.
- cloudflared starts from
  `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist`.
- Health cron/launchd checks resume and restart unhealthy services.

Verify:

```bash
./scripts/status.sh
```

If PWA is red:

```bash
launchctl kickstart -k gui/$(id -u)/com.34dev.jarvis-pwa
```

If cloudflared is red:

```bash
sudo launchctl kickstart -k system/com.cloudflare.cloudflared
```

If launchd recovery settings are red:

```bash
./scripts/harden-launchd-plists.sh
```

Reload the affected launchd jobs during a maintenance window after
hardening plists.
