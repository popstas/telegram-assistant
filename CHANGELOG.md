# Changelog


## Unreleased

### Features

- rich-markdown: Expand Obsidian wikilinks in articles (#23)
- rich-markdown: Fill media attributes from ffprobe/ffmpeg (#21)

### Refactor

- cli: Extract rich-send input handling out of `messages send` (#20)

## v0.10.0 - 2026-07-28

### Features

- members: Read-only members list with single-request membership check (#19)

## v0.9.0 - 2026-07-27

### Features

- rich-markdown: Split soft line breaks
- Verify rich-markdown docs and record the proven tg:// media wire facts
- Pin rich-markdown acceptance edge cases
- Document rich-markdown spacing, local media and collage grouping
- Group consecutive rich-markdown media into <tg-collage>
- Upload and send local media in rich messages
- Resolve local media from rich markdown into tg:// references
- Spike script for local media inside a rich message
- Surface rich markdown paragraph spacing flag on CLI, HTTP, MCP
- Wire rich markdown paragraph spacing into send_message
- Rich markdown paragraph spacer normalization
- Rich markdown block scanner
- Rich_markdown kwarg on telegram_messages_send MCP tool
- --rich-markdown on messages send CLI
- Rich_markdown field on POST /telegram/messages
- Rich_markdown send in domain service and Telethon backend
- Spike live rich-message send via InputRichMessageMarkdown

### Bug Fixes

- review: Address rich-markdown spacing and local-media review findings
- review: Harden rich-send id extraction and error taxonomy

### Documentation

- Document rich_markdown send across CLI/HTTP/MCP
- Note access-rule resolve failure task and fix spike session path
- Link telegram-download-chat and move ToS warning below features

### Miscellaneous

- Todo, plans

### Testing

- Verify rich-message acceptance criteria

### Task

- Clear completed rich-markdown and messages todos

## v0.8.0 - 2026-07-26

### Features

- Add from_date/to_date search range to CLI, HTTP and MCP
- Search via single messages.SearchRequest with pagination
- Add from_date/to_date contract to messages search domain
- Pace pin/unpin and retry FLOOD_WAIT across surfaces
- Add folder_id access rules and union same-named folders
- Key folder membership by folder_id instead of title
- Never overwrite files on messages download
- Document messages edit/pin/unpin/download/search + access perf knobs
- Verify messages ops + access perf acceptance criteria
- Messages search surfaces (CLI + HTTP + MCP)
- Messages search domain op + Telethon adapter (READ gate)
- Messages download surfaces (CLI + HTTP + MCP)
- Messages download domain op + Telethon adapter (READ gate)
- Messages pin/unpin surfaces (CLI + HTTP + MCP)
- Messages pin/unpin domain op + Telethon adapter
- Messages edit surfaces (CLI + HTTP + MCP)
- Messages edit domain op + edit_only_session_messages gate
- Wire folder-membership cache into authorizer (read-through, stale fallback)
- Membership cache table + store (variant C storage)
- Build folder membership without get_entity (variant A)
- access: Per-rule delete-only override

### Bug Fixes

- review: Address PR #17 review findings
- Address codex review findings
- review: Rebuild membership cache on access hot-add + cover error/edge paths

### Documentation

- Document download no-overwrite, pin pacing, search range and folder_id rules
- Add rich message send implementation plan
- Add Features section to README

### Miscellaneous

- Pin bump-my-version to 1.x to fix release bump crash

### Testing

- Verify PR #17 acceptance criteria and knob-less config defaults

### Task

- Clear completed items from TODO
- Queue messages edit/pin/download/search tasks
- Add slow-send investigation (folder-rule list_folders)

## v0.7.0 - 2026-06-21

### Features

- groups: Include group name in answer, log request, lock intl phone warning
- Document group-create lang/telegram_id/answer HTTP fields in README
- Verify acceptance criteria for group-create lang/telegram_id/answer
- HTTP endpoint wiring and response coverage for group-create lang/telegram_id/answer
- Phone-client/telegram_id branching and localized answer in group create
- Add lang/telegram_id request fields and answer to group-create models
- topics: Add open/reopen command across all surfaces

### Bug Fixes

- groups: Treat telegram_id "0" as absent so phone client warns
- groups: Don't cache phone-without-telegram_id warning so retry re-evaluates
- review: Prevent numeric user ids being misread as phone clients
- topics: Remove idempotency from open/close so every call executes
- topics: Treat TOPIC_NOT_MODIFIED as no-op and make open/close re-runnable

### Documentation

- Warn that auto-adding members can get the account banned

### Miscellaneous

- groups: Log client-member branch decision and idempotency replay

### Task

- Mark group-create lang/telegram_id/answer todos done
- Add lang/telegram_id request fields and phone-without-id check

## v0.6.0 - 2026-06-16

### Features

- groups: Add managers field as a members alias in group create
- groups: Import phone+name contacts during group create
- Finalize rename docs and regenerate changelog
- Verify rename acceptance criteria across all surfaces
- Add group/topic rename round-trips to live e2e scripts
- Document groups/topics rename in skill and README
- Add MCP telegram_groups_rename and telegram_topics_rename tools
- Add topic rename HTTP endpoints
- Add group rename HTTP endpoint
- Add topics rename CLI command
- Add groups rename CLI command
- Add topic rename domain service and Telethon backend
- Add group rename domain service and Telethon backend
- Add rename idempotency keys for groups and topics
- planfix: Clean up /task in topics, keep topic name **BREAKING**

### Bug Fixes

- members: Treat bare numeric member refs as Telegram user ids
- review: Cover rename pending/failed HTTP arms + MCP arg guards

### Documentation

- plan: Rename groups and topics across all surfaces

### Miscellaneous

- Move completed rename plan to docs/plans/completed

### Task

- Add rename groups and topics tools task; clear completed items

## v0.5.0 - 2026-06-09

### Features

- Verify acceptance criteria for access/delete/MCP batch
- Docs + inventory sync for access/delete/MCP batch
- Extend live e2e scripts with delete/reply_to/recent-minutes/base64/file_urls steps
- Conditional health check + prompt for message in skill flow
- Tests for quieter /health Telethon logging
- Slim MCP send args + mcp.disabled_tools filtering
- Base64 inline file attachments for message send
- Reliable file_urls upload via download-to-temp
- Messages recent --minutes time-window filter
- Reply_to on message send across CLI/HTTP/MCP
- Session-limit delete flag + delete surfaces (CLI/HTTP/MCP)
- Delete-message domain operation gated on delete permission
- SentMessageRegistry tracking process-sent message ids
- Access management CLI commands (list/check/add)
- Backward-compatible multi-chat/multi-permission access rules
- Independent-capability authorizer + delete permission
- Config hot-reload via watchdog
- Verify mcp acceptance criteria
- Document optional mcp server
- Add mcp oauth integration tests
- Expose MCP Telegram tools
- Mount fastmcp streamable http server
- Build local oauth authorization server
- Add mcp config validation

### Bug Fixes

- reload: Make Docker hot-reload work and stop reload cycle
- health: Silence per-probe Telethon app logs on /health
- Record mass-send message ids in SentMessageRegistry
- Preserve real base64 attachment filename in Telegram
- review: Correct access config template + harden base64/logging tests
- health: Skip default folder probe
- health: Preserve Telegram auth status
- health: Allow slower Telegram probes
- http: Keep health responsive during outages
- mcp: Allow public reverse proxy host
- mcp: Allow browser OAuth preflights
- review: Secure mcp oauth tools

### Documentation

- skill: Require AskUserQuestion for confirmations and message input
- plans: Revise feature-batch plan from revdiff review
- plans: Add access/delete/MCP feature-batch plan
- todo: Add MCP follow-up tasks
- todo: Fix completed MCP plan link
- Move plans

### Task

- Drop completed items, add /health log-reduction task
- Refresh docker TODO queue
- Expand MCP server task with FastMCP SDK + full OAuth AS decisions
- Clear completed items from TODO (shipped in v0.4.0)

## v0.4.0 - 2026-06-06

### Features

- Message operations expansion (#11)

### Documentation

- Add plan
- plan: Plan message ops expansion

## v0.3.0 - 2026-06-05

### Features

- Entity resolver, read/write access control, and first-class get-recent read op (#8)

### Documentation

- Adopt access-control plan into ralphex format
- Pin e2e approach in access-control draft (extend e2e scripts)
- Draft plan for access control + entity resolver + read op
- Add AGENTS.md documenting the release workflow

### Task

- Mark PyPI publishing done (v0.2.1 live)

## v0.2.1 - 2026-06-05

### Miscellaneous

- Relicense under MIT
- Publish to PyPI on release + add bump-my-version config (#7)

## v0.2.0 - 2026-06-05

### Features

- Verify acceptance criteria for first-day fixes
- Clean up service messages after group creation
- Verify chat existence before replaying group_create
- Tolerate blank user references in group create and bulk add
- Default group permissions for create_topics and pin_messages
- Per-request topics_layout on group creation
- Configurable chat-title postfix on group creation
- observability: Quiet Telethon health-check noise
- Configurable topics layout (list/tabs) across config, CLI, and HTTP (#4)
- config: Add ~/.config fallback and bootstrap
- Verify acceptance criteria for telegram skill and dry-run
- Add error guidance, clarification templates, and scope boundaries to SKILL.md
- Document all 13 resource/action pairs and scenarios in SKILL.md
- Add SKILL.md skeleton and shared agent rules
- Add --dry-run to folders add-chat and operations retry
- Add --dry-run to members bulk-add and messages send
- Add --dry-run to groups create and topics create/bulk-create/close
- Audit CLI dry-run state and define shared envelope contract
- Run live e2e against real Telegram and harden production wiring (task 18)
- Add e2e test scaffolding (task 17)
- Verify MVP acceptance criteria (task 16)
- Add Docker image and deployment scaffolding
- Add structured logging and alert hooks
- Add send message/command HTTP + CLI with mass mode
- Add bulk remove members HTTP + CLI with dry-run and force guard
- Add bulk add members HTTP + CLI with privacy handling
- Add topic close HTTP + CLI with idempotency
- Add bulk topic create HTTP + CLI with per-item queue
- Add single topic create HTTP + CLI with first-message logic
- Add group create HTTP + CLI with idempotency and folder placement
- Add chat folder resolution and folder operations
- Add async worker queue with FLOOD_WAIT handling
- Add SQLite persistence and idempotency layer
- Add /health endpoint and health CLI with shared probes
- Add Telethon session wrapper and auth CLI
- Scaffold telegram-assistant project

### Bug Fixes

- review: Classify chat_exists errors by type, fix tautological test
- Address codex review findings
- Address code review findings
- review: Unblock bulk-remove --dry-run for protected accounts
- review: Address code review findings

### Documentation

- Document group-create options and new config defaults
- Add CLAUDE.md with architecture and command reference
- Enforce e2e tests
- Mark Task 18 e2e checkboxes as not-automatable
- Add creds, repeat e2e
- Add task 17: e2e tests

### Miscellaneous

- Ignore installed skills and .claude
- Chmod 644 docs
- Drop exec bit on scripts/*.sh
- Add git-cliff changelog and GitHub Actions workflows
- Initial commit with spec and ralphex plan

### Refactor

- Rename cleanup_service_messages -> cleanup_planfix_messages, default off

