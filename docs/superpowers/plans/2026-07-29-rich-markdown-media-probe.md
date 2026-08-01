# rich_markdown Media Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill Telegram media attributes ourselves from `ffprobe`/`ffmpeg` instead of relying on Telethon's metadata inference and the server's partial re-parse, so large videos stop rendering as empty rectangles and an animated `.gif` actually attaches.

**Architecture:** A new pure module `messages/media_probe.py` wraps `ffprobe`/`ffmpeg` subprocess calls and returns plain data (`MediaProbe`) or `None`. `TelethonMessageBackend._upload_rich_files` probes each non-photo file once (off the event loop via `asyncio.to_thread`), passes the result into `_document_attributes()` — which becomes the single place video/audio/animation attributes are built — attaches a generated thumbnail for videos, and uploads a converted mp4 in place of a `.gif`. A `.gif` with no `ffmpeg` on PATH is rejected in the domain layer before the operation row is opened.

**Tech Stack:** Python 3.12, Telethon >= 1.44, structlog (`observability.logging.get_logger`), external `ffprobe`/`ffmpeg` binaries (optional, not pip), pytest + pytest-asyncio (asyncio mode auto), ruff.

## Global Constraints

- Use the project venv: `source .venv/bin/activate` (or call `.venv/bin/pytest` / `.venv/bin/ruff` directly).
- Lint must pass: `ruff check src tests` (line-length 100, py312, ignores E501).
- **No Telegram traffic in tests.** Every test uses in-memory fakes. Do **not** run `scripts/e2e_*.sh` or any `scripts/spike_rich_*.py` — the account has already been blocked once for suspicious activity.
- **No subprocess call may run on the event loop.** `probe_media`, `extract_thumbnail` and `convert_gif_to_mp4` are synchronous and block for up to 20/30/120 seconds; every call from `TelethonMessageBackend` goes through `await asyncio.to_thread(...)`. `ffmpeg_available()` / `ffprobe_available()` are `shutil.which` lookups and may be called directly.
- **`media_probe.py` must not import telethon.** It returns plain dataclasses; the backend turns them into `DocumentAttribute*` objects. This is what lets the domain layer (`messages/service.py`) import `ffmpeg_available` without pulling Telethon in.
- **A failed probe is never an error.** `probe_media` and `extract_thumbnail` return `None` for every failure mode (binary missing, non-zero exit, malformed JSON, timeout, no matching stream). The send continues with the pre-existing stub attributes plus a warning. The single exception is `convert_gif_to_mp4`, which raises `MediaConversionError` — a `ValueError` subclass, so it lands on the surfaces' existing 400 / exit-2 path rather than an empty 500.
- **Never `shell=True`.** Every subprocess call passes an argv list.
- The rich-markdown body is **not** rewritten by any of this. `media_kind()`, `scan_media()` and the `tg://video?id=…` reference for a `.gif` stay exactly as they are — conversion is purely about the upload's shape.
- Existing behaviour that must survive: `tg://audio` only resolves when the document carries a `DocumentAttributeAudio`, and a `.gif` document must carry `DocumentAttributeAnimated`. Both are proven wire facts (see CLAUDE.md).
- Spec: `docs/superpowers/specs/2026-07-29-rich-markdown-media-probe-design.md`.

---

### Task 1: `probe_media()` — read real media metadata with ffprobe

**Files:**
- Create: `src/telegram_assistant/messages/media_probe.py`
- Test: `tests/test_media_probe.py` (new file)

**Interfaces:**
- Consumes: nothing.
- Produces: `MediaProbe(duration: float, width: int | None, height: int | None, has_video: bool, has_audio: bool)` (frozen dataclass), `probe_media(path: Path | str) -> MediaProbe | None`, `ffprobe_available() -> bool`, `ffmpeg_available() -> bool`, `PROBE_TIMEOUT_SECONDS = 20`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_media_probe.py`:

```python
"""Tests for the ffprobe/ffmpeg wrappers behind rich-message media uploads."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.messages import media_probe
from telegram_assistant.messages.media_probe import (
    MediaProbe,
    ffmpeg_available,
    ffprobe_available,
    probe_media,
)


@pytest.fixture(autouse=True)
def _clear_which_cache() -> Any:
    """``ffprobe_available``/``ffmpeg_available`` are process-cached; a test
    that fakes PATH must not leak its answer into the next one."""
    ffprobe_available.cache_clear()
    ffmpeg_available.cache_clear()
    yield
    ffprobe_available.cache_clear()
    ffmpeg_available.cache_clear()


FFPROBE_JSON = json.dumps(
    {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 854,
                "height": 480,
                "disposition": {"attached_pic": 0},
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "73.681000"},
    }
).encode()


def _fake_run(stdout: bytes, returncode: int = 0) -> Any:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, returncode, stdout, b"")

    return run


def test_probe_media_reads_duration_and_dimensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run(FFPROBE_JSON))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    probe = probe_media(clip)

    assert probe == MediaProbe(
        duration=73.681, width=854, height=480, has_video=True, has_audio=True
    )


def test_probe_media_ignores_cover_art_as_a_video_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ``.mp3`` with embedded artwork reports an ``mjpeg`` video stream. Its
    1400x1400 artwork is not the media's dimensions, and the file is not a
    video — taking either would send an audio document shaped like a video."""
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 1400,
                    "height": 1400,
                    "disposition": {"attached_pic": 1},
                },
                {"codec_type": "audio", "codec_name": "mp3"},
            ],
            "format": {"duration": "184.5"},
        }
    ).encode()
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run(payload))
    song = tmp_path / "song.mp3"
    song.write_bytes(b"\x00")

    probe = probe_media(song)

    assert probe is not None
    assert probe.has_video is False
    assert probe.width is None
    assert probe.height is None
    assert probe.duration == 184.5


def test_probe_media_returns_none_without_ffprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: None)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    assert probe_media(clip) is None


@pytest.mark.parametrize(
    "stdout,returncode",
    [(b"", 1), (b"not json", 0), (b"[]", 0)],
    ids=["nonzero-exit", "malformed-json", "not-an-object"],
)
def test_probe_media_returns_none_on_a_bad_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: bytes, returncode: int
) -> None:
    """A failed probe is a fallback, never an error: the send still goes out
    with the stub attributes."""
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run(stdout, returncode))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    assert probe_media(clip) is None


def test_probe_media_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, media_probe.PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    assert probe_media(clip) is None


def test_probe_media_never_uses_a_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, FFPROBE_JSON, b"")

    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    clip = tmp_path / "a file.mp4"
    clip.write_bytes(b"\x00")

    probe_media(clip)

    assert isinstance(seen["argv"], list)
    assert seen["argv"][0] == "ffprobe"
    assert str(clip) in seen["argv"]
    assert seen["kwargs"].get("shell") in (None, False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_media_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegram_assistant.messages.media_probe'`

- [ ] **Step 3: Write the implementation**

Create `src/telegram_assistant/messages/media_probe.py`:

```python
"""Media metadata for rich-message uploads, read with ``ffprobe``/``ffmpeg``.

Telethon's ``utils.get_attributes()`` needs a metadata library (``hachoir``) to
read a media file. Without one it returns a stub
``DocumentAttributeVideo(duration=0, w=1, h=1, supports_streaming=False)`` for
*every* mp4 and no ``DocumentAttributeAudio`` at all. Telegram's server repairs
the metadata by re-parsing the upload, but only for smaller files — measured
live on 2026-07-29, seven videos up to 6.30 MB came back with real duration,
dimensions and thumbnails while three from 12.72 MB up kept ``duration=0,
w=1, h=1, thumbs=None`` and rendered as an empty rectangle in the clients. The
threshold is undocumented, so nothing here keys off file size: we fill the
attributes ourselves for every file.

The module is deliberately free of Telethon imports — it returns plain data and
the backend turns it into ``DocumentAttribute*`` objects. That is what lets the
domain layer import :func:`ffmpeg_available` for its pre-flight check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PROBE_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class MediaProbe:
    """What ``ffprobe`` could tell us about one media file.

    ``duration`` is seconds and ``0.0`` when it could not be determined;
    ``width``/``height`` are ``None`` unless a real (non-cover-art) video stream
    reported them.
    """

    duration: float
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool


@lru_cache(maxsize=None)
def ffprobe_available() -> bool:
    """True when ``ffprobe`` is on PATH. Cached: PATH does not move at runtime."""
    return shutil.which("ffprobe") is not None


@lru_cache(maxsize=None)
def ffmpeg_available() -> bool:
    """True when ``ffmpeg`` is on PATH. Cached: PATH does not move at runtime."""
    return shutil.which("ffmpeg") is not None


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes] | None:
    """Run *argv* without a shell, returning ``None`` for any failure to run.

    A missing binary, a permission problem and a timeout are all the same
    answer to the caller: no metadata, fall back.
    """
    try:
        return subprocess.run(  # noqa: S603 - argv list, never a shell string
            argv, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(*values: Any) -> float:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def _is_cover_art(stream: dict[str, Any]) -> bool:
    """True for an embedded-artwork stream (an ``.mp3``'s album cover).

    ffprobe reports it as a video stream, so an unfiltered read would call an
    audio file a video and take the artwork's dimensions as the media's.
    """
    disposition = stream.get("disposition")
    return isinstance(disposition, dict) and bool(disposition.get("attached_pic"))


def probe_media(path: Path | str) -> MediaProbe | None:
    """Return what ``ffprobe`` knows about *path*, or ``None`` on any failure."""
    if not ffprobe_available():
        return None
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if completed is None or completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_streams = payload.get("streams")
    streams = [s for s in raw_streams if isinstance(s, dict)] if isinstance(raw_streams, list) else []
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not _is_cover_art(s)),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    container = payload.get("format")
    duration = _positive_float(
        container.get("duration") if isinstance(container, dict) else None,
        (video or {}).get("duration"),
        (audio or {}).get("duration"),
    )
    return MediaProbe(
        duration=duration,
        width=_positive_int((video or {}).get("width")),
        height=_positive_int((video or {}).get("height")),
        has_video=video is not None,
        has_audio=audio is not None,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_media_probe.py -v && .venv/bin/ruff check src tests`
Expected: PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/media_probe.py tests/test_media_probe.py
git commit -m "feat(rich-markdown): read media metadata with ffprobe"
```

---

### Task 2: `extract_thumbnail()` and `convert_gif_to_mp4()`

**Files:**
- Modify: `src/telegram_assistant/messages/media_probe.py` (append after `probe_media`)
- Test: `tests/test_media_probe.py` (append)

**Interfaces:**
- Consumes: `_run`, `ffmpeg_available`, `PROBE_TIMEOUT_SECONDS` from Task 1.
- Produces: `extract_thumbnail(path: Path | str, *, duration: float = 0.0) -> bytes | None`, `convert_gif_to_mp4(path: Path | str) -> Path`, `MediaConversionError(ValueError)`, `THUMBNAIL_TIMEOUT_SECONDS = 30`, `CONVERT_TIMEOUT_SECONDS = 120`, `THUMBNAIL_WIDTH = 320`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_media_probe.py`:

```python
def test_extract_thumbnail_returns_the_jpeg_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run(b"\xff\xd8jpeg"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    assert media_probe.extract_thumbnail(clip, duration=73.68) == b"\xff\xd8jpeg"


def test_extract_thumbnail_seeks_into_the_clip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Frame 0 of a real recording is often black; seek 10% in instead."""
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, b"\xff\xd8jpeg", b"")

    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    media_probe.extract_thumbnail(clip, duration=100.0)

    argv = seen["argv"]
    assert argv[0] == "ffmpeg"
    assert "-ss" in argv
    assert float(argv[argv.index("-ss") + 1]) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "stdout,returncode",
    [(b"", 0), (b"\xff\xd8", 1)],
    ids=["empty-output", "nonzero-exit"],
)
def test_extract_thumbnail_returns_none_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: bytes, returncode: int
) -> None:
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run(stdout, returncode))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    assert media_probe.extract_thumbnail(clip, duration=1.0) is None


def test_extract_thumbnail_returns_none_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: None)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")

    assert media_probe.extract_thumbnail(clip, duration=1.0) is None


def test_convert_gif_to_mp4_returns_the_converted_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        Path(argv[-1]).write_bytes(b"fake mp4 bytes")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    gif = tmp_path / "loop.gif"
    gif.write_bytes(b"GIF89a")

    converted = media_probe.convert_gif_to_mp4(gif)
    try:
        assert converted.suffix == ".mp4"
        assert converted.read_bytes() == b"fake mp4 bytes"
    finally:
        converted.unlink(missing_ok=True)


def test_convert_gif_to_mp4_cleans_up_after_a_failed_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A half-written temp file must not survive: the caller's ``finally`` only
    knows about a path it was handed."""
    created: list[Path] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        created.append(Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 1, b"", b"moov atom not found")

    monkeypatch.setattr(media_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    gif = tmp_path / "loop.gif"
    gif.write_bytes(b"GIF89a")

    with pytest.raises(media_probe.MediaConversionError) as excinfo:
        media_probe.convert_gif_to_mp4(gif)

    assert "moov atom not found" in str(excinfo.value)
    assert created and not created[0].exists()


def test_convert_gif_to_mp4_without_ffmpeg_names_the_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_probe.shutil, "which", lambda name: None)
    gif = tmp_path / "loop.gif"
    gif.write_bytes(b"GIF89a")

    with pytest.raises(media_probe.MediaConversionError) as excinfo:
        media_probe.convert_gif_to_mp4(gif)

    assert "ffmpeg" in str(excinfo.value)
    assert "mp4" in str(excinfo.value)


def test_media_conversion_error_is_a_value_error() -> None:
    """Surfaces map ``ValueError`` to 400 / exit 2; anything else would surface
    as an empty 500."""
    assert issubclass(media_probe.MediaConversionError, ValueError)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_media_probe.py -v -k "thumbnail or convert or conversion"`
Expected: FAIL — `AttributeError: module ... has no attribute 'extract_thumbnail'`

- [ ] **Step 3: Write the implementation**

Add `import os` and `import tempfile` to the module imports, then append to `src/telegram_assistant/messages/media_probe.py`:

```python
THUMBNAIL_TIMEOUT_SECONDS = 30
CONVERT_TIMEOUT_SECONDS = 120
THUMBNAIL_WIDTH = 320


class MediaConversionError(ValueError):
    """``ffmpeg`` could not produce the upload shape Telegram needs.

    A ``ValueError`` on purpose: every surface already maps one to 400 / exit 2
    with its message, while an unmapped error would surface as an empty 500.
    """


def extract_thumbnail(path: Path | str, *, duration: float = 0.0) -> bytes | None:
    """Return JPEG bytes for a video preview, or ``None`` on any failure.

    Telegram's clients draw an empty rectangle for a video with no thumbnail
    and no real dimensions, which is the symptom this whole module exists for.
    The frame is taken 10% into the clip — frame 0 of a real recording is
    frequently black — and written to stdout, so no temp file is involved.
    """
    if not ffmpeg_available():
        return None
    seek = max(0.0, duration * 0.1)
    completed = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{seek:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={THUMBNAIL_WIDTH}:-2",
            "-f",
            "mjpeg",
            "-",
        ],
        timeout=THUMBNAIL_TIMEOUT_SECONDS,
    )
    if completed is None or completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


def convert_gif_to_mp4(path: Path | str) -> Path:
    """Convert *path* to a silent mp4 in a temp file and return its path.

    Telegram stores "GIFs" as silent mp4 documents marked
    ``DocumentAttributeAnimated``; an actual ``image/gif`` upload does not
    attach to an article at all. ``yuv420p`` needs even dimensions, hence the
    ``trunc`` scale filter, and ``+faststart`` puts the moov atom first so the
    clients can start playing without the whole file.

    The caller owns the returned path and must unlink it.
    """
    if not ffmpeg_available():
        raise MediaConversionError(
            f"{path}: Telegram only attaches an animated GIF to an article as an "
            f"mp4, and ffmpeg was not found on PATH — install ffmpeg or convert "
            f"the file to mp4 yourself"
        )
    handle, target = tempfile.mkstemp(prefix="tg-gif-", suffix=".mp4")
    os.close(handle)
    target_path = Path(target)
    completed = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-an",
            str(target_path),
        ],
        timeout=CONVERT_TIMEOUT_SECONDS,
    )
    failed = (
        completed is None
        or completed.returncode != 0
        or not target_path.exists()
        or target_path.stat().st_size == 0
    )
    if failed:
        target_path.unlink(missing_ok=True)
        detail = "ffmpeg could not be run"
        if completed is not None:
            stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
            detail = stderr[-500:] or f"ffmpeg exited with {completed.returncode}"
        raise MediaConversionError(f"{path}: GIF to mp4 conversion failed: {detail}")
    return target_path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_media_probe.py -v && .venv/bin/ruff check src tests`
Expected: PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/media_probe.py tests/test_media_probe.py
git commit -m "feat(rich-markdown): add thumbnail extraction and GIF-to-mp4 conversion"
```

---

### Task 3: Build video and audio attributes from the probe

**Files:**
- Modify: `src/telegram_assistant/messages/telethon_backend.py` (module imports at the top; `_document_attributes` at ~line 210; the upload loop in `_upload_rich_files` at ~lines 431-442)
- Test: `tests/test_messages_telethon_backend.py` (append at the end of the file)

**Interfaces:**
- Consumes: `MediaProbe`, `probe_media` from Task 1.
- Produces: `_document_attributes(path: str, kind: str, *, probe: MediaProbe | None) -> tuple[list[Any], str]` — the probe is now supplied by the caller, so it is fetched once per file and reused by Task 4's thumbnail step.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_messages_telethon_backend.py`:

```python
# ---------------------------------------------------------------------------
# Media attributes from the ffprobe-backed prober
# ---------------------------------------------------------------------------


def _fake_probe(monkeypatch: Any, probe: Any) -> None:
    """Make every ``probe_media`` call in the backend answer with *probe*."""
    from telegram_assistant.messages import media_probe

    monkeypatch.setattr(media_probe, "probe_media", lambda path: probe)


@pytest.mark.asyncio
async def test_video_carries_the_probed_duration_and_dimensions(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Telethon's stub is ``duration=0, w=1, h=1`` for every mp4 and the server
    only repairs small files — a 12 MB+ video keeps the stub and renders as an
    empty rectangle."""
    from telethon.tl.types import DocumentAttributeVideo

    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=73.681, width=854, height=480, has_video=True, has_audio=True),
    )
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    attributes = client.upload_media[0].media.attributes
    video_attrs = [a for a in attributes if isinstance(a, DocumentAttributeVideo)]
    # Exactly one: two DocumentAttributeVideo in one document is a malformed
    # request, so the probed one replaces Telethon's stub rather than joining it.
    assert len(video_attrs) == 1
    assert video_attrs[0].duration == 74
    assert video_attrs[0].w == 854
    assert video_attrs[0].h == 480
    assert video_attrs[0].supports_streaming is True


@pytest.mark.asyncio
async def test_audio_carries_the_probed_duration(tmp_path: Any, monkeypatch: Any) -> None:
    """The hard-coded ``duration=0`` made ``tg://audio`` resolve but showed a
    zero-length track in the clients."""
    from telethon.tl.types import DocumentAttributeAudio

    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=184.5, width=None, height=None, has_video=False, has_audio=True),
    )
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://audio?id=voice)\n",
        rich_files=(audio,),
    )

    attributes = client.upload_media[0].media.attributes
    audio_attrs = [a for a in attributes if isinstance(a, DocumentAttributeAudio)]
    assert len(audio_attrs) == 1
    assert audio_attrs[0].duration == 184


@pytest.mark.asyncio
async def test_a_failed_probe_keeps_the_send_working(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """No ffprobe on the box is not a send failure: the pre-probe stub goes out,
    exactly as it did before this feature."""
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

    _fake_probe(monkeypatch, None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=clip)\n\n![](tg://audio?id=voice)\n",
        rich_files=(video, audio),
    )

    assert any(
        isinstance(a, DocumentAttributeVideo)
        for a in client.upload_media[0].media.attributes
    )
    # Still present, so tg://audio keeps resolving without a prober.
    assert any(
        isinstance(a, DocumentAttributeAudio)
        for a in client.upload_media[1].media.attributes
    )


@pytest.mark.asyncio
async def test_a_failed_probe_is_logged_with_the_file(
    tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """Uploads used to write nothing to the log at all, so a broken article was
    undiagnosable after the fact."""
    _fake_probe(monkeypatch, None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    with caplog.at_level(
        "WARNING", logger="telegram_assistant.messages.telethon_backend"
    ):
        await backend.send_message(
            chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
        )

    assert "clip.mp4" in caplog.text


@pytest.mark.asyncio
async def test_a_photo_is_never_probed(tmp_path: Any, monkeypatch: Any) -> None:
    """A photo is uploaded as ``InputMediaUploadedPhoto`` and has no attributes
    at all — probing it would spend a subprocess per image for nothing."""
    from telegram_assistant.messages import media_probe

    calls: list[str] = []
    monkeypatch.setattr(
        media_probe, "probe_media", lambda path: calls.append(str(path)) or None
    )
    photo = _rich_file(tmp_path, "shot.png", "shot", "photo")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown="![](tg://photo?id=shot)\n", rich_files=(photo,)
    )

    assert calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py -v -k "probed or failed_probe or never_probed"`
Expected: FAIL — the video attribute still has `duration=0, w=1, h=1`.

- [ ] **Step 3: Write the implementation**

In `src/telegram_assistant/messages/telethon_backend.py`, add to the imports at the top:

```python
import asyncio

from telegram_assistant.messages import media_probe
from telegram_assistant.observability.logging import get_logger
```

and after the imports:

```python
_log = get_logger(__name__)
```

Replace `_document_attributes` (line 210) with:

```python
def _document_attributes(
    path: str, kind: str, *, probe: media_probe.MediaProbe | None
) -> tuple[list[Any], str]:
    """Build the ``InputMediaUploadedDocument`` attributes for one rich file.

    This is the single place a video/audio/animation attribute is built.
    Telethon's ``utils.get_attributes()`` is still asked, but only for the
    filename attribute and the mime type: without a metadata library it returns
    a stub ``DocumentAttributeVideo(duration=0, w=1, h=1)`` for every mp4 and no
    ``DocumentAttributeAudio`` at all. Telegram repairs the metadata server-side
    for smaller uploads only (measured live: fine up to 6.30 MB, still stubbed
    from 12.72 MB up), and a client that receives 1x1 with no duration draws an
    empty rectangle.

    *probe* is supplied by the caller so one ``ffprobe`` run serves both this
    and the thumbnail. ``None`` means the probe failed: the pre-probe behaviour
    is kept — the stub for video, ``duration=0`` for audio, which is what makes
    ``tg://audio`` resolve at all — and a warning names the file.
    """
    from telethon import utils
    from telethon.tl import types

    raw_attributes, mime_type = utils.get_attributes(path)
    attributes = list(raw_attributes)
    if kind == "video":
        if probe is not None and probe.has_video and probe.width and probe.height:
            attributes = [
                attr
                for attr in attributes
                if not isinstance(attr, types.DocumentAttributeVideo)
            ]
            attributes.append(
                types.DocumentAttributeVideo(
                    duration=round(probe.duration),
                    w=probe.width,
                    h=probe.height,
                    supports_streaming=True,
                )
            )
        else:
            _log.warning(
                "rich media video metadata unavailable, sending stub attributes",
                path=path,
                reason="no_probe" if probe is None else "no_video_stream",
            )
    elif kind == "audio":
        if probe is not None:
            attributes = [
                attr
                for attr in attributes
                if not isinstance(attr, types.DocumentAttributeAudio)
            ]
            attributes.append(
                types.DocumentAttributeAudio(duration=round(probe.duration))
            )
        else:
            _log.warning(
                "rich media audio metadata unavailable, sending stub attributes",
                path=path,
                reason="no_probe",
            )
            if not any(
                isinstance(attr, types.DocumentAttributeAudio) for attr in attributes
            ):
                attributes.append(types.DocumentAttributeAudio(duration=0))
    if (
        kind == "video"
        and mime_type == "image/gif"
        and not any(
            isinstance(attr, types.DocumentAttributeAnimated) for attr in attributes
        )
    ):
        attributes.append(types.DocumentAttributeAnimated())
    return attributes, mime_type
```

In `_upload_rich_files`, replace the `else` branch that builds the document (lines 436-442) with:

```python
                else:
                    probe = await asyncio.to_thread(
                        media_probe.probe_media, rich_file.path
                    )
                    attributes, mime_type = _document_attributes(
                        rich_file.path, rich_file.kind, probe=probe
                    )
                    media = types.InputMediaUploadedDocument(
                        file=handle, mime_type=mime_type, attributes=attributes
                    )
```

This branch is the non-photo one, so no `kind == "photo"` guard is needed — a photo is uploaded as `InputMediaUploadedPhoto` above and never probed.

`asyncio.to_thread` is not optional: `probe_media` blocks for up to 20 seconds and this runs on the event loop.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py tests/test_messages_rich_media.py -v && .venv/bin/ruff check src tests`
Expected: PASS — including the pre-existing `test_rich_send_video_carries_video_attribute`, `test_rich_send_audio_gets_audio_attribute` and `test_rich_send_gif_carries_the_animated_attribute`.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/telethon_backend.py tests/test_messages_telethon_backend.py
git commit -m "feat(rich-markdown): fill video and audio attributes from the probe"
```

---

### Task 4: Attach a generated thumbnail to every video

**Files:**
- Modify: `src/telegram_assistant/messages/telethon_backend.py` (`_upload_rich_files`)
- Test: `tests/test_messages_telethon_backend.py` (the `_UploadingClient.upload_file` fake at ~line 728; append new tests at the end)

**Interfaces:**
- Consumes: `extract_thumbnail` from Task 2, the per-file `probe` from Task 3.
- Produces: nothing new; `InputMediaUploadedDocument` now carries `thumb=` for videos whose thumbnail could be generated.

- [ ] **Step 1: Update the fake and write the failing tests**

The fake's `upload_file` takes a single positional argument; the thumbnail is uploaded from bytes with a `file_name`. Change it in `tests/test_messages_telethon_backend.py` (~line 728):

```python
    async def upload_file(self, path: Any, **kwargs: Any) -> Any:
        if self._upload_error is not None:
            raise self._upload_error
        label = kwargs.get("file_name") or str(path)
        self.uploads.append(label)
        return f"handle:{label}"
```

Append the tests:

```python
@pytest.mark.asyncio
async def test_video_carries_a_generated_thumbnail(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A large video comes back from the server with ``thumbs=None``; without a
    preview the clients draw an empty rectangle even with correct dimensions."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=73.681, width=854, height=480, has_video=True, has_audio=True),
    )
    seen: dict[str, Any] = {}

    def fake_thumb(path: Any, *, duration: float) -> bytes:
        seen["duration"] = duration
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(media_probe, "extract_thumbnail", fake_thumb)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    assert client.upload_media[0].media.thumb == "handle:thumb.jpg"
    # The probe is reused rather than run a second time for the seek offset.
    assert seen["duration"] == pytest.approx(73.681)


@pytest.mark.asyncio
async def test_a_missing_thumbnail_does_not_fail_the_send(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=5.0, width=100, height=100, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    assert client.upload_media[0].media.thumb is None


@pytest.mark.asyncio
async def test_audio_gets_no_thumbnail(tmp_path: Any, monkeypatch: Any) -> None:
    """Only videos need a preview frame; running ffmpeg over every mp3 would
    spend a subprocess for nothing."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    calls: list[Any] = []
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=184.5, width=None, height=None, has_video=False, has_audio=True),
    )
    monkeypatch.setattr(
        media_probe,
        "extract_thumbnail",
        lambda path, *, duration: calls.append(path) or None,
    )
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://audio?id=voice)\n",
        rich_files=(audio,),
    )

    assert calls == []
    assert client.upload_media[0].media.thumb is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py -v -k "thumbnail"`
Expected: FAIL — `media.thumb` is `None` in the first test.

- [ ] **Step 3: Write the implementation**

In `_upload_rich_files`, extend the document branch written in Task 3:

```python
                else:
                    probe = await asyncio.to_thread(
                        media_probe.probe_media, rich_file.path
                    )
                    attributes, mime_type = _document_attributes(
                        rich_file.path, rich_file.kind, probe=probe
                    )
                    thumb = None
                    if rich_file.kind == "video":
                        thumb = await self._upload_video_thumbnail(
                            path=rich_file.path,
                            duration=probe.duration if probe is not None else 0.0,
                        )
                    media = types.InputMediaUploadedDocument(
                        file=handle,
                        mime_type=mime_type,
                        attributes=attributes,
                        thumb=thumb,
                    )
```

and add the helper to `TelethonMessageBackend`:

```python
    async def _upload_video_thumbnail(self, *, path: str, duration: float) -> Any:
        """Return an uploaded preview frame for *path*, or ``None``.

        Telegram's clients draw an empty rectangle for a video with no
        thumbnail, and the server only generates one for smaller uploads. A
        failure here is never a send failure — the article goes out without a
        preview and the reason is logged.
        """
        thumb_bytes = await asyncio.to_thread(
            media_probe.extract_thumbnail, path, duration=duration
        )
        if not thumb_bytes:
            _log.warning("rich media thumbnail could not be generated", path=path)
            return None
        try:
            return await self._client.upload_file(thumb_bytes, file_name="thumb.jpg")
        except Exception as exc:  # noqa: BLE001 - a preview is never worth the send
            _log.warning(
                "rich media thumbnail upload failed", path=path, error=str(exc)
            )
            return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/telethon_backend.py tests/test_messages_telethon_backend.py
git commit -m "feat(rich-markdown): attach a generated thumbnail to rich-message videos"
```

---

### Task 5: Upload a `.gif` as a converted mp4

**Files:**
- Modify: `src/telegram_assistant/messages/telethon_backend.py` (`_document_attributes` signature and its animation branch; `_upload_rich_files` loop body)
- Test: `tests/test_messages_telethon_backend.py` (rewrite `test_rich_send_gif_carries_the_animated_attribute` at ~line 1029; append new tests)

**Interfaces:**
- Consumes: `convert_gif_to_mp4`, `MediaConversionError` from Task 2.
- Produces: `_document_attributes(path: str, kind: str, *, probe: MediaProbe | None, animated: bool = False, file_name: str | None = None) -> tuple[list[Any], str]`.

- [ ] **Step 1: Rewrite the existing GIF test and write the new ones**

Replace `test_rich_send_gif_carries_the_animated_attribute` (~line 1029) with:

```python
@pytest.mark.asyncio
async def test_a_gif_is_uploaded_as_a_converted_mp4(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An ``image/gif`` upload does not attach to an article at all. Telegram
    stores "GIFs" as silent mp4 documents marked ``DocumentAttributeAnimated``,
    so the file is converted before it is uploaded."""
    from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeFilename

    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=3.5, width=480, height=270, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=loop)\n",
        rich_files=(gif,),
    )

    media = client.upload_media[0].media
    assert media.mime_type == "video/mp4"
    assert any(isinstance(a, DocumentAttributeAnimated) for a in media.attributes)
    names = [a.file_name for a in media.attributes if isinstance(a, DocumentAttributeFilename)]
    # The temp file's name must not leak: the article shows the author's name.
    assert names == ["loop.mp4"]
    assert client.uploads == [str(converted)]


@pytest.mark.asyncio
async def test_the_converted_gif_temp_file_is_removed(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=3.5, width=480, height=270, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=loop)\n",
        rich_files=(gif,),
    )

    assert not converted.exists()


@pytest.mark.asyncio
async def test_the_converted_gif_temp_file_is_removed_after_a_failed_send(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A failed upload must not leave the temp mp4 behind."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=3.5, width=480, height=270, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient(upload_error=RuntimeError("boom"))
    backend = TelethonMessageBackend(client)

    with pytest.raises(Exception):
        await backend.send_message(
            chat_id=1,
            text="",
            rich_markdown="![](tg://video?id=loop)\n",
            rich_files=(gif,),
        )

    assert not converted.exists()


@pytest.mark.asyncio
async def test_a_failed_gif_conversion_is_a_value_error(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """``MediaConversionError`` reaches the surfaces as a ``ValueError`` so the
    operator reads the ffmpeg reason on exit 2, not an empty 500."""
    from telegram_assistant.messages import media_probe

    def boom(path: Any) -> Any:
        raise media_probe.MediaConversionError("ffmpeg: moov atom not found")

    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", boom)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    with pytest.raises(ValueError, match="moov atom"):
        await backend.send_message(
            chat_id=1,
            text="",
            rich_markdown="![](tg://video?id=loop)\n",
            rich_files=(gif,),
        )

    assert client.upload_media == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py -v -k "gif"`
Expected: FAIL — the mime type is still `image/gif` and nothing is converted.

- [ ] **Step 3: Write the implementation**

Change the `_document_attributes` signature and replace its mime-based animation branch (the trigger moves from "the mime is `image/gif`" to "the source file was a `.gif`", because the uploaded file is now an mp4):

```python
def _document_attributes(
    path: str,
    kind: str,
    *,
    probe: media_probe.MediaProbe | None,
    animated: bool = False,
    file_name: str | None = None,
) -> tuple[list[Any], str]:
```

Replace the trailing `image/gif` block with:

```python
    if animated and not any(
        isinstance(attr, types.DocumentAttributeAnimated) for attr in attributes
    ):
        attributes.append(types.DocumentAttributeAnimated())
    if file_name is not None:
        attributes = [
            attr
            for attr in attributes
            if not isinstance(attr, types.DocumentAttributeFilename)
        ]
        attributes.append(types.DocumentAttributeFilename(file_name=file_name))
    return attributes, mime_type
```

and extend the docstring with:

```
    *animated* marks the document as an animation. It is driven by the source
    file being a ``.gif``, not by the mime type: by the time this runs the
    upload is already the converted mp4. *file_name* overrides the name derived
    from that temp file so the article shows the author's own name.
```

Rewrite the `_upload_rich_files` loop body so the conversion happens before the upload and its temp file is always removed:

```python
        uploaded: list[Any] = []
        for rich_file in rich_files:
            source = Path(rich_file.path)
            is_gif = rich_file.kind == "video" and source.suffix.lower() == ".gif"
            temp_path: Path | None = None
            try:
                if is_gif:
                    # Outside the RPC try/except below: a conversion failure is
                    # bad input, not a Telegram rights or FLOOD_WAIT problem.
                    temp_path = await asyncio.to_thread(
                        media_probe.convert_gif_to_mp4, source
                    )
                upload_path = str(temp_path) if temp_path is not None else rich_file.path
                try:
                    handle = await self._client.upload_file(upload_path)
                    if rich_file.kind == "photo":
                        media: Any = types.InputMediaUploadedPhoto(file=handle)
                    else:
                        probe = await asyncio.to_thread(
                            media_probe.probe_media, upload_path
                        )
                        attributes, mime_type = _document_attributes(
                            upload_path,
                            rich_file.kind,
                            probe=probe,
                            animated=is_gif,
                            file_name=f"{source.stem}.mp4" if is_gif else None,
                        )
                        thumb = None
                        if rich_file.kind == "video":
                            thumb = await self._upload_video_thumbnail(
                                path=upload_path,
                                duration=probe.duration if probe is not None else 0.0,
                            )
                        media = types.InputMediaUploadedDocument(
                            file=handle,
                            mime_type=mime_type,
                            attributes=attributes,
                            thumb=thumb,
                        )
                    # uploadMedia binds the upload to the destination peer, which
                    # is also where a media-rights rejection surfaces first.
                    result = await self._client(
                        functions.messages.UploadMediaRequest(peer=peer, media=media)
                    )
                except Exception as exc:
                    raise _translate_rich_send_error(exc, chat_id=chat_id) from exc
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            if rich_file.kind == "photo":
                uploaded.append(
                    photo_type(
                        id=rich_file.id,
                        photo=utils.get_input_photo(getattr(result, "photo", result)),
                    )
                )
            else:
                uploaded.append(
                    document_type(
                        id=rich_file.id,
                        document=utils.get_input_document(
                            getattr(result, "document", result)
                        ),
                    )
                )
        return uploaded
```

Add `from pathlib import Path` to the module imports if it is not already there. The probe now runs against `upload_path` (the converted mp4 for a `.gif`, the original file otherwise) — probing the source `.gif` would report the GIF's own dimensions rather than the mp4's.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py tests/test_messages_rich_media.py -v && .venv/bin/ruff check src tests`
Expected: PASS — including `test_a_photo_is_never_probed` from Task 3.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/telethon_backend.py tests/test_messages_telethon_backend.py
git commit -m "feat(rich-markdown): upload an animated GIF as a converted mp4"
```

---

### Task 6: Reject a `.gif` before the operation row when ffmpeg is missing

**Files:**
- Modify: `src/telegram_assistant/messages/service.py` (`_validate_rich_files`, the per-file loop that ends at line 332)
- Test: `tests/test_messages_rich_media.py` (append after `test_duplicate_rich_file_ids_are_rejected`, ~line 899)

**Interfaces:**
- Consumes: `ffmpeg_available` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_messages_rich_media.py`:

```python
async def test_a_gif_without_ffmpeg_is_rejected_before_the_operation_row(
    tmp_path: Path, store: OperationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ffmpeg the GIF cannot be converted, and an unconverted GIF does
    not attach at all — failing here leaves the idempotency key free for the
    retry after the install."""
    from telegram_assistant.messages import media_probe

    monkeypatch.setattr(media_probe, "ffmpeg_available", lambda: False)
    gif = _touch(tmp_path / "loop.gif")
    with pytest.raises(ValueError, match="ffmpeg"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="![](tg://video?id=loop)\n",
                rich_files=(RichFile(id="loop", path=str(gif), caption="", kind="video"),),
                operation_id="rich-files-gif-no-ffmpeg",
            ),
        )
    key = idempotency.message_send_key(
        telegram_chat_id=-100,
        telegram_topic_id=None,
        operation_id="rich-files-gif-no-ffmpeg",
    )
    assert store.find_by_idempotency_key(key) is None


async def test_a_gif_with_ffmpeg_passes_validation(
    tmp_path: Path, store: OperationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_assistant.messages import media_probe

    monkeypatch.setattr(media_probe, "ffmpeg_available", lambda: True)
    gif = _touch(tmp_path / "loop.gif")
    backend = RecordingBackend()

    result = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown="![](tg://video?id=loop)\n",
            rich_files=(RichFile(id="loop", path=str(gif), caption="", kind="video"),),
            operation_id="rich-files-gif-ok",
        ),
    )

    assert result.telegram_message_id is not None


async def test_a_non_gif_video_never_asks_about_ffmpeg(
    tmp_path: Path, store: OperationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An mp4 sends fine with no ffmpeg at all — only the attributes degrade."""
    from telegram_assistant.messages import media_probe

    monkeypatch.setattr(media_probe, "ffmpeg_available", lambda: False)
    clip = _touch(tmp_path / "clip.mp4")
    backend = RecordingBackend()

    result = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown="![](tg://video?id=clip)\n",
            rich_files=(RichFile(id="clip", path=str(clip), caption="", kind="video"),),
            operation_id="rich-files-mp4-no-ffmpeg",
        ),
    )

    assert result.telegram_message_id is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages_rich_media.py -v -k "ffmpeg"`
Expected: FAIL — `DID NOT RAISE ValueError` in the first test.

- [ ] **Step 3: Write the implementation**

In `src/telegram_assistant/messages/service.py`, add the import next to the other `messages` imports at the top:

```python
from telegram_assistant.messages import media_probe
```

and append to the per-file loop in `_validate_rich_files`, after the readability check (line 332):

```python
        if (
            os.path.splitext(rich_file.path)[1].lower() == ".gif"
            and not media_probe.ffmpeg_available()
        ):
            raise ValueError(
                f"rich_files entry needs ffmpeg: {rich_file.path} is a GIF, and "
                f"Telegram only attaches one to an article as an mp4 — install "
                f"ffmpeg or convert the file to mp4 yourself"
            )
```

Extend the function's docstring with:

```
    The ``.gif`` check belongs here rather than in the backend for the same
    reason: the conversion is what makes the file attachable at all, so a box
    with no ffmpeg must fail before the operation row exists.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_rich_media.py -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/service.py tests/test_messages_rich_media.py
git commit -m "feat(rich-markdown): reject a GIF up front when ffmpeg is missing"
```

---

### Task 7: Log every rich-media upload

**Files:**
- Modify: `src/telegram_assistant/messages/telethon_backend.py` (`_upload_rich_files` loop)
- Test: `tests/test_messages_telethon_backend.py` (append)

**Interfaces:**
- Consumes: `_log` from Task 3.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_messages_telethon_backend.py`:

```python
@pytest.mark.asyncio
async def test_each_rich_media_upload_is_logged(
    tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """Uploading 34 files used to write nothing to the log even at DEBUG, so a
    broken article could not be diagnosed after the fact."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=73.681, width=854, height=480, has_video=True, has_audio=True),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    photo = _rich_file(tmp_path, "shot.png", "shot", "photo")
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    with caplog.at_level("INFO", logger="telegram_assistant.messages.telethon_backend"):
        await backend.send_message(
            chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(photo, video)
        )

    assert "shot.png" in caplog.text
    assert "clip.mp4" in caplog.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py -v -k "upload_is_logged"`
Expected: FAIL — `assert 'shot.png' in ''`

- [ ] **Step 3: Write the implementation**

In `_upload_rich_files`, immediately after `result = await self._client(...)` succeeds (inside the inner `try`, as the last statement of it), add:

```python
                    _log.info(
                        "rich media uploaded",
                        path=rich_file.path,
                        kind=rich_file.kind,
                        file_id=rich_file.id,
                        size_bytes=source.stat().st_size if source.exists() else None,
                        converted=is_gif,
                    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_telethon_backend.py -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telegram_assistant/messages/telethon_backend.py tests/test_messages_telethon_backend.py
git commit -m "feat(rich-markdown): log every rich-media upload"
```

---

### Task 8: Documentation and TODO

**Files:**
- Modify: `CLAUDE.md` (the "Local media in an article is CLI-only" bullet under Architecture, where the four proven `tg://` wire facts are recorded)
- Modify: `README.md` (the setup block around line 54, next to the Telethon 1.44 migration note at line 144)
- Modify: `skills/telegram-assistant/SKILL.md` (the `messages send` rich-markdown section, ~lines 543-580)
- Modify: `docs/TODO.md`
- Copy: `skills/telegram-assistant/SKILL.md` → `~/.claude/skills/telegram-assistant/SKILL.md`

- [ ] **Step 1: Update CLAUDE.md**

In the "Local media in an article is CLI-only" bullet, after the sentence recording the `.gif` / `DocumentAttributeAnimated` treatment, replace that treatment's description with the new behaviour and add the shared path:

```markdown
Media attributes are **not** left to Telethon or to the server. Without a metadata library `utils.get_attributes()` returns a stub `DocumentAttributeVideo(duration=0, w=1, h=1, supports_streaming=False)` for every mp4 and no `DocumentAttributeAudio` at all; Telegram repairs the metadata by re-parsing the upload, but only for smaller files — measured live 2026-07-29 (Saved Messages, msg 407429/407430), seven videos up to 6.30 MB came back with real duration, dimensions, `thumbs=2` and `supports_streaming=True`, while three from 12.72 MB up kept `duration=0, w=1, h=1, thumbs=None` and rendered as an **empty rectangle** in the clients. The threshold is undocumented, so nothing keys off file size: `messages/media_probe.py` (no Telethon imports, plain data out) runs `ffprobe` once per non-photo file and `_document_attributes()` builds the one video/audio/animation attribute from it — replacing Telethon's, never joining it, since two `DocumentAttributeVideo` in a document is a malformed request. Videos additionally get an `ffmpeg`-generated preview frame (10% in, frame 0 of a real recording is often black) as `thumb=`; the missing preview is what makes the empty rectangle, so it is generated for every video rather than for large ones only. A **cover-art** stream (`disposition.attached_pic`) is not a video stream — an `.mp3` with artwork would otherwise be shaped like a video and take the artwork's dimensions. `ffprobe`/`ffmpeg` are **optional external binaries, not pip dependencies**: a failed probe is never an error (the pre-probe stub goes out, with a `WARNING` naming the file), because a box with no ffmpeg must still be able to send. The one exception is an **animated `.gif`**, which does not attach to an article at all as `image/gif` — Telegram stores "GIFs" as silent mp4 documents marked `DocumentAttributeAnimated`, so `convert_gif_to_mp4()` uploads a converted mp4 in its place (temp file removed in a `finally`, including the failure path) and the original `.gif` name with an `.mp4` suffix is written back as the filename attribute so the temp name never reaches the article. Since conversion is what makes the file attachable, a `.gif` with no `ffmpeg` on PATH is rejected by `_validate_rich_files` **before the operation row is opened**, leaving the idempotency key free for the retry after the install. Every probe/convert/thumbnail call is a blocking subprocess and runs through `asyncio.to_thread`, never on the event loop. The markdown body is untouched by all of this: a `.gif` is still referenced as `tg://video?id=…` and `media_kind()` is unchanged.
```

- [ ] **Step 2: Update README.md**

Under the setup instructions (near line 54), add:

```markdown
`ffmpeg` and `ffprobe` are optional external binaries (not pip dependencies) used only by
`messages send --rich-markdown` with local media: they fill in video/audio duration and
dimensions, generate a video preview frame, and convert an animated `.gif` into the mp4
Telegram needs. Without them videos still send but may render as an empty rectangle in the
clients, and a `.gif` is rejected with a message telling you to install ffmpeg or convert
the file yourself. Install with `apt install ffmpeg` (Debian/Ubuntu) or `brew install ffmpeg`.
```

- [ ] **Step 3: Update SKILL.md and re-sync it**

In the `messages send` rich-markdown section, add to the notes about local media:

```markdown
- Local media in an article needs `ffmpeg`/`ffprobe` on the box for correct
  playback metadata. An animated `.gif` is **converted to mp4** automatically;
  without `ffmpeg` a `.gif` is rejected (exit 2) with a message naming the fix.
  Videos without a probe still send — they may just show as an empty
  rectangle — and the reason is in the server log at `WARNING`.
```

Then re-sync:

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
```

- [ ] **Step 4: Update docs/TODO.md**

Mark both items done by changing `- [ ]` to `- [x]` on:
- `rich_markdown: анимированный .gif не прикрепляется — нужна конвертация в mp4`
- `rich_markdown: крупные видео уходят с заглушечными атрибутами и рендерятся пустыми`

Also remove the already-completed `- [x] Разгрузить messages send в CLI` item and its sub-bullets (line 14) — it shipped in PR #20 and lives in git history now.

Leave the `part=True` / post-send verification item open: it is explicitly out of scope, **except** its `Заодно` sub-bullet about upload logging, which Task 7 delivers — edit that sub-bullet to drop the "ни одной строки в лог" complaint and keep only the CLI-output-truncation part.

- [ ] **Step 5: Verify the skill inventory guard and full suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: PASS — `tests/test_skill_inventory.py` in particular (it fails when the CLI catalog drifts from the skill).

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md skills/telegram-assistant/SKILL.md docs/TODO.md
git commit -m "docs(rich-markdown): record the ffprobe-backed media attribute path"
```

---

## Verification

After all tasks:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

Both must pass. Do **not** run any live e2e or spike script.

Optional manual check, **only on the user's explicit request** (one send to Saved Messages, not a full e2e run):

```bash
.venv/bin/telegram-assistant messages send --entity me --rich-markdown /tmp/article.md --vault-dir <vault>
```

with an article embedding one large mp4 and one animated `.gif`; confirm in a client that the
video shows a preview and a duration and that the GIF plays.
