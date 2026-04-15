#!/usr/bin/env python3
"""
One-time setup: register an OAuth client with the Workspace MCP server,
authenticate via browser, and store the tokens in an Anthropic vault credential.

After this, a cron job (refresh_mcp_token.py) keeps the token alive.

Requirements:
    pip install httpx anthropic

Environment variables (in .env or exported):
    ANTHROPIC_API_KEY       — Anthropic API key
    ANTHROPIC_VAULT_ID      — Vault ID (vlt_...)
    MCP_SERVER_URL          — Workspace MCP base URL (e.g. https://yourco.workspacemcp.com)

Usage:
    python3 gws_oauth_setup.py
"""

import http.server
import os
import secrets
import hashlib
import base64
import threading
import urllib.parse
import webbrowser

import httpx
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────
MCP_SERVER = os.environ["MCP_SERVER_URL"]             # e.g. https://yourco.workspacemcp.com
VAULT_ID = os.environ["ANTHROPIC_VAULT_ID"]           # e.g. vlt_01...
CALLBACK_PORT = 8976
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

SCOPES = " ".join([
    "openid",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
])


# ── Helpers ───────────────────────────────────────────────────────────────────
def split_token(token: str, chunk_size: int = 500) -> dict:
    """Split a long token into metadata-safe chunks (rt_0, rt_1, ...)."""
    import math
    chunks = {}
    for i in range(math.ceil(len(token) / chunk_size)):
        chunks[f"rt_{i}"] = token[i * chunk_size : (i + 1) * chunk_size]
    chunks["rt_count"] = str(len(chunks))
    return chunks


# ── Step 1: Register dynamic OAuth client ─────────────────────────────────────
def register_client():
    print("Registering OAuth client with MCP server...")
    resp = httpx.post(f"{MCP_SERVER}/register", json={
        "client_name": "managed-agent-client",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })
    resp.raise_for_status()
    data = resp.json()
    print(f"  Client ID: {data['client_id']}")
    return data["client_id"], data.get("client_secret", "")


# ── Step 2: Browser-based authorization (PKCE) ───────────────────────────────
def get_auth_code(client_id):
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": SCOPES,
    }
    auth_url = f"{MCP_SERVER}/authorize?{urllib.parse.urlencode(params)}"

    auth_code = [None]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [None])[0] == state:
                auth_code[0] = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Done. Close this window.</h2>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    print("Opening browser for Google login...")
    webbrowser.open(auth_url)
    t.join(timeout=120)
    server.server_close()

    if not auth_code[0]:
        raise RuntimeError("No authorization code received")
    return auth_code[0], code_verifier


# ── Step 3: Exchange code for tokens ──────────────────────────────────────────
def exchange_code(client_id, client_secret, code, verifier):
    print("Exchanging code for tokens...")
    resp = httpx.post(f"{MCP_SERVER}/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    resp.raise_for_status()
    data = resp.json()
    if not data.get("refresh_token"):
        raise RuntimeError("No refresh token returned")
    print(f"  Access token obtained (expires in {data.get('expires_in', '?')}s)")
    print(f"  Refresh token obtained")
    return data["access_token"], data["refresh_token"]


# ── Step 4: Create vault credential ──────────────────────────────────────────
def create_vault_credential(access_token, client_id, client_secret, refresh_token):
    print("Creating vault credential...")
    ac = anthropic.Anthropic()

    # Archive any existing credential for this MCP server
    mcp_url = f"{MCP_SERVER}/mcp"
    creds = ac.beta.vaults.credentials.list(vault_id=VAULT_ID)
    for c in creds.data:
        if getattr(c.auth, "mcp_server_url", "") == mcp_url and c.archived_at is None:
            print(f"  Archiving old credential: {c.id}")
            ac.beta.vaults.credentials.archive(credential_id=c.id, vault_id=VAULT_ID)

    # Build metadata with split refresh token
    meta = {"client_id": client_id, "client_secret": client_secret}
    meta.update(split_token(refresh_token))

    cred = ac.beta.vaults.credentials.create(
        vault_id=VAULT_ID,
        display_name="Google Workspace MCP",
        auth={
            "type": "mcp_oauth",
            "access_token": access_token,
            "mcp_server_url": mcp_url,
        },
        metadata=meta,
    )
    print(f"  Credential created: {cred.id}")
    return cred.id


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    client_id, client_secret = register_client()
    code, verifier = get_auth_code(client_id)
    access_token, refresh_token = exchange_code(client_id, client_secret, code, verifier)
    cred_id = create_vault_credential(access_token, client_id, client_secret, refresh_token)

    print(f"\nDone. Vault credential: {cred_id}")
    print(f"The Pipedream cron (refresh_mcp_token.py) will keep this alive.")
    print(f"No re-auth needed unless the refresh token expires (30 days of inactivity).")


if __name__ == "__main__":
    main()
