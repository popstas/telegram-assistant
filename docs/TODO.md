# TODO

- [ ] Rename project `telegram-planfix-assistant` → `telegram-?` (decouple name from Planfix; it does more than messaging).
  Candidate names (`telegram-X`): **operator** (ties to existing `operations` domain), **orchestrator** (queued/idempotent multi-step coordination), **gateway** (API to MTProto admin actions), ops, provisioner, agent, controller, actions, commander, conductor, broker. Top picks: `telegram-operator`, `telegram-orchestrator`.
  Scope to settle: package/dir name (`src/telegram_planfix_assistant/`), CLI entrypoint (`telegram-planfix-assistant`), Docker image, README/SKILL/docs, config paths, session dir.
- [ ] Add an integration/agent-setup layer — an `INTEGRATION.md` guide (à la [obsidian-agent-base/INTEGRATION.md](https://github.com/popstas/obsidian-agent-base/blob/main/INTEGRATION.md)) that lets Claude Code wire this assistant into another project.
  Should be an interactive, agent-driven setup: ask one question at a time (with defaults), then scaffold/edit the consuming project's files. Cover: `data/config.yml` (folder name, defaults, postfix, cleanup flags, bearer token), Telethon `auth` session bootstrap, HTTP-vs-CLI choice, Planfix wiring, and a final validation checklist. Keep it in sync with `SKILL.md`/`README.md`.
