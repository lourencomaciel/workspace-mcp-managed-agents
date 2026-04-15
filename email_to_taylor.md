Hi Taylor,

Here they are! Three files:

- **gws_oauth_setup.py** — one-time setup. Registers a dynamic OAuth client via /register, opens a browser for Google login (PKCE), exchanges the code at /token, and stores the tokens in an Anthropic vault credential. The refresh token is split across multiple metadata keys since vault metadata values cap at 512 chars and the JWT is ~1,344 chars.

- **refresh_mcp_token.py** — the cron job. Reads the refresh token from vault metadata, calls /token, writes the fresh access token + rotated refresh token back. Runs on Pipedream every 50 minutes. Also works as a Pipedream handler (has a handler(pd) function) or as a standalone script via crontab.

- **README.md** — setup instructions and an architecture diagram.

The only secret needed is the Anthropic API key. Everything else (MCP client credentials, refresh token) lives in the vault credential metadata, so it's shared across the team.

Let me know if you want me to turn this into a proper repo or if this format works for sharing.

Best,
Lou
