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
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
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


@cache
def ffprobe_available() -> bool:
    """True when ``ffprobe`` is on PATH. Cached: PATH does not move at runtime."""
    return shutil.which("ffprobe") is not None


@cache
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
