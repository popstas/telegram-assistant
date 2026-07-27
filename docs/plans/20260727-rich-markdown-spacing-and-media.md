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

- [ ] add `spaced_paragraphs: bool = True` to `SendMessageRequest`; include it in `to_payload()` so the audit trail records what was sent
- [ ] respect config `telegram.defaults.rich_markdown_spaced_paragraphs: bool`, default: True
- [ ] in `send_message`, normalise the markdown **before** the length check, so `MAX_RICH_MARKDOWN_CHARS` bounds what actually goes to Telegram; pass the normalised text down through the existing only-when-set `extra` block
- [ ] add `warnings: tuple[str, ...] = ()` to `SendMessageResult` (defaulting empty, serialised in `to_dict()`/`from_dict()`) and carry the normalization warnings through
- [ ] keep the redaction rule in `to_payload()` applied to the **normalised** markdown
- [ ] write tests: default send is spaced; `spaced_paragraphs=False` sends byte-for-byte; a spaced article over the char limit raises `ValueError` naming the post-normalization length; warnings reach the result; the legacy-signature fake still passes
- [ ] run `pytest` and `ruff check src tests` — must pass before task 4

### Task 4: Surface the flag (CLI, HTTP, MCP)

- [ ] CLI: add `--spaced-paragraphs`, `--no-spaced-paragraphs` to `messages send` (only meaningful with `--rich-markdown`; error with exit code 2 when used without it)
- [ ] CLI dry-run: echo `spaced_paragraphs`, `rich_markdown_chars` (post-normalization), `rich_markdown_blocks`, `rich_markdown_media`, and any warnings — never the body
- [ ] CLI real run: print warnings alongside the result JSON
- [ ] HTTP: add `spaced_paragraphs: bool = True` to `MessageSendBody`, rejected in `_shape` when `rich_markdown` is absent (422, matching the existing exclusivity errors)
- [ ] MCP: add the same kwarg to the send tool, documented in its docstring; keep it out of the plain-send call path (the `test_mcp_plain_send_omits_rich_markdown_kwarg` contract)
- [ ] write tests per surface: default on, flag off, flag without `--rich-markdown` errors, dry-run markers present, MCP/HTTP legacy fakes unaffected
- [ ] run `pytest` and `ruff check src tests` — must pass before task 5

### Task 5: Spike — local media inside a rich message

- [ ] create `scripts/spike_rich_media.py` modelled on `scripts/spike_rich_message.py` (same preconditions and exit codes: 2 = precondition missing, 3 = server rejected), taking `--entity` (default `me`) and a local image/video path
- [ ] upload the file with `client.upload_file`, turn it into `InputPhoto`/`InputDocument` (via `messages.uploadMedia` on the target peer where needed) and build `InputRichFilePhoto(id=…, photo=…)` / `InputRichFileDocument(id=…, document=…)`
- [ ] try each candidate markdown reference in turn and report which the server accepts: `![](<id>)`, `![](tg://file?id=<id>)`, `![alt](<id> "caption")`, and the HTML form `<img src="<id>"/>` via `InputRichMessageHTML`
- [ ] read the sent message back and print the resulting `RichMessage` block list, so the accepted syntax is proven, not assumed
- [ ] record the findings in this plan (accepted syntax, id format, whether captions survive, whether video needs a document thumbnail); if **none** of the candidates works, mark ⚠️ here and skip tasks 6–7, continuing at task 8
- [ ] write a unit test for the pure candidate-builder helper the spike imports (list of variants, id substitution) — the network part stays manual
- [ ] run `pytest` and `ruff check src tests` — must pass before task 6

### Task 6: Resolve local media from markdown (domain + CLI)

- [ ] add `scan_media(markdown, *, base_dir, vault_dir=None)` to `rich_markdown.py`: classify each media block as remote (http/https, untouched) or local, resolve local targets relative to the markdown file's directory, and resolve Obsidian `![[name.png|caption|size]]` embeds by searching `vault_dir` (nearest match; ambiguity is an error, not a guess)
- [ ] carry `caption` from the markdown title, falling back to the alt text; drop the Obsidian `|size` suffix
- [ ] add `rich_files: tuple[RichFile, ...]` to `SendMessageRequest` (`RichFile = {id, path, caption, kind}`), validated in `send_message` (file exists, readable, ≤ 50 media, only allowed with `rich_markdown`) and recorded in `to_payload()` as metadata only (path + kind, never contents)
- [ ] rewrite the markdown so each local media block references the file id in the syntax the spike proved; unresolvable local media is an error naming the file (no silent drop)
- [ ] CLI: resolve local media by default, add repeatable `--rich-file <id>=<path>` for files outside the article directory, and `--vault-dir` for Obsidian lookups; dry-run lists the resolved files (path, kind, caption) without uploading
- [ ] write tests: relative path, absolute path, Obsidian embed, ambiguous embed error, missing file error, remote URL untouched, `--rich-file` override, >50 media rejected, `rich_files` without `rich_markdown` rejected
- [ ] run `pytest` and `ruff check src tests` — must pass before task 7

### Task 7: Upload and send media (`telethon_backend`)

- [ ] extend `TelethonMessageBackend.send_message`/`_send_rich_message` with an only-when-set `rich_files` kwarg
- [ ] import `InputRichFilePhoto`/`InputRichFileDocument` through the existing version-tolerant probe pattern; a Telethon without them raises `RichMessageUnsupported` naming the version (never `MessageSendFailed`)
- [ ] upload each file once (`client.upload_file` → `messages.uploadMedia` → `InputPhoto`/`InputDocument`), build the `files=` list in markdown order, and pass it into `InputRichMessageMarkdown`
- [ ] map a media-rights rejection (`ChatSendMediaForbiddenError` and friends) to a clear domain error naming the chat — no silent fallback to a plain or media-less send
- [ ] write tests with a fake client: files uploaded in order, ids match the markdown references, the kwarg is omitted for a media-less article (legacy fake stays green), unsupported-Telethon path, media-rights error mapping
- [ ] run `pytest` and `ruff check src tests` — must pass before task 8

### Task 8: Group consecutive media into `<tg-collage>`

- [ ] add grouping to `normalize_rich_markdown`: a run of 2+ consecutive media blocks (no text between them) is wrapped in `<tg-collage>` … `</tg-collage>` with blank lines inside, per the dialect
- [ ] respect config `telegram.defaults.rich_markdown_grouping`, default: `collage`
- [ ] accept a per-group override argument (`media_groups=[{index, mode: "collage"|"slideshow"|"none"}]`) so a caller can turn one run into `<tg-slideshow>` or leave it ungrouped; unknown indexes are an error
- [ ] expose the detected groups in the normalization result (index, media count, the trailing ~50 characters of the preceding text) so surfaces can describe them
- [ ] never group media that is already inside an author-written `<tg-collage>`/`<tg-slideshow>`/`<details>` block
- [ ] CLI: `--media-group <index>=<collage|slideshow|none>` (repeatable); dry-run lists detected groups with their preceding text and chosen mode
- [ ] write tests: two runs grouped, single media untouched, override to slideshow/none, author-written collage untouched, group info reported, grouping counted correctly against the block limit
- [ ] run `pytest` and `ruff check src tests` — must pass before task 9

### Task 9: Skill dialogue and documentation

- [ ] `skills/telegram-assistant/SKILL.md`: document that spacing is on by default and `--spaced-paragraphs`, `--no-spaced-paragraphs` turns it off; document local media (auto-resolve, `--rich-file`, `--vault-dir`) as CLI-only
- [ ] `skills/telegram-assistant/SKILL.md`: add the grouping dialogue — when the dry-run reports media groups, ask via `AskUserQuestion` whether any group should change; if yes, one question per group: «После текста `<preceding text>` как сгруппировать медиа?» with options `Collage` / `Slideshow` / `Ungrouped`, then re-run the dry-run with the chosen `--media-group` flags. A single group left as-is needs no second question
- [ ] `skills/telegram-assistant/SKILL.md`: extend the rich-send error list with the new messages (unresolvable media, >50 media, media rights, `--rich-file` parse errors)
- [ ] update `README.md` (Commands + the rich-send description + MCP tool notes) and `CLAUDE.md` (the rich-send bullet under Architecture: normalization owns spacing/grouping, `rich_files` is CLI-only, block/media limits)
- [ ] update `docs/TODO.md`: check off both rich-markdown items
- [ ] write/extend `tests/test_skill_inventory.py` expectations if the CLI catalog changed
- [ ] run `pytest` and `ruff check src tests` — must pass before task 10

### Task 10: Verify acceptance criteria

- [ ] verify every requirement from Overview is implemented (spacing default + flag, block-limit fallback with warning, local media auto-resolve + `--rich-file`, collage grouping + overrides, skill dialogue)
- [ ] verify edge cases: article with no paragraphs at all, article that is only media, media inside fenced code (must not be treated as media), U+00A0 already present, `spaced_paragraphs=False` plus media, exactly 500 blocks / exactly 50 media
- [ ] run the full test suite (`pytest`)
- [ ] run `ruff check src tests` — all issues fixed
- [ ] verify test coverage of `messages/rich_markdown.py` is at project standard (80%+)

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
- Run `.venv/bin/python scripts/spike_rich_media.py --entity me` once more after implementation to confirm the shipped syntax still matches the spike's finding.

**External system updates**:

- Re-sync `skills/telegram-assistant/SKILL.md` → `~/.claude/skills/telegram-assistant/SKILL.md` (the skill is loaded from the home copy).
- Non-Premium verification of rich sends remains open (separate `docs/TODO.md` item) and applies to media articles too.
