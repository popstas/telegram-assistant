# Obsidian Wikilink Stripping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand Obsidian `[[wikilinks]]` to plain text in every rich-markdown article, on all three surfaces, so a vault note sent to Telegram no longer shows raw brackets and canonical note names.

**Architecture:** One new pure pass, `strip_wikilinks()`, in `messages/rich_markdown.py`, called as the first step of `normalize_rich_markdown()`. It reuses the module's existing `scan_blocks()` to find code blocks and `_CODE_SPAN_RE` to mask inline code, then rewrites each surviving `[[…]]` in place. The count it reports rides on `RichMarkdownNormalization` next to the existing per-pass flags and surfaces in the CLI dry run.

**Tech Stack:** Python 3.12, `re`, pytest. No new dependencies.

## Global Constraints

- **The rule is one rule, no special cases:** a wikilink expands to its alias when one is present, otherwise to its target with `#` replaced by ` > ` and a leading `#` dropped.
- **Only the first `|` separates** target from alias; further pipes belong to the alias (`[[A|B|C]]` → `B|C`).
- **An empty half falls back to the other:** `[[Note|]]` → `Note`, `[[|Стас]]` → `Стас`. Both halves empty (`[[]]`, `[[|]]`) is not a link — ship it verbatim, never collapse it to an empty string.
- **`![[…]]` embeds are never touched.** They are media, owned by `scan_media`. The absent `!` is the discriminator.
- **Inline code spans and fenced code blocks are never touched.** Reuse the existing `_CODE_SPAN_RE`; do not write a second matcher and do not hand-roll backtick pairing.
- **Code-span masking tests containment, not overlap** — the same rule `iter_line_media_refs` uses (`span_start <= start and end <= span_end`).
- **Identity on no-op.** Every pass in this module returns its input string by identity when it changes nothing; this one must too, or CRLF and the trailing newline stop surviving byte-for-byte.
- **No knob.** No CLI flag, no config key. Literal `[[…]]` in a Telegram article is always a defect.
- **No Telegram traffic in tests.** Every test uses in-memory fakes.
- Lint: `ruff check src tests` (line-length 100, py312).

---

### Task 1: The `strip_wikilinks()` pass

**Files:**
- Modify: `src/telegram_assistant/messages/rich_markdown.py` (add the pattern near `_CODE_SPAN_RE` at line 168; add the functions after `strip_yaml_frontmatter`, which ends around line 417)
- Test: `tests/test_rich_markdown_wikilinks.py` (create)

**Interfaces:**
- Consumes: `scan_blocks(markdown) -> tuple[Block, ...]`, `split_lines(markdown) -> list[str]`, `_CODE_SPAN_RE`, and `Block` (fields `kind: str`, `start: int`, `end: int`, `children: tuple[Block, ...]`; `start`/`end` are a half-open range into the *document's* normalised line list, children included).
- Produces: `strip_wikilinks(markdown: str) -> tuple[str, int]` — the rewritten article and the number of links expanded. Returns `(markdown, 0)` **by identity** when nothing changed. Task 2 calls this.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rich_markdown_wikilinks.py`:

```python
"""The Obsidian wikilink pass: [[Target|Alias]] becomes plain text.

Telegram has no wikilink syntax, so an unexpanded link reaches the reader as
literal brackets plus the vault's canonical note name instead of the word the
author wrote.
"""

import pytest

from telegram_assistant.messages.rich_markdown import strip_wikilinks


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("[[Андрей Смирнов]]", "Андрей Смирнов"),
        ("[[Станислав Попов|Стасу]]", "Стасу"),
        ("[[#Спорные моменты]]", "Спорные моменты"),
        ("[[tasks#Настроить statusline]]", "tasks > Настроить statusline"),
        ("[[Note#Heading|Алиас]]", "Алиас"),
        # Only the first pipe separates; the rest belong to the alias.
        ("[[A|B|C]]", "B|C"),
        # An empty half falls back to the other one.
        ("[[Note|]]", "Note"),
        ("[[|Стас]]", "Стас"),
        # A block reference falls out of the same rule with no special case.
        ("[[note#^blk]]", "note > ^blk"),
        # Surrounding prose is untouched, and several links share one line.
        (
            "Отдал [[Денис Баталин|Дэну]] и [[Ирина Шлыкова|Ирине]].",
            "Отдал Дэну и Ирине.",
        ),
    ],
)
def test_expands_wikilinks(source: str, expected: str) -> None:
    assert strip_wikilinks(source) == (expected, source.count("[["))


@pytest.mark.parametrize("source", ["[[]]", "[[|]]", "[[#]]"])
def test_degenerate_link_is_left_verbatim(source: str) -> None:
    """No target and no alias is not a link.

    Collapsing it to an empty string would silently delete characters the
    author typed — worse than leaving a curiosity in the text.
    """
    assert strip_wikilinks(source) == (source, 0)


def test_media_embed_is_left_to_scan_media() -> None:
    """``![[…]]`` is media; ``scan_media`` rewrites it into a ``tg://`` ref."""
    source = "![[Pasted image 1.png|Закат]]"
    assert strip_wikilinks(source) == (source, 0)


def test_embed_and_link_on_one_line() -> None:
    source = "![[shot.png]] обсудили с [[Ольга Цветцых]]"
    assert strip_wikilinks(source) == ("![[shot.png]] обсудили с Ольга Цветцых", 1)


def test_inline_code_span_is_opaque() -> None:
    """An article documenting this dialect writes `[[Note]]` and means the text."""
    source = "Пиши `[[Note]]`, получишь Note."
    assert strip_wikilinks(source) == (source, 0)


def test_code_span_overlapping_a_link_does_not_shield_it() -> None:
    """Containment, not overlap — the rule ``iter_line_media_refs`` uses.

    The code span here starts inside the link and ends outside it. Skipping on
    overlap would ship the raw brackets.
    """
    source = "[[Note|запусти `make]] сначала`"
    text, count = strip_wikilinks(source)
    assert count == 1
    assert "[[" not in text


def test_fenced_code_block_is_opaque() -> None:
    source = "```\n[[Note]]\n```\n"
    assert strip_wikilinks(source) == (source, 0)


def test_fenced_code_inside_a_quote_is_opaque() -> None:
    source = "> ```\n> [[Note]]\n> ```\n"
    assert strip_wikilinks(source) == (source, 0)


def test_link_inside_a_quote_is_expanded() -> None:
    source = "> Сказал [[Андрей Смирнов|Андрей]]\n"
    assert strip_wikilinks(source) == ("> Сказал Андрей\n", 1)


def test_table_cell_pipe_no_longer_breaks_the_row() -> None:
    """``[[A|B]]`` in a table currently splits the cell on its own pipe."""
    source = "| [[Станислав Попов|Стас]] | да |\n"
    assert strip_wikilinks(source) == ("| Стас | да |\n", 1)


def test_returns_input_by_identity_when_unchanged() -> None:
    """Identity is what keeps CRLF and the trailing newline byte-for-byte."""
    source = "# Заголовок\r\n\r\nПростой текст.\r\n"
    text, count = strip_wikilinks(source)
    assert text is source
    assert count == 0


def test_trailing_newline_survives_a_rewrite() -> None:
    source = "Спросил у [[Ольга Андрющенко|Оли]].\n"
    assert strip_wikilinks(source) == ("Спросил у Оли.\n", 1)


def test_is_idempotent() -> None:
    source = "Отдал [[Денис Баталин|Дэну]].\n"
    once, first = strip_wikilinks(source)
    twice, second = strip_wikilinks(once)
    assert first == 1
    assert second == 0
    assert twice is once
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rich_markdown_wikilinks.py -v`
Expected: FAIL at collection — `ImportError: cannot import name 'strip_wikilinks'`.

- [ ] **Step 3: Add the pattern**

In `src/telegram_assistant/messages/rich_markdown.py`, directly below `_CODE_SPAN_RE` (line 168) and its comment block, add:

```python
#: An Obsidian wikilink: ``[[target]]`` or ``[[target|alias]]``. The negative
#: lookbehind is what keeps ``![[file.png]]`` out — that is a media embed, owned
#: by :func:`scan_media`, and expanding it here would strip the file reference
#: down to prose before anything could upload it.
_WIKILINK_RE = re.compile(r"(?<!!)\[\[(?P<body>[^\[\]]*)\]\]")
```

- [ ] **Step 4: Write the implementation**

Add after `strip_yaml_frontmatter` (which ends around line 417), before `split_lines`:

```python
def _expand_wikilink(body: str) -> str | None:
    """Return the text a wikilink's body should become, or ``None`` to keep it.

    One rule, no special cases: the alias wins when there is one, otherwise the
    target reads as Obsidian renders it — ``#`` becomes ``>`` and a leading
    ``#`` (a link into the current note) simply drops. Block references
    (``note#^blk``) fall out of that unchanged, which is why they need no
    branch of their own.
    """

    target, _, alias = body.partition("|")
    alias = alias.strip()
    if alias:
        return alias
    target = target.strip()
    if target.startswith("#"):
        target = target[1:]
    if not target:
        # Neither half carries text, so this is not a link. Returning None
        # ships it verbatim: silently deleting characters the author typed is
        # worse than leaving a curiosity in the article.
        return None
    return target.replace("#", " > ")


def _strip_line_wikilinks(line: str) -> tuple[str, int]:
    """Expand every wikilink on one line, leaving inline code spans alone."""

    code_spans = [match.span() for match in _CODE_SPAN_RE.finditer(line)]
    out: list[str] = []
    cursor = 0
    count = 0
    for match in _WIKILINK_RE.finditer(line):
        start, end = match.span()
        # Containment, not overlap — the rule iter_line_media_refs uses. A span
        # that merely overlaps the link (a backtick inside the alias) must not
        # shield it, or the raw brackets ship.
        if any(span_start <= start and end <= span_end for span_start, span_end in code_spans):
            continue
        text = _expand_wikilink(match.group("body"))
        if text is None:
            continue
        out.append(line[cursor:start])
        out.append(text)
        cursor = end
        count += 1
    if not count:
        return line, 0
    out.append(line[cursor:])
    return "".join(out), count


def _code_line_indices(blocks: tuple[Block, ...] | list[Block]) -> set[int]:
    """Document line indices covered by a code block, nesting included."""

    found: set[int] = set()
    for block in blocks:
        if block.kind == "code":
            found.update(range(block.start, block.end))
        elif block.children:
            found.update(_code_line_indices(block.children))
    return found


def strip_wikilinks(markdown: str) -> tuple[str, int]:
    """Expand Obsidian ``[[wikilinks]]`` to plain text; report how many.

    Telegram has no wikilink syntax, so an unexpanded link reaches the reader
    as literal brackets around the vault's canonical note name — not the word
    the author wrote. Unlike :func:`strip_yaml_frontmatter` and
    :func:`scan_media`, which answer "this is a file from a vault" and are
    therefore CLI-only, a wikilink is meaningless in Telegram no matter which
    surface submitted it, so this runs for all three.

    Code is opaque: fenced blocks via :func:`scan_blocks`, inline spans via
    :data:`_CODE_SPAN_RE`. An article documenting this dialect writes
    ``[[Note]]`` inside backticks and means the characters.

    Returns ``(markdown, 0)`` by identity when nothing changed, which is what
    keeps CRLF and the trailing newline intact for a byte-for-byte send.
    """

    if "[[" not in markdown:
        return markdown, 0

    code_lines = _code_line_indices(scan_blocks(markdown))
    lines = split_lines(markdown)
    out: list[str] = []
    total = 0
    for index, line in enumerate(lines):
        if index in code_lines or "[[" not in line:
            out.append(line)
            continue
        rewritten, count = _strip_line_wikilinks(line)
        out.append(rewritten)
        total += count
    if not total:
        return markdown, 0
    text = "\n".join(out)
    return (text + "\n" if markdown.endswith(("\n", "\r")) else text), total
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rich_markdown_wikilinks.py -v`
Expected: PASS, all tests, no warnings.

- [ ] **Step 6: Run the full suite and lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: the whole suite green (no existing test asserts that `[[…]]` survives; if one does, it predates this feature — report it rather than editing it silently), ruff clean.

- [ ] **Step 7: Commit**

```bash
git add src/telegram_assistant/messages/rich_markdown.py tests/test_rich_markdown_wikilinks.py
git commit -m "feat(rich-markdown): expand Obsidian wikilinks to plain text"
```

---

### Task 2: Wire the pass into normalization

**Files:**
- Modify: `src/telegram_assistant/messages/rich_markdown.py` — `RichMarkdownNormalization` (line 254) and `normalize_rich_markdown` (line 650, body starts line 694)
- Test: `tests/test_rich_markdown_normalize.py` (extend), `tests/test_messages_rich_surfaces.py` (extend), `tests/test_mcp_tools.py` (extend)

**Interfaces:**
- Consumes: `strip_wikilinks(markdown: str) -> tuple[str, int]` from Task 1.
- Produces: `RichMarkdownNormalization.wikilinks: int` — the number of links expanded, defaulting to `0`. Task 3 reads it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rich_markdown_normalize.py`:

```python
def test_normalize_expands_wikilinks_and_counts_them() -> None:
    result = normalize_rich_markdown("Отдал [[Денис Баталин|Дэну]].\n")
    assert "[[" not in result.markdown
    assert "Дэну" in result.markdown
    assert result.wikilinks == 1


def test_normalize_reports_zero_wikilinks_when_there_are_none() -> None:
    assert normalize_rich_markdown("Просто текст.\n").wikilinks == 0


def test_wikilinks_are_expanded_before_blocks_are_counted() -> None:
    """The pass edits inside lines, so blocks must be counted after it.

    A wikilink's own pipe splits a table cell; expanding it first is what makes
    the reported block structure the one Telegram actually receives.
    """
    source = "| a | b |\n| --- | --- |\n| [[Станислав Попов|Стас]] | да |\n"
    result = normalize_rich_markdown(source)
    assert "| Стас | да |" in result.markdown
```

Append to `tests/test_messages_rich_surfaces.py`, beside `test_http_rich_send_passes_markdown_to_backend` (line 135), reusing that module's `RecordingMessageBackend`, `_client` and `AUTH`:

```python
def test_http_rich_send_expands_wikilinks() -> None:
    """The pass is not CLI-only: an agent relaying note text has the same defect."""
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": "Спросил у [[Станислав Попов|Стаса]].\n",
            "operation_id": "rich-http-wikilink",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    sent = backend.sent[0]["rich_markdown"]
    assert "Спросил у Стаса." in sent
    assert "[[" not in sent
```

Append to `tests/test_mcp_tools.py`, beside `test_mcp_send_rich_markdown_reaches_backend` (line 692), reusing that module's `FakeRichMessageBackend`, `_client`, `_mint_token`, `_initialize` and `_call_tool`:

```python
def test_mcp_send_rich_markdown_expands_wikilinks(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """Same proof for the MCP tool surface — expansion is surface-independent."""
    backend = FakeRichMessageBackend()
    with _client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "rich_markdown": "Отдал [[Денис Баталин|Дэну]].\n",
                "operation_id": "mcp-rich-wikilink",
            },
        )

    assert result["isError"] is False, result
    sent = backend.sent[0]["rich_markdown"]
    assert "Отдал Дэну." in sent
    assert "[[" not in sent
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rich_markdown_normalize.py -k wikilink -v`
Expected: FAIL with `AttributeError: 'RichMarkdownNormalization' object has no attribute 'wikilinks'`.

- [ ] **Step 3: Add the field**

In `RichMarkdownNormalization` (line 254), after `lines_split: bool = False`:

```python
    #: How many Obsidian wikilinks :func:`strip_wikilinks` expanded. Unlike the
    #: flags above this is a count, because it is what a surface reports to the
    #: operator — there is no knob to explain, only a number.
    wikilinks: int = 0
```

- [ ] **Step 4: Call the pass first**

In `normalize_rich_markdown`, replace the opening two lines of the body (line 694-695):

```python
    blocks = scan_blocks(markdown)
    warnings: list[str] = []
```

with:

```python
    # First, and before scan_blocks: this pass edits *inside* lines, so every
    # later pass — and the block count the 500-block rollback weighs — must see
    # the text that will actually be sent. It also settles a table cell whose
    # wikilink pipe would otherwise split it.
    markdown, wikilinks = strip_wikilinks(markdown)
    blocks = scan_blocks(markdown)
    warnings: list[str] = []
```

Then in the `return RichMarkdownNormalization(...)` at the end of the function, add the argument:

```python
        wikilinks=wikilinks,
```

Note that `grouped is not markdown` and the other identity comparisons in the function keep working: they compare against the rebound local `markdown`, which is exactly what the later passes received.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rich_markdown_normalize.py tests/test_messages_rich_surfaces.py tests/test_mcp_tools.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite and lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: green, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add src/telegram_assistant/messages/rich_markdown.py tests/
git commit -m "feat(rich-markdown): expand wikilinks on every surface"
```

---

### Task 3: Dry-run marker and documentation

**Files:**
- Modify: `src/telegram_assistant/cli/rich_send.py` — `rich_dry_run_markers` (line 249)
- Modify: `CLAUDE.md`, `README.md`, `skills/telegram-assistant/SKILL.md`
- Test: `tests/test_messages_rich_spacing_flag.py` — despite its name it is the de-facto owner of the CLI dry-run marker assertions (`rich_markdown_blocks`, `rich_markdown_media`, and the plain-send `None` shape), and it already has the `_cli_setup(tmp_path, monkeypatch, markdown=…)` harness. Reuse it rather than standing up a second CLI harness.

**Interfaces:**
- Consumes: `RichMarkdownNormalization.wikilinks` from Task 2.
- Produces: the dry-run key `rich_markdown_wikilinks: int | None` (`None` for a plain send, matching every other `rich_*` marker).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_messages_rich_spacing_flag.py`, after `test_cli_dry_run_plain_send_has_no_spacing_markers` (line 308):

```python
def test_cli_dry_run_reports_expanded_wikilinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, markdown="Отдал [[Денис Баталин|Дэну]].\n"
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    resolved = json.loads(result.stdout.strip().splitlines()[-1])["resolved"]
    assert resolved["rich_markdown_wikilinks"] == 1
    assert backend.sent == []


def test_cli_dry_run_plain_send_has_no_wikilink_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker shape must never depend on the mode."""
    config_file, _md_file, _backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    resolved = json.loads(result.stdout.strip().splitlines()[-1])["resolved"]
    assert resolved["rich_markdown_wikilinks"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest -k "wikilink and dry_run" -v`
Expected: FAIL with `KeyError: 'rich_markdown_wikilinks'`.

- [ ] **Step 3: Add the marker**

In `rich_dry_run_markers` (`src/telegram_assistant/cli/rich_send.py`), inside the `markers` dict, directly after the `"rich_markdown_media"` entry:

```python
        "rich_markdown_wikilinks": (
            normalization.wikilinks if normalization is not None else None
        ),
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest -k "wikilink" -v`
Expected: PASS.

- [ ] **Step 5: Pay the documentation debt in CLAUDE.md**

`CLAUDE.md` currently opens the rich-markdown bullet with:

> **`messages/rich_markdown.py` owns every rewrite of an article, and it only ever inserts or wraps lines.**

That sentence is now false — this pass edits within a line. Rewrite the claim so it stays true and still explains *why* the module has no markdown parser, then document the pass itself: the rule and its table, that `![[…]]` and code (fenced and inline, containment not overlap) are excluded, that it runs first and before `scan_blocks` so the block count matches what is sent, that it is idempotent and identity-on-no-op, that it has no knob because a literal `[[…]]` is always a defect, and that — unlike `strip_yaml_frontmatter` and `scan_media` — it is **not** CLI-only, with the reason (a wikilink is meaningless in Telegram whoever submitted it).

- [ ] **Step 6: Update README.md and the skill**

In `README.md`, note the pass alongside the other normalization passes in the rich-markdown section. In `skills/telegram-assistant/SKILL.md`, add it to the `messages send` rich-markdown description and add `rich_markdown_wikilinks` to the listed dry-run markers. Then re-sync:

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
```

- [ ] **Step 7: Run the full suite and lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: green (including `tests/test_skill_inventory.py`, which fails when the CLI catalog drifts from the skill), ruff clean.

- [ ] **Step 8: Commit**

```bash
git add src/telegram_assistant/cli/rich_send.py tests/ CLAUDE.md README.md skills/telegram-assistant/SKILL.md
git commit -m "docs(rich-markdown): report and document wikilink expansion"
```

---

## Manual verification (optional, requires the human)

The live check is one send of a real vault note to Saved Messages. It is a
mutating live call, so **ask first — every time**; never run it on your own
initiative. The note
`/home/popstas/projects/text/obsidian/home/Notes/2026/07/telegram-assistant/Планёрка 29.07.2026.md`
holds 19 wikilinks and is the article whose earlier send (message 408926)
motivated this work; a `--dry-run` against it needs no permission and should
report `rich_markdown_wikilinks: 19`.
