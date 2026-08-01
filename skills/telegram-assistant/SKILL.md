---
name: telegram-assistant
description: Translate human Telegram requests into safe `telegram-assistant` CLI calls. Use when the user asks to create or close Telegram groups/topics, add or remove members, send messages, inspect or move chats between Telegram folders, or check/retry queued operations through the `telegram-assistant` project. Triggers on phrases like «добавь @username в чат», «создай топик», «закрой топик», «отправь сообщение в чат», «перенеси чат в folder», «проверь операцию», «health».
---

# telegram-assistant skill

This skill teaches the agent how to turn a human request into a safe invocation
of the existing `telegram-assistant` CLI. The agent does not build a
new Telegram bot, does not call Telethon directly, and does not change Telegram
state without an explicit human confirmation.

The detailed resource/action catalogue and the per-scenario instructions live
in later sections of this file (see "Resources & actions" and "Scenarios").
This section is the contract that applies to every action.

## Project layout the agent must know

- The CLI entry point is `telegram-assistant`. Every action below is a
  subcommand of it (`groups create`, `topics bulk-create`, `members bulk-add`,
  `messages send`, `folders inspect`, `operations status`, ...).
- The runtime config lives at `data/config.yml`. The agent reads it for
  defaults and never edits it by hand — the only write path is the
  `access add` command (and only after a confirmed `--dry-run`). Notable keys:
  - `telegram.default_chat_folder.folder_name` — used as `--folder-name` when
    the human request does not name a folder explicitly.
  - `telegram.pin_min_interval_seconds` (default `2.0`, `0` disables) — minimum
    seconds between two pin/unpin calls on one chat. Explains why a pin series
    is slow; read it before assuming a pin command hung.
  - `mcp` — optional Streamable-HTTP MCP/OAuth config. This skill still uses
    the CLI for Telegram actions; only inspect MCP settings when the human
    explicitly asks about MCP server setup or smoke testing.
- Telethon session, SQLite database and bearer token also live under `data/`.
  The agent does not touch them; it only invokes the CLI.
- The skill itself is loaded from `./skills/telegram-assistant/SKILL.md`
  inside the project repository.

## Primary interface: the CLI, not Telethon

- The agent's only way to change Telegram state is the CLI shipped with the
  project. Do not import Telethon, do not call the HTTP API directly, and do
  not call the optional MCP server directly unless the human explicitly asks to
  test or debug MCP itself. Do not write custom Python that bypasses the CLI.
- If a request cannot be expressed as one of the listed CLI commands, the
  agent stops and asks the human instead of inventing a new code path.

## Liveness check: `health`

Run `health` **only when there is a reason to** — do not probe proactively when
nothing is wrong:

```bash
telegram-assistant health
```

- Do **not** run `health` before a change just to be safe. Go straight to the
  command flow. Run `health` only when something actually looks wrong:
  a command fails, a dry-run reports an auth/DB/folder problem, the session
  looks unauthorised, or the human explicitly asks «проверь health».
- If `health` reports a problem (auth missing, DB unreachable, default folder
  missing, etc.), stop and report it. Do not attempt to "fix" it by running
  other commands.
- `health` is read-only; no confirmation is needed.
- Once `health` has succeeded in a session, do not repeat it for later
  commands unless a new failure surfaces.

## Confirmation policy

Commands fall into three buckets:

1. **Read-only** — `health`, `folders inspect`, `operations status`,
   `groups get-layout`, `members list`. Run them immediately, no
   confirmation, no `--dry-run`.
2. **State-changing, single object** — `groups create`, `groups set-layout`,
   `groups rename`, `topics create`, `topics close`, `topics open`,
   `topics rename`,
   `messages send` (single chat),
   `messages react`, `messages forward`, `notifications mute`,
   `notifications unmute`, `folders add-chat`, `folders remove-chat`,
   `operations retry`. Always:
   prepare command → run with `--dry-run` →
   show the plan and dry-run output → ask for explicit human confirmation
   **via the `AskUserQuestion` tool** → run the same command without
   `--dry-run`.
3. **State-changing, bulk or destructive** — `topics bulk-create`,
   `members bulk-add`, `members bulk-remove`, `messages send` in fan-out mode
   (folder + topic name). Same flow as bucket 2, plus the plan must show how
   many objects are affected, and `members bulk-remove` must explicitly list
   every user it would touch.

Confirmation rules:

- The agent **must** request every confirmation through the
  `AskUserQuestion` tool — never as plain inline text. Present the dry-run
  plan, then call `AskUserQuestion` with a clear yes/no choice (e.g.
  «Выполнить» / «Отмена»). Run the real command only after the human picks
  the affirmative option.
- A confirmation is "explicit" when the human selects the affirmative
  `AskUserQuestion` option (or writes «да», «выполни», «подтверждаю», «ок»,
  presses a confirmation button, or similar). Silence,
  «давай посмотрим», «может быть» are not confirmations.
- A confirmation applies only to the exact command shown in the plan. If
  parameters change (different chat, different list of users, different
  text), the agent prepares a new plan and a new dry-run.
- The agent never confirms on behalf of the human and never auto-retries a
  destructive command after a failure.

## Protected accounts

- Technical/service accounts of this project and `@planfix_bot` must never be
  removed from chats automatically. `members bulk-remove` against them is
  only allowed when the human asks for it explicitly and approves the
  `--force` flag in the plan.
- If a dry-run shows that a destructive command would touch a protected
  account, the agent stops, names the account, and asks the human whether to
  proceed with `--force`.

## Temporary files in `/tmp`

Bulk commands (`topics bulk-create`, `members bulk-add`,
`members bulk-remove`, fan-out `messages send`) take a `--file` argument
pointing to a CSV or JSON file. The agent prepares those files itself.
`messages send --rich-markdown` follows the same rules with a UTF-8
markdown file (e.g. `/tmp/telegram-assistant-article.md`).

Rules:

- Write the temporary file under `/tmp`, never inside the project repository
  and never inside `data/`.
- Use a descriptive, deterministic name so the same session can find it
  again, e.g.
  - `/tmp/telegram-assistant-topics.csv`
  - `/tmp/telegram-assistant-users.csv`
  - `/tmp/telegram-assistant-message.json`
  - `/tmp/telegram-assistant-article.md` (for `--rich-markdown`)
- Show the file path and the file contents to the human as part of the plan,
  so the dry-run output can be verified against what the agent actually
  prepared.
- Treat each temporary file as belonging to the current request. The agent
  may overwrite it for a fresh request in the same session, but never edits
  a file mid-flight (between dry-run and the real run).
- Never commit these files. They live only on the runtime machine.

## The 11-step agent algorithm

Every state-changing request is processed in the same order. The agent does
not skip steps, even if the request looks obvious.

1. Read the human request (or forwarded message) verbatim.
2. Determine resource and action from the catalogue
   (see "Resources & actions"). If the request maps to no entry, stop and
   ask.
3. Extract parameters: chat, topic, users, role, text, `external_ref`
   (alias `planfix_task_id`), folder. Treat anything missing as missing —
   never invent values.
4. If a required parameter is missing or ambiguous, ask a short clarifying
   question (one question, no preamble). For `messages send`, if the message
   text is missing, ask the human for it (AskUserQuestion) — never invent the message.
5. Do **not** run `telegram-assistant health` proactively. Skip it when nothing
   is wrong and go to the next step; run it only if a later command or dry-run
   surfaces an auth/DB/folder problem, or the human asks for it.
6. For bulk-style commands, prepare a temporary CSV/JSON in `/tmp` as
   described above.
7. For state-changing commands that support `--dry-run`, run with
   `--dry-run` first. The supported set is: `groups create`,
   `groups set-layout`, `groups rename`, `topics create`,
   `topics bulk-create`, `topics close`, `topics open`, `topics rename`,
   `members bulk-add`, `members bulk-remove`,
   `messages send`, `messages react`, `messages forward`,
   `notifications mute`, `notifications unmute`, `folders add-chat`,
   `folders remove-chat`, `operations retry`.
8. Present a short plan to the human: what was found (chat id, folder,
   matched users), the full command that would run, and the relevant parts
   of the dry-run output (`status = dry_run`, planned actions, validation
   errors if any).
9. Ask for an explicit confirmation **via the `AskUserQuestion` tool**
   (a yes/no choice on the exact command shown). Do not move on after
   silence or vague replies, and do not accept a confirmation collected
   any other way.
10. Run the real command — the same command as in step 7, with `--dry-run`
    removed.
11. Return a short result: done / already done / skipped / Telegram error /
    needs manual review (`needs_review`). Reuse the wording from the CLI
    output; do not paraphrase error codes.

## Scope of the skill

This skill only describes how to drive the existing CLI. It does not
authorise the agent to:

- write a new Telegram bot or HTTP service;
- add Planfix-side automation;
- call Telethon or the HTTP API directly;
- change Telegram state without a confirmed plan;
- guess at chats, topics or usernames when the match is not exact;
- use real client names, real usernames or real invite links in examples.

When in doubt the agent stops and asks; that is the default, not the
exception.

## Resources & actions

The agent translates every request into exactly one resource/action pair
from the table below. If a request maps to nothing in this table, the
agent stops and asks for clarification — it does not invent a new path.

| Resource | Action | When to pick | CLI command |
|---|---|---|---|
| `auth` | `login` | The human asks to (re-)log in the technical Telegram account. The agent never runs this itself. | `telegram-assistant auth` |
| `health` | `check` | Pre-flight before any change; or the human asks "is everything alive?". | `telegram-assistant health` |
| `groups` | `create` | Create a new supergroup (title or `external_ref`, alias `planfix_task_id`), optionally with members/admins and folder placement. | `telegram-assistant groups create ...` |
| `groups` | `set-layout` | Change the topics layout (list ↔ tabs) on an existing forum supergroup. | `telegram-assistant groups set-layout ...` |
| `groups` | `get-layout` | Read the current topics layout (`list` or `tabs`) for a forum supergroup. | `telegram-assistant groups get-layout ...` |
| `groups` | `rename` | Rename an existing supergroup (change its title). | `telegram-assistant groups rename ...` |
| `topics` | `create` | Add one forum topic to an existing supergroup. | `telegram-assistant topics create ...` |
| `topics` | `bulk-create` | Add several topics to one chat from a CSV/JSON list. | `telegram-assistant topics bulk-create ...` |
| `topics` | `close` | Close (but not delete) an existing topic. | `telegram-assistant topics close ...` |
| `topics` | `open` | Reopen a previously closed topic. | `telegram-assistant topics open ...` |
| `topics` | `rename` | Rename an existing forum topic (change its title). | `telegram-assistant topics rename ...` |
| `members` | `bulk-add` | Add one or many users to a chat, optionally as admin. | `telegram-assistant members bulk-add ...` |
| `members` | `bulk-remove` | Remove one or many users from a chat (kick or permanent ban). | `telegram-assistant members bulk-remove ...` |
| `members` | `list` | Read-only: list a chat's participants (`--query`, `--filter all\|admins\|bots`, `--limit`, default 200), or check one user's membership with `--user` (READ-gated, never writes). | `telegram-assistant members list ...` |
| `messages` | `send` | Send a message or service command to one chat/topic, or fan it out across a folder. `--rich-markdown <file.md>` sends a Telegram rich message (article) instead of plain text, with paragraph spacing, line splitting and `<tg-collage>` grouping on by default (`--no-spaced-paragraphs`, `--no-line-breaks`, `--media-group`) and local media resolved from the article's directory (`--rich-file`, `--vault-dir`). | `telegram-assistant messages send ...` |
| `messages` | `recent` | Read-only: return the most recent messages from a chat (READ-gated; default limit 5). | `telegram-assistant messages recent ...` |
| `messages` | `react` | Set (`--emoji`) or clear (`--clear`) an emoji reaction on a message (`--message-id`, WRITE-gated). | `telegram-assistant messages react ...` |
| `messages` | `forward` | Forward one or more messages (`--message-id`, repeatable) from a source to a target chat (READ-gated source, WRITE-gated target). | `telegram-assistant messages forward ...` |
| `messages` | `delete` | Delete one or more messages (`--message-id`, repeatable) from a chat (DELETE-gated; `--revoke`/`--no-revoke`, `--dry-run`, `--force`). Honors `telegram.access.delete_only_session_messages` (default true, overridable per access rule). | `telegram-assistant messages delete ...` |
| `messages` | `edit` | Edit the text/caption of a sent message (`--message-id`, `--text`, WRITE-gated; `--dry-run`). Honors `telegram.access.edit_only_session_messages` (default true, overridable per access rule). | `telegram-assistant messages edit ...` |
| `messages` | `pin` | Pin a message in a chat (`--message-id`, WRITE-gated; `--silent`, `--pm-oneside`, `--dry-run`). Paced server-side against Telegram's pin `FLOOD_WAIT`. | `telegram-assistant messages pin ...` |
| `messages` | `unpin` | Unpin a message (`--message-id`) or all pinned messages (`--all`) in a chat (WRITE-gated; `--dry-run`). Shares the pin pacing gate. | `telegram-assistant messages unpin ...` |
| `messages` | `download` | Download the media of an existing message to a local file (`--message-id`, `--out` file / `--dir` directory, READ-gated; `--max-bytes`, `--dry-run`). Never overwrites — a taken name becomes `report (1).pdf`. | `telegram-assistant messages download ...` |
| `messages` | `search` | Read-only: text-search a chat's messages newest-first (`--query`, READ-gated; `--from`, `--limit`, `--minutes` or `--from-date`/`--to-date`, `--topic-id`). | `telegram-assistant messages search ...` |
| `notifications` | `mute` | Mute a chat/contact's notifications, forever or for `--duration` hours. | `telegram-assistant notifications mute ...` |
| `notifications` | `unmute` | Restore normal notifications for a chat/contact. | `telegram-assistant notifications unmute ...` |
| `folders` | `inspect` | Read-only: list chats inside a Telegram folder. | `telegram-assistant folders inspect ...` |
| `folders` | `add-chat` | Move an existing chat into a folder. | `telegram-assistant folders add-chat ...` |
| `folders` | `remove-chat` | Remove a chat from a folder (idempotent no-op if absent). | `telegram-assistant folders remove-chat ...` |
| `operations` | `status` | Read-only: show queue status for a previously created operation. | `telegram-assistant operations status ...` |
| `operations` | `retry` | Reset a failed or `needs_review` operation so the worker can re-run it. | `telegram-assistant operations retry ...` |
| `access` | `list` | Read-only: print the effective access policy (allow-all, or the deny-by-default rules and the capabilities each grants). | `telegram-assistant access list` |
| `access` | `check` | Resolve a chat and report whether the policy grants `read`/`write`/`delete` (exit 0 granted, 3 denied, 2 unresolved). `unresolved_refs` in the payload names stale `chat:` rules, which are skipped with a warning rather than failing the command. | `telegram-assistant access check --entity <ref> --permission read\|write\|delete` |
| `access` | `add` | Append one access rule (`--entity`/`--folder`/`--all` + `--permission read,write,delete`) to `data/config.yml`; hot-reload applies it live. Supports `--dry-run`. | `telegram-assistant access add ...` |

### Per-pair extraction and flag rules

For every pair below: **Extract** = what the agent must lift verbatim
from the request; **Required flags** = flags without which the agent
asks instead of guessing; **From config** = flags the agent may default
from `data/config.yml`; **Temp file** = whether `/tmp/...` is needed;
**Automation** = what the agent does without asking; **Confirmation** =
when a real (non-dry-run) call is allowed; **Typical errors** = error
messages the agent must surface verbatim instead of paraphrasing.

Most chat-targeting commands (`messages send`, `messages recent`,
`messages react`, `groups rename`, `topics create`/`close`/`open`/`rename`/`bulk-create`,
`members bulk-add`/`bulk-remove`, `notifications mute`/`unmute`,
`folders add-chat`/`remove-chat`) also accept
`--entity` as a flexible alternative to
`--chat-id` / `--chat-name`. `messages forward` uses the same reference
forms via `--from-entity` / `--to-entity` (alternatives to
`--from-chat-id` / `--to-chat-id`). `--entity` takes a numeric id (with or
without the `-100` prefix), an `@username`, a `t.me` / invite link, a
phone, or an exact chat title; exactly one of `--chat-id` / `--chat-name`
/ `--entity` is allowed per call. When the resolver cannot resolve the
reference the CLI exits with code 2 (`EntityNotFoundError` /
`AmbiguousEntityError`) — surface the message and ask, do not guess.

If the project configures `telegram.access` in `data/config.yml`, every
chat-scoped command is gated (read / write / delete — capabilities are
independent, so `write` does not imply `read`). When the policy does not
permit a chat or destination folder the CLI exits with code 3 and prints
`access denied ...`. The agent surfaces that verbatim and stops — it never
widens access on its own initiative. Granting access is only done via an
explicit human request through `access add` (confirmed after `--dry-run`),
never silently to get a blocked command through.

A rule targets one chat (`chat`), a list (`chats`), a folder **by name**
(`folder`), a folder **by id** (`folder_id`), or the wildcard (`all`). Telegram
allows two folders with the same title, and a `folder:` rule deliberately
**unions all** folders with that name — every chat in either one is covered. To
grant on exactly one of two same-named folders, the config needs a `folder_id`
rule (the folder id is shown by `folders inspect`). `access add` writes only
`--entity` / `--folder` / `--all` rules; a `folder_id` rule is a hand-edit of
`data/config.yml`, so the agent reports the need and lets the human make it.

Folder rules also cover the folder *listing* on the remote surfaces: the HTTP
`GET /telegram/folders/{name}` route and the MCP `telegram_folders_inspect` tool
require `read` on the folder (403 otherwise) because the payload lists every
chat in it; the local `folders inspect` CLI is ungated. After `folders add-chat`
/ `remove-chat` or a `groups create` placement, the cached folder-membership map
is dropped, so a fresh grant (or revocation) is in force on the very next
command rather than after `folder_cache_ttl` seconds.

#### `auth` / `login`

- Extract: nothing.
- Required flags: none.
- From config: none.
- Temp file: no.
- Automation: none — this is interactive and prompts for phone, code,
  and optional 2FA password in a terminal. The agent never invokes it
  and never collects credentials in chat.
- Confirmation: the human runs it themselves.
- Typical errors: `Auth failed: ...` (surface as-is and stop).

#### `health` / `check`

- Extract: nothing.
- Required flags: none.
- From config: none.
- Temp file: no.
- Automation: run automatically once per session before the first
  state-changing command (algorithm step 5).
- Confirmation: not required (read-only).
- Typical errors: any non-zero exit or any non-`ok` field in the
  output — stop, repeat the message, do not try to "fix" it.

#### `groups` / `create`

- Extract: `--title` (or `--external-ref`), `--admin` and `--member`
  lists (`--manager` is an alias for `--member` — same regular-member
  role, just a separate bucket), optional `--contact "<phone>|<name>"`
  entries (repeatable) for users only reachable by phone, optional
  `--about`, optional
  `--topics-layout` (`list` or `tabs`) when the human names how topics
  should open. `--external-ref` is the generic idempotency anchor;
  `--planfix-task-id` is a backward-compat alias that maps onto it.
- Required flags: at least one of `--title` or `--external-ref`
  (`--planfix-task-id`).
- From config: `--folder-name` defaults to
  `telegram.default_chat_folder.folder_name`; reserve admins/members
  come from `telegram.reserve_admins` / `telegram.reserve_members`;
  `--topics-layout` defaults to `telegram.defaults.topics_layout`;
  members get `telegram.defaults.default_member_permissions` (create
  topics / pin messages). When the Planfix plugin is enabled
  (`plugins.planfix.enabled: true`), the effective Telegram title also
  gets `plugins.planfix.group_title_postfix` appended (the idempotency
  key stays on the raw title), and `plugins.planfix.cleanup_messages`
  (opt-in, default off) removes the bot's welcome, the `/task` command,
  and the bot's reply after creation — surface the effective title in
  the plan so the postfix is visible.
- Temp file: no — admins and members go on the command line as repeated
  `--admin @employee_username` / `--member @member_username` flags;
  phone contacts as repeated `--contact "<phone>|<name>"`.
- Automation: include `--external-ref` (or the `--planfix-task-id`
  alias) when the human gives one; with the Planfix plugin enabled the
  dry-run plan shows whether `@planfix_bot` is among planned members so
  the `/task <id>` service message will actually fire, the
  `effective_title` (raw title + postfix), and the resolved
  `topics_layout`. For `--contact`, the phone is normalised (dirty
  formats like `89222222222`, `+7-922-222-22-22`, and `t.me` phone
  links all collapse to `+<digits>`); the dry-run plan shows the
  canonical phone. Each contact is imported into the technical account's
  Telegram contacts (so the user becomes resolvable) and then added as a
  regular member — a phone with no Telegram account is recorded in
  `skipped`/`contacts_imported` and the group is still created.
- Confirmation: required after dry-run.
- Typical errors: `group create requires external_ref or non-empty
  title`, `invalid --contact ...: expected "<phone>|<name>"`,
  `invalid phone reference: ...` (un-normalisable phone), folder errors
  from `resolve_folder`, `GroupCreateFailed`, `GroupCreateNeedsReview`.

#### `groups` / `set-layout`

- Extract: `--chat-id` (numeric supergroup id) and `--layout` when the
  human names one (`list` or `tabs`).
- Required flags: `--chat-id`.
- From config: `--layout` defaults to `telegram.defaults.topics_layout`
  when the human does not name a target layout. The agent surfaces the
  effective layout in the plan so the human can override it.
- Temp file: no.
- Automation: trigger on requests like «переключи топики чата X на
  tabs/list» or «сделай в чате X вкладки/список». Always treat this as
  a single-object state change. The operation is idempotent — replaying
  the same layout completes immediately.
- Confirmation: required after dry-run.
- Typical errors: `invalid --layout 'grid': expected 'list' or 'tabs'`
  (CLI exit code 2), `GroupLayoutSetNeedsReview` (FLOOD_WAIT — retry via
  `operations retry`), `GroupLayoutSetFailed` (Telethon error captured
  on the operation row, e.g. chat is not a forum, missing admin rights).

#### `groups` / `get-layout`

- Extract: `--chat-id`.
- Required flags: `--chat-id`.
- From config: none.
- Temp file: no.
- Automation: read-only, run immediately when the human asks «какой
  layout у чата X» / «как сейчас отображаются топики в X». No `--dry-run`.
- Confirmation: not required (read-only).
- Typical errors: `groups get-layout failed: ...` (chat not found, chat
  is not a forum, session not authorized) — surface verbatim and stop.

#### `groups` / `rename`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  `--new-title` (the new group title), optional `--reason`.
- Required flags: exactly one chat reference and `--new-title`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first
  (resolves + validates without renaming), show the plan, wait for
  confirmation, then run without `--dry-run`. Map «переименуй чат X в
  "Новое название"» → `--entity X --new-title "Новое название"`. The
  operation is idempotent on the target title — replaying the same rename
  completes immediately; a different title is a fresh operation.
- Confirmation: required (bucket 2).
- Typical errors: `group rename requires a non-empty --new-title`,
  `GroupRenameNeedsReview` (FLOOD_WAIT — retry via `operations retry`),
  `GroupRenameFailed` (Telethon error captured on the operation row),
  `access denied ...` (exit code 3), entity not-found / ambiguous (exit
  code 2).

#### `topics` / `create`

- Extract: `--topic-name`, chat reference (`--chat-name` or `--chat-id`),
  optional `--external-ref` (alias `--planfix-task-id`), optional `--message`.
- Required flags: `--topic-name` and exactly one of `--chat-name` /
  `--chat-id`.
- From config: `--folder-name` defaults to the configured chat folder
  when `--chat-name` is used.
- Temp file: no.
- Automation: prefer `--chat-name` + `--folder-name` over numeric chat
  ids when the human names a chat by title.
- Confirmation: required after dry-run.
- Typical errors: `exactly one of --chat-id or --chat-name must be
  supplied`, `topic create requires non-empty topic_name`,
  `AmbiguousTopicNameError`, folder lookup errors.

#### `topics` / `bulk-create`

- Extract: chat reference, list of topics (each: `topic_name`, optional
  `external_ref` (alias `planfix_task_id`), optional `message`).
- Required flags: chat reference and `--file`.
- From config: `--folder-name` default.
- Temp file: yes — write a CSV
  (`external_ref,topic_name,message`) or a JSON list to
  `/tmp/telegram-assistant-topics.csv` (or `.json`).
- Automation: dedupe rows locally before writing the file; still run
  `--dry-run` and rely on its `duplicate_topic_name_in_file` /
  `duplicate_external_ref_in_file` flags.
- Confirmation: required after dry-run. Show the row count and any
  warnings the dry-run reported.
- Typical errors: `--file is required (CSV or JSON)`, `--file path does
  not exist: ...`, per-row duplicate / already-exists warnings.

#### `topics` / `close`

- Extract: chat reference and topic reference (`--topic-name` or
  `--topic-id`), optional `--reason`.
- Required flags: exactly one of each pair.
- From config: `--folder-name` default.
- Temp file: no.
- Automation: none — closing is destructive even though history is
  preserved.
- Confirmation: required after dry-run; the plan must call out
  `already_closed: true` if the dry-run reports it.
- Typical errors: `TopicNotFoundError`, `AmbiguousTopicNameError`,
  folder errors.

#### `topics` / `open`

- Extract: chat reference and topic reference (`--topic-name` or
  `--topic-id`), optional `--reason`.
- Required flags: exactly one of each pair.
- From config: `--folder-name` default.
- Temp file: no.
- Automation: none — WRITE-gated state change (reopening a closed topic).
- Confirmation: required after dry-run; the plan must call out
  `already_open: true` if the dry-run reports it (real run is a no-op).
- Typical errors: `TopicNotFoundError`, `AmbiguousTopicNameError`,
  folder errors.

#### `topics` / `rename`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  topic reference (`--topic-id` or `--topic-name`), `--new-title` (the new
  topic title), optional `--reason`.
- Required flags: exactly one chat reference, exactly one of `--topic-id`
  / `--topic-name`, and `--new-title`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first
  (resolves chat + topic and validates without renaming), show the plan,
  wait for confirmation, then run without `--dry-run`. Map «переименуй
  топик "Старое" в чате X в "Новое"» → `--entity X --topic-name "Старое"
  --new-title "Новое"`. The operation is idempotent on the target title —
  replaying the same rename completes immediately; a different title is a
  fresh operation.
- Confirmation: required (bucket 2).
- Typical errors: `topic rename requires a non-empty --new-title`,
  `TopicNotFoundError`, `AmbiguousTopicNameError`,
  `TopicRenameNeedsReview` (FLOOD_WAIT — retry via `operations retry`),
  `TopicRenameFailed`, `access denied ...` (exit code 3), entity
  not-found / ambiguous (exit code 2).

#### `members` / `bulk-add`

- Extract: chat reference, users (with role), optional `--operation-id`.
- Required flags: chat reference, and at least one of `--file`,
  `--user`, `--admin`.
- From config: `--folder-name` default.
- Temp file: yes when there are several users — write CSV (`user,role`)
  or JSON to `/tmp/telegram-assistant-users.csv`. Inline
  `--user`/`--admin` flags are fine for one or two entries.
- Automation: build the file from the request; pass `--admin` for
  managers/leads, `--user` for everyone else.
- Confirmation: required after dry-run; the plan must list users that
  are already in the chat and users the dry-run says cannot be added.
- Typical errors: `no users supplied: use --file, --user, or --admin`,
  role validation errors, `BulkMemberAddNeedsReview` for users that
  need manual handling.

#### `members` / `bulk-remove`

- Extract: chat reference and list of users; `--mode` (`ban_unban` for
  kick, `ban` for permanent blacklist) when the human is specific.
- Required flags: chat reference and at least one of `--file`/`--user`;
  the real run also needs `--yes`.
- From config: `--folder-name` default; the protected-user set is read
  from `telegram.reserve_admins`, `telegram.reserve_members`, and the
  hard-coded `@planfix_bot`.
- Temp file: yes for multi-user removals — `/tmp/telegram-planfix-
  assistant-users.csv` (one user per line is enough).
- Automation: always run `--dry-run` first, even for a single user.
  Never include a protected user without an explicit human ask, and
  never add `--force` on the agent's own initiative.
- Confirmation: required after dry-run, must list every user the
  dry-run would touch; if any are protected, the plan must name them
  and the agent asks again before adding `--force`.
- Typical errors: `refusing to remove without --yes (or use --dry-run
  to preview)`, protected-account refusals, `BulkMemberRemoveNeedsReview`.

#### `messages` / `send`

- Extract: `--text`, chat/topic references, optional `--operation-id`,
  optional attachments (`--file` for a local file path, `--file-url`
  for an http(s) URL — both repeatable), optional scheduling
  (`--schedule-at` ISO-8601 datetime, or `--delay` relative duration
  like `10m`, `2h`, `1d`), optional `--reply-to <message_id>` to
  thread the send as a reply (targeted-only; in a forum it wins over the
  topic root, keeping the reply inside the topic), and optional
  `--rich-markdown <file.md>` to send an article instead of plain text
  (with its own knobs: `--no-spaced-paragraphs`, `--no-line-breaks`,
  `--rich-file <reference>=<path>`, `--vault-dir <dir>`, `--media-group
  <index>=<collage|slideshow|none>`).
- Required flags: exactly one targeting shape — targeted
  (`--chat-id`/`--chat-name` + optional `--topic-id`/`--topic-name`)
  or mass (`--mass` or no chat ref, plus `--topic-name` and
  `--folder-name`). `--text` is required unless at least one
  `--file`/`--file-url` is supplied (in which case `--text` is the
  caption) or `--rich-markdown` is used. Attachments, scheduling,
  `--reply-to`, and `--rich-markdown` are targeted-only — never combine
  them with `--mass`.
- From config: `--folder-name` default for both targeted resolution and
  mass mode.
- Temp file: **only** for `--rich-markdown` — plain message text goes via
  `--text`, attachments via repeated `--file`/`--file-url`. `--file`
  points at a path that exists on the server running the CLI; the agent
  does not upload bytes. If the human pastes a long multi-line message,
  escape it for the shell; do not write it to a file the CLI cannot read.
  A rich article, in contrast, *must* be written to a UTF-8 `.md` file
  (e.g. `/tmp/article.md`) and passed as `--rich-markdown /tmp/article.md`.
  An existing note (an Obsidian file, a report) can be passed **as-is** —
  its local `![](image.png)` / `![[Pasted image.png]]` embeds are
  resolved relative to that file's own directory, so do not copy it to
  `/tmp` when it has local media; point `--rich-markdown` at the original.
  A leading `---` … `---` YAML frontmatter block is dropped on read, so a
  vault note needs no hand-editing; do not strip it yourself.
- Automation: pass service commands (`/task 123456`) verbatim. Pass at
  most one of `--schedule-at` / `--delay`. Map «отправь через 2 часа» →
  `--delay 2h`, «запланируй на 2026-06-07T09:00» → `--schedule-at`. The
  dry-run JSON echoes `files`, `file_urls`, `schedule_at`, `scheduled`,
  and `reply_to_message_id` so the plan can show attachments, the resolved
  send time, and any reply target. For a rich send it echoes the article
  as markers only — `rich_markdown: true`, `rich_markdown_chars`
  (post-normalization), `rich_markdown_blocks`, `rich_markdown_media`,
  `rich_markdown_wikilinks`, `rich_markdown_file`, `spaced_paragraphs`
  (the effective decision), `spaced` (what the pass actually did),
  `line_breaks`, `media_grouping`, `rich_markdown_groups` and
  `rich_files` — never the body; show the human those, plus the file
  path they can re-read.
- Rich markdown (`--rich-markdown`): use it when the human asks for a
  post/article/статья with formatting Telegram's plain text cannot carry
  — headings, tables, quotes, long-form (>4096 chars, up to 32 768). The
  server parses the markdown itself; the dialect is `#`…`######`
  headings, tables with alignment, task lists, `>` quotes, fenced code
  with a language, `---` dividers, `~~strike~~`, `==marked==`,
  `||spoiler||`, footnotes, math, and media — by **public https URL**
  (`![](https://…jpg "caption")`) or, on the CLI only, by **local file**
  (see below). Limits: 1..32 768 characters, ~500 blocks and 50 media
  attachments; the first is checked before any Telegram call, the other
  two are reported as warnings. Never combine it with
  `--text`/`--file`/`--file-url`/`--mass`. If a rich send fails, do
  **not** silently retry it as a plain `--text` message — report the
  error and ask.
- Wikilinks (**always on, no flag**): Obsidian `[[target]]` /
  `[[target|alias]]` links are expanded to plain text before any other
  pass runs — the alias wins when present, otherwise the target reads
  as Obsidian renders it (a leading `#` drops, every other `#` becomes
  ` > `). Only the first `|` splits target from alias, so further pipes
  stay in the alias; an empty half falls back to the other; `[[]]`/
  `[[|]]` are not links and ship verbatim. `![[…]]` embeds and anything
  inside code (inline or fenced) are left alone. This runs on every
  surface, not just the CLI — unlike frontmatter stripping and local
  media, a wikilink is meaningless in Telegram whoever sent it. Nesting
  (`[[[[a]]]]`) expands too, up to a bounded depth — an unrealistic note
  past that ships the remainder verbatim. The
  dry-run reports the count as `rich_markdown_wikilinks`.
- Paragraph spacing (**on by default**): the server renders neighbouring
  paragraphs tight against each other, so the CLI/HTTP/MCP insert a
  U+00A0-only spacer paragraph between two paragraphs and before every
  heading. Add `--no-spaced-paragraphs` (HTTP/MCP: `spaced_paragraphs:
  false`) only when the human asks for the markdown to go unchanged,
  e.g. because they hand-tuned the spacing. It switches off the spacer
  pass only — media grouping and local-media rewriting are independent,
  so mention `--media-group <i>=none` if they want the source truly
  byte-for-byte, and note wikilinks are always expanded regardless (no
  knob) — a `[[…]]`-bearing article can never go byte-for-byte. The flag is an error
  (exit 2 / 422) without `--rich-markdown`. The default also comes from
  `telegram.defaults.rich_markdown_spaced_paragraphs`. Spacers count
  toward both the character and the block limit; if spacing would push
  the article past 500 blocks the CLI sends it unspaced and warns.
- Line breaks (**on by default**): Telegram parses the markdown itself and
  folds a *single* newline inside a paragraph into a space, so two lines an
  author wrote under one another (the usual Obsidian «ссылка, ссылка» pair)
  would arrive as one run-on line. Each line of a paragraph is split into its
  own paragraph instead — the clients render those tight, so the result is two
  lines with no blank line between them, and the U+00A0 spacer above is
  deliberately not inserted there. Only top-level paragraphs are split; a
  quote, a list item or an HTML container keeps the author's shape. Add
  `--no-line-breaks` (HTTP/MCP: `line_breaks: false`) only when the human wants
  the paragraph left as written; the default also comes from
  `telegram.defaults.rich_markdown_line_breaks`. Like `--spaced-paragraphs`, it
  is an error (exit 2 / 422) without `--rich-markdown`.
- Local media (**CLI-only** — HTTP/MCP articles still take https URLs
  only): local `![](photo.png)`, `![](../img/clip.mp4)` and Obsidian
  `![[Pasted image 1.png|caption|300]]` embeds are resolved by default
  against the article's own directory, uploaded, and referenced from the
  markdown, so an Obsidian note can be sent unedited. `.jpg/.jpeg/.png/
  .webp` are photos, `.mp4/.mov/.webm/.mkv/.avi/.m4v/.gif` video,
  `.mp3/.ogg/.oga/.opus/.m4a/.wav/.flac` audio; any other suffix is an
  error. Captions come from the media
  title (`"caption"`), falling back to the alt text. For a file that
  does not live next to the article use `--rich-file
  <reference>=<path>` (repeatable; the reference is the target as
  written in the markdown, its URL-decoded form, or its bare file
  name), and `--vault-dir <dir>` to search a whole directory tree by
  file name — motivated by an Obsidian vault whose attachments sit
  elsewhere, but it applies to plain `![](photo.png)` targets too.
  A media line with its caption on the next line is resolved like any
  other. Unresolvable or ambiguous media is an **error naming the file** — the
  send is never made with the media silently dropped. The dry-run lists
  every resolved file under `rich_files` (`id`, `path`, `kind`,
  `caption`) without reading a byte. A chat that forbids media rejects
  the whole article (see the error list).
- Local media in an article needs `ffmpeg`/`ffprobe` on the box for correct
  playback metadata. An animated `.gif` is **converted to mp4** automatically;
  without `ffmpeg` a `.gif` is rejected (exit 2) with a message naming the fix.
  Videos without a probe still send — they may just show as an empty
  rectangle — and the reason is in the server log at `WARNING`. The Docker
  image ships both binaries, so this only bites on a host install without
  them.
- Media grouping (**default `collage`**): a run of 2+ consecutive media
  blocks with no text between them is wrapped in `<tg-collage>`, so the
  usual two or three Obsidian screenshots render as one collage instead
  of stretching the article. The dry-run reports every run under
  `rich_markdown_groups` (`index`, `size`, `mode`, `preceding_text`,
  `caption`).
  Override one run with `--media-group <index>=<collage|slideshow|none>`
  (repeatable; the index is the 0-based `index` from that list, an
  unknown one is exit 2). Media the author already wrapped in
  `<tg-collage>`/`<tg-slideshow>`/`<details>` is never re-grouped. The
  default comes from `telegram.defaults.rich_markdown_grouping`; HTTP
  and MCP get that default but have no per-group override.
- Group caption: Telegram shows **no** caption on a medium inside a
  collage/slideshow, only one caption for the group, so the captions of a
  grouped run are listed comma-separated as the group's caption
  (`Отлив, Прилив`) — that is the `caption` field of each
  `rich_markdown_groups` entry. Media without a caption adds nothing, and
  a run where nobody has one gets no caption at all. Quote the caption in
  the plan: it is what the human will actually see under the collage, and
  the per-image captions they wrote will not be shown. Switching a run to
  `none` brings the individual captions back.
- Grouping dialogue: when the dry-run reports a non-empty
  `rich_markdown_groups`, ask the human with `AskUserQuestion` whether
  any group should change before confirming the send — see the
  «`messages send` — rich message (article)» recipe for the exact
  question shape. A single group the human leaves as-is needs no second
  question.
- Confirmation: required after dry-run. Mass mode plans must list every
  resolved chat row and call out `would_skip` rows with their reason
  (`topic_not_found`, `topic_ambiguous`, `list_topics_failed: ...`).
- Typical errors: `messages send requires non-empty --text,
  --rich-markdown, or at least one --file/--file-url attachment`, `--mass cannot be
  combined with --chat-id or --chat-name`, `--file/--file-url/--schedule-at/--delay/--reply-to are
  only supported for targeted sends`, `provide only one of --schedule-at or
  --delay`, past-schedule rejection (exit code 2), missing/empty
  attachment file, non-http(s) `--file-url`, `MessageSendNeedsReview`.
  Rich-specific (all exit code 2, printed to stderr):
  `--rich-markdown cannot be combined with --text, --file, or --file-url`,
  `--rich-markdown is only supported for targeted sends, not mass mode`,
  `--rich-markdown file cannot be read: ...`, `--rich-markdown file is
  not valid UTF-8: ...`, `--rich-markdown file is empty: ...`,
  `--rich-markdown exceeds 32768 characters (N given)`,
  `--spaced-paragraphs/--no-spaced-paragraphs is only meaningful with
  --rich-markdown`, `--rich-file/--vault-dir are only meaningful with
  --rich-markdown`, `--media-group is only meaningful with
  --rich-markdown`, `--line-breaks/--no-line-breaks is only meaningful with
  --rich-markdown`, `--rich-file must be <reference>=<path> (... given)`,
  `--media-group must be <index>=<collage|slideshow|none> (... given)`.
  Media-specific (also exit code 2): `media file not found: <ref>
  (searched <dir>); pass --rich-file <name>=<path> or --vault-dir`,
  `media reference '<ref>' matches N files: ...; pass --rich-file
  <name>=<path> to choose one`, `unsupported media type for rich
  message: <path> (a rich message carries photo, video or audio only)`,
  `rich file override(s) match no media in the article: ...`, `rich file
  override '<ref>' points at a missing file: <path>`, `rich_files
  exceeds Telegram's 50 media attachments (N given)`, `unknown media
  group index N: the article has M media group(s)`, and — from Telegram
  itself — `chat <id> does not allow the media in this rich message:
  ChatSendMediaForbiddenError: ...` (the chat forbids media; the whole
  article is rejected, so ask the human for another chat or an article
  without media rather than retrying).
  Warnings (printed to stderr as `warning: ...`, the send still runs):
  `spaced_paragraphs disabled: N blocks would exceed the 500-block
  limit`, `article has N blocks, over Telegram's 500-block limit`,
  `article has N media attachments, over Telegram's 50 limit`. Relay
  them to the human — the last two mean Telegram may reject the send.

#### `messages` / `recent`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  optional `--limit` (count of recent messages), optional `--minutes`
  (only messages newer than `now - minutes`).
- Required flags: exactly one chat reference.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: read-only — run immediately when the human asks «покажи
  последние сообщения чата X» / «что писали в X». No `--dry-run`.
  `--limit` defaults to 5; pass it through only when the human names a
  count. Add `--minutes N` when the human scopes by time («что писали
  за последний час» → `--minutes 60`); it composes with `--limit`.
- Confirmation: not required (read-only). Still READ-gated by the
  `telegram.access` policy — if the chat is not permitted the CLI exits
  non-zero with `access denied`; surface that and stop.
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity
  must be supplied`, `--minutes must be a positive integer`,
  `access denied ...` (exit code 3), entity
  not-found / ambiguous (exit code 2).

#### `messages` / `react`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  `--message-id` (the message to react to), and either `--emoji` (set) or
  `--clear` (remove).
- Required flags: exactly one chat reference, `--message-id`, and exactly
  one of `--emoji` / `--clear`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first, show
  the plan, wait for confirmation, then run without `--dry-run`. Map
  «поставь 👍 на сообщение N» → `--emoji 👍 --message-id N`; «убери реакцию»
  → `--clear`.
- Confirmation: required (bucket 2).
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity must
  be supplied`, `--message-id must be a positive integer`, `provide either
  --emoji or --clear, not both`, `access denied ...` (exit code 3), entity
  not-found / ambiguous (exit code 2).

#### `messages` / `forward`

- Extract: source reference (`--from-chat-id` / `--from-entity`), target
  reference (`--to-chat-id` / `--to-entity`, or the normal target aliases
  `--chat-id` / `--chat-name` / `--entity`), and one or more `--message-id`
  (repeat the flag per message to forward).
- Required flags: exactly one source reference, exactly one target reference,
  and at least one `--message-id`.
- From config: `--folder-name` default when resolving target `--chat-name`.
- Temp file: no.
- Automation: none — forwarding is READ-gated on the source and WRITE-gated
  on the target. Run `--dry-run` first, show the plan, wait for confirmation,
  then run without `--dry-run`. Map «перешли сообщение N из чата A в чат B» →
  `--from-entity A --to-entity B --message-id N`.
- Confirmation: required (bucket 2).
- Typical errors: `at least one --message-id is required`, `every
  --message-id must be a positive integer`, `exactly one of --from-chat-id or
  --from-entity must be supplied`, `exactly one target must be supplied`,
  `access denied ...` (exit code 3), entity
  not-found / ambiguous (exit code 2).

#### `messages` / `delete`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`), one or
  more `--message-id` (repeat the flag per message), optional
  `--revoke`/`--no-revoke`, optional `--force`.
- Required flags: exactly one chat reference and at least one `--message-id`.
- From config: `--folder-name` default when resolving `--chat-name`. The
  `telegram.access.delete_only_session_messages` flag (default `true`) limits
  deletes to messages this server process sent; it can be overridden per
  access rule (chat > folder > `all` > policy default), so a chat like `me`
  may set `delete_only_session_messages: false` while the global default
  stays `true`. One fail-safe: if any `chat:` rule ref fails to resolve and
  that rule set `delete_only_session_messages: true` (same for
  `edit_only_session_messages`), the `true` applies to **every** chat until
  the config is fixed — the hardened chat can no longer be identified. If a
  delete/edit is unexpectedly refused, run `access check` and fix the ref it
  lists under `unresolved_refs`.
- Temp file: no.
- Automation: none — DELETE-gated destructive change. Run `--dry-run` first
  (resolves + authorizes + runs the session-limit check without deleting),
  show the plan, wait for confirmation, then run without `--dry-run`. Default
  `--revoke` (delete for everyone); add `--no-revoke` only when the human asks
  to delete just for themselves. Map «удали сообщение N в чате X» →
  `--entity X --message-id N`.
- Confirmation: required (bucket 2/3 — destructive). The plan must list every
  message id that would be deleted and whether revoke is on.
- Typical errors: `at least one --message-id is required`, `every --message-id
  must be a positive integer`, `exactly one of --chat-id, --chat-name, or
  --entity must be supplied`, `message delete forbidden ...` (id not sent by
  this process while `delete_only_session_messages` is on), `access denied ...`
  (exit code 3 — chat lacks the `delete` capability), entity not-found /
  ambiguous (exit code 2).

#### `messages` / `edit`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  `--message-id` (the message to edit), `--text` (the new text/caption).
- Required flags: exactly one chat reference, `--message-id`, and `--text`.
- From config: `--folder-name` default when resolving `--chat-name`. The
  `telegram.access.edit_only_session_messages` flag (default `true`) limits
  edits to messages this server process sent; it can be overridden per access
  rule (chat > folder > `all` > policy default), so a chat may set
  `edit_only_session_messages: false` while the global default stays `true`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first (resolves
  + authorizes + runs the session-limit check without editing), show the plan,
  wait for confirmation, then run without `--dry-run`. Map «поправь сообщение N
  в чате X на "новый текст"» → `--entity X --message-id N --text "новый текст"`.
- Confirmation: required (bucket 2).
- Typical errors: `messages edit requires non-empty --text`, `--message-id must
  be a positive integer`, `message edit forbidden ...` (id not sent by this
  process while `edit_only_session_messages` is on), Telegram edit-limit errors
  surfaced as clear domain messages (not own message, ~48h edit window elapsed,
  text unmodified), `access denied ...` (exit code 3 — chat lacks the `write`
  capability), entity not-found / ambiguous (exit code 2).

#### `messages` / `pin`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  `--message-id` (the message to pin), optional `--silent` (no notification),
  optional `--pm-oneside` (pin only on your side in a private chat).
- Required flags: exactly one chat reference and `--message-id`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first, show the
  plan, wait for confirmation, then run without `--dry-run`. Map «закрепи
  сообщение N в чате X» → `--entity X --message-id N`; add `--silent` when the
  human asks to pin without pinging members.
- Pacing: rapid pin bursts are what Telegram answers with `FLOOD_WAIT`, so the
  server paces them itself — at most one pin/unpin per chat every
  `telegram.pin_min_interval_seconds` (default `2.0`; `0` disables). The gate is
  shared through SQLite, so parallel CLI runs and the HTTP/MCP server wait for
  each other. A pin series of N messages therefore takes ~N×interval seconds;
  that is expected, not a hang — do not "work around" it by re-running faster.
  A `FLOOD_WAIT` from Telegram is slept through and retried within a bounded
  budget; only an exhausted budget (or a wait past the cap) surfaces as an error.
- Confirmation: required (bucket 2).
- Typical errors: `--message-id must be a positive integer`, `exactly one of
  --chat-id, --chat-name, or --entity must be supplied`, `access denied ...`
  (exit code 3 — chat lacks the `write` capability), entity not-found /
  ambiguous (exit code 2), `messages pin rate-limited by Telegram: ... Retry
  after Ns (next attempt at <ISO timestamp>)` — surface the next-attempt time
  verbatim and wait for it instead of retrying immediately.

#### `messages` / `unpin`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`), and
  either `--message-id` (unpin one) or `--all` (unpin every pinned message).
- Required flags: exactly one chat reference and exactly one of `--message-id`
  / `--all`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first, show the
  plan, wait for confirmation, then run without `--dry-run`. Map «открепи
  сообщение N в чате X» → `--entity X --message-id N`; «открепи всё» → `--all`.
- Pacing: same shared per-chat gate as `messages pin` (see above) — pins and
  unpins on one chat pace against each other.
- Confirmation: required (bucket 2).
- Typical errors: `provide either --message-id or --all, not both`,
  `--message-id must be a positive integer`, `exactly one of --chat-id,
  --chat-name, or --entity must be supplied`, `access denied ...` (exit code 3),
  entity not-found / ambiguous (exit code 2), `messages unpin rate-limited by
  Telegram: ... Retry after Ns (next attempt at <ISO timestamp>)`.

#### `messages` / `download`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  `--message-id` (the message whose media to download), and exactly one of
  `--out` (target file path) / `--dir` (target directory, original filename is
  kept); optional `--max-bytes` (reject media larger than this).
- Required flags: exactly one chat reference, `--message-id`, and exactly one
  of `--out` / `--dir`.
- From config: `--folder-name` default when resolving `--chat-name`. Paths are
  server-side — the file is written on the machine running the CLI. (The CLI is
  local/trusted and picks the path freely; the HTTP/MCP `out_dir` is instead
  confined to `telegram.download_root`, default the system temp dir, so a
  remote READ-only caller cannot write to an arbitrary directory.)
- Temp file: no (the download target is chosen by the human, not `/tmp` scratch).
- Automation: READ-gated — it reads the message and writes a local file. Run
  `--dry-run` first (resolves + authorizes + reports the planned target path
  without downloading), then run without `--dry-run`. Map «скачай файл из
  сообщения N чата X в /srv/out/» → `--entity X --message-id N --dir /srv/out/`.
- Never overwrites: when the target name is already taken the download goes to
  the first free `name (1).ext`, `name (2).ext`, … instead of replacing the
  existing file (the name is claimed atomically, so parallel downloads get
  distinct files, each created mode `0600` — owner-only, because the default
  root is the world-writable system temp dir). A missing target directory is
  created. The reported `path` in
  the result is the **actual** file written — read it from the output rather
  than assuming the requested name; the `--dry-run` path is the first free name
  at check time and is best-effort (a later download may take it first).
- Confirmation: not strictly a Telegram state change, but it writes to disk —
  show the resolved target path in the plan before the real run, and report the
  final `path` afterwards when it differs from the planned one.
- Typical errors: `provide exactly one of --out or --dir`, `--message-id must be
  a positive integer`, message has no downloadable media, media exceeds
  `--max-bytes`, `access denied ...` (exit code 3 — chat lacks the `read`
  capability), entity not-found / ambiguous (exit code 2).

#### `messages` / `search`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  `--query` (the required non-empty search text), optional `--from` (restrict
  to a sender), optional `--limit` (max matches, default 20), optional
  `--topic-id` (search inside
  one forum topic), and **one** time scope: either `--minutes` (relative window,
  only messages newer than `now - minutes`) or the fixed range
  `--from-date` + `--to-date`.
- Required flags: exactly one chat reference and `--query`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: read-only — run immediately when the human asks «найди в чате X
  сообщения про "..."». No `--dry-run`. Every filter (`--query`, `--from`,
  `--topic-id`, the date range) goes to Telegram in **one** server-side search
  request and is paged until `--limit` matches are collected, so a bounded range
  finds old messages too. Results are newest-first. Paging is capped at 20
  search requests, and rows dropped by the local filters do not count toward
  `--limit`, so a heavily filtered query can come back short of `--limit` even
  when older matches exist — narrow the date range or the sender rather than
  reporting "that's all there is".
- Date range: `--from-date` and `--to-date` take ISO-8601 timestamps **with a
  timezone** (e.g. `2026-07-01T00:00:00+03:00`), are **inclusive** on both ends,
  and must be given together. Map «найди в X сообщения про "оплата" с 1 по 10
  июля» → `--from-date 2026-07-01T00:00:00+03:00 --to-date
  2026-07-10T23:59:59+03:00`. Ask for the year/timezone rather than guessing
  when the human's phrasing is ambiguous. The result echoes the applied bounds
  normalised to UTC (`from_date`/`to_date`) — quote those when reporting what
  was actually searched.
- Confirmation: not required (read-only). Still READ-gated by the
  `telegram.access` policy — if the chat is not permitted the CLI exits with
  `access denied`; surface that and stop.
- Typical errors: `messages search requires a non-empty --query`, `--limit must
  be a positive integer`, `--minutes must be a positive integer`, `--from-date
  must be an ISO-8601 timestamp with timezone (got ...)`, `search_messages
  requires both from_date and to_date when using a date range`,
  `search_messages requires a timezone-aware from_date`, `search_messages
  requires from_date <= to_date`, `search_messages accepts either minutes or a
  from_date/to_date range, not both`, `exactly one of --chat-id, --chat-name, or
  --entity must be supplied` (all exit code 2), `access denied ...`
  (exit code 3), entity not-found / ambiguous (exit code 2).

#### `notifications` / `mute`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`),
  optional `--duration` (mute window in hours).
- Required flags: exactly one chat reference.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first,
  show the plan, wait for confirmation, then run without `--dry-run`.
  Omit `--duration` to mute forever; pass it only when the human names a
  number of hours («замьють на 3 часа» → `--duration 3`).
- Confirmation: required (bucket 2).
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity
  must be supplied`, `--duration must be a positive number of hours`,
  `access denied ...` (exit code 3), entity not-found / ambiguous (exit
  code 2).

#### `notifications` / `unmute`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`).
- Required flags: exactly one chat reference.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none — WRITE-gated state change. Run `--dry-run` first,
  show the plan, wait for confirmation, then run without `--dry-run`.
- Confirmation: required (bucket 2).
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity
  must be supplied`, `access denied ...` (exit code 3), entity not-found
  / ambiguous (exit code 2).

#### `folders` / `inspect`

- Extract: optional `--folder-name`.
- Required flags: none — defaults to the configured chat folder.
- From config: `--folder-name` default.
- Temp file: no.
- Automation: run immediately when the human asks "what chats are in
  folder X" or to disambiguate a chat lookup.
- Confirmation: not required (read-only).
- Typical errors: `FolderError` messages (folder missing, etc.).

#### `folders` / `add-chat`

- Extract: chat reference (`--chat-name` / `--chat-id`), optional
  `--folder-name`.
- Required flags: exactly one of `--chat-name` / `--chat-id`.
- From config: `--folder-name` default.
- Temp file: no.
- Automation: none beyond defaulting the folder name.
- Confirmation: required after dry-run; if the dry-run reports
  `already_in_folder: true`, restate that to the human and skip the
  real run unless they insist.
- Typical errors: `exactly one of --chat-id or --chat-name must be
  supplied`, `FolderError`.

#### `folders` / `remove-chat`

- Extract: chat reference (`--chat-name` / `--chat-id` / `--entity`),
  optional `--folder-name`.
- Required flags: exactly one of `--chat-name` / `--chat-id` / `--entity`.
- From config: `--folder-name` default.
- Temp file: no.
- Automation: none beyond defaulting the folder name.
- Confirmation: required after dry-run; if the dry-run reports
  `already_absent: true`, restate that to the human and skip the real run
  unless they insist.
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity must
  be supplied`, `FolderError`.

#### `operations` / `status`

- Extract: `--operation-id`.
- Required flags: `--operation-id`.
- From config: none.
- Temp file: no.
- Automation: run as soon as the human gives an operation id.
- Confirmation: not required (read-only).
- Typical errors: `operation <id> not found`.

#### `operations` / `retry`

- Extract: `--operation-id`.
- Required flags: `--operation-id`.
- From config: none.
- Temp file: no.
- Automation: always run `operations status` first and show the human
  the failing items before any retry attempt.
- Confirmation: required after dry-run.
- Typical errors: `operation <id> not found`, `operation <id> is
  completed; nothing to retry`.

#### `access` / `list`

- Extract: nothing.
- Required flags: none.
- From config: reads the loaded `telegram.access` policy.
- Temp file: no.
- Automation: read-only — run immediately when the human asks «покажи
  права доступа» / «какие чаты разрешены». No `--dry-run`.
- Confirmation: not required (read-only).
- Typical errors: none beyond config-load failures — surface verbatim.

#### `access` / `check`

- Extract: chat reference (`--entity`), `--permission` (one of
  `read`/`write`/`delete`).
- Required flags: `--entity` and `--permission`.
- From config: reads the loaded `telegram.access` policy.
- Temp file: no.
- Automation: read-only diagnostic — run immediately when the human asks
  «есть ли доступ на запись в чат X» and report the verdict and matched rule.
  No `--dry-run`.
- Confirmation: not required (read-only).
- Typical errors: exit `0` granted, exit `3` denied (`access denied ...`),
  exit `2` when the entity cannot be resolved — surface the message and stop.

#### `access` / `add`

- Extract: exactly one target (`--entity` / `--folder` / `--all`) and
  `--permission` (comma-separated subset of `read,write,delete`).
- Required flags: one target and `--permission`.
- From config: writes into `data/config.yml` (the one place the agent may edit
  it, and only for `access add`); hot-reload applies the rule within ~2s.
- Temp file: no.
- Automation: none — this widens access. Run `--dry-run` first to show the
  resulting rule, wait for explicit confirmation, then run without `--dry-run`.
  Warn the human that adding the first rule to a config with no
  `telegram.access` block switches the policy from allow-all to
  deny-by-default.
- Confirmation: required (bucket 2 — it changes policy).
- Typical errors: `exactly one of --entity, --folder, or --all must be
  supplied`, `--permission must list at least one of read,write,delete`,
  validation errors from the access-rule model (surface verbatim), entity
  not-found / ambiguous (exit code 2).

## Scenarios

Every scenario below uses anonymized identifiers only. The agent
re-uses these patterns and replaces them with the values the human
gave — never with real usernames, chat titles, or invite links from
its own memory.

### `groups create`

Request: «Создай группу для клиента Клиент / проект, задача 123456,
менеджер @manager_username, в работе @employee_username и
@member_username.»

1. Resource/action: `groups` / `create`.
2. Extracted: title `Клиент / проект`, `--planfix-task-id 123456`,
   `--admin @manager_username`, `--member @employee_username`,
   `--member @member_username`. Folder defaults to the configured
   `Planfix clients`. Add `--topics-layout tabs` only if the human asks
   for tabs; otherwise the configured `topics_layout` default applies.
3. Skip `health` unless a problem surfaces (don't probe when nothing is wrong).
4. Dry-run:

   ```bash
   telegram-assistant groups create \
     --title "Клиент / проект" \
     --planfix-task-id 123456 \
     --admin @manager_username \
     --member @employee_username \
     --member @member_username \
     --dry-run
   ```

5. Show plan: title and `effective_title` (raw title + configured
   postfix), folder (`Planfix clients`), resolved `topics_layout`,
   admins, members, reserve accounts from `resolved`, and
   `planned_actions` (create group, set default member permissions,
   set topics layout, add each member, promote each admin, create
   invite link, place into folder, send `/task 123456` if
   `@planfix_bot` is in planned members, clean up service messages).
6. Wait for «да» / «выполни». Re-run the same command without
   `--dry-run`.

### `groups set-layout`

Request: «Переключи топики чата -1003911170598 на tabs.»

1. Resource/action: `groups` / `set-layout`.
2. Extracted: `--chat-id -1003911170598`, `--layout tabs`. If the human
   does not name a layout, fall back to
   `telegram.defaults.topics_layout` and surface that choice in the plan.
3. Skip `health` unless a problem surfaces (don't probe when nothing is wrong).
4. Dry-run:

   ```bash
   telegram-assistant groups set-layout \
     --chat-id -1003911170598 \
     --layout tabs \
     --dry-run
   ```

5. Show resolved chat id, target layout, layout source
   (`cli` vs `config`), and the single planned action
   (`set topics layout to 'tabs' for chat -1003911170598`).
6. Wait for «да» / «выполни», then re-run the same command without
   `--dry-run`. On `needs_review` (FLOOD_WAIT) point the human at
   `operations retry`; do not auto-retry.

### `groups get-layout`

Request: «Какой layout у чата -1003915612716?»

1. Resource/action: `groups` / `get-layout`. No dry-run, no
   confirmation.
2. Run:

   ```bash
   telegram-assistant groups get-layout \
     --chat-id -1003915612716
   ```

3. Return the single-word output (`list` or `tabs`) verbatim. If the
   CLI exits non-zero, surface the message and stop.

### `groups rename`

Request: «Переименуй чат Клиент / проект в "Клиент / проект (архив)".»

1. Resource/action: `groups` / `rename`.
2. Extracted: `--chat-name "Клиент / проект"`,
   `--new-title "Клиент / проект (архив)"`. Folder defaults to the
   configured chat folder for the `--chat-name` lookup.
3. Skip `health` unless a problem surfaces (don't probe when nothing is wrong).
4. Dry-run:

   ```bash
   telegram-assistant groups rename \
     --chat-name "Клиент / проект" \
     --new-title "Клиент / проект (архив)" \
     --dry-run
   ```

5. Show resolved chat id, the new title, and the single planned action
   (`rename chat <id> to '<new title>'`).
6. Wait for «да» / «выполни», then re-run the same command without
   `--dry-run`. On `needs_review` (FLOOD_WAIT) point the human at
   `operations retry`; do not auto-retry.

### `topics create`

Request: «Создай топик "Документы" в чате Клиент / проект.»

1. Resource/action: `topics` / `create`.
2. Extracted: `--topic-name "Документы"`, `--chat-name "Клиент / проект"`.
   Folder defaults to `Planfix clients`.
3. Dry-run:

   ```bash
   telegram-assistant topics create \
     --topic-name "Документы" \
     --chat-name "Клиент / проект" \
     --dry-run
   ```

4. Show resolved chat id, planned actions (create topic, send first
   message). If `existing_topic_ids` is non-empty, surface the warning
   and ask the human whether to continue.
5. Wait for confirmation, then run without `--dry-run`.

### `topics bulk-create`

Request: «Заведи в чате Клиент / проект топики "Документы" и "Оплата".»

1. Resource/action: `topics` / `bulk-create`.
2. Prepare `/tmp/telegram-assistant-topics.csv`:

   ```csv
   external_ref,topic_name,message
   ,Документы,
   ,Оплата,
   ```

   Show the path and the contents in the plan.
3. Dry-run:

   ```bash
   telegram-assistant topics bulk-create \
     --chat-name "Клиент / проект" \
     --file /tmp/telegram-assistant-topics.csv \
     --dry-run
   ```

4. Show `items_count`, planned actions, and any warnings about
   duplicates or already-existing topic names.
5. Wait for confirmation, then re-run without `--dry-run`. Reuse the
   same `--operation-id` only if the human asks to resume a previous
   batch.

### `topics close`

Request: «Закрой топик "Документы" в чате Клиент / проект.»

1. Resource/action: `topics` / `close`.
2. Dry-run:

   ```bash
   telegram-assistant topics close \
     --chat-name "Клиент / проект" \
     --topic-name "Документы" \
     --dry-run
   ```

3. Show resolved `telegram_topic_id`, `already_closed`. If the topic is
   already closed, repeat that and ask the human whether they still
   want to run the no-op.
4. Otherwise wait for explicit confirmation and run without
   `--dry-run`.

### `topics open`

Request: «Открой топик "Документы" в чате Клиент / проект.»

1. Resource/action: `topics` / `open`.
2. Dry-run:

   ```bash
   telegram-assistant topics open \
     --chat-name "Клиент / проект" \
     --topic-name "Документы" \
     --dry-run
   ```

3. Show resolved `telegram_topic_id`, `already_open`. If the topic is
   already open, repeat that and ask the human whether they still
   want to run the no-op.
4. Otherwise wait for explicit confirmation and run without
   `--dry-run`.

### `topics rename`

Request: «Переименуй топик "Документы" в чате Клиент / проект в "Архив".»

1. Resource/action: `topics` / `rename`.
2. Dry-run:

   ```bash
   telegram-assistant topics rename \
     --chat-name "Клиент / проект" \
     --topic-name "Документы" \
     --new-title "Архив" \
     --dry-run
   ```

   Use `--topic-id <id>` instead of `--topic-name` when the human gives a
   numeric topic id.
3. Show resolved chat id, resolved `telegram_topic_id`, and the new
   title. If the topic name is ambiguous (`AmbiguousTopicNameError`) or
   not found (`TopicNotFoundError`), surface the message and ask.
4. Wait for explicit confirmation and run without `--dry-run`. On
   `needs_review` (FLOOD_WAIT) point the human at `operations retry`; do
   not auto-retry.

### `members bulk-add`

Request: «Добавь @employee_username и @member_username в чат
Клиент / проект, @manager_username — админом.»

1. Resource/action: `members` / `bulk-add`.
2. Prepare `/tmp/telegram-assistant-users.csv`:

   ```csv
   user,role
   @employee_username,member
   @member_username,member
   @manager_username,admin
   ```

3. Dry-run:

   ```bash
   telegram-assistant members bulk-add \
     --chat-name "Клиент / проект" \
     --file /tmp/telegram-assistant-users.csv \
     --dry-run
   ```

4. Show planned actions, users already in the chat, users the dry-run
   says cannot be added.
5. Wait for confirmation. Real run uses the same command without
   `--dry-run`.

### `members bulk-remove`

Request: «Убери @employee_username из чата Клиент / проект.»

1. Resource/action: `members` / `bulk-remove`.
2. Always start with `--dry-run`. For more than one user, write
   `/tmp/telegram-assistant-users.csv` with one user per line.
   For a single user, an inline `--user @employee_username` is fine.
3. Dry-run:

   ```bash
   telegram-assistant members bulk-remove \
     --chat-name "Клиент / проект" \
     --user @employee_username \
     --dry-run
   ```

4. Show every user the dry-run would touch. If any user is in the
   protected set (configured reserve accounts or `@planfix_bot`), name
   them, refuse to add `--force` on initiative, and ask whether to
   proceed.
5. After explicit confirmation, run without `--dry-run` and with
   `--yes`. Only add `--force` when the human approves it for the
   specific protected users named in the plan.

### `members list`

Request: «Кто состоит в чате Клиент / проект?» / «Есть ли
@planfix_bot в этих чатах?»

1. Resource/action: `members` / `list`. Read-only — run it immediately,
   no `--dry-run` (there is none), no confirmation.
2. Membership of one user is a **separate mode**: `--user <ref>` answers
   it with a single request per chat and returns `is_member` plus the
   role. Never use `members bulk-add --dry-run` for this — it plans an
   add without checking membership (`action: would_add` even for a user
   who is already there), so it answers a different question.

   ```bash
   telegram-assistant members list \
     --chat-name "Клиент / проект" \
     --user @planfix_bot
   ```

3. Full roster: omit `--user`. `--filter all|admins|bots` picks the
   Telegram-side filter, `--query <substring>` searches by
   username/first/last name (server-side for the default filter), and
   `--limit` caps the walk (default 200). `--user` and `--query` are
   mutually exclusive.
4. Read the reply: `participants_count` is the chat's total and
   `truncated: true` means the walk stopped early (limit reached, or
   Telegram's ~10k ceiling on a full enumeration) — say so instead of
   presenting a partial list as complete.
5. Checking many chats: loop the `--user` form over the chat ids from
   `folders inspect`. There is no folder-wide mode by design — one
   request per chat keeps it read-only and cheap.
6. Typical errors: `exactly one of --chat-id, --chat-name, or --entity
   must be supplied` and `unknown filter '...'` (exit 2); a chat with no
   READ grant exits 3.

### `messages send` — targeted

Request: «Отправь /task 123456 в топик "Документы" чата
Клиент / проект.»

1. Resource/action: `messages` / `send`.
2. Dry-run:

   ```bash
   telegram-assistant messages send \
     --text "/task 123456" \
     --chat-name "Клиент / проект" \
     --topic-name "Документы" \
     --dry-run
   ```

3. Show resolved chat id, topic id, planned action.
4. Wait for confirmation, then re-run without `--dry-run`.

### `messages send` — mass mode

Request: «Напомни во всех чатах Planfix clients в топике "Оплата"
прислать акт.»

1. Resource/action: `messages` / `send`. Warn the human up front that
   this fans out to every chat in the folder.
2. Dry-run:

   ```bash
   telegram-assistant messages send \
     --text "Пришлите, пожалуйста, акт сверки" \
     --topic-name "Оплата" \
     --mass \
     --dry-run
   ```

3. Show the full per-chat table from the dry-run: which chats will
   receive the message, which will be skipped (`topic_not_found`,
   `topic_ambiguous`, `list_topics_failed: ...`) and how many sends
   would actually happen.
4. Require an explicit confirmation that names the chat count before
   re-running without `--dry-run`.

### `messages send` — media / scheduled

Request: «Отправь файл /srv/exports/report.pdf в чат Клиент / проект
через 2 часа.»

1. Resource/action: `messages` / `send`. Targeted send with one
   attachment and a relative delay.
2. Dry-run:

   ```bash
   telegram-assistant messages send \
     --chat-name "Клиент / проект" \
     --folder-name "Planfix clients" \
     --file /srv/exports/report.pdf \
     --text "Отчёт" \
     --delay 2h \
     --dry-run
   ```

3. Show resolved chat id, the `files` / `file_urls` lists, and the
   resolved `schedule_at` / `scheduled` fields from the dry-run JSON.
   For URL attachments use `--file-url https://...` (repeatable); for an
   absolute send time use `--schedule-at 2026-06-07T09:00:00`. Pass at
   most one of `--schedule-at` / `--delay`.
4. Wait for confirmation, then re-run without `--dry-run`.

### `messages send` — rich message (article)

Request: «Опубликуй в чате Клиент / проект статью по итогам недели —
с заголовками и таблицей.»

1. Resource/action: `messages` / `send`. This is the one send shape that
   needs a temp file: plain `--text` cannot carry headings or tables.
2. Write the article to a UTF-8 markdown file (`/tmp/weekly.md`) using
   the Telegram rich dialect — `#`/`##` headings, aligned tables, `>`
   quotes, fenced code, `---`, and images as public https URLs
   (`![](https://…jpg "caption")`) or local files. Keep it under 32 768
   characters. Do not hand-insert blank-line tricks for spacing — the
   CLI inserts U+00A0 spacer paragraphs itself. If the human points at
   an existing note with local embeds (an Obsidian file), pass **that
   file** rather than copying the text: the media is resolved relative
   to the article's own directory.
3. Dry-run:

   ```bash
   telegram-assistant messages send \
     --chat-name "Клиент / проект" \
     --rich-markdown /tmp/weekly.md \
     --dry-run
   ```

4. Show the resolved chat id and the article markers from the dry-run
   JSON (`rich_markdown: true`, `rich_markdown_chars`,
   `rich_markdown_blocks`, `rich_markdown_media`, `rich_markdown_wikilinks`,
   `rich_markdown_file`, `spaced_paragraphs`, `spaced`, `line_breaks`,
   `media_grouping`, `rich_markdown_groups`, plus `rich_files` when the
   article carries local media) — the body is deliberately not echoed, so
   quote the file path and, if the human wants to review the text, show
   the file contents yourself. Relay any `warnings` verbatim.
5. If the dry-run reports a non-empty `rich_markdown_groups`, ask about
   the grouping **before** asking for the send confirmation. One
   `AskUserQuestion` call: «В статье N групп подряд идущих медиа, все
   будут отправлены как `<mode>`. Изменить группировку?» — `<mode>` is
   the `mode` the dry-run reported for those groups (`collage` unless
   `telegram.defaults.rich_markdown_grouping` says otherwise) — with
   options
   `Оставить как есть` / `Изменить`. Only if the human picks
   `Изменить`, ask one question per group — «После текста
   `<preceding_text>` как сгруппировать медиа?» with options
   `Collage` / `Slideshow` / `Ungrouped` — and re-run the dry-run with
   the chosen `--media-group <index>=<collage|slideshow|none>` flags
   (the `index` is the one from `rich_markdown_groups`). A single group
   the human leaves as-is needs no second question. Carry the same
   `--media-group` flags into the real send. Name each group's `caption`
   in the question when it is non-empty («подпись: `Отлив, Прилив`»):
   that one caption replaces the per-image captions, which a collage or
   slideshow does not show — a reason a human may pick `Ungrouped`.
6. Wait for confirmation, then re-run without `--dry-run`. Never add
   `--text`/`--file`/`--file-url`/`--mass` to a rich send. If it fails,
   report the error verbatim — do not fall back to a plain text send
   without asking, and do not strip the media to work around a
   `does not allow the media in this rich message` error without asking.

### `messages recent`

Request: «Покажи последние сообщения в чате Клиент / проект.»

1. Resource/action: `messages` / `recent`. Read-only, no dry-run, no
   confirmation.
2. Run (default limit 5; add `--limit N` only if the human names a
   count):

   ```bash
   telegram-assistant messages recent \
     --chat-name "Клиент / проект"
   ```

   `--entity` works too, e.g. `--entity @member_username` or
   `--entity -1001234567890`.
3. Return the recent messages verbatim. If the CLI exits with `access
   denied` (code 3) or an entity error (code 2), surface the message
   and stop.

### `folders inspect`

Request: «Покажи чаты в папке Planfix clients.»

1. Resource/action: `folders` / `inspect`. No dry-run, no confirmation.
2. Run:

   ```bash
   telegram-assistant folders inspect \
     --folder-name "Planfix clients"
   ```

3. Return the list of chats verbatim.

### `folders add-chat`

Request: «Перенеси чат Клиент / проект в папку Planfix clients.»

1. Resource/action: `folders` / `add-chat`.
2. Dry-run:

   ```bash
   telegram-assistant folders add-chat \
     --folder-name "Planfix clients" \
     --chat-name "Клиент / проект" \
     --dry-run
   ```

3. Show `folder_id`, resolved chat, and `already_in_folder`. If the
   chat is already there, restate that and skip the real run unless
   the human insists.
4. Otherwise wait for confirmation, then run without `--dry-run`.

### `folders remove-chat`

Request: «Убери чат Клиент / проект из папки Planfix clients.»

1. Resource/action: `folders` / `remove-chat`.
2. Dry-run:

   ```bash
   telegram-assistant folders remove-chat \
     --folder-name "Planfix clients" \
     --chat-name "Клиент / проект" \
     --dry-run
   ```

3. Show `folder_id`, resolved chat, and `already_absent`. If the chat is
   not in the folder, restate that and skip the real run unless the human
   insists.
4. Otherwise wait for confirmation, then run without `--dry-run`.

### `operations status`

Request: «Что с операцией op_2026_05_19_abcd?»

1. Resource/action: `operations` / `status`. No dry-run, no
   confirmation.
2. Run:

   ```bash
   telegram-assistant operations status \
     --operation-id op_2026_05_19_abcd
   ```

3. Return the per-status counts and any failing items.

### `operations retry`

Request: «Повтори операцию op_2026_05_19_abcd.»

1. Resource/action: `operations` / `retry`. First run `operations
   status` to show the human the current state.
2. Dry-run:

   ```bash
   telegram-assistant operations retry \
     --operation-id op_2026_05_19_abcd \
     --dry-run
   ```

3. Show `would_reset_operation`, the list of items that would be
   reset, and any "no-op" warning.
4. Wait for confirmation, then re-run without `--dry-run`.

### `auth`

Request: «Перелогинь технический аккаунт.»

1. Resource/action: `auth` / `login`. The agent does not run this.
2. Tell the human to run `telegram-assistant auth` themselves
   in a terminal where they can enter the phone, the code, and the
   optional 2FA password. The agent never asks for these values in
   chat and never relays them.
3. Once the human confirms the relogin is done, the agent re-runs
   `health` before any further state-changing command.

## When the agent must stop and ask

The agent stops and asks the human in any of the following situations.
"Stop" means: do not run another command, do not guess, do not retry.
Reuse the short templates from "Clarification templates" below.

- The request maps to no entry in the Resources & actions table — the
  resource/action is unclear or the request asks for something the CLI
  cannot do.
- A required parameter is missing: no username for `members bulk-add`
  / `members bulk-remove`, no chat reference for any chat-scoped
  command, no topic for `topics create` / `topics close` / `topics open`, no text for
  `messages send`, no `--operation-id` for `operations status` /
  `operations retry`.
- A lookup is ambiguous: more than one chat matches the title, more
  than one topic matches the name, more than one user matches the
  alias.
- `telegram-assistant health` reported any non-`ok` field or
  a non-zero exit. Surface the message verbatim; do not run anything
  else first.
- `--dry-run` returned an error or `status != dry_run`. Show the
  error to the human and ask before retrying or changing parameters.
- The dry-run plan touches a protected account (configured reserve
  admins / reserve members or `@planfix_bot`) — never add `--force`
  on the agent's own initiative.
- The human asks for an action that is not in the resource/action
  table (writing a new bot, calling Telethon directly, hand-editing
  `data/config.yml` outside the `access add` command, etc.). Decline and
  ask whether the CLI flow covers what they need.
- The request implies the agent should run `auth` itself, or collect
  a phone, code or 2FA password from the chat.

## Clarification templates

Keep clarifications short and direct. One question per turn, no
preamble, no apologies. Plain Russian, no bureaucratese.

- Missing username:
  «Не вижу username, кого добавить?»
- Missing chat:
  «В каком чате это сделать? Назови чат или укажи `--chat-id`.»
- Missing topic:
  «В каком топике? Укажи название топика.»
- Missing message text:
  «Какой текст отправить?»
- Missing operation id:
  «Какая операция? Пришли её id.»
- Ambiguous chat:
  «Нашёл несколько чатов с похожим названием, какой выбрать?»
- Ambiguous topic:
  «В этом чате несколько топиков с таким названием. Какой именно?»
- Ambiguous user:
  «Этому имени соответствует несколько аккаунтов, какой именно?»
- Health is not ok:
  «`health` показал проблему: <сообщение>. Что делаем?»
- Dry-run failed:
  «dry-run упал: <сообщение>. Проверь название/параметры?»
- Protected account touched:
  «В плане затронут технический аккаунт <имя>. Добавить `--force`
  и продолжить?»
- Action is out of scope:
  «Этого в CLI нет. Подойдёт <ближайшая команда> или нужно ручное
  действие?»

## What is out of scope

The skill describes how to drive the existing CLI. It does not
authorise the agent to:

- write another Telegram bot or any new long-running service;
- stand up a new HTTP API, webhook receiver, or worker;
- add Planfix-side automation (Planfix scenarios, webhooks, custom
  fields) — the project handles Planfix elsewhere;
- import `telethon` or talk to Telegram MTProto directly;
- call the project's HTTP API from inside the skill — the CLI is the
  only entry point;
- change Telegram state without a confirmed plan;
- guess a chat, topic, or user when the match is not exact — always
  ask;
- bypass `--dry-run` for any state-changing command that supports it;
- remove protected accounts (reserve admins / reserve members /
  `@planfix_bot`) without an explicit human ask and `--force`;
- collect phones, login codes, or 2FA passwords in chat — `auth` is
  always run by a human in a terminal;
- use real client names, real usernames, or real invite links in any
  example, plan, or log line — only the anonymized identifiers
  documented under "Scenarios".
