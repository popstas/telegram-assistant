# MCP Inspector E2E

This project keeps CI and automated integration tests fake-backed: no live
Google or Telegram calls run from `pytest`. The fake Google OAuth flow and fake
Telegram backends are covered by:

```bash
pytest tests/test_mcp_integration.py
```

Use MCP Inspector for a manual smoke test after the server is running with
`mcp.enabled: true` in `data/config.yml`:

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
- The login flow completes with the configured Google OAuth test client.
- `initialize` succeeds after authentication.
- `tools/list` returns the `telegram_` tools.
- A read-only tool such as `telegram_health` can be called without a Telegram
  session. Tools that require Telegram should return a service-unavailable tool
  error when the Telethon session is not authorized.

Live Google OAuth and live Telegram e2e are optional. Skip and record them as
skipped when Google client credentials are unavailable, or when there is no
authorized Telethon session at the configured `telegram.session_path`. Run live
Telegram checks only against a test account because message, member, folder,
notification, topic, and group tools can mutate the account.
