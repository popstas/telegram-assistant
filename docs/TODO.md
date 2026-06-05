# TODO

- [ ] Rename project `telegram-planfix-assistant` → `telegram-?` (decouple name from Planfix; it does more than messaging).
  Candidate names (`telegram-X`): **operator** (ties to existing `operations` domain), **orchestrator** (queued/idempotent multi-step coordination), **gateway** (API to MTProto admin actions), ops, provisioner, agent, controller, actions, commander, conductor, broker. Top picks: `telegram-operator`, `telegram-orchestrator`.
  Scope to settle: package/dir name (`src/telegram_planfix_assistant/`), CLI entrypoint (`telegram-planfix-assistant`), Docker image, README/SKILL/docs, config paths, session dir.
