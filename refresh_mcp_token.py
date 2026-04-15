#!/usr/bin/env python3
"""
Cron job: refresh the Workspace MCP token in the Anthropic vault.

Reads the refresh token from vault credential metadata, calls the MCP server's
/token endpoint, writes the fresh access token + rotated refresh token back.

Run this every 50 minutes via Pipedream, crontab, or any scheduler.

Requirements:
    pip install httpx

Environment variables:
    ANTHROPIC_API_KEY  — Anthropic API key
    MCP_SERVER_URL     — Workspace MCP base URL (e.g. https://yourco.workspacemcp.com)
    VAULT_ID           — Anthropic vault ID (vlt_...)

Pipedream usage:
    Add ANTHROPIC_API_KEY, MCP_SERVER_URL, VAULT_ID as account-level env vars.
    Create a scheduled workflow (every 50 min) with a Python step containing:

        import os, httpx

        def handler(pd):
            # ... paste the refresh logic below ...
"""

import os
import sys
from datetime import datetime

import httpx

API_KEY = os.environ["ANTHROPIC_API_KEY"]
MCP_SERVER = os.environ["MCP_SERVER_URL"]           # e.g. https://yourco.workspacemcp.com
VAULT_ID = os.environ["VAULT_ID"]                   # e.g. vlt_01...
MCP_MCP_URL = f"{MCP_SERVER}/mcp"
TOKEN_ENDPOINT = f"{MCP_SERVER}/token"

HEADERS = {
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "managed-agents-2026-04-01",
}


def find_credential():
    """Find the active mcp_oauth credential for the Workspace MCP server."""
    resp = httpx.get(
        f"https://api.anthropic.com/v1/vaults/{VAULT_ID}/credentials?beta=true",
        headers=HEADERS,
    )
    resp.raise_for_status()
    for c in resp.json()["data"]:
        if (c["auth"].get("mcp_server_url") == MCP_MCP_URL
                and c.get("archived_at") is None
                and c["auth"].get("type") == "mcp_oauth"):
            return c
    return None


def reassemble_refresh_token(metadata: dict) -> str:
    """Reassemble the refresh token from split metadata keys."""
    count = int(metadata.get("rt_count", "0"))
    return "".join(metadata.get(f"rt_{i}", "") for i in range(count))


def split_refresh_token(token: str, chunk_size: int = 500) -> dict:
    """Split a refresh token into metadata-safe chunks."""
    chunks = {}
    for i in range(0, len(token), chunk_size):
        chunks[f"rt_{i // chunk_size}"] = token[i:i + chunk_size]
    chunks["rt_count"] = str(len([k for k in chunks if k.startswith("rt_")]))
    return chunks


def refresh():
    # 1. Find credential
    cred = find_credential()
    if not cred:
        raise RuntimeError("No active GWS MCP credential found in vault")

    meta = cred["metadata"]
    client_id = meta["client_id"]
    client_secret = meta.get("client_secret", "")
    refresh_token = reassemble_refresh_token(meta)

    if not refresh_token:
        raise RuntimeError("No refresh token in vault metadata")

    # 2. Refresh
    resp = httpx.post(TOKEN_ENDPOINT, data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    tokens = resp.json()

    # 3. Build updated metadata with rotated refresh token
    new_rt = tokens.get("refresh_token", refresh_token)
    new_meta = {"client_id": client_id, "client_secret": client_secret}
    new_meta.update(split_refresh_token(new_rt))

    # 4. Update vault credential
    httpx.post(
        f"https://api.anthropic.com/v1/vaults/{VAULT_ID}/credentials/{cred['id']}?beta=true",
        headers={**HEADERS, "content-type": "application/json"},
        json={
            "auth": {"type": "mcp_oauth", "access_token": tokens["access_token"]},
            "metadata": new_meta,
        },
    ).raise_for_status()

    return {"status": "ok", "expires_in": tokens.get("expires_in", 3600)}


# ── CLI usage ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = refresh()
        print(f"[{ts}] OK — token refreshed (expires in {result['expires_in']}s)")
    except Exception as e:
        print(f"[{ts}] FAILED — {e}", file=sys.stderr)
        sys.exit(1)


# ── Pipedream handler ─────────────────────────────────────────────────────────
def handler(pd):
    """Pipedream entry point. Set env vars in Pipedream account settings."""
    return refresh()
