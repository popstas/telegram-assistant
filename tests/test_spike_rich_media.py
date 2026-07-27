"""Unit tests for the pure helpers of ``scripts/spike_rich_media.py``.

The spike's network half is manual (it sends real messages), but the candidate
list, the id substitution and the photo/document split are what the shipped
media path reuses, so they are pinned here. The script lives outside the
package, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SPIKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "spike_rich_media.py"


def _load_spike():
    spec = importlib.util.spec_from_file_location("spike_rich_media", SPIKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


spike = _load_spike()


def test_build_candidates_puts_the_proven_syntax_first() -> None:
    names = [candidate.name for candidate in spike.build_candidates("photo1")]
    assert names == [
        "tg-scheme",
        "tg-scheme-alt-caption",
        "bare-id",
        "tg-file-url",
        "attach-scheme",
        "alt-and-caption",
        "html-img",
    ]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("photo", "![](tg://photo?id=photo1)"),
        ("video", "![](tg://video?id=photo1)"),
        ("audio", "![](tg://audio?id=photo1)"),
    ],
)
def test_proven_candidate_scheme_follows_the_upload_kind(
    kind: str, expected: str
) -> None:
    """A photo named through tg://video fails RICH_MESSAGE_VIDEO_INVALID."""
    (candidate,) = [
        c for c in spike.build_candidates("photo1", kind=kind) if c.name == "tg-scheme"
    ]
    assert candidate.syntax == expected


def test_build_candidates_substitutes_the_id_everywhere() -> None:
    candidates = spike.build_candidates("my-file_2")
    for candidate in candidates:
        assert "my-file_2" in candidate.syntax, candidate.name
        # the reference must appear in the article itself, not just be described
        assert candidate.syntax in candidate.body, candidate.name


def test_build_candidates_names_are_unique_and_kinds_are_known() -> None:
    candidates = spike.build_candidates("photo1")
    names = [candidate.name for candidate in candidates]
    assert len(set(names)) == len(names)
    assert {candidate.kind for candidate in candidates} == {"markdown", "html"}
    # exactly one HTML probe, sent through InputRichMessageHTML
    html = [candidate for candidate in candidates if candidate.kind == "html"]
    assert [candidate.name for candidate in html] == ["html-img"]


def test_each_candidate_body_names_itself() -> None:
    """A read-back (or a scroll through Saved Messages) must identify the syntax."""
    for candidate in spike.build_candidates("photo1"):
        assert f"rich media spike: {candidate.name}" in candidate.body


def test_alt_and_caption_candidate_carries_both() -> None:
    candidates = spike.build_candidates("photo1", alt="ALT", caption="CAP")
    (candidate,) = [c for c in candidates if c.name == "alt-and-caption"]
    assert candidate.syntax == '![ALT](photo1 "CAP")'


def test_markdown_candidates_put_the_media_on_its_own_line() -> None:
    """Media is a separate block in the dialect — never inline with prose."""
    for candidate in spike.build_candidates("photo1"):
        if candidate.kind != "markdown":
            continue
        lines = candidate.body.splitlines()
        assert candidate.syntax in lines, candidate.name
        index = lines.index(candidate.syntax)
        assert lines[index - 1] == ""
        assert lines[index + 1] == ""


def test_html_candidate_escapes_the_described_reference() -> None:
    (candidate,) = [c for c in spike.build_candidates("photo1") if c.kind == "html"]
    assert candidate.syntax == '<img src="photo1"/>'
    # the prose copy is entity-escaped so it renders as text, not as a second image
    assert "&lt;img src=&quot;photo1&quot;/&gt;" in candidate.body


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("photo.png", "photo"),
        ("PHOTO.PNG", "photo"),
        ("shot.jpg", "photo"),
        ("shot.jpeg", "photo"),
        ("sticker.webp", "photo"),
        ("clip.mp4", "document"),
        ("voice.ogg", "document"),
        ("track.mp3", "document"),
        ("anim.gif", "document"),
        ("README", "document"),
    ],
)
def test_classify_file(name: str, expected: str) -> None:
    assert spike.classify_file(Path("/tmp") / name) == expected
    assert spike.classify_file(f"/tmp/{name}") == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("photo.png", "photo"),
        ("Pasted image 20260727.png", "Pasted-image-20260727"),
        ("отчёт.png", "file"),
        ("a(b)[c].png", "a-b-c"),
        ("---.png", "file"),
        ("my_file-2.mp4", "my_file-2"),
        # A dot in the id is RICH_MESSAGE_FILE_ID_INVALID, so the stem is used.
        ("archive.tar.png", "archive-tar"),
    ],
)
def test_default_file_id(name: str, expected: str) -> None:
    assert spike.default_file_id(Path("/tmp") / name) == expected


def test_default_file_id_output_is_safe_inside_a_markdown_reference() -> None:
    from telegram_assistant.messages.rich_markdown import RICH_FILE_ID_RE

    file_id = spike.default_file_id("some file (1) [copy].png")
    assert not set(file_id) & set(" ()[]\"'")
    # The server's own grammar, not just "no brackets".
    assert RICH_FILE_ID_RE.match(file_id)
    assert f"![](tg://photo?id={file_id})" in spike.build_candidates(file_id)[0].body
