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
