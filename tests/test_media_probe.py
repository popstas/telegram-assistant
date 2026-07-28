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
