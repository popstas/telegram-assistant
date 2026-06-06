# Changelog


## Unreleased

### Features

- Folders remove-chat across domain, Telethon, CLI, and HTTP
- Wire media and scheduled sends into messages CLI/HTTP
- TelethonMessageBackend for text/media/scheduled sends
- Media/scheduled send request model and domain behavior

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

