# TODO

- [x] Add an **HTTP MCP server** exposing the assistant's operations as MCP tools, with **OAuth via Google**.
  Build an optional Streamable-HTTP MCP endpoint mounted at `/mcp` in the existing FastAPI app, disabled by default.
  Use the official `mcp` Python SDK (FastMCP) — build a `FastMCP` server and mount its streamable-HTTP ASGI app at
  `/mcp` inside the existing app. Reuse the current domain services, backend factories, `OperationStore`, entity
  resolver, plugin registry, and `telegram.access` read/write policy.

  Auth plan: add a local MCP-compatible OAuth Authorization Server in the same FastAPI process. It uses Google OAuth/
  OIDC for login, then mints audience-bound MCP access tokens. Google identity is only a login gate: allowed emails/
  domains share the existing instance-level Telegram access policy.

  Tool plan: expose `telegram_`-prefixed MCP tools for health, messages, recent messages, forwarding, reactions,
  groups, topic layout, topics, members, folders, notifications, and operations. Mutating tools execute directly like
  the HTTP API, with MCP annotations, OAuth, access checks, and existing idempotency.

  Config/test plan: add optional `mcp:` config for server URL, issuer URL, Google client credentials, allowed emails/
  domains, required scopes, token TTLs, and signing secret. Cover config validation, fake-Google OAuth flow, token
  audience/scope checks, MCP `initialize` / `tools/list` / representative `tools/call`, unchanged `/telegram/*` bearer
  auth, and fake-backend tool behavior. Implemented in
  `docs/plans/completed/20260607-mcp-server-google-oauth.md`; manual Inspector/live
  Google-Telegram checks remain post-completion/manual.
