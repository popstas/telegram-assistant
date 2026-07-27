# telegram-assistant

Telegram automation service for the Planfix ↔ Telegram integration.

Runtime surfaces share one domain layer:

- HTTP API (FastAPI) on port `8085` with bearer-token auth — primary entry point for Planfix and automations.
- MCP server (Streamable HTTP) mounted at `/mcp` when explicitly enabled — for MCP clients such as Claude or MCP Inspector, authenticated through the local OAuth Authorization Server.
- CLI (`telegram-assistant`) — mirrors every HTTP endpoint plus admin commands (`auth`, `operations status`, `operations retry`).
- Worker/queue — performs Telegram operations with throttling and `FLOOD_WAIT` handling.

Runs on MTProto via Telethon under a technical Telegram user account.

> **Just watching and forwarding messages?** If you only need to *match and
> forward* messages (not send/manage them), see
> [popstas/telegram-resender](https://github.com/popstas/telegram-resender).
>
> **Just want to read-only download any chat history?** See
> [popstas/telegram-download-chat](https://github.com/popstas/telegram-download-chat).

## Features

- **Messages** — send targeted or folder-wide mass messages with file/URL attachments (albums), scheduling/delay, and threaded replies; send **rich messages** (Telegram articles: headings, tables, quotes, code, up to 32 768 chars) from markdown; read recent history, text-search a chat (relative window or fixed date range), react with emoji, forward, edit, pin/unpin (server-paced), download a message's media, and delete.
- **Groups** — create supergroups (with topics, invite links, member/admin/phone-contact seeding), rename, and switch forum layout (`list` / `tabs`).
- **Topics** — create single or bulk forum topics (CSV/JSON), close, reopen, and rename.
- **Members** — bulk add (optionally as admin) and bulk remove (kick or ban); references by `@username`, user id, or phone-contact.
- **Notifications** — mute (indefinitely or for a duration) and unmute chats or contacts.
- **Folders** — inspect chat folders and move chats in or out of them.
- **Queue & operations** — a worker performs Telegram actions with throttling and `FLOOD_WAIT` handling; inspect and retry queued operations.
- **Idempotency** — group/topic creation is idempotent on a generic `external_ref`.
- **Surfaces** — one domain layer behind three interfaces: HTTP API (FastAPI, bearer auth), CLI (`telegram-assistant`), and an optional MCP server (Streamable HTTP with local OAuth).
- **Access control** — deny-by-default `read` / `write` / `delete` rules per chat, chat list, folder (by name or id), or wildcard, hot-reloaded from config within ~2s.
- **Planfix plugin** — optional, off by default: `/task <ref>` service messages and `@planfix_bot` welcome cleanup for the Planfix ↔ Telegram integration.

> ⚠️ **Warning**
>
> **Automatically adding members can get your account banned.** Telegram's anti-spam
> system flags MTProto user accounts that programmatically add people to groups —
> especially **by phone number** or without the person's consent — as spam, and may
> **freeze or delete the account** (no real user report is required; detection is
> automated). To stay within [Telegram's ToS](https://telegram.org/tos):
> - Prefer **invite links** (`create_invite_link`) so people join the group themselves,
>   instead of passing `members` / `admins` / `contacts` / `telegram_id`.
> - **Never add users by phone number.**
> - Adding your own account or a bot (e.g. `@planfix_bot` via `reserve_members`) to your
>   own group is low risk; adding non-consenting human users is what triggers bans.
> - Treat the technical account as disposable, warm it up, and keep actions rate-limited.

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Place a config file at data/config.yml (see Configuration below)
telegram-assistant auth      # interactive Telethon login
telegram-assistant health    # show current health
uvicorn telegram_assistant.http_api.app:create_app --factory --port 8085
```

## Commands

Every CLI subcommand maps 1:1 to an HTTP endpoint (except the admin-only commands `auth`, `operations status`, and `operations retry`). Run any command with `--help` for full flag documentation.

Top-level:

- `auth` — interactive Telethon login for the technical account.
- `health` — report service health (Telegram session, database, default folder).
- `version` — print the installed version.

`groups` — manage Telegram supergroups:

- `groups create` — create a Telegram supergroup for a Planfix client. Accepts `--topics-layout list|tabs` to pick the forum layout for this group (defaults to `telegram.defaults.topics_layout`). `--manager` is an alias for `--member` (same regular-member role, concatenated). Accepts repeatable `--contact "<phone>|<name>"` for members only reachable by phone: the phone is normalised (dirty formats like `89222222222`, `+7-922-222-22-22`, and `t.me/+phone` links all collapse to `+<digits>`), the user is imported into the technical account's Telegram contacts (making them resolvable), then added as a regular member; a phone with no Telegram account is recorded and skipped.
- `groups set-layout` — set the topics layout (`list` vs `tabs`) for an existing forum chat.
- `groups get-layout` — read the current topics layout (`list` or `tabs`) for a forum chat.
- `groups rename` — rename an existing supergroup (change its title; WRITE-gated, idempotent by target title).

`topics` — manage forum topics:

- `topics create` — create a single forum topic in an existing supergroup.
- `topics bulk-create` — bulk-create topics from a CSV or JSON file.
- `topics close` — close an existing forum topic (the topic and its history are kept).
- `topics open` — reopen a closed forum topic (the topic and its history are kept).
- `topics rename` — rename an existing forum topic (`--topic-id` or `--topic-name`; WRITE-gated, idempotent by target title).

`members` — manage group membership:

- `members bulk-add` — bulk-add members to an existing supergroup, optionally promoting to admin.
- `members bulk-remove` — bulk-remove members from a supergroup (kick or permanently ban).

`messages` — send messages and service commands:

- `messages send` — send a message or service command (targeted or folder-wide mass mode). Attach local files with repeated `--file` and/or remote URLs with repeated `--file-url` (multiple attachments send an album); defer delivery with `--schedule-at` (ISO-8601 datetime) or `--delay` (relative duration like `10m`, `2h`, `1d`); thread a reply with `--reply-to <message_id>`. `--text` may be omitted for media-only sends. Attachments, scheduling, and `--reply-to` apply to targeted sends only, not mass mode. `--rich-markdown <file.md>` sends the file's contents as a Telegram **rich message** (article) instead of plain text, with its own knobs (`--no-spaced-paragraphs`, `--no-line-breaks`, `--rich-file <reference>=<path>`, `--vault-dir <dir>`, `--media-group <index>=<collage|slideshow|none>`) — see below.
- `messages recent` — read the most recent messages from a chat (READ-gated; `--limit` defaults to 5, optional `--minutes N` keeps only messages newer than `now - N` minutes).
- `messages react` — set (`--emoji`) or clear (`--clear`) an emoji reaction on a message (`--message-id`, WRITE-gated).
- `messages forward` — forward one or more messages (`--message-id`, repeatable) from a source (`--from-chat-id`/`--from-entity`) to a target (`--to-chat-id`/`--to-entity`, or the usual target aliases `--chat-id`/`--chat-name`/`--entity`); READ-gated on the source, WRITE-gated on the target.
- `messages delete` — delete one or more messages (`--message-id`, repeatable) from a chat (DELETE-gated). `--revoke`/`--no-revoke` toggles delete-for-everyone (default revoke); `--dry-run` resolves + authorizes without deleting; `--force` is carried for surface consistency. Honors `telegram.access.delete_only_session_messages` (default `true`, overridable per access rule): when active, only messages this server process sent may be deleted.
- `messages edit` — edit the text/caption of a sent message (`--message-id`, `--text`, WRITE-gated; `--dry-run`). Honors `telegram.access.edit_only_session_messages` (default `true`, overridable per access rule): when active, only messages this server process sent may be edited. Telegram edit limits (own messages only, ~48h window, text must change) surface as clear domain errors, not 500s.
- `messages pin` — pin a message in a chat (`--message-id`, WRITE-gated; `--silent` to pin without notifying, `--pm-oneside` to pin only on your side in a private chat, `--dry-run`). Pin/unpin calls are paced per chat (`telegram.pin_min_interval_seconds`, default `2.0`) and retry `FLOOD_WAIT` automatically; a paced-out call prints the next allowed attempt time.
- `messages unpin` — unpin a message (`--message-id`) or every pinned message (`--all`) in a chat (WRITE-gated; `--dry-run`). Shares the per-chat pin pacing gate.
- `messages download` — download the media/document/voice of an existing message to a local server-side file (`--message-id`, READ-gated). Choose exactly one of `--out` (target file) / `--dir` (target directory, keeps the original filename); `--max-bytes` rejects oversized media; `--dry-run` reports the planned path without downloading. Downloads never overwrite: a taken name becomes `report (1).pdf`, `report (2).pdf`, … and the response returns the actual path written. Files are created mode `0600` (owner-only).
- `messages search` — text-search a chat's messages newest-first (`--query` required, READ-gated). Optional `--from` (sender), `--limit` (max matches, default 20), `--topic-id` (search inside one forum topic), and one time scope — either `--minutes` (relative window like `recent`) or the fixed inclusive range `--from-date` + `--to-date` (ISO-8601 with timezone, both required together, not combinable with `--minutes`). `--query`, `--from`, `--topic-id` and the `--from-date`/`--to-date` range go to Telegram in a single server-side search request and are paged until `--limit` matches are collected, so a bounded range reaches old messages too; the output echoes the applied UTC bounds. Paging is capped at 20 search requests: rows dropped by the local filters do not count toward `--limit`, so a heavily filtered query can return fewer rows than asked for even when older matches exist — narrow the range or the sender. `--minutes` works differently: it trims the newest `--limit` hits client-side, so it can return fewer rows than `--limit` and cannot see past them — use the date range for historical searches. Read-only, no `--dry-run`.

`notifications` — mute and unmute chat/contact notifications:

- `notifications mute` — mute a chat or contact, indefinitely or for `--duration` hours.
- `notifications unmute` — restore normal notifications for a chat or contact.

Most chat-targeting commands accept `--entity` (a numeric id with/without `-100`, `@username`, `t.me`/invite link, phone, or exact title) as a flexible alternative to `--chat-id`/`--chat-name`.

Member references in `members`/`admins` (group create) and in `members bulk-add`/`bulk-remove` may be a `@username`, a bare username, a `t.me` link, or a numeric **Telegram user id** (e.g. `1234556`). A bare number is treated as a user id, not a phone — phone numbers must include a leading `+` (e.g. `+15551234567`). Resolving a bare user id requires that the technical account's session already knows the user (cached access hash); `@username`/`t.me` links are the robust path otherwise.

`folders` — inspect and manage chat folders:

- `folders inspect` — inspect a chat folder and list its chats.
- `folders add-chat` — move an existing chat into a folder.
- `folders remove-chat` — remove a chat from a folder (idempotent: a no-op if the chat is not in the folder).

### Rich messages (articles)

`messages send --rich-markdown <file.md>` sends the file's contents as a Telegram **rich message** — the server parses the markdown itself and delivers a single article, so a >4096-character post is *not* split. The same input is `rich_markdown` (a string, not a path) on `POST /telegram/messages` and on the `telegram_messages_send` MCP tool.

- **Dialect** (parsed server-side): `#`…`######` headings, tables with alignment, task lists, `>` quotes, fenced code with a language, `---` dividers, `~~strike~~`, `==marked==`, `||spoiler||`, footnotes, math, `<details>`, and media as a standalone block — by public HTTPS URL (`![](https://…jpg "caption")`, fetched by the server) or, **on the CLI only**, by local file (uploaded and referenced as `tg://photo?id=…` / `tg://video?id=…` / `tg://audio?id=…`). Headings, lists, tables, quotes, code, dividers, URL media and uploaded local media are verified over MTProto; the remaining constructs are documented for the Bot API twin of the same server feature and are unverified here.
- **Paragraph spacing** (on by default): the server renders neighbouring paragraphs tight against each other, so the markdown is rewritten before sending — a U+00A0-only spacer paragraph is inserted between two consecutive paragraphs, before every heading, and after every medium (a photo/video/audio block, or the `<tg-collage>`/`<tg-slideshow>` a run was grouped into), but never *before* media, never after a heading, never inside code/tables/lists/quotes/HTML blocks, and never next to a spacer the author already wrote. Turn it off with `--no-spaced-paragraphs` (HTTP/MCP: `spaced_paragraphs: false`) to keep the author's own spacing; the default also comes from `telegram.defaults.rich_markdown_spaced_paragraphs`. Note that this switches off *only* the spacer pass — media grouping and local-media rewriting are independent, so a truly byte-for-byte send also needs `--no-line-breaks`, `telegram.defaults.rich_markdown_grouping: none` (or `--media-group <index>=none` per run) and an article with no local media. Spacers count toward both limits below: if spacing would push the article past 500 blocks it is sent unspaced with the warning `spaced_paragraphs disabled: N blocks would exceed the 500-block limit`.
- **Line breaks** (on by default): Telegram parses the markdown itself and, like CommonMark, folds a *single* newline inside a paragraph into a space — so an Obsidian note's
  ```
  Фотоальбом - https://…
  Видео плейлист - https://…
  ```
  would arrive as one run-on line. Each line of a paragraph is therefore split into its own paragraph, which the clients render tight against each other: two lines, no gap — the spacer above is deliberately *not* inserted between them. Only top-level paragraphs are split; lines inside a quote, a list item or an HTML container keep the author's shape. Turn it off with `--no-line-breaks` (HTTP/MCP: `line_breaks: false`); the default also comes from `telegram.defaults.rich_markdown_line_breaks`. The extra paragraphs count toward the 500-block limit like any other.
- **Media grouping** (default `collage`): a run of 2+ consecutive media blocks with no text between them is wrapped in `<tg-collage>` … `</tg-collage>`, so consecutive screenshots render as one collage instead of stretching the article. Media the author already wrapped in `<tg-collage>`/`<tg-slideshow>`/`<details>` is never re-grouped. The mode comes from `telegram.defaults.rich_markdown_grouping` (`collage` | `slideshow` | `none`); the CLI additionally overrides a single run with `--media-group <index>=<collage|slideshow|none>` (repeatable, the index is the 0-based position reported by `--dry-run`; an unknown index is an error). HTTP/MCP get the config default but no per-group override.
- **Group caption**: Telegram's clients show no caption under an individual medium *inside* a group — only one caption for the group itself (the per-medium captions still reach the server, they are simply not rendered) — so a wrapped run whose media carry captions gets them listed, comma-separated, as a `<figcaption>` inside the container tag (`<figcaption>Отлив, Прилив</figcaption>`); media with no caption contribute nothing, and a run where none has one is wrapped exactly as before. The caption Telegram will show is reported per run by `--dry-run` (`rich_markdown_groups[].caption`), and it costs no block — the server folds it into the group's own caption field.
- **Local media** (**CLI-only**): local media references in the article — a relative or absolute path (`![](photo.png)`) or an Obsidian embed (`![[Pasted image 1.png|caption|300]]`, including the alignment/size/`%` caption forms) — are resolved against the markdown file's own directory, uploaded once each, and rewritten into `tg://` references, so an Obsidian note can be sent unedited. `.jpg/.jpeg/.png/.webp` is a photo, `.mp4/.mov/.webm/.mkv/.avi/.m4v/.gif` a video (document — `.gif` is an animation), `.mp3/.ogg/.oga/.opus/.m4a/.wav/.flac` audio; any other suffix is an error, because the dialect has no fourth reference scheme to write it into. Captions come from the media title, falling back to the alt text. A reference is resolved in order: an explicit override, then as an absolute path, then against the article's own directory, then **by file name** under `--vault-dir` (nearest match wins; a tie is an error, never a guess). `--rich-file <reference>=<path>` (repeatable) points one reference at a file outside the article's directory — the key may be the reference as written, its URL-decoded form, or its bare file name. `--vault-dir <dir>` sets the root of that by-name search; it is motivated by Obsidian vaults whose attachments sit elsewhere, but it applies to a plain `![](photo.png)` just as much as to `![[photo.png]]`. Media is resolved whether it stands alone or has a caption line right below it. Unresolvable media, an ambiguous embed, and an override that matched nothing are all errors naming the file — nothing is silently dropped. HTTP/MCP never resolve server-local paths and keep HTTPS-URL media only.
- **Obsidian frontmatter** (**CLI-only**): a leading `---` … `---` YAML block is dropped when the file is read, next to the BOM strip and for the same reason — this dialect has no notion of frontmatter, so the article would otherwise open with a divider and a large heading reading `tags: [...] date: ...`. Only an exact `---` on the first line starts a block, only a matching `---` ends one, and the lines between them must read as YAML (a `key: value` entry first, then only entries, `- ` items, indented continuations or blank lines), so a note that merely begins with a horizontal rule keeps it — even when a later `---` divider would otherwise close the pair; a file that is *nothing but* frontmatter is reported as empty. HTTP/MCP take a markdown string an agent composed rather than a note file, so their input is passed through untouched.
- **Limits**: 1..32 768 characters (validated locally after normalization, inclusive), ~500 blocks and 50 media attachments (counted locally and reported as warnings — Telegram is the authority); the server additionally caps nesting and table columns and reports its own errors (`RICH_MESSAGE_MARKDOWN_INVALID`, `RICH_MESSAGE_TEXT_TOO_LONG`, …).
- **Exclusivity**: `--rich-markdown` is a targeted-send-only alternative to the message body — it cannot be combined with `--text`, `--file`, `--file-url` (HTTP/MCP: `text`, `file_urls`, `base64_files`) or with mass mode. `--spaced-paragraphs`/`--no-spaced-paragraphs`, `--line-breaks`/`--no-line-breaks`, `--rich-file`, `--vault-dir` and `--media-group` are errors without `--rich-markdown` (CLI exit 2; HTTP `spaced_paragraphs`/`line_breaks` without `rich_markdown` is a `422`).
- Everything else is unchanged: entity resolution, the WRITE gate, `--operation-id` idempotency, topic/reply targeting (`--topic-id`/`--reply-to`), and scheduling (`--schedule-at`/`--delay`) all work. `--dry-run` reports the article as markers (`rich_markdown`, post-normalization `rich_markdown_chars`, `rich_markdown_blocks`, `rich_markdown_media`, `rich_markdown_file`, `spaced_paragraphs`, `spaced`, `line_breaks`, `media_grouping`, `rich_markdown_groups`, `rich_files`) rather than echoing a 32k body — the listed files are never read. Normalization warnings go to stderr as `warning: ...` on a real send and ride the result JSON as `warnings`.
- A chat that forbids media rejects the **whole** article: Telegram's `ChatSendMediaForbiddenError` (and its per-type siblings) becomes a `RichMediaForbidden` error naming the chat (HTTP `400`, CLI exit 2). There is no media-less fallback — an article's media is part of its body.
- If Telegram accepts the request but the response carries no readable message id, the send is **not** marked failed — the operation goes to `needs_review` (the queue never auto-retries it), because the article may well have been delivered. Check the chat before `operations retry`; a blind re-send under a fresh key would duplicate it.
- A failed rich send is **not** silently retried as plain text — it surfaces as the normal send error, and the caller decides. Requires `telethon >= 1.44` (layer 227), now the project's minimum pin; if an older Telethon is force-installed anyway, only the rich send fails, with an explicit version error (HTTP `500 {"error": "rich_message_unsupported"}`, CLI exit 1, MCP error message) — and the idempotency key is left free, so the same `--operation-id` sends normally once Telethon is upgraded.

> **Migration note (Telethon 1.44 required):** rich messages use `InputRichMessageMarkdown` (layer 227), so the dependency floor moved from `telethon>=1.36` to `telethon>=1.44`. Existing installs must re-run `pip install -e ".[dev]"` (or rebuild the Docker image) when upgrading.

Mutating CLI commands support `--dry-run` before the real run. Local `--file` attachments must exist, be regular files, and be non-empty. `--file-url` must be a valid `http`/`https` URL with a host. `--schedule-at` and `--delay` are mutually exclusive and must resolve to a future time. `messages react` requires exactly one of `--emoji` or `--clear`; `notifications mute --duration` must be positive. `folders remove-chat` accepts `--chat-id`, `--chat-name`, or `--entity`, plus optional `--folder-id`.

### Access control

`telegram.access` in `data/config.yml` gates which chats/folders this instance may read, write, or delete in. Omitting it means allow-all (backward compatible); once present it is deny-by-default. Capabilities are **independent** — `read`, `write`, and `delete` each grant *only* themselves, so `write` no longer implies `read`. Matching rules combine as a set-union of capabilities. Denials surface as a non-zero CLI exit (code 3) and `HTTP 403` on the API.

Each rule names exactly one *target kind* — a single chat (`chat`/`--entity`), a list of chats (`chats`), a folder by name (`folder`), a folder by id (`folder_id`), or the wildcard (`all`) — and a capability set via `permissions: [read, write, delete]` (or the legacy singular `permission`, default `write`). Legacy single-target / singular-permission rules still parse and apply. A common shape is a wildcard `all: read` baseline layered with targeted `[write]` or `[write, delete]` rules:

```yaml
telegram:
  access:
    delete_only_session_messages: true   # default; only delete messages this process sent
    edit_only_session_messages: true     # default; only edit messages this process sent
    folder_cache_ttl: 300                # seconds; persistent membership cache TTL (0 disables)
    rules:
      - all: true
        permissions: [read]
      - folder: "Planfix clients"         # by name: unions *all* folders with this title
        permissions: [read, write]
      - folder_id: 5                      # by id: exactly one folder, even if the title repeats
        permissions: [read, write, delete]
      - chats: ["@some_chat", -1001234567890]
        permissions: [read, write, delete]
      - chat: me                          # per-rule exception: prune your own Saved Messages
        permissions: [write, delete]
        delete_only_session_messages: false
        edit_only_session_messages: false
```

Telegram allows **two folders with the same title**. A `folder:` rule is therefore a union: it grants on every chat in *any* folder with that name — safe by construction, but not selective. To target exactly one of two same-named folders, use `folder_id:` with the folder's numeric id (`folders inspect` prints it). Both kinds count as folder-level for `delete_only_session_messages` / `edit_only_session_messages` specificity, and both may carry their own per-rule override. The `access add` CLI writes `--entity` / `--folder` / `--all` rules; a `folder_id` rule is added by editing `data/config.yml` directly (hot-reload picks it up within ~2s).

Folder-level rules also gate the folder listing itself on the remote surfaces: `GET /telegram/folders/{name}` and the MCP `telegram_folders_inspect` tool need `read` on the folder (by name or id) and answer **403** otherwise, since the payload enumerates every chat in it. The local `folders inspect` CLI is not gated. Moving a chat between folders (`folders add-chat` / `remove-chat`) and creating a group into a folder drop the cached membership map, so a new grant — or a revoked one — applies to the next call instead of waiting out `folder_cache_ttl`.

`delete_only_session_messages` also accepts a **per-rule override**: any rule may set it to `true`/`false` for the chats/folders it targets, keeping the safe global default while relaxing (or tightening) a specific target. The effective value for a delete is resolved by specificity — chat rule > folder rule > `all` rule > policy default — and a restrictive `true` wins over `false` when rules at the same level conflict.

`edit_only_session_messages` (default `true`) is the exact mirror for `messages edit`: when active, only messages this server process sent (tracked in an in-memory registry, fresh per CLI run) may be edited. It accepts the same per-rule override with the same specificity resolution (chat > folder > `all` > policy default, restrictive `true` wins). Set it `false` (globally or per rule) to allow editing arbitrary own messages.

Both defaults apply **even when `telegram.access` is omitted** (the allow-all mode), and the sent-message registry lives in memory per process — so a one-shot CLI run starts with an empty registry and `messages edit` / `messages delete` there only reach messages that same invocation sent. To edit or delete arbitrary own messages from the CLI, add a `telegram.access` block setting the flag to `false` (globally or on a targeted rule); remember that adding the block also switches the policy to deny-by-default, so include a baseline rule such as `all: true` with the capabilities you need.

`folder_cache_ttl` (seconds, default `300`; `0` disables persistent caching) tunes the folder-membership cache that speeds up access checks when any `folder:` / `folder_id:` rule is present. Without it, every gated operation would re-fetch each folder's chat membership from Telegram; instead the membership map is built once from `InputPeer` ids (no per-chat `get_entity`), keyed by **folder id** (so same-named folders stay separate), and persisted to SQLite. A subsequent call within the TTL reuses the cached map — the big win for the CLI, where each invocation is a fresh process. When a refetch fails, a stale cached map is served (bounded by TTL + the outage window); the cache is cleared automatically on config hot-reload so access-rule edits apply cleanly.

Config edits are hot-reloaded: a `watchdog` observer on `data/config.yml` re-runs the loader with a 2s debounce and atomically swaps the live config on success (a parse/validation error keeps the last-good config), so access-rule changes apply within ~2s without restarting the server.

> **Migration note (HTTP folder inspect is now gated):** `GET /telegram/folders/{name}`
> previously answered for any folder regardless of `telegram.access`. It now requires
> `read` on that folder (by name or id), matching the MCP `telegram_folders_inspect`
> tool — its payload enumerates every chat in the folder. Deployments running with an
> access policy must add a `read` rule for folders their HTTP clients inspect. The
> local `folders inspect` CLI stays ungated.

> **Migration note (capabilities are now independent):** earlier versions had `write` imply `read`. That implication is gone — a chat granted only `write` is now **denied** `read` (e.g. `messages recent` will fail). Update existing configs to list `read` explicitly wherever it is needed, e.g. `permissions: [read, write]`.

`access` — inspect and edit the access policy (CLI + skill only; not exposed over MCP):

- `access list` — print the effective policy (allow-all, or the deny-by-default rules and the capabilities each grants).
- `access check --entity <ref> --permission read|write|delete` — resolve a chat and report the grant verdict (exit `0` granted, `3` denied, `2` unresolved).
- `access add` — append one rule (`--entity`/`--folder`/`--all` + `--permission read,write,delete`) to `data/config.yml`; the hot-reload watcher then applies it live. `--dry-run` prints the rule without writing.

`operations` — inspect and retry queued operations:

- `operations status` — show the status of an operation, including per-item summary.
- `operations retry` — reset a failed/`needs_review` operation (and its items) back to pending.

Updating this list: descriptions are sourced from each Typer command's docstring in `src/telegram_assistant/cli/main.py`. When you add or rename a command, update this section, `skills/telegram-assistant/SKILL.md`, and re-run `pytest tests/test_skill_inventory.py` — the inventory guard fails if the README/skill catalog drifts from the CLI.

## HTTP API

All `/telegram/*` endpoints require `Authorization: Bearer <token>` and use the same access policy as the CLI.

- `POST /telegram/groups` creates a supergroup. In addition to `title`, `about`, `admins`, `members`, `managers`, `external_ref`, and `topics_layout`, the HTTP body accepts two optional fields (string **or** list-of-strings — the first element is used): `lang` (`"ru"`/`"en"`, default `ru`) and `telegram_id`. Every response now also carries a localized **`answer`** summary string alongside `operation_id`/`operation_status`. When `members[0]` is a phone-style `t.me` reference (`https://t.me/79222222222`, `https://t.me/+79222222222`): if `telegram_id` is set, the client is added by that numeric id and `answer` is «Группа создана, клиент добавлен»; if `telegram_id` is empty, the group is still created but that client member is **skipped** (recorded in `skipped` with `reason: "phone_without_telegram_id"`) and `answer` warns that the client cannot be connected by phone number without a telegram id. Otherwise `answer` is «Группа создана». These three behaviors (`lang`, `telegram_id`, `answer`) are currently **HTTP-only** — they are not exposed on the CLI or MCP surfaces.
- `POST /telegram/messages` sends targeted or mass messages. Targeted bodies accept `telegram_chat_id`, `entity`, or `chat_name` + `folder_name`, plus optional `telegram_topic_id`/`topic_name`, `file_urls`, base64 `base64_files` (`{filename, mime, content_b64}`, default max 1 MB each), `reply_to_message_id`, `schedule_at`, `delay_seconds`, and `operation_id`. `rich_markdown` (markdown source, 1..32 768 chars) sends a rich message/article instead of a text body — targeted-only, and mutually exclusive with `text`, `file_urls`, `base64_files`, and mass mode (conflicts are rejected by the body validator as `422`, out-of-range markdown as `400`). The optional `spaced_paragraphs` (`true`/`false`, unset = `telegram.defaults.rich_markdown_spaced_paragraphs`) toggles the U+00A0 paragraph spacing and `line_breaks` (same shape, unset = `telegram.defaults.rich_markdown_line_breaks`) toggles splitting a paragraph's lines apart; both are themselves a `422` without `rich_markdown`; media grouping follows `telegram.defaults.rich_markdown_grouping` with no per-group override over HTTP, and local-file media is CLI-only. HTTP server-local `files` are rejected; use `file_urls` (downloaded to a temp file with size/time limits) or `base64_files` for media over HTTP. Responses include `telegram_message_id`, `telegram_message_ids` for albums, `scheduled`, `schedule_at`, `operation_id`, `operation_status`, and `warnings` — a list of non-fatal notes (currently rich-markdown normalization notes about the 500-block / 50-media budget, or a rolled-back spacer pass), empty for a plain send and never an indication that the send failed.
- `POST /telegram/messages/reactions` sets or clears a reaction with `message_id` plus exactly one of `emoji` or `clear=true`.
- `POST /telegram/messages/forward` forwards `message_ids` from `from_chat_id`/`from_entity` to `to_chat_id`/`to_entity`.
- `POST /telegram/messages/delete` deletes `message_ids` from a target chat (DELETE-gated). Optional `revoke` (default `true`), `dry_run`, and `force`. Honors `telegram.access.delete_only_session_messages`; the backend factory returns `503` when the session is not connected.
- `POST /telegram/messages/edit` edits the text/caption of a sent message (`message_id`, `text`, WRITE-gated). Target with `telegram_chat_id`, `entity`, or `chat_name` + `folder_name`/`folder_id`. Optional `dry_run`. Honors `telegram.access.edit_only_session_messages`; Telegram edit-limit errors map to `400`, access denial to `403`, and the backend factory returns `503` when the session is not connected.
- `POST /telegram/messages/pin` pins a message (`message_id`, WRITE-gated) with optional `silent` and `pm_oneside`; target with `telegram_chat_id`, `entity`, or `chat_name` + `folder_name`/`folder_id`. `POST /telegram/messages/unpin` unpins one `message_id` or, with `unpin_all: true`, every pinned message. Both accept `dry_run` and return `503` when the session is not connected. Both are paced per chat (`telegram.pin_min_interval_seconds`) and retry `FLOOD_WAIT` internally; when the retry budget is exhausted the flood-wait response body carries `retry_after_seconds`/`retry_at` and the standard `Retry-After` header.
- `POST /telegram/messages/download` downloads an existing message's media to a **server-side** file (READ-gated). Target with `telegram_chat_id`, `entity`, or `chat_name` + `folder_name`/`folder_id`. Body carries `message_id` plus optional `out_dir` and `max_bytes`; `out_dir` is confined to `telegram.download_root` (default: the system temp dir) — a relative value is resolved inside the root, one escaping it is rejected with `400`, and omitting it uses the root — so a READ-only caller cannot pick an arbitrary write location. An existing file is never overwritten: the download goes to the first free `name (1).ext` and the response's `path` is the file actually written (plus size and mime; no base64/streaming in this iteration). Files are created mode `0600` (owner-only). Returns `503` when the session is not connected.
- `GET /telegram/messages/search` text-searches a chat newest-first (READ-gated); query params mirror `recent` in name (`query` required, plus `from_user`, `limit` — **default 20** here, not `recent`'s 5 — `minutes`, `topic_id`) and add the fixed inclusive range `from_date`/`to_date` (ISO-8601 with timezone, required together, mutually exclusive with `minutes`; invalid ranges → `400`). The response echoes the applied bounds normalised to UTC. Paging is capped at 20 search requests, so a query whose hits are mostly dropped by the local filters can return fewer than `limit` rows even when older matches exist — narrow the range or the sender. Returns `503` when the session is not connected.
- `POST /telegram/notifications/mute` and `/telegram/notifications/unmute` mute or unmute a target chat/contact; mute accepts positive `duration_hours`.
- `GET /telegram/folders/{folder_name}` returns the folder snapshot (id, name, chats). **READ-gated on the folder**: the folder is resolved first and the gate runs on the snapshot's own `folder_name`/`folder_id`, so a `folder_id:` rule grants an inspect requested by name; a denied caller gets `403` and no chat list.
- `DELETE /telegram/folders/{folder_name}/chats` removes `chat_id`, `chat_name`, or `entity` from a folder and returns `already_absent` when no change was needed.

## MCP server (optional)

The MCP interface is disabled by default. If the `mcp:` block is absent, or
present with `enabled: false`, no `/mcp` route or OAuth endpoints are mounted
and the app behaves like the HTTP/CLI-only service.

When enabled, `create_app()` mounts the official FastMCP Streamable-HTTP app at
`/mcp` and exposes `telegram_` tools for the same operations as the HTTP API
and CLI: health, messages, groups, topics, members, folders, notifications, and
operation status/retry. The tools reuse the same backend factories, entity
resolver, `OperationStore`, plugin registry, and `telegram.access` policy; MCP
does not create or repair the Telethon session.

MCP clients discover and authenticate through the local OAuth Authorization
Server in the same FastAPI process:

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource/mcp`
- `/register`
- `/authorize`
- `/token`

Google OAuth/OIDC is used only as a login gate. After Google verifies the user,
the local server enforces `allowed_emails` / `allowed_domains`, then mints
signed, audience-bound MCP access and refresh tokens. The Google allowlist
decides who may connect; `telegram.access` still decides which chats/folders
the tools may read or write. Operation-store tools also require the optional
OAuth scope `telegram:admin`, which is granted only to identities matching
`admin_emails` / `admin_domains`.

MCP tool catalog:

| Tool | Key arguments |
| --- | --- |
| `telegram_health` | none |
| `telegram_messages_recent` | `chat_id`, `limit`, optional `minutes` (only messages newer than `now - minutes`) |
| `telegram_messages_send` | `telegram_chat_id`/`entity`, `text`, `rich_markdown` (markdown article, 1..32 768 chars; exclusive with `text`/`file_urls`/`base64_files`), `spaced_paragraphs` and `line_breaks` (rich sends only — an error without `rich_markdown`; unset = `telegram.defaults.rich_markdown_spaced_paragraphs` / `…_line_breaks`), `telegram_topic_id`/`topic_name`, `file_urls`, base64 `base64_files`, `schedule_at`, `delay_seconds`, `reply_to_message_id`, `operation_id`; `chat_name`/`folder_name`/`folder_id` and server-local `files` are not part of the MCP surface (target via `entity`); article media must be an HTTPS URL — local-file media and per-group `--media-group` overrides are CLI-only |
| `telegram_messages_forward` | `from_chat_id`/`from_entity`, `to_chat_id`/`to_entity`, `message_ids`, `operation_id` |
| `telegram_messages_delete` | `telegram_chat_id`/`entity`, `message_ids`, `revoke`, `dry_run`, `force`; gated on DELETE, honors `delete_only_session_messages` |
| `telegram_messages_edit` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, `message_id`, `text`, `dry_run`; WRITE-gated, honors `edit_only_session_messages` |
| `telegram_messages_pin` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, `message_id`, `silent`, `pm_oneside`, `dry_run`; WRITE-gated, per-chat paced, flood-wait errors carry `retry_after_seconds`/`retry_at` |
| `telegram_messages_unpin` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, `message_id` or `unpin_all`, `dry_run`; WRITE-gated, shares the pin pacing gate |
| `telegram_messages_download` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, `message_id`, `out_dir` (confined to `telegram.download_root`), `max_bytes`, `dry_run`; READ-gated, writes a server-side file without ever overwriting (`name (1).ext` on collision; returns the saved path/size/mime, no byte streaming) |
| `telegram_messages_search` | `chat_id`/`entity`, `query`, `from_user`, `limit`, `topic_id`, and either `minutes` or the inclusive `from_date`/`to_date` range (aware datetimes, required together); READ-gated, newest-first, echoes the applied UTC bounds |
| `telegram_messages_react` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `message_id`, `emoji` or `clear` |
| `telegram_groups_create` | `title`, `about`, `admins`, `members`, `managers` (alias of `members`), `contacts` (`[{phone, name}]` — imported to contacts then added), `folder_name`/`folder_id`, `external_ref`, `topics_layout`, reserve/skip flags |
| `telegram_groups_rename` | `new_title`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, optional `reason`; WRITE-gated, idempotent by target title |
| `telegram_topics_layout` | `chat_id`, optional `layout` (`list`/`tabs`) |
| `telegram_topics_create` | `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `external_ref`, `message` |
| `telegram_topics_bulk_create` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `items`, `mode`, `continue_on_error`, `operation_id` |
| `telegram_topics_close` | `topic_id` or `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, optional `delete_messages`, `operation_id` |
| `telegram_topics_open` | `topic_id` or `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, optional `reason`; WRITE-gated, executes every call (already-open is a Telegram-level no-op) |
| `telegram_topics_rename` | `new_title`, `topic_id` or `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, optional `reason`; WRITE-gated, idempotent by target title |
| `telegram_members_add` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `items`, `mode`, `continue_on_error`, `operation_id` |
| `telegram_members_remove` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `items`, `mode`, `continue_on_error`, `operation_id` |
| `telegram_folders_inspect` | `folder_name`, optional `folder_id`; READ-gated on the folder (403 otherwise, since the payload lists every chat in it) |
| `telegram_folders_add_chat` | `folder_name`, `chat_id`/`chat_name`/`entity`, optional `folder_id` |
| `telegram_folders_remove_chat` | `folder_name`, `chat_id`/`chat_name`/`entity`, optional `folder_id` |
| `telegram_notifications_mute` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `duration_hours` |
| `telegram_notifications_unmute` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name` |
| `telegram_operations_status` | `operation_id`; requires `telegram:admin` |
| `telegram_operations_retry` | `operation_id`, `dry_run`; requires `telegram:admin` |

OAuth client behavior: `/register` is public Dynamic Client Registration with
`token_endpoint_auth_method=none`; `/authorize` requires PKCE S256 and a
`resource` matching `server_url`; `/token` supports `authorization_code` and
`refresh_token`. Redirect URIs registered by clients must use a trusted
loopback host (`localhost`, `127.0.0.1`, `::1`) or a host/URI configured in
`allowed_redirect_hosts` / `allowed_redirect_uris`. Registered clients, pending
Google states, and authorization codes are process-local memory, so clients
must re-register after process restart. `required_scopes` are required by the
MCP mount; `telegram:admin` is advertised only when `admin_emails` or
`admin_domains` is configured for operation status/retry clients.

Minimal enabled config:

```yaml
mcp:
  enabled: true
  server_url: "https://assistant.example.com/mcp"
  issuer_url: "https://assistant.example.com"
  google_client_id: "GOOGLE_CLIENT_ID"
  google_client_secret: "GOOGLE_CLIENT_SECRET"
  allowed_emails:
    - "owner@example.com"
  allowed_domains: []
  admin_emails: []
  admin_domains: []
  allowed_redirect_hosts: []
  allowed_redirect_uris: []
  required_scopes:
    - "mcp"
  access_token_ttl_seconds: 3600
  refresh_token_ttl_seconds: 2592000
  signing_secret: "<output-of-openssl-rand-base64-32>"
  disabled_tools: []   # e.g. ["telegram_groups_*", "telegram_health"]
```

> **Migration note (`telegram_messages_send` args):** the send tool dropped
> `chat_name`, `folder_name`, `folder_id`, and server-local `files`. Target the
> chat through `entity` (or `telegram_chat_id`) and attach media via `file_urls`
> or base64 `base64_files`. MCP clients passing the removed args must migrate.

`disabled_tools` omits tools from the mounted MCP surface. An entry ending in
`*` matches by prefix (e.g. `telegram_groups_*` hides every group tool);
otherwise it matches the exact tool name. The filter is applied at mount and
re-applied on config hot-reload, so editing `data/config.yml` adds or restores
tools without a restart.

For Google OAuth, create a Web application client and register
`<issuer_url>/authorize` as an authorized redirect URI. If the service is behind
a reverse proxy, `server_url` and `issuer_url` must be the public URLs seen by
the MCP client. `server_url` is the protected resource and token audience; it
normally includes `/mcp`. Keep the Google secret and `signing_secret` out of
version control. `signing_secret` must be at least 32 characters and must not
be a docs placeholder. Rotating `signing_secret` invalidates existing MCP
tokens.

Manual smoke testing is documented in `docs/mcp-inspector-e2e.md`.

## Configuration

Config is read from `data/config.yml` by default. The `data/` directory is excluded from version control and holds the Telethon session, SQLite database, and secrets.

If `./data/config.yml` is absent, the loader falls back to `~/.config/telegram-assistant/config.yml`. On a clean machine, running any CLI command without `--config` will create a template at that path with `REPLACE_ME` placeholders for `api_id`, `api_hash`, and `bearer_token` — fill them in and re-run.

To reach Telegram through a proxy, set `telegram.proxy_url` to a single URL — supported schemes are `socks5`, `socks4`, `http`, and `https`. Credentials and explicit ports are optional:

```yaml
telegram:
  proxy_url: "socks5://user:pass@host:1080"   # or http://host:8080, socks4://host, ...
```

Leave it unset (or remove the line) to connect directly.

`telegram.download_root` confines where remote (HTTP/MCP) `messages download` calls may write. It defaults to the system temp directory; a caller-supplied `out_dir` is resolved against this root — symlinks and all, so a link inside the root is not a way out of it — and rejected when it escapes, so a READ-only remote identity cannot pick an arbitrary write location. An unusable target directory (a file in its place, or one the server may not write to) is rejected as bad input, not a server error. The CLI (local, trusted) is not confined. Regardless of surface, a download never overwrites an existing file — the target name is claimed atomically and a taken name becomes `report (1).pdf`, `report (2).pdf`, … — and a missing target directory is created, with the file itself created mode `0600` so private-chat media is not readable by other local users of a shared download root. The symlink check rejects links that already exist when the request is validated; it is not a defense against a local attacker who creates one in the moment between validation and the download itself — point `download_root` at a directory only the service can write to if that matters.

```yaml
telegram:
  download_root: "/data/downloads"   # default: the system temp dir
```

`telegram.pin_min_interval_seconds` paces `messages pin` / `messages unpin`. Telegram answers rapid pin bursts with `FLOOD_WAIT`, so the server enforces a minimum interval between two pin-ops on the same chat before calling Telegram, and sleeps through + retries a `FLOOD_WAIT` within a bounded budget. The gate lives in SQLite (`rate_gate` table), so separate CLI processes and the HTTP/MCP server pace against each other rather than each keeping its own timer. When the retry budget is exhausted — or a single wait exceeds the internal cap — the error reports `retry_after_seconds` / the next allowed attempt time instead of blocking.

```yaml
telegram:
  pin_min_interval_seconds: 2.0   # default; 0 disables pacing
```

Defaults applied to new supergroups — and to rich-message normalization — live under `telegram.defaults`:

```yaml
telegram:
  defaults:
    enable_topics: true
    create_invite_link: true
    topics_layout: "list"        # "list" | "tabs" — applied after groups create
    default_member_permissions:
      create_topics: true        # let ordinary members create forum topics
      pin_messages: true         # let ordinary members pin messages
    rich_markdown_spaced_paragraphs: true   # U+00A0 spacer paragraphs in articles
    rich_markdown_line_breaks: true         # split a paragraph's own lines apart
    rich_markdown_grouping: "collage"       # "collage" | "slideshow" | "none"
```

`topics_layout` controls how the forum opens after `groups create`: `"list"` shows topics as a vertical list (Telegram's default), `"tabs"` shows them as horizontal tabs. The CLI `groups create --topics-layout` and `groups set-layout --layout` flags, and the `POST /telegram/groups` / `POST /telegram/groups/layout` bodies (`topics_layout`), override the default per call.

`default_member_permissions` sets the new group's default banned rights so ordinary members can `create_topics` and `pin_messages`. Other default rights are left untouched.

`rich_markdown_spaced_paragraphs`, `rich_markdown_line_breaks` and `rich_markdown_grouping` set the defaults for the rich-message normalization described under [Rich messages (articles)](#rich-messages-articles); per-call flags (`--no-spaced-paragraphs` / `spaced_paragraphs`, `--no-line-breaks` / `line_breaks`, `--media-group`) override them.

### MCP config (optional)

`mcp` is optional and disabled by default:

- `enabled` defaults to `false`.
- When `enabled: false`, all other fields may be omitted.
- When `enabled: true`, `server_url`, `issuer_url`, `google_client_id`,
  `google_client_secret`, `signing_secret`, and at least one of
  `allowed_emails` or `allowed_domains` are required.
- OAuth redirect URIs must use a trusted loopback host (`localhost`,
  `127.0.0.1`, `::1`) or match `allowed_redirect_hosts` /
  `allowed_redirect_uris`.
- `required_scopes` defaults to `["mcp"]`; every MCP access token must contain
  these scopes. `telegram:admin` is advertised and granted only when
  `admin_emails` or `admin_domains` is configured, and is required by operation
  status/retry tools.
- `access_token_ttl_seconds` defaults to `3600`; `refresh_token_ttl_seconds`
  defaults to `2592000`.
- `signing_secret` must be at least 32 characters; generate it with a command
  such as `openssl rand -base64 32`.

### Idempotency anchor

Group/topic creation is idempotent on a generic `external_ref` (CLI `--external-ref`, HTTP `external_ref`). For backward compatibility the CLI `--planfix-task-id` flag and the HTTP `planfix_task_id` field are accepted as aliases that map onto `external_ref`. With no `external_ref`, groups key on the exact title and topics key on `chat_id + topic_name`.

### Planfix plugin (optional, off by default)

Planfix-specific behavior lives behind an opt-in plugin. With it disabled the core has **zero Planfix knowledge** — `external_ref` still anchors idempotency, but there is no `/task <id>` service message, no `@planfix_bot` welcome cleanup, and `@planfix_bot` is not treated as a protected account. Enable it under `plugins`:

```yaml
plugins:
  planfix:
    enabled: true                 # turn on Planfix-specific behavior
    bot_username: "@planfix_bot"  # group member that receives the /task command
    group_title_postfix: ""       # appended to the Telegram chat title at creation
    cleanup_messages: false       # delete welcome / /task / bot-reply after creation (opt-in)
    task_reply_wait_seconds: 5    # how long to poll for the bot's /task reply
```

When enabled and `external_ref` is set on a group whose members include `bot_username`, the plugin sends `/task <external_ref>` after creation. For **topics** the plugin behaves the same way, but the surviving first message is the **topic name**: the core posts the topic name first, then the plugin posts `/task <external_ref>` as a second message — so after cleanup the topic still shows its name. `group_title_postfix` is appended to the Telegram chat title at creation time but deliberately kept out of the idempotency key, so a replay of the same `external_ref` still matches on the raw title. `cleanup_messages` (default `false`) deletes the bot's welcome message, the `/task <id>` command, and the bot's reply to it — in topics this is scoped to the topic and never touches the topic-name message; `task_reply_wait_seconds` is how long to poll for that reply before deleting only the welcome + command. All cleanup is best-effort: failures are recorded in the operation's `skipped` list and never fail the create.

See `docs/plans/completed/20260518-telegram-planfix-assistant-mvp.md` for the full configuration schema and feature scope.

## Docker

The service ships as a slim Python 3.12 image. Runtime state (Telethon session, SQLite database, `data/config.yml`, bearer token) lives in `/data`, which must be mounted as a volume — nothing sensitive is baked into the image.

Build and run with `docker compose`:

```bash
mkdir -p data
cp path/to/your/config.yml data/config.yml   # fill in api_id, api_hash, bearer_token, etc.
docker compose up -d
curl http://127.0.0.1:8085/health
```

Run a one-shot CLI invocation against the same volume:

```bash
docker compose run --rm telegram-assistant \
    telegram-assistant health
```

The `auth` CLI is interactive (it prompts for phone, code, and optional 2FA password), so run it with a TTY attached:

```bash
docker compose run --rm -it telegram-assistant \
    telegram-assistant auth
```

The Telethon session is written to `/data` and persists across container restarts.

A self-contained smoke script lives at `scripts/docker-smoke.sh`. It builds the image, starts a throwaway container with a temporary `data/config.yml`, polls `GET /health` until it returns `200`, and tears everything down.

```bash
bash scripts/docker-smoke.sh
```

## Tests

```bash
pytest
```
