# Obsidian wikilink stripping in rich markdown

**Date:** 2026-08-01
**Status:** approved, ready for planning

## Problem

An Obsidian note sent as a Telegram article carries its internal
`[[wikilinks]]` verbatim. Telegram has no such syntax, so the reader sees raw
brackets and, for aliased links, the vault's canonical note name instead of the
word the author wrote.

This is not hypothetical. Message 408926 in Saved Messages — the note
`Notes/2026/07/telegram-assistant/Планёрка 29.07.2026.md`, sent 2026-08-01 —
went out carrying 19 wikilinks, among them `[[Станислав Попов|Стасу]]` and
`[[Денис Баталин|Дэн]]`. The vault holds 554 of them across its notes.

## Rule

A wikilink is `[[…]]` **not** preceded by `!`. It expands to:

- its **alias** when one is present (the text after the first `|`), otherwise
- its **target**, with `#` replaced by ` > ` and a leading `#` dropped.

| input | output |
|---|---|
| `[[Андрей Смирнов]]` | `Андрей Смирнов` |
| `[[Станислав Попов\|Стасу]]` | `Стасу` |
| `[[#Спорные моменты]]` | `Спорные моменты` |
| `[[tasks#Настроить statusline]]` | `tasks > Настроить statusline` |
| `[[Note#Heading\|Алиас]]` | `Алиас` |

That single rule is the whole specification — there are no special cases. Block
references (`[[note#^blk]]` → `note > ^blk`) fall out of it for free; the vault
contains none, so no code is written for them specifically.

Only the **first** `|` separates target from alias; any further pipes belong to
the alias, so `[[A|B|C]]` yields `B|C` — which is what Obsidian renders.

An empty half falls back to the other one: `[[Note|]]` yields `Note` and
`[[|Стас]]` yields `Стас`. A link with **both** halves empty (`[[]]`, `[[|]]`)
is not a link at all and is shipped verbatim rather than collapsed to an empty
string — silently deleting a character run the author typed is worse than
leaving a curiosity in the text.

## What the pass must not touch

- **`![[…]]` embeds.** Those are media, owned by `scan_media`, which rewrites
  them into `tg://` references. The leading `!` is what distinguishes them, so
  the pattern must require its absence.
- **Inline code spans.** An article documenting this dialect writes
  `` `[[Note]]` `` and means the characters. Mask them with the same
  `_CODE_SPAN_RE` `scan_media` already uses — do not write a second matcher, and
  do not hand-roll backtick pairing. (A naive `` `[^`]*\[\[…\]\][^`]*` `` probe
  run against the vault during design produced only false positives: it matched
  the span *between* two separate code spans.)
- **Fenced code blocks.** Opaque to the block scanner for the same reason.

## Placement

`strip_wikilinks()` in `messages/rich_markdown.py`, called as the **first** step
of `normalize_rich_markdown()`, before `scan_blocks()`.

All three surfaces get it, because `send_message` normalises once for every
surface. This differs deliberately from `strip_yaml_frontmatter()` and
`scan_media()`, which are CLI-only: those answer "this is a file from a vault", a
question only the CLI's file-read boundary can ask. A wikilink, by contrast, is
meaningless in Telegram no matter who submitted it — an MCP client relaying note
text has the same defect as a CLI reading the note directly.

Running before `scan_blocks()` is what makes the block count honest: this pass
edits *inside* lines, so blocks must be counted against the text that will
actually be sent. It also fixes a latent bug for free — `| [[A|B]] |` in a table
currently breaks the cell on the wikilink's own pipe, and after the pass it does
not.

## Invariants

- **Identity on no-op.** Returns the input string by identity when it changes
  nothing, so CRLF and the trailing newline survive byte-for-byte, exactly as
  the other passes promise.
- **Idempotent.** No `[[` survives the pass, so re-running it is a no-op —
  unlike `_split_paragraph_lines`, which deliberately gives up idempotency.
- **Usually shrinks, but can grow.** Removing `[[`/`]]`/an alias pipe is -4
  or more, but each `#` in a bare target becomes `" > "` (+2 net per `#`), so
  a target with three or more `#` grows the source overall. The
  `MAX_RICH_MARKDOWN_CHARS` pre-check that runs *before* normalisation is
  unaffected and stays where it is: it guards the event loop ahead of the
  WRITE gate, and that reason is independent of which direction a pass moves
  the length — it is deliberately conservative rather than exact. A
  33k-character source dense with wikilinks would be rejected before the pass
  could bring it under the limit; that is accepted, not an oversight. The
  post-normalisation over-limit check names `"wikilink expansion"` in
  `grew_by` alongside the other passes, so growth caused by this pass is
  never blamed on — or hidden behind — paragraph spacing/line splitting/media
  grouping.

## Reporting

`rich_markdown_wikilinks: <count>` in the dry-run payload — how many links the
pass expanded. No CLI flag and no config knob: literal `[[…]]` in a Telegram
article is always a defect, so there is nothing to switch off.

## Testing

Unit tests in the existing rich-markdown test module:

- each row of the rule table above
- `![[file.png]]` left untouched, and the interaction with `scan_media` (embeds
  already rewritten to `![](tg://…)` by the time the CLI normalises)
- a wikilink inside an inline code span and inside a fenced block, both left
  verbatim
- a wikilink whose caption contains a code span (overlap, not containment — the
  case `scan_media` documents as the one silent drop it must never make)
- identity return on markdown with no wikilinks, including CRLF input
- idempotency: normalising the output again changes nothing
- `| [[A|B]] |` in a table yields a well-formed single cell
- HTTP and MCP sends strip too — the surface-level proof that this is not
  CLI-only

## Documentation debt

`CLAUDE.md` currently states that `messages/rich_markdown.py` "owns every
rewrite of an article, and it only ever inserts or wraps lines." This pass is
the first exception — it edits within a line. That sentence must be rewritten in
the same change, or the most load-bearing paragraph in the file starts lying.

`README.md` and `skills/telegram-assistant/SKILL.md` need the behaviour noted
alongside the other normalization passes, and the skill re-synced to
`~/.claude/skills/telegram-assistant/SKILL.md`.
