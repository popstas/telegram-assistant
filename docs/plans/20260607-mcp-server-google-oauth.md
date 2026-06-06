# HTTP MCP Server with Google OAuth

## Overview

Add an optional Streamable-HTTP MCP server that exposes the assistant's operations as MCP tools,
mounted at `/mcp` inside the existing FastAPI app and **disabled by default**. The server reuses the
current domain services, backend factories, `OperationStore`, entity resolver, plugin registry, and
`telegram.access` read/write policy — MCP becomes a third interface alongside HTTP and CLI without
forking domain logic.

Access is gated by a local, MCP-compatible OAuth Authorization Server running in the same FastAPI
process. It uses Google OAuth/OIDC purely as a login gate, then mints audience-bound MCP access
tokens. Google identity authorizes *who may connect*; the existing instance-level `telegram.access`
policy still governs *what they may do*. The result lets an MCP client (Claude, MCP Inspector)
authenticate with Google and drive Telegram operations through the same safety rails as the HTTP API.

## Context

- Impacted area: `src/telegram_assistant/http_api/` (new `mcp` submodule + mount in `app.py`),
  `src/telegram_assistant/config/models.py` (new `mcp:` config), and the domain packages it reuses
  (`messages/`, `groups/`, `topics/`, `members/`, `folders/`, `notifications/`, `entities/`,
  `access/`, `persistence/`).
- Reuse, do not fork: domain `service.py` layers, the `app.state.*_backend_factory` factories (which
  return `None` → MCP tool must surface "service unavailable" the same way HTTP returns 503),
  `OperationStore` idempotency, the `EntityResolver`, and the `Authorizer` read/write gate.
- Existing contract to preserve: `/health` stays open and must respond even with an unauthorized
  session; `/telegram/*` keeps its bearer-token auth unchanged; backend factories returning `None`
  must not 500.
- Implementation decisions (settled before adoption): use the official `mcp` Python SDK (FastMCP),
  mount its streamable-HTTP ASGI app at `/mcp`; build the **full** local OAuth Authorization Server
  (discovery metadata, dynamic client registration, Google OIDC login gate, audience-bound token
  minting + validation); e2e via MCP Inspector against a fake/test OAuth flow, with live
  Google/Telegram recorded as optional/skipped when creds/session are unavailable.
- Adopted from `docs/TODO.md` (the expanded "HTTP MCP server with OAuth via Google" item).

## Development Approach

- Testing approach: regular (unit/integration with fakes — no real Telegram or Google traffic)
- Complete each task fully before moving to the next
- Update this plan when scope changes during implementation
- The Telethon session is created only by `telegram-assistant auth`; the MCP server, like the HTTP
  API, never logs in — it must degrade gracefully when the session is unauthorized

## Testing Strategy

- Unit tests required for every code-changing Task, using the existing fake-backend / fake-config
  patterns (`tests/conftest.py` `minimal_config_yaml`, injected fakes)
- The OAuth flow is tested against a **fake Google** OIDC provider — no live Google calls in CI
- Run `pytest` after each Task before proceeding; `ruff check src tests` must stay clean
- Manual e2e = MCP Inspector (`npx @modelcontextprotocol/inspector`) pointed at `/mcp` with the
  fake/test OAuth flow; live Google/Telegram e2e is documented as optional and skipped without creds

## Technical Details

- **Mount model**: `create_app()` constructs a `FastMCP` server and mounts `mcp.streamable_http_app()`
  at `/mcp` only when `config.mcp` is present and `mcp.enabled` is true; otherwise the route is absent
  and the app behaves exactly as today.
- **OAuth Authorization Server** (same FastAPI process):
  - Discovery: `/.well-known/oauth-authorization-server` and the MCP protected-resource metadata so
    clients can auto-discover endpoints.
  - Dynamic Client Registration endpoint (`/register`).
  - `/authorize`: redirects to Google for OIDC login, validates the returned id_token, and enforces
    the allowed emails/domains allowlist before issuing an authorization code.
  - `/token`: exchanges the code for an audience-bound, scoped MCP access token (signed with the
    configured signing secret; TTLs from config).
  - Token validation middleware on `/mcp`: verifies signature, audience, expiry, and required scopes.
- **`mcp:` config** (all optional; absence ⇒ server disabled): `enabled`, `server_url`, `issuer_url`,
  Google `client_id` / `client_secret`, `allowed_emails` / `allowed_domains`, `required_scopes`,
  access/refresh token TTLs, and `signing_secret`.
- **Tools** — `telegram_`-prefixed, one per existing operation, executed directly like the HTTP API:
  `telegram_health`, `telegram_messages_send`, `telegram_messages_recent`, `telegram_messages_forward`,
  `telegram_messages_react`, `telegram_groups_create`, `telegram_topics_layout`,
  `telegram_topics_create`/`telegram_topics_close`, `telegram_members_add`/`telegram_members_remove`,
  `telegram_folders_inspect`/`telegram_folders_add_chat`/`telegram_folders_remove_chat`,
  `telegram_notifications_mute`/`telegram_notifications_unmute`, and
  `telegram_operations_status`/`telegram_operations_retry`. Each carries MCP annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) matching the operation's
  nature.
- **Error mapping**: backend factory `None` → tool error equivalent to HTTP 503; `AccessDenied` →
  tool error equivalent to HTTP 403; entity-not-found → 404-equivalent; ambiguous entity →
  409-equivalent. Access checks reuse the same WRITE/READ mapping the HTTP layer applies.

## Implementation Steps

### Task 1: Add `mcp:` config model and validation

- [ ] Add an `McpConfig` Pydantic model in `config/models.py` (fields: `enabled` default false,
      `server_url`, `issuer_url`, Google `client_id`/`client_secret`, `allowed_emails`,
      `allowed_domains`, `required_scopes`, token TTLs, `signing_secret`) and attach it as optional
      `mcp` on the telegram/app config
- [ ] Define validation rules: when `enabled` is true, the credentials/secret/issuer needed to run
      the AS must be present; when `mcp` is omitted the server is fully disabled (backward compatible)
- [ ] Ensure `config.loader.load_config()` surfaces clear errors for an incomplete enabled `mcp:` block
- [ ] write tests for config parsing/validation (absent block, disabled block, valid enabled block,
      enabled-but-incomplete block)
- [ ] run project tests - must pass before next task

### Task 2: Build the local OAuth Authorization Server

- [ ] Implement discovery endpoints: `/.well-known/oauth-authorization-server` and the MCP
      protected-resource metadata, populated from `McpConfig`
- [ ] Implement Dynamic Client Registration (`/register`)
- [ ] Implement `/authorize`: redirect to Google OIDC, validate the returned id_token, and enforce
      the allowed-emails/allowed-domains allowlist before issuing an authorization code
- [ ] Implement `/token`: exchange the code for an audience-bound, scoped, signed MCP access token
      with config-driven TTLs
- [ ] Implement token-validation logic (signature, audience, expiry, required scopes) for reuse by
      the `/mcp` mount
- [ ] Introduce a fake-Google OIDC test double so the flow is exercisable without live Google
- [ ] write tests for the fake-Google login flow, allowlist enforcement, and token audience/scope/TTL
- [ ] run project tests - must pass before next task

### Task 3: Mount the FastMCP streamable-HTTP server at `/mcp`

- [ ] Add the official `mcp` SDK to project dependencies
- [ ] Construct a `FastMCP` server and mount its streamable-HTTP ASGI app at `/mcp` in `create_app()`,
      gated on `mcp.enabled` (absent/disabled ⇒ no `/mcp` route)
- [ ] Enforce the Task 2 token validation on `/mcp` requests (reject missing/invalid/expired/wrong-
      audience/insufficient-scope tokens)
- [ ] Verify `/health` stays open and `/telegram/*` bearer auth is unchanged when MCP is enabled
- [ ] write tests for the mount toggle, `initialize`/`tools/list` reachability, and token enforcement
- [ ] run project tests - must pass before next task

### Task 4: Expose `telegram_` tools over the domain services

- [ ] Register `telegram_`-prefixed MCP tools mapping to existing operations (health, messages send,
      recent messages, forward, reactions, groups create, topic layout, topics, members, folders,
      notifications, operations), each reusing the corresponding domain service via the existing
      backend factories
- [ ] Attach MCP annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`) to
      each tool to match the operation's nature
- [ ] Thread the `telegram.access` `Authorizer` and `EntityResolver` into the tools, preserving the
      HTTP WRITE/READ mapping and `OperationStore` idempotency
- [ ] Map failures to actionable tool errors: backend-unavailable (≈503), `AccessDenied` (≈403),
      entity-not-found (≈404), ambiguous entity (≈409)
- [ ] write tests with fake backends for representative read and write tools, access-denied, and
      backend-unavailable paths
- [ ] run project tests - must pass before next task

### Task 5: Integration tests for the MCP protocol + OAuth surface

- [ ] Add tests covering MCP `initialize`, `tools/list`, and a representative `tools/call` end-to-end
      through the fake-Google OAuth flow and fake backends
- [ ] Assert token audience/scope checks reject mis-scoped or wrong-audience tokens at `tools/call`
- [ ] Assert unchanged `/telegram/*` bearer auth and open `/health` behavior with MCP enabled
- [ ] Document the MCP Inspector manual-e2e procedure and mark live Google/Telegram e2e as
      optional/skipped when creds/session are unavailable
- [ ] write tests covering the above protocol/auth integration paths
- [ ] run project tests - must pass before next task

### Task 6: Update docs and sync the skill

- [ ] Update `README.md` (Commands/usage) to document the optional MCP server and its config
- [ ] Update `skills/telegram-assistant/SKILL.md` and re-sync it to
      `~/.claude/skills/telegram-assistant/SKILL.md` in the same change
- [ ] Document the `mcp:` config block (fields, enable/disable, OAuth setup) in the project docs
- [ ] write/adjust tests so the `tests/test_skill_inventory.py` guard passes
- [ ] run project tests - must pass before next task

### Task 7: Verify acceptance criteria

- [ ] verify all requirements from Overview are implemented (optional `/mcp` mount disabled by
      default; full Google-OAuth AS with audience-bound tokens; `telegram_` tools reusing domain
      services + access policy + idempotency; `/health` and `/telegram/*` unchanged)
- [ ] run full project test suite (`pytest`)
- [ ] run project linter (`ruff check src tests`) - all issues must be fixed

## Post-Completion

*Items requiring manual intervention - no checkboxes, informational only*

- Manual e2e with MCP Inspector (`npx @modelcontextprotocol/inspector`) against `/mcp` using the
  fake/test OAuth flow.
- Live Google OAuth + live Telegram e2e require real Google client credentials and an authorized
  Telethon session at `data/sessions/.../session.session`; run only against a test account, and
  record as skipped when unavailable.
- Provision real Google OAuth client credentials and populate the `mcp:` block in `data/config.yml`
  before enabling the server in any real deployment.
