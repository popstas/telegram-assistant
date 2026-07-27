# Rich markdown: spacing between paragraphs + inline local media

## Overview

Two related improvements to the `rich_markdown` send path (Telegram *article*), both discovered while sending a real Obsidian note:

1. **Paragraph spacing.** The server parses markdown into `PageBlock`s and renders neighbouring `PageBlockParagraph`s tight against each other — the blank lines of the source are lost, so a long article reads as a wall of text. Verified by hand: a paragraph consisting of a single non-breaking space (U+00A0) between two paragraphs produces a visible gap. Make that the **default** (also before headings), with `--no-spaced-paragraphs` / `spaced_paragraphs: false` to send the markdown byte-for-byte.
2. **Inline local media.** Today an article can only reference media by public HTTPS URL, so every `![[Pasted image ….png|caption|size]]` / `.mp4` embed of an Obsidian note has to be stripped before sending. MTProto (unlike the Bot API) exposes `InputRichMessageMarkdown.files: list[InputRichFile]`, so a local file can be uploaded and referenced from the markdown. Consecutive media blocks are grouped into `<tg-collage>` by default; the skill asks the human whether any group should be a slideshow or stay ungrouped.

Key benefit: a markdown file written for a human (an Obsidian note, a report) can be sent as-is and look like an article, instead of being hand-massaged into a Telegram-specific dialect.

### Constraints from Telegram (authoritative source)

`~/projects/js/telegram-functions-bot/node_modules/@grammyjs/types/rich.d.ts` (docstring of `InputRichMessage`, Bot API 10.1) is the fullest spec of the dialect available locally:

- **32 768** UTF-8 characters (already enforced as `MAX_RICH_MARKDOWN_CHARS`).
- **500 blocks**, including nested blocks, list items, table rows, quotation and details blocks.
- 16 levels of nested formatting, **50 media attachments**, 20 table columns.
- Media is a **separate block only**, `![alt](https://…/photo.jpg "Caption")`; type is derived from MIME + URL (`.jpg` photo, `.mp4` video, `.mp3` audio, `.ogg` voice note, `.gif` animation); the title after the URL is the caption.
- Grouping: `<tg-collage>` / `<tg-slideshow>` wrap markdown media blocks (blank-line separated inside the tag).
- Practical note from that project's design doc (`docs/superpowers/specs/2026-07-05-grammy-port-design.md`): markdown containing media makes the send **require media rights in the chat** — a chat that forbids media rejects the whole article.

## Context (from discovery)

Files/components involved:

- `src/telegram_assistant/messages/service.py` — `MAX_RICH_MARKDOWN_CHARS` (:60), `SendMessageRequest.rich_markdown` (:377), `to_payload()` redaction (:423), rich validation block in `send_message` (:547-561), the only-when-set `extra` kwargs block (:633-647).
- `src/telegram_assistant/messages/telethon_backend.py` — `_import_rich_markdown_type()` (:126), `TelethonMessageBackend.send_message` rich branch (:257-268), `_send_rich_message()` (:296+), `_extract_rich_message_id()` (:139).
- `src/telegram_assistant/cli/main.py` — `--rich-markdown` option (:3343), file read/validate (:3430-3465), dry-run payload markers (:3720-3760), the real send call (:3848).
- `src/telegram_assistant/http_api/messages.py` — `MessageSendBody.rich_markdown` (:170) and the `_shape` exclusivity validator (:182-231), route wiring (:932).
- `src/telegram_assistant/http_api/mcp/tools.py` — `rich_markdown` tool kwarg (:1202-1232) and the shared body path (:760).
- `skills/telegram-assistant/SKILL.md` — rich-send section (:542-595) + the `/tmp` temp-file rules (:123-135); must be re-synced to `~/.claude/skills/telegram-assistant/SKILL.md`.
- `scripts/spike_rich_message.py` — the pattern for a raw-MTProto spike (exit 2 = precondition missing, 3 = server rejected).
- Tests: `tests/test_messages_rich_send.py`, `tests/test_cli_messages.py`, `tests/test_http_messages.py`, `tests/test_mcp_*`, plus the legacy-signature fakes (`LegacySendBackend`, `CliLegacyMessageBackend`, `test_mcp_plain_send_omits_rich_markdown_kwarg`) that pin the only-when-set kwargs contract.

Related patterns found:

- **Only-when-set kwargs**: new backend kwargs are passed only when the caller set them, so backends predating the feature keep working; pinned by legacy-signature fakes on every surface.
- **Version-tolerant type import**: `_import_rich_markdown_type()` (mirroring `topics/telethon_backend.py`) — reuse it for `InputRichFilePhoto`/`InputRichFileDocument`.
- **Domain owns validation**: shared validators (e.g. `normalize_search_range`) live in the domain and are called by every surface before any Telegram round-trip.
- **CLI is local/trusted, HTTP/MCP are not**: `telegram.download_root` confines remote write paths; the CLI passes paths straight through. Same split applies here — local media resolution is CLI-only.

Dependencies identified: no new third-party dependency (decision below); Telethon ≥ 1.44 already pinned.

## Decisions (from planning)

- **Scope**: both TODO items in one plan (they touch the same code path).
- **Architecture (option A)**: a new domain module `messages/rich_markdown.py` with pure functions (block scanner, `normalize_rich_markdown()`, `scan_media()`). No markdown-it-py: the Telegram dialect diverges from CommonMark (`==mark==`, `||spoiler||`, `<tg-collage>`, footnotes) and a parse→render round-trip risks rewriting the author's text. The scanner only ever *inserts* lines and *wraps* media runs; every other byte passes through untouched.
- **Block-limit behaviour**: count blocks; if adding spacers would exceed 500, send **without** spacers and return a warning (cosmetics must not break a send).
- **Local media input**: both — auto-resolve from the markdown (relative paths and Obsidian `![[…]]` embeds) **and** an explicit repeatable `--rich-file` flag for files outside the article's directory. Captions come from the markdown media title (`"caption"`) when present, falling back to the alt text.
- **Remote surfaces**: local media is **CLI-only** for now. HTTP/MCP keep https-URL media in the markdown; they still get spacing + grouping.
- **If the spike fails**: try every candidate reference syntax (`![](<id>)`, `![](tg://file?id=…)`, HTML `<img src="<id>">`, `InputRichMessageHTML`) before giving up; only then mark ⚠️ and land the rest.
- **Testing approach**: Regular (code first, then tests in the same task).

## Development Approach

- **Testing approach**: Regular — implement, then write/extend tests inside the same task.
- Complete each task fully before moving to the next.
- Make small, focused changes.
- **CRITICAL: every task MUST include new/updated tests** for its code changes.
  - unit tests for new and modified functions, success **and** error paths.
  - update existing tests when behaviour changes (the default spacing changes what every existing rich-send test sends — expect churn in `tests/test_messages_rich_send.py`).
- **CRITICAL: all tests must pass before starting the next task.**
- **CRITICAL: update this plan file when scope changes during implementation.**
- Run `pytest` and `ruff check src tests` after each task.
- Maintain backward compatibility: backends and surfaces that never heard of `rich_files`/`spaced_paragraphs` must keep working (legacy-signature fakes stay green).

## Testing Strategy

- **Unit tests**: required for every task. The scanner/normalizer is pure and table-driven — cover fenced code, tables, lists, quotes, `<details>`/`<tg-collage>` blocks, headings, media runs, CRLF, and text that already contains U+00A0.
- **E2E**: this project has no UI e2e. The live scripts (`scripts/e2e_*.sh`) currently fail at every send because `Client chat test` rejects sends (see `docs/TODO.md`), so they cannot gate this work — real verification is the manual send listed in Post-Completion.
- **Spike**: `scripts/spike_rich_media.py` sends a real message and is run manually, like `scripts/spike_rich_message.py`; it is not part of `pytest`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with ➕ prefix.
- Document issues/blockers with ⚠️ prefix.
- Update the plan if implementation deviates from the original scope.

## What Goes Where

- **Implementation Steps** (`[ ]`): code, tests, docs inside this repo.
- **Post-Completion** (no checkboxes): manual sends against a real account, the skill re-sync to `~/.claude`, anything needing a human eye on rendering.

## Implementation Steps

### Task 1: Block scanner in `messages/rich_markdown.py`

- [x] create `src/telegram_assistant/messages/rich_markdown.py` with a line-based scanner that splits markdown into typed blocks: `paragraph`, `heading`, `list`, `table`, `quote`, `code` (fenced, content never inspected), `divider`, `media`, `html` (`<details>`, `<tg-collage>`, `<tg-slideshow>`), `footnote`
- [x] recognise a media line: `![alt](target "optional caption")` alone on its line; expose `target`, `alt`, `caption`, and whether the target is an http(s) URL or a local reference; also recognise the Obsidian embed form `![[file.png|caption|size]]`. All obsidian caption formats - https://raw.githubusercontent.com/alangrainger/obsidian-image-captions/refs/heads/main/README.md
- [x] add `count_blocks(blocks)` approximating Telegram's rule (list items, table rows and nested blocks count individually) plus `count_media(blocks)`
- [x] export the module's public names from `messages/__init__.py` alongside the existing helpers
- [x] write table-driven tests for the scanner (fenced code containing `#`/`|`/`![]()`, tables, nested lists, quotes, `<details>` with blank lines inside, CRLF input, trailing whitespace)
- [x] write tests for `count_blocks`/`count_media` (including the 500/50 boundaries)
- [x] run `pytest` and `ruff check src tests` — must pass before task 2

**Task 1 notes** (decisions made while implementing, they constrain later tasks):

- Public API: `scan_blocks(markdown) -> tuple[Block, ...]`, `parse_media_line(line) -> MediaRef | None`, `count_blocks`, `count_media`, `iter_media`, `split_lines`, plus `Block`, `MediaRef`, `MAX_RICH_BLOCKS = 500`, `MAX_RICH_MEDIA = 50`, `HTML_BLOCK_TAGS`, `BLOCK_KINDS`.
- `Block` is a frozen dataclass with `kind`, verbatim `lines`, half-open `start`/`end` line indexes into the normalised document, `weight` (its share of the 500-block budget, children included), optional `media`, `children` (for `quote`/`html`), `level` (headings), `html_tag`.
- Weights: leaf blocks 1; `list` = 1 + items; `table` = 1 + non-delimiter rows; `quote`/`html` = 1 + nested `count_blocks`. Documented as an approximation used to *warn*, never to reject.
- ➕ Obsidian caption grammar covers more than `|caption|size`: alignment segments (`left`/`center`/`right`), size `NxN`, size-only embeds (`![[a.png|150]]`), the markdown-with-size form `![Cap|150](a.png)`, and the filename macros `%` / `%.%`. All are parsed into `caption` / `size` / `alignment`.
- Media is only its own block when the line stands alone (followed by a blank line, EOF, another block start, or another media line). `![](a.png)` followed by prose stays inside the paragraph — so a run of Obsidian embeds on consecutive lines is still a run of media blocks (what Task 8 groups).
- `<details>`/`<tg-collage>`/`<tg-slideshow>` bodies are scanned into `children`, which is how Task 8 can tell an author-written group from a run it may wrap itself.
- Setext headings (`Title` + `===`/`---`) are typed `heading`, not paragraph + divider, so Task 2 never inserts a spacer between the two lines.

### Task 2: Spacer insertion (`normalize_rich_markdown`)

- [x] add `normalize_rich_markdown(markdown, *, spaced_paragraphs=True) -> RichMarkdownNormalization` returning the rewritten markdown, block count, media count and warnings
- [x] insert a U+00A0-only line as its own block between two consecutive plain paragraphs, and before a heading of any level (`#`…`######`); never after a heading, never inside code/table/list/quote/html blocks
- [x] skip insertion where the author already separated blocks with a spacer (idempotent: normalising twice yields the same text)
- [x] when the spaced result would exceed 500 blocks, fall back to the unspaced markdown and emit the warning `spaced_paragraphs disabled: N blocks would exceed the 500-block limit`
- [x] emit a warning when the unspaced article itself exceeds 500 blocks or 50 media (do not raise — Telegram is the authority)
- [x] write tests: spacing between paragraphs, before headings, no spacing inside code/list/table/quote, idempotency, block-limit fallback (with warning), `spaced_paragraphs=False` returns input unchanged
- [x] run `pytest` and `ruff check src tests` — must pass before task 3

**Task 2 notes** (decisions made while implementing, they constrain later tasks):

- Public API added: `normalize_rich_markdown`, `RichMarkdownNormalization` (`markdown`, `blocks`, `media`, `spaced`, `warnings`; Task 8 adds `groups`), `NBSP`, `SPACER_LINE`, `is_spacer_line`, `is_spacer_block` — all re-exported from `messages/__init__.py`.
- ➕ **A spacer line is not blank to the scanner.** `' '.isspace()` is `True` in Python, so the Task 1 blank checks would have swallowed the module's own spacers and doubled them on every re-normalisation. Blankness is now `_is_blank()` (whitespace **and** no U+00A0); a spacer line is its own `paragraph` block wherever it appears, and `_starts_new_block()` breaks a paragraph before one. This is also what makes the block count honest — Telegram charges a spacer as a `PageBlockParagraph`.
- Insertion rule: between two consecutive top-level `paragraph` blocks, and before a `heading` (ATX or setext) — but never when the *previous* block is a heading (so heading→heading stays tight), never adjacent to an existing spacer, never before the first block. Nothing else pairs (paragraph↔list/table/quote/media/html/code are left alone).
- Byte fidelity: when no insertion point is found the **original string is returned by identity**, so CRLF and the trailing newline survive untouched and `spaced_paragraphs=False` is a strict no-op. Once anything is inserted the output is LF-normalised (a trailing newline is re-appended).
- Warnings are additive, never fatal: the fallback warning uses the exact wording from this plan, and an article already over 500 blocks / 50 media gets `article has N blocks, over Telegram's 500-block limit` / `article has N media attachments, over Telegram's 50 limit`. An already-over-limit article receives *both* (spacing is still skipped — it must not be made worse).
- `spaced` is `True` whenever the pass ran and stuck, including a document that needed no spacer at all.

### Task 3: Wire normalization into `send_message`

- [x] add `spaced_paragraphs: bool = True` to `SendMessageRequest`; include it in `to_payload()` so the audit trail records what was sent
- [x] respect config `telegram.defaults.rich_markdown_spaced_paragraphs: bool`, default: True
- [x] in `send_message`, normalise the markdown **before** the length check, so `MAX_RICH_MARKDOWN_CHARS` bounds what actually goes to Telegram; pass the normalised text down through the existing only-when-set `extra` block
- [x] add `warnings: tuple[str, ...] = ()` to `SendMessageResult` (defaulting empty, serialised in `to_dict()`/`from_dict()`) and carry the normalization warnings through
- [x] keep the redaction rule in `to_payload()` applied to the **normalised** markdown
- [x] write tests: default send is spaced; `spaced_paragraphs=False` sends byte-for-byte; a spaced article over the char limit raises `ValueError` naming the post-normalization length; warnings reach the result; the legacy-signature fake still passes
- [x] run `pytest` and `ruff check src tests` — must pass before task 4

**Task 3 notes** (decisions made while implementing, they constrain later tasks):

- `send_message` normalises once and then rebinds `request = replace(request, rich_markdown=normalization.markdown)`, so the operation payload, the length check and the backend kwarg can never disagree about what was sent. That is also why the redaction rule needed no change — `to_payload()` is called after the rebind, and the payload now reads back as the *normalised* markdown (spacers included).
- The over-limit `ValueError` names the post-normalization length and appends `after paragraph spacing` **only** when the pass actually changed the text, so a source that was already too long is not blamed on spacing. Existing surface tests that only match `32768` stay green.
- `spaced_paragraphs` is a *request-level* knob, never a backend kwarg: the only-when-set `extra` block still passes just `rich_markdown`, so the legacy-signature fakes are untouched by this task.
- ➕ Config is read through `spaced_paragraphs_default(config)` (exported from `messages/`) rather than three duck-typed `getattr` chains — CLI, HTTP and MCP all call it when building the request. It tolerates a missing config / a config predating the knob (→ `True`). Task 4's per-call flag layers **over** this value.
- `SendMessageResult.warnings` is serialised as a list and read back with `payload.get("warnings") or ()`, so operation rows written before this task replay as "no warnings" instead of raising. A replay reports the original send's warnings, not a fresh normalisation.
- ➕ Existing test churn was a single assertion (`test_send_message_rich_markdown_service_command_is_redacted`): a paragraph followed by a heading now gets a spacer, so the backend sees the normalised text. Everything else in the suite already used markdown with no insertion point.

### Task 4: Surface the flag (CLI, HTTP, MCP)

- [x] CLI: add `--spaced-paragraphs`, `--no-spaced-paragraphs` to `messages send` (only meaningful with `--rich-markdown`; error with exit code 2 when used without it)
- [x] CLI dry-run: echo `spaced_paragraphs`, `rich_markdown_chars` (post-normalization), `rich_markdown_blocks`, `rich_markdown_media`, and any warnings — never the body
- [x] CLI real run: print warnings alongside the result JSON
- [x] HTTP: add `spaced_paragraphs: bool = True` to `MessageSendBody`, rejected in `_shape` when `rich_markdown` is absent (422, matching the existing exclusivity errors)
- [x] MCP: add the same kwarg to the send tool, documented in its docstring; keep it out of the plain-send call path (the `test_mcp_plain_send_omits_rich_markdown_kwarg` contract)
- [x] write tests per surface: default on, flag off, flag without `--rich-markdown` errors, dry-run markers present, MCP/HTTP legacy fakes unaffected
- [x] run `pytest` and `ruff check src tests` — must pass before task 5

**Task 4 notes** (decisions made while implementing, they constrain later tasks):

- ⚠️ Deviation: the surface field is `spaced_paragraphs: bool | None = None`, **not** `bool = True`. With a `True` default there is no way to tell "caller asked for spacing" from "caller sent a plain text message", so the plan's own rule — reject it when `rich_markdown` is absent — would have failed every plain send. `None` means *not set* and defers to `spaced_paragraphs_default(config)`; an explicit `true`/`false` layers over it. Same three-level precedence on all three surfaces: flag → `telegram.defaults.rich_markdown_spaced_paragraphs` → built-in `True`.
- The CLI flag is one Typer option pair, `--spaced-paragraphs/--no-spaced-paragraphs`, typed `bool | None`; the without-`--rich-markdown` check runs next to the other rich input checks, so it costs no backend connection (exit 2 in dry-run and real runs alike).
- ➕ The dry-run preview now *runs* `normalize_rich_markdown` rather than reporting the source size: `rich_markdown_chars` is post-normalization (and so is the `would`/`planned_actions` prose), joined by `rich_markdown_blocks`, `rich_markdown_media`, `spaced_paragraphs` (the effective decision) and `spaced` (what the pass actually did — a block-limit rollback turns it `False` while `spaced_paragraphs` stays `True`). All five are `None` for a plain send. The body is still never echoed.
- ➕ Dry-run also warns when the *normalised* article exceeds `MAX_RICH_MARKDOWN_CHARS`: the CLI's own pre-read check bounds the source, so without this a preview would report a plan the real send is guaranteed to reject.
- The real run echoes each `warnings` entry to **stderr** as `warning: <text>` before printing the result JSON (which already carries them from Task 3), so stdout stays a single parseable JSON line.
- `spaced_paragraphs` remains a request-level knob — no new backend kwarg — so the legacy-signature fakes are untouched; `tests/test_messages_rich_spacing_flag.py` re-pins the MCP plain-send contract against `FakeMessageBackend` anyway, since the tool grew a new parameter.

### Task 5: Spike — local media inside a rich message

- [x] create `scripts/spike_rich_media.py` modelled on `scripts/spike_rich_message.py` (same preconditions and exit codes: 2 = precondition missing, 3 = server rejected), taking `--entity` (default `me`) and a local image/video path
- [x] upload the file with `client.upload_file`, turn it into `InputPhoto`/`InputDocument` (via `messages.uploadMedia` on the target peer where needed) and build `InputRichFilePhoto(id=…, photo=…)` / `InputRichFileDocument(id=…, document=…)`
- [x] try each candidate markdown reference in turn and report which the server accepts: `![](<id>)`, `![](tg://file?id=<id>)`, `![alt](<id> "caption")`, and the HTML form `<img src="<id>"/>` via `InputRichMessageHTML`
- [x] read the sent message back and print the resulting `RichMessage` block list, so the accepted syntax is proven, not assumed
- [x] record the findings in this plan (accepted syntax, id format, whether captions survive, whether video needs a document thumbnail) — ✅ **done for real**: the spike was run against Saved Messages during Task 6 and all four questions are answered below, so tasks 6–7 are unblocked and no ⚠️ skip is needed
- [x] write a unit test for the pure candidate-builder helper the spike imports (list of variants, id substitution) — the network part stays manual
- [x] run `pytest` and `ruff check src tests` — must pass before task 6

**Task 5 notes** (decisions made while implementing, they constrain later tasks):

- ~~⚠️ **The spike has not been run.**~~ **Resolved during Task 6** — the spike was run against Saved Messages (`--entity me`), the findings below are real, and tasks 6–7 proceed on a proven syntax rather than a guess.
- Confirmed locally against the installed Telethon 1.44.0: `InputRichFilePhoto(id: str, photo: TypeInputPhoto)`, `InputRichFileDocument(id: str, document: TypeInputDocument)`, and **both** `InputRichMessageMarkdown` and `InputRichMessageHTML` take `files: list[TypeInputRichFile]`. `types.Message` has a `rich_message` field, which is what the read-back prints. So the id is a **caller-chosen string**, not a Telegram file id — the open question is purely how the markdown names it.
- `@grammyjs/types/rich.d.ts` is no help here: it documents the *Bot API* dialect, where "Media blocks support only HTTP and HTTPS URLs". The `files:` list is MTProto-only, hence the spike.
- Public (unit-tested) helpers the later tasks reuse: `build_candidates(file_id, *, alt, caption) -> tuple[Candidate, ...]`, `classify_file(path) -> "photo" | "document"` (photo iff the suffix is `.jpg/.jpeg/.png/.webp`; `.gif` is an animation ⇒ document), `default_file_id(path)` (slug of the stem, non-`[A-Za-z0-9_.-]` → `-`, empty ⇒ `file1`, so a Cyrillic or bracketed filename can never break the `![](…)` it is written into).
- Candidate order: `bare-id`, `tg-file-url`, `attach-scheme` (➕ added — the Bot API's own multipart convention was the obvious fourth guess), `alt-and-caption`, `html-img`. Each candidate is a **whole article** that names its own candidate in an `# heading`, so Saved Messages says which syntax produced which message without a read-back.
- One send per candidate, and a rejection does **not** stop the loop — the point is which of the five the server accepts, so all are tried and the accepted names are printed at the end. Exit 3 means *every* candidate was rejected (or the upload itself failed); exit 0 means at least one stuck.
- `--only <names>` runs a subset (an unknown name is a precondition failure, exit 2), `--file-id` overrides the derived id, `--dry-run` prints every candidate article without uploading.

**Task 5 findings** — ✅ **the spike was run** (2026-07-27, `--entity me`, Telethon 1.44, premium account). Six probe rounds; the answer came from the Bot API 10.2 twin (`InputRichMessageMedia`, documented in `@grammyjs/types@latest/rich.d.ts`: *"List of media that are specified in the markdown or html fields using `tg://photo?id=`, `tg://video?id=`, and `tg://audio?id=` links"*) and was then confirmed live.

- **accepted syntax**: `![alt](tg://photo?id=<id> "caption")`, `![](tg://video?id=<id>)`, `![](tg://audio?id=<id>)`. The scheme **must match the uploaded media**: a photo named through `tg://video` fails `RICH_MESSAGE_VIDEO_INVALID`, a document through `tg://photo` fails `RICH_MESSAGE_PHOTO_INVALID`. Every non-`tg://` form is rejected with `RICH_MESSAGE_PHOTO_URL_INVALID` — bare id, relative path, absolute path, `file://`, `attach://<id>`, `tg://file?id=`, `tg://rich?id=`, `https://<host>/<id>`, `<img src="<id>">` via `InputRichMessageHTML`. Read-back proves it: `RichMessage` carries a real `PageBlockPhoto(photo_id=…)` plus `photos=[…]`, not a link.
- **id format**: an ASCII identifier, `[A-Za-z0-9_-]+`. A dot (`photo.png`), a space or a non-ASCII character (`фото`) is rejected with `RICH_MESSAGE_FILE_ID_INVALID` — note this fires *before* the URL check, which is why round 1 (dot-free ids) and round 2 (dotted ids) returned different errors. 64 characters are accepted; dashes and underscores are fine.
- **captions survive**: yes — `PageBlockPhoto.caption` is a populated `PageCaption` for both `![](tg://photo?id=x)` and `![alt](tg://photo?id=x "cap")`.
- **video needs a document thumbnail**: no. `InputMediaUploadedDocument` with `DocumentAttributeVideo` (no `thumb`) was accepted; `.mp3` with `DocumentAttributeAudio` was accepted through `tg://audio`.
- ➕ **`files` does not intercept http(s) URLs.** With a `files` list present, a real remote URL is still fetched normally, so remote and local media compose freely in one article.
- ➕ The rejected guesses stay in `build_candidates` as a regression probe; `--only tg-scheme` runs just the proven one. `default_file_id` was **wrong** as landed in Task 5 (its slug allowed `.`, which the server rejects) and now delegates to the domain's `make_rich_file_id`.

### Task 6: Resolve local media from markdown (domain + CLI)

- [x] add `scan_media(markdown, *, base_dir, vault_dir=None)` to `rich_markdown.py`: classify each media block as remote (http/https, untouched) or local, resolve local targets relative to the markdown file's directory, and resolve Obsidian `![[name.png|caption|size]]` embeds by searching `vault_dir` (nearest match; ambiguity is an error, not a guess)
- [x] carry `caption` from the markdown title, falling back to the alt text; drop the Obsidian `|size` suffix
- [x] add `rich_files: tuple[RichFile, ...]` to `SendMessageRequest` (`RichFile = {id, path, caption, kind}`), validated in `send_message` (file exists, readable, ≤ 50 media, only allowed with `rich_markdown`) and recorded in `to_payload()` as metadata only (path + kind, never contents)
- [x] rewrite the markdown so each local media block references the file id in the syntax the spike proved; unresolvable local media is an error naming the file (no silent drop)
- [x] CLI: resolve local media by default, add repeatable `--rich-file <id>=<path>` for files outside the article directory, and `--vault-dir` for Obsidian lookups; dry-run lists the resolved files (path, kind, caption) without uploading
- [x] write tests: relative path, absolute path, Obsidian embed, ambiguous embed error, missing file error, remote URL untouched, `--rich-file` override, >50 media rejected, `rich_files` without `rich_markdown` rejected
- [x] run `pytest` and `ruff check src tests` — must pass before task 7

**Task 6 notes** (decisions made while implementing, they constrain later tasks):

- Public API added to `rich_markdown.py` (all re-exported from `messages/`): `scan_media`, `MediaScan` (`markdown`, `files`, `remote`), `RichFile` (`id`, `path`, `caption`, `kind`), `MediaResolutionError` / `AmbiguousMediaError` (both `ValueError`, so every surface's existing 400 / exit-2 path already covers them), `media_kind`, `rich_file_reference`, `make_rich_file_id`, `RICH_FILE_SCHEMES`, `RICH_FILE_ID_RE`, `MAX_RICH_FILE_ID_CHARS`, `PHOTO_/VIDEO_/AUDIO_SUFFIXES`.
- **Kind is decided once, from the suffix**, because the scheme in the markdown and the upload shape must agree (`RICH_MESSAGE_VIDEO_INVALID` otherwise). `.gif` is an animation ⇒ a document ⇒ `tg://video`; a suffix in none of the three sets (`.pdf`) raises rather than being guessed — the dialect has no fourth scheme to write it into.
- **Byte fidelity by identity, again**: an article with no *local* media returns the input string unchanged (`scan.markdown is markdown`), so a remote-only or media-less body keeps its CRLF and trailing newline. Only a rewrite normalises to LF, matching `_insert_spacers`.
- Rewriting replaces `ref.raw` *inside* the original line rather than replacing the whole line, so media nested in a block quote keeps its `> ` prefix and no special case is needed for quote children (whose `Block.start` are true document indexes but whose `lines` are de-quoted).
- Resolution order per reference: override (keyed by the target as written, its URL-decoded form, **or** its bare file name — `--rich-file <ref>=<path>` reads naturally either way) → absolute path → path relative to the article's directory → by-name walk of `vault_dir` (default: the article's directory). "Nearest" is path-step distance from the article; a tie raises `AmbiguousMediaError` listing the candidates. `os.walk`, not `rglob`: an Obsidian file name may contain `[` or `*`.
- ➕ **An override that matched nothing is an error.** A silently-ignored `--rich-file` would send the article without the file the operator explicitly supplied — the same "no silent drop" rule the plan states for unresolvable media.
- The same resolved path referenced twice yields **one** `RichFile` and one upload; two different files with the same stem get `shot`, `shot-2`. Captions stay per-reference (they live in the markdown); `RichFile.caption` records the first one for the dry-run listing.
- `_validate_rich_files` runs **before** the operation row is opened and *does* touch the filesystem (unlike `_validate_attachment_refs`, which is deliberately pure): the ids are already written into the markdown, so a missing file would send an article whose media points at nothing, and failing early keeps the idempotency key free for the fixed retry. It also re-checks the id grammar and the kind, so a hand-built request cannot produce `RICH_MESSAGE_FILE_ID_INVALID`.
- ➕ **The service already passes `rich_files` down** through the only-when-set `extra` block (Task 7 only has to make the Telethon backend accept and upload it). A media-less article still hits the backend with the pre-media signature, which `LegacySendBackend` pins. Until Task 7 lands, a CLI send *with* local media will fail in the Telethon backend — the domain, the CLI and the payload are complete, the upload is not.
- Local media stays **CLI-only** as decided: HTTP/MCP never call `scan_media`, so a remote caller cannot name a server-side path. `--rich-file`/`--vault-dir` without `--rich-markdown` is exit 2, like `--spaced-paragraphs`.
- ➕ Dry-run gained `resolved.rich_files` (`id`, `path`, `kind`, `caption`; `None` for a plain send) — the files are *listed*, never read, so a preview still touches no bytes and no network.

### Task 7: Upload and send media (`telethon_backend`)

- [x] extend `TelethonMessageBackend.send_message`/`_send_rich_message` with an only-when-set `rich_files` kwarg
- [x] import `InputRichFilePhoto`/`InputRichFileDocument` through the existing version-tolerant probe pattern; a Telethon without them raises `RichMessageUnsupported` naming the version (never `MessageSendFailed`)
- [x] upload each file once (`client.upload_file` → `messages.uploadMedia` → `InputPhoto`/`InputDocument`), build the `files=` list in markdown order, and pass it into `InputRichMessageMarkdown`
- [x] map a media-rights rejection (`ChatSendMediaForbiddenError` and friends) to a clear domain error naming the chat — no silent fallback to a plain or media-less send
- [x] write tests with a fake client: files uploaded in order, ids match the markdown references, the kwarg is omitted for a media-less article (legacy fake stays green), unsupported-Telethon path, media-rights error mapping
- [x] run `pytest` and `ruff check src tests` — must pass before task 8

**Task 7 notes** (decisions made while implementing, they constrain later tasks):

- `_import_rich_file_types()` mirrors `_import_rich_markdown_type()` and is probed **only when `rich_files` is non-empty**, so a media-less article on an old-but-≥1.44 build never pays for it. Its `RichMessageUnsupported` is raised *before* the first upload, so nothing has been sent to Telegram when the version is wrong.
- ➕ **New domain error `RichMediaForbidden`** (in `messages/service.py`, exported from `messages/`), raised for Telegram's `ChatSend{Media,Photos,Videos,Docs,Audios,Gifs,Voices}ForbiddenError` and `MediaCaptionTooLongError`, naming the chat id and the upstream class. It is a **`ValueError`** subclass on purpose: an unmapped `RuntimeError` surfaces as Starlette's *empty* 500 (the same trap `RichMessageUnsupported` had to work around), while `ValueError` already lands on every surface's 400 / exit-2 path with the message intact. The mapping wraps **both** the upload loop and the send RPC — an article's media may be a remote URL, so a media-rights refusal can arrive with nothing uploaded — via `_translate_rich_send_error()`, which tries media rights first and otherwise falls through to `translate_flood_wait`, so FLOOD_WAIT during an upload is still a queue-visible pause.
- The peer is resolved **once**, before the uploads: `messages.uploadMedia` binds the upload to the destination peer, so the same `InputPeer` must serve the upload and the send.
- `files=` is passed to `InputRichMessageMarkdown` through an only-when-set `rich_kwargs` dict, so a media-less article still constructs `files=None` — the shape the pre-media test pins.
- ➕ Document attributes come from Telethon's own `utils.get_attributes()` (which derives `DocumentAttributeVideo` from a `video/*` mime type even with no metadata library installed), with **one** gap filled: without `hachoir` it emits no `DocumentAttributeAudio` at all, and the Task 5 findings proved an `.mp3` is only reachable through `tg://audio` when it carries one, so `_document_attributes()` appends `DocumentAttributeAudio(duration=0)` for `kind == "audio"` when it is missing. `.gif` (kind `video`, mime `image/gif`) is left to Telegram's own animation conversion.
- A plain (non-rich) send that somehow carries `rich_files` is rejected with `ValueError` rather than silently dropping the uploads — the ids are only meaningful to the markdown that names them.

### Task 8: Group consecutive media into `<tg-collage>`

- [x] add grouping to `normalize_rich_markdown`: a run of 2+ consecutive media blocks (no text between them) is wrapped in `<tg-collage>` … `</tg-collage>` with blank lines inside, per the dialect
- [x] respect config `telegram.defaults.rich_markdown_grouping`, default: `collage` — ~~⚠️ **the live `data/config.yml` already sets this key**, and `TelegramDefaults` forbids extra inputs, so *every* command currently fails with `telegram.defaults.rich_markdown_grouping: Extra inputs are not permitted` until this task lands~~ **resolved**: the key is now declared (`RichMarkdownGrouping = Literal["collage", "slideshow", "none"]`) and the live config loads again
- [x] accept a per-group override argument (`media_groups=[{index, mode: "collage"|"slideshow"|"none"}]`) so a caller can turn one run into `<tg-slideshow>` or leave it ungrouped; unknown indexes are an error
- [x] expose the detected groups in the normalization result (index, media count, the trailing ~50 characters of the preceding text) so surfaces can describe them
- [x] never group media that is already inside an author-written `<tg-collage>`/`<tg-slideshow>`/`<details>` block
- [x] CLI: `--media-group <index>=<collage|slideshow|none>` (repeatable); dry-run lists detected groups with their preceding text and chosen mode
- [x] write tests: two runs grouped, single media untouched, override to slideshow/none, author-written collage untouched, group info reported, grouping counted correctly against the block limit
- [x] run `pytest` and `ruff check src tests` — must pass before task 9

**Task 8 notes** (decisions made while implementing, they constrain later tasks):

- Public API added to `rich_markdown.py` (all re-exported from `messages/`): `MediaGroup` (`index`, `size`, `preceding_text`, `mode`), `MediaGroupChoice` (`index`, `mode`), `MediaGroupError`, `MEDIA_GROUP_MODES`, `MEDIA_GROUP_TAGS`, `MEDIA_GROUP_CONTEXT_CHARS`, `DEFAULT_MEDIA_GROUP_MODE`. `RichMarkdownNormalization` gained `groups`, plus ➕ `grouped`/`spacers_added` — with two rewriting passes, "the text changed" no longer means "spacing grew it", and the over-limit `ValueError` in `send_message` must name the pass that actually did it (`after paragraph spacing`, `after media grouping`, or both).
- Order is **group → space → count**: the spacer pass runs on the grouped text, so the reported block count and the 500-block rollback both see the container blocks the send will actually carry (a collage costs `1 + its media`).
- A run is 2+ **top-level** media blocks with no other block between them. Media inside a quote or an author-written `<tg-collage>`/`<tg-slideshow>`/`<details>` lives in `Block.children` and is never re-grouped — that is what makes "never regroup an author's own group" fall out of Task 1's scanner rather than needing a special case.
- Byte fidelity by identity for the third time: nothing wrapped ⇒ the input string is returned unchanged, so `grouping: none` (or a media-less article) keeps its CRLF and trailing newline. A wrap rebuilds only the run's own line range — the media lines survive verbatim, the whitespace between them is replaced by the blank lines the dialect wants inside the tag, and a blank line is added on each side when the run sat tight against its text.
- `media_groups` accepts every shape a surface naturally has: `{index: mode}`, `MediaGroupChoice`s, `{"index": …, "mode": …}` dicts, or `(index, mode)` pairs. An index naming no run raises `MediaGroupError` (a `ValueError`, so every surface's 400 / exit-2 path already covers it) — the "no silent drop" rule the plan states for media, applied to grouping.
- `SendMessageRequest` gained `media_grouping` (config default via ➕ `media_grouping_default(config)`, the twin of `spaced_paragraphs_default`) and `media_groups`; both are recorded in `to_payload()` and neither is a backend kwarg, so the legacy-signature fakes are untouched.
- The CLI pre-checks `--media-group` indexes against the article's real runs *before* opening a backend (a cheap `grouping="none"` pass), so a typo is exit 2 with no connection; the same error is also caught around the send as a fence.
- Grouping has **no** all-or-nothing CLI flag — the config knob sets the mode and `--media-group` overrides one run. HTTP/MCP get the config default but no per-group override (they cannot see the dry-run's group list; the CLI is the surface with the human in the loop).
- Dry-run gained `media_grouping` (the effective default) and `rich_markdown_groups` (`index`, `size`, `mode`, `preceding_text`) — `preceding_text` is the whitespace-collapsed tail of the nearest text block above the run, `…`-prefixed when truncated at 50 chars. That is the list Task 9's `AskUserQuestion` dialogue reads.
- ➕ Existing test churn: two cases in `tests/test_rich_markdown_normalize.py` used bare media runs to test the *spacer* pass and now pass `grouping="none"` to isolate it.

### Task 9: Skill dialogue and documentation

- [x] `skills/telegram-assistant/SKILL.md`: document that spacing is on by default and `--spaced-paragraphs`, `--no-spaced-paragraphs` turns it off; document local media (auto-resolve, `--rich-file`, `--vault-dir`) as CLI-only
- [x] `skills/telegram-assistant/SKILL.md`: add the grouping dialogue — when the dry-run reports media groups, ask via `AskUserQuestion` whether any group should change; if yes, one question per group: «После текста `<preceding text>` как сгруппировать медиа?» with options `Collage` / `Slideshow` / `Ungrouped`, then re-run the dry-run with the chosen `--media-group` flags. A single group left as-is needs no second question
- [x] `skills/telegram-assistant/SKILL.md`: extend the rich-send error list with the new messages (unresolvable media, >50 media, media rights, `--rich-file` parse errors)
- [x] update `README.md` (Commands + the rich-send description + MCP tool notes) and `CLAUDE.md` (the rich-send bullet under Architecture: normalization owns spacing/grouping, `rich_files` is CLI-only, block/media limits)
- [x] update `docs/TODO.md`: check off both rich-markdown items
- [x] write/extend `tests/test_skill_inventory.py` expectations if the CLI catalog changed
- [x] run `pytest` and `ruff check src tests` — must pass before task 10

**Task 9 notes** (decisions made while implementing, they constrain later tasks):

- The CLI **catalog** did not change (no new commands), so `test_skill_inventory.py` needed no row edits. Instead it gained a narrower guard aimed at what *did* change: `RICH_SEND_FLAGS` (`--rich-markdown`, `--no-spaced-paragraphs`, `--rich-file`, `--vault-dir`, `--media-group`) is asserted three ways — still declared on the Typer command (so a rename cannot leave the doc checks passing against stale text), named in `SKILL.md`, and named in `README.md` — plus a check that the grouping dialogue is still documented (`rich_markdown_groups` + `AskUserQuestion` + `Slideshow`). A flag the agent has never read about is a flag it will not use.
- ➕ The grouping dialogue is **two-stage**, not one question per group up front: the dry-run's group list is shown, then a single «Изменить группировку?» question, and only a `Изменить` answer expands into one question per group. The plan's own "a single group left as-is needs no second question" rule generalises — an article with six collages should not cost six questions when the default is what the human wants.
- ➕ SKILL.md now tells the agent **not to copy a note with local media to `/tmp`**: media resolves against the markdown file's own directory, so copying the text there breaks every embed. That contradicted the pre-existing "write the article to `/tmp`" rule, which now applies only to articles the agent authors itself.
- ➕ CLAUDE.md gained **two** new bullets rather than growing the rich-send one (it was already the longest in the file): one for `rich_markdown.py` (insert-or-wrap only, no parser; identity return as the byte-fidelity mechanism; group → space → count; warnings-not-rejections with the 500-block rollback as the single exception; why surface fields are `bool | None`), one for local media (resolution order, the `tg://` scheme↔upload agreement, the id grammar, `RichMediaForbidden`, CLI-only). The header above them now reads "Five cross-cutting behaviours". This also covers Task 11's "record the proven MTProto media syntax in CLAUDE.md".
- `docs/TODO.md`: both rich-markdown items are checked off with a pointer to this plan. The one piece of the media item that was **not** built — `noautolink` / `rtl` passthrough on `InputRichMessageMarkdown` — was split out into its own open TODO item rather than being silently closed with the rest.
- The skill was re-synced to `~/.claude/skills/telegram-assistant/SKILL.md` (verified identical), so the Post-Completion "external system updates" entry for it is already done.

### Task 10: Verify acceptance criteria

- [x] verify every requirement from Overview is implemented (spacing default + flag, block-limit fallback with warning, local media auto-resolve + `--rich-file`, collage grouping + overrides, skill dialogue)
- [x] verify edge cases: article with no paragraphs at all, article that is only media, media inside fenced code (must not be treated as media), U+00A0 already present, `spaced_paragraphs=False` plus media, exactly 500 blocks / exactly 50 media
- [x] run the full test suite (`pytest`)
- [x] run `ruff check src tests` — all issues fixed
- [x] verify test coverage of `messages/rich_markdown.py` is at project standard (80%+)

**Task 10 notes** (verification results):

- Requirements checked against the shipped code, not the notes: `--rich-markdown`, `--spaced-paragraphs/--no-spaced-paragraphs`, `--media-group`, `--rich-file`, `--vault-dir` all present in `messages send --help`; `MessageSendBody.spaced_paragraphs` with its `_shape` rejection and the `media_grouping_default` wiring present in `http_api/messages.py`; the same kwarg + docstring in the MCP send tool; the grouping dialogue and the flag list pinned by `tests/test_skill_inventory.py`.
- ➕ New file `tests/test_rich_markdown_edge_cases.py` (15 tests) pins the plan's edge-case list as an executable acceptance check rather than a one-off eyeball: no-paragraph / heading-only / empty-and-blank articles return the input **by identity**; an all-media article becomes exactly one `<tg-collage>` (3 blocks) while a single media block is untouched; a `![](shot.png)` inside a fence is invisible to *both* the normalizer (`media == 0`, no wrap) and `scan_media` (no upload planned, identity return) even when the file really exists next to the article; an author's U+00A0 in running text neither suppresses the spacer nor counts as a block, and an article of author-written spacers is identity-stable.
- The boundary is **inclusive**: a spaced result landing on exactly 500 blocks is kept with no warning, 501 rolls back; an unspaced article at exactly 500 blocks and an article with exactly 50 media do not warn. These assert the plan's rule, not the implementation's current arithmetic.
- ➕ `spaced_paragraphs=False` plus media turned out to be two cases worth separating: grouping is **independent** of spacing (spacing off still collages, and the group's `preceding_text` is still reported), and with both passes off the markdown reaches the backend byte-for-byte — pinned end-to-end through `send_message` with CRLF, no trailing newline and a `rich_files` tuple, so "cosmetics off" cannot quietly cost the uploads.
- Results: `pytest` **1993 passed**, `ruff check src tests` clean, and `coverage` on `messages/rich_markdown.py` is **98%** (666 statements, 14 missed) — comfortably over the 80% bar.

### Task 11: [Final] Update documentation

- [ ] re-read `README.md` / `CLAUDE.md` / `SKILL.md` diffs for accuracy against the final behaviour
- [ ] record the proven MTProto media syntax in `CLAUDE.md` (it is not documented in Telethon's stubs and will otherwise be re-derived)

## Technical Details

**Normalization result**

```python
@dataclass(frozen=True)
class RichMarkdownNormalization:
    markdown: str                 # what actually goes to Telegram
    blocks: int                   # approximate block count after normalization
    media: int                    # media blocks (local + remote)
    spaced: bool                  # False when the spacer pass was skipped/disabled
    warnings: tuple[str, ...]
    groups: tuple[MediaGroup, ...]  # index, size, preceding_text, mode
```

**Spacer** is `" "` alone on a line, surrounded by blank lines, so the server parses it as its own `PageBlockParagraph`. Measured on a real article: 103 blocks → 180 blocks with 77 spacers, rendered with visible gaps (message `407166`).

**Media reference flow (CLI)**

```
article.md ──scan_media()──▶ [remote urls | local paths]
                                   │
              resolve (article dir, --vault-dir, --rich-file)
                                   ▼
              SendMessageRequest.rich_files = (RichFile(id, path, caption, kind), …)
              rich_markdown rewritten: ![caption](<id>)      ← syntax proven by Task 5
                                   ▼
              TelethonMessageBackend: upload_file → InputPhoto/InputDocument
                                   ▼
              InputRichMessageMarkdown(markdown=…, files=[InputRichFile…])
```

**Ordering inside `send_message`**: normalise → length check → attachment/exclusivity checks → access gate → operation row. Normalising before the length check is deliberate: the caller must be told about the size that will actually be sent.

**Backward compatibility**: `rich_files` and the normalised markdown both ride the existing only-when-set `extra` block; a backend with the pre-media signature never sees a new kwarg. The legacy-signature fakes (`LegacySendBackend`, `CliLegacyMessageBackend`, `test_mcp_plain_send_omits_rich_markdown_kwarg`) stay as the contract test.

## Post-Completion

*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Manual verification** (requires an authorized session; the live e2e scripts are blocked by the broken `Client chat test` chat):

- Send the same Obsidian article (`Пхукет 2026 - рассказ.md`) with local embeds intact to a private chat and check: paragraph gaps, headings separated, images/videos inline, consecutive images rendered as a collage.
- Send with `--no-spaced-paragraphs` and confirm the markdown is unchanged byte-for-byte.
- Send an article to a chat where the account lacks media rights and confirm the error names that cause.
- ~~Run `.venv/bin/python scripts/spike_rich_media.py --file <photo.png> --entity me` as a prerequisite for Tasks 6–7~~ — **done during Task 6**; the answer is recorded under "Task 5 findings" and the shipped helpers were re-verified against it (`--only tg-scheme,tg-scheme-alt-caption`, both accepted, read back as `PageBlockPhoto`). Worth one more run after Task 7 to confirm the *uploading* path end to end.

**External system updates**:

- Re-sync `skills/telegram-assistant/SKILL.md` → `~/.claude/skills/telegram-assistant/SKILL.md` (the skill is loaded from the home copy).
- Non-Premium verification of rich sends remains open (separate `docs/TODO.md` item) and applies to media articles too.
