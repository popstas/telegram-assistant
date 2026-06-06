# MCP Inspector E2E

This project keeps CI and automated integration tests fake-backed: no live
Google or Telegram calls run from `pytest`. The fake Google OAuth flow and fake
Telegram backends are covered by:

```bash
pytest tests/test_mcp_integration.py
```

## Config

The MCP server is absent unless `data/config.yml` contains `mcp.enabled: true`.
With the block omitted, or with `enabled: false`, `/mcp` and the OAuth routes
are not mounted.

Enabled example:

```yaml
mcp:
  enabled: true
  server_url: "http://localhost:8085/mcp"
  issuer_url: "http://localhost:8085"
  google_client_id: "GOOGLE_CLIENT_ID"
  google_client_secret: "GOOGLE_CLIENT_SECRET"
  allowed_emails:
    - "owner@example.com"
  allowed_domains: []
  required_scopes:
    - "mcp"
  access_token_ttl_seconds: 3600
  refresh_token_ttl_seconds: 2592000
  signing_secret: "replace-with-a-long-random-secret"
```

Fields:

- `enabled` defaults to `false`.
- `server_url` is the protected MCP resource and token audience. It normally
  ends with `/mcp`.
- `issuer_url` is the public base URL of this service's OAuth Authorization
  Server.
- `google_client_id` / `google_client_secret` are from a Google OAuth Web
  application client.
- `allowed_emails` and `allowed_domains` gate which Google identities may
  connect. At least one must be non-empty when MCP is enabled.
- `required_scopes` defaults to `["mcp"]`; the MCP mount rejects tokens missing
  any configured required scope.
- `access_token_ttl_seconds` and `refresh_token_ttl_seconds` control local MCP
  token lifetimes.
- `signing_secret` signs local access and refresh tokens. Rotating it
  invalidates existing MCP tokens.

If the app is behind a reverse proxy, set `server_url` and `issuer_url` to the
external URLs seen by the MCP client, not the private container URL.

## Google OAuth setup

Create a Google OAuth Web application client and add this authorized redirect
URI:

```text
<issuer_url>/authorize
```

For the local example above, that is:

```text
http://localhost:8085/authorize
```

Google is only the login gate. After login, this app enforces
`allowed_emails` / `allowed_domains` and mints audience-bound MCP tokens.
Tool-level read/write access is still governed by `telegram.access`.

## Inspector smoke test

Use MCP Inspector after the server is running with `mcp.enabled: true` in
`data/config.yml`:

```bash
source .venv/bin/activate
uvicorn telegram_assistant.http_api.app:create_app --factory --port 8085
npx @modelcontextprotocol/inspector
```

In Inspector, choose Streamable HTTP and point it at:

```text
http://localhost:8085/mcp
```

Expected checks:

- OAuth discovery resolves the authorization server and protected-resource
  metadata from the running app.
- Dynamic client registration succeeds.
- The login flow completes with the configured Google OAuth client.
- `initialize` succeeds after authentication.
- `tools/list` returns the `telegram_` tools.
- A read-only tool such as `telegram_health` can be called without a Telegram
  session. Tools that require Telegram should return a service-unavailable tool
  error when the Telethon session is not authorized.

Useful endpoints to verify directly:

```bash
curl -s http://localhost:8085/.well-known/oauth-authorization-server
curl -s http://localhost:8085/.well-known/oauth-protected-resource/mcp
```

Live Google OAuth and live Telegram e2e are optional. Skip and record them as
skipped when Google client credentials are unavailable, or when there is no
authorized Telethon session at the configured `telegram.session_path`. Run live
Telegram checks only against a test account because message, member, folder,
notification, topic, and group tools can mutate the account.
