"""Unit tests for the pure helpers of ``scripts/spike_rich_collage_caption.py``.

The spike's network half is manual (it sends real messages), but the candidate
bodies are the answer it proved — the winning one is exactly what the shipped
grouping pass emits — and its ``PageCaption`` flattening is what read the answer
off the wire. The script lives outside the package, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

SPIKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "spike_rich_collage_caption.py"


def _load_spike():
    spec = importlib.util.spec_from_file_location("spike_rich_collage_caption", SPIKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


spike = _load_spike()


def test_candidates_cover_every_spelling_that_was_tried() -> None:
    names = [candidate.name for candidate in spike.build_candidates()]
    assert names == [
        "figcaption-block",
        "figcaption-attached",
        "figcaption-close-line",
        "plain-line",
        "caption-attribute",
        "slideshow-figcaption-block",
    ]


def test_every_candidate_references_both_uploads() -> None:
    for candidate in spike.build_candidates():
        assert f"tg://photo?id={spike.FIRST_ID}" in candidate.body
        assert f"tg://photo?id={spike.SECOND_ID}" in candidate.body


def test_the_winning_candidate_is_what_the_grouping_pass_emits() -> None:
    """The shipped pass must not drift from the spelling this spike proved."""
    from telegram_assistant.messages import normalize_rich_markdown

    winner = next(c for c in spike.build_candidates() if c.name == "figcaption-block")
    grouped = normalize_rich_markdown(
        f'![](https://x/a.jpg "{spike.CAPTION}")\n\n![](https://x/b.jpg)',
        spaced_paragraphs=False,
    ).markdown
    marker = f"<figcaption>{spike.CAPTION}</figcaption>\n\n</tg-collage>"
    assert marker in winner.body
    assert marker in grouped


def test_caption_text_flattens_nested_rich_text() -> None:
    plain = SimpleNamespace(text=SimpleNamespace(text="Подпись"))
    concat = SimpleNamespace(
        text=SimpleNamespace(
            texts=[SimpleNamespace(text="Под"), SimpleNamespace(text="пись")]
        )
    )
    assert spike._caption_text(plain) == "Подпись"
    assert spike._caption_text(concat) == "Подпись"


def test_caption_text_reports_an_empty_caption_as_none() -> None:
    # ``TextEmpty`` has no ``text``/``texts`` at all — that is what an
    # uncaptioned block comes back as, and the spike must not print it as an
    # empty string that could read like a caption.
    assert spike._caption_text(None) is None
    assert spike._caption_text(SimpleNamespace(text=SimpleNamespace())) is None


def test_generated_png_is_a_valid_one_pixel_image() -> None:
    data = spike._tiny_png(220, 40, 40)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1, 1)
    assert data.endswith(b"IEND\xaeB`\x82")
