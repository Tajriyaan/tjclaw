#!/usr/bin/env python3
"""
HuggingClaw Cloudflare KeepAlive Setup
Deploys a Cloudflare Worker with a cron trigger to ping the HF Space every 10 minutes,
preventing it from sleeping on the free tier.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

API_BASE = "https://api.cloudflare.com/client/v4"
KEEPALIVE_STATUS_FILE = Path("/tmp/huggingclaw-cloudflare-keepalive-status.json")


def cf_request(method, path, token, body=None, content_type="application/json"):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
            msg = (err_body.get("errors") or [{"message": "Unknown"}])[0].get("message", "Unknown")
        except Exception:
            msg = f"HTTP {e.code}"
        raise RuntimeError(f"Cloudflare API {e.code}: {msg}")
    if not payload.get("success"):
        errors = payload.get("errors") or [{"message": "Unknown CF API error"}]
        raise RuntimeError(errors[0].get("message", "Unknown CF API error"))
    return payload["result"]


def cf_upload_worker(account_id, worker_name, token, script_source):
    """Upload Worker as ES module via multipart/form-data."""
    boundary = "HuggingClawBoundary1337"
    metadata = json.dumps({
        "main_module": "worker.js",
        "bindings": [],
        "compatibility_date": "2024-09-23",
        "usage_model": "bundled",
    })
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="metadata"\r\n')
    parts.append(b"Content-Type: application/json\r\n\r\n")
    parts.append(metadata.encode())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n')
    parts.append(b"Content-Type: application/javascript+module\r\n\r\n")
    parts.append(script_source.encode())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{API_BASE}/accounts/{account_id}/workers/scripts/{worker_name}",
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Worker upload HTTP {e.code}: {detail}")
    if not payload.get("success"):
        errors = payload.get("errors") or [{"message": "Unknown CF API error"}]
        raise RuntimeError(f"Worker upload error: {errors[0].get('message')}")


def render_keepalive_worker(target_url):
    """ES module Worker with cron trigger support."""
    return f"""\
// HuggingClaw KeepAlive Worker — ES Module format
const TARGET_URL = {json.dumps(target_url)};

async function ping(source) {{
  const ts = new Date().toISOString();
  try {{
    const r = await fetch(TARGET_URL, {{
      method: "GET",
      headers: {{ "user-agent": "HuggingClaw-KeepAlive/1.0", "cache-control": "no-cache" }},
      cf: {{ cacheTtl: 0, cacheEverything: false }},
    }});
    return {{ ok: r.ok, status: r.status, source, target: TARGET_URL, timestamp: ts }};
  }} catch (err) {{
    return {{ ok: false, status: 0, source, target: TARGET_URL, timestamp: ts, error: err.message }};
  }}
}}

export default {{
  async fetch(request, env, ctx) {{
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health" || url.pathname === "/ping") {{
      const result = await ping("manual");
      return new Response(JSON.stringify(result, null, 2), {{
        status: result.ok ? 200 : 502,
        headers: {{ "content-type": "application/json" }},
      }});
    }}
    return new Response("Not found", {{ status: 404 }});
  }},
  async scheduled(event, env, ctx) {{
    ctx.waitUntil(ping("cron"));
  }},
}};
"""


def slugify(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or "huggingclaw-keepalive")[:63].rstrip("-")


def get_space_host():
    space_host = os.environ.get("SPACE_HOST", "").strip()
    if space_host:
        return space_host
    author = os.environ.get("SPACE_AUTHOR_NAME", "").strip()
    repo = os.environ.get("SPACE_REPO_NAME", "").strip()
    if author and repo:
        return f"{author}-{repo}.hf.space".lower()
    return ""


def derive_keepalive_worker_name():
    explicit = os.environ.get("CLOUDFLARE_KEEPALIVE_WORKER_NAME", "").strip()
    if explicit:
        return slugify(explicit)
    space_host = get_space_host()
    if space_host:
        return slugify(space_host.replace(".hf.space", "") + "-keepalive")
    return "huggingclaw-keepalive"


def write_status(payload):
    payload = {**payload, "timestamp": payload.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    KEEPALIVE_STATUS_FILE.write_text(json.dumps(payload), encoding="utf-8")
    try:
        KEEPALIVE_STATUS_FILE.chmod(0o600)
    except OSError:
        pass


def main():
    api_token = os.environ.get("CLOUDFLARE_WORKERS_TOKEN", "").strip()
    if not api_token:
        return 0

    enabled = os.environ.get("CLOUDFLARE_KEEPALIVE_ENABLED", "true").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        write_status({"configured": False, "status": "disabled", "message": "Keep-awake disabled."})
        return 0

    try:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if not account_id:
            accounts = cf_request("GET", "/accounts", api_token)
            if not accounts:
                raise RuntimeError("No Cloudflare accounts found for this token.")
            account_id = accounts[0]["id"]

        subdomain_info = cf_request("GET", f"/accounts/{account_id}/workers/subdomain", api_token)
        subdomain = (subdomain_info or {}).get("subdomain", "").strip()
        if not subdomain:
            raise RuntimeError("Workers subdomain not configured.")

        space_host = get_space_host()
        if not space_host:
            write_status({"configured": False, "status": "skipped", "message": "SPACE_HOST not set."})
            return 0

        space_host = space_host.removeprefix("https://").removeprefix("http://").split("/")[0]
        cron = os.environ.get("CLOUDFLARE_KEEPALIVE_CRON", "*/10 * * * *").strip()
        target_url = os.environ.get("CLOUDFLARE_KEEPALIVE_URL", f"https://{space_host}/health").strip()
        worker_name = derive_keepalive_worker_name()
        worker_source = render_keepalive_worker(target_url)

        cf_upload_worker(account_id, worker_name, api_token, worker_source)

        cf_request(
            "POST",
            f"/accounts/{account_id}/workers/scripts/{worker_name}/subdomain",
            api_token,
            body=json.dumps({"enabled": True, "previews_enabled": True}).encode(),
        )
        cf_request(
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{worker_name}/schedules",
            api_token,
            body=json.dumps([{"cron": cron}]).encode(),
        )

        worker_url = f"https://{worker_name}.{subdomain}.workers.dev"
        write_status({
            "configured": True, "status": "configured",
            "workerName": worker_name, "workerUrl": worker_url,
            "targetUrl": target_url, "cron": cron,
            "message": f"Pinging {target_url} on schedule: {cron}",
        })
        print(f"Cloudflare keepalive deployed: {worker_url} (cron: {cron})")
        return 0

    except Exception as exc:
        print(f"Cloudflare keepalive setup failed: {exc}", file=sys.stderr)
        write_status({"configured": False, "status": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
