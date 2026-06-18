# HuggingClaw — HF Spaces Optimizations

All changes are backwards-compatible. Every default can be overridden via HF Space Variables.

---

## Dockerfile changes

### 1. Split apt install into three cached layers
**Problem:** One giant `apt-get install` layer means any package change (e.g. Chromium update) busts
the entire cache and rebuilds everything, causing unnecessarily long HF Spaces rebuild times.

**Fix:** Separated into:
- Layer 1: base tools (`git`, `curl`, `python3`, etc.) — changes almost never
- Layer 2: `pip install huggingface_hub hf_transfer` — independent from apt
- Layer 3: Chromium + X11/font deps — large but independently cacheable

### 2. Increased HEALTHCHECK start-period: 90s → 150s
**Problem:** HF Spaces free-tier cold boots can take 90–120 s. The 90 s start-period caused the
health check to declare the container unhealthy before the gateway even finished starting,
triggering a hard restart loop.

**Fix:** `--start-period=150s`. Also raised `--timeout` from 5 s to 10 s to tolerate a slow
`/health` response under CPU contention at startup.

### 3. Node.js heap cap via NODE_OPTIONS
**Problem:** On free-tier HF Spaces (2 GB RAM), Node.js has no default heap limit and can be OOM-killed
by the kernel, leaving the space in a broken state with no useful error message.

**Fix:** Added `--max-old-space-size=1536` (1.5 GB cap). This leaves ~512 MB headroom for
Chromium, Python sync scripts, and OS overhead. Override by setting `NODE_OPTIONS` as an HF Space Variable.

---

## start.sh changes

### 4. Removed `-u` (nounset) from `set -euo pipefail`
**Problem:** HF Spaces injects a number of env vars that may be absent early in the boot sequence
(e.g. `SPACE_HOST`, `HF_TOKEN`, custom user variables). With `-u` set, any reference to an unset
variable causes an immediate `unbound variable` exit — killing the Space before startup completes.

**Fix:** Changed to `set -eo pipefail`. Individual critical sections can re-add `-u` locally
where tight variable hygiene is needed.

### 5. Increased GATEWAY_READY_TIMEOUT default: 90s → 150s
**Problem:** Same cold-boot issue as the health check. The gateway (OpenClaw) can take 60–120 s to
start on free-tier HF Spaces because it fetches plugins and restores workspace on first boot.
The 90 s timeout caused `start.sh` to declare failure and exit before the gateway was ready.

**Fix:** Default is now 150 s. Override: set `GATEWAY_READY_TIMEOUT=<seconds>` as an HF Space Variable.

### 6. One free retry on gateway startup failure (prod mode)
**Problem:** In non-DEV_MODE, any gateway startup failure caused an immediate `exit 1`, taking the
whole Space down permanently until manually restarted. HF Spaces occasionally has transient
network/disk issues at cold boot that a single retry would have recovered from.

**Fix:** On first failure in prod mode, `start.sh` now waits 15 s and retries once before exiting.
DEV_MODE behaviour is unchanged (keeps looping).

### 7. Browser warmup initial sleep: 8s → 3s
**Problem:** Cosmetic/latency. The managed Chromium warmup unconditionally slept 8 s before even
trying to start the browser, adding unnecessary latency to every cold boot even when Chromium was
already ready.

**Fix:** Reduced to 3 s. The retry loop (up to 6 × 5 s attempts) still handles the case where
Chromium needs more time.

---

## How to apply

Replace the original `Dockerfile` and `start.sh` with the versions in this directory, then push
to your HF Space. No new env vars are required — all changes are drop-in defaults.

## Tuning cheat-sheet

| Goal | HF Space Variable | Recommended value |
|---|---|---|
| More gateway startup time | `GATEWAY_READY_TIMEOUT` | `180` |
| Larger Node heap | `NODE_OPTIONS` | `--max-old-space-size=2048 --require /opt/cloudflare-proxy.js` |
| Disable browser plugin | `BROWSER_PLUGIN_MODE` | `disabled` |
| Verbose gateway logs | `GATEWAY_VERBOSE` | `1` |
| Strict startup (fail fast) | `HUGGINGCLAW_STARTUP_STRICT` | `true` |
