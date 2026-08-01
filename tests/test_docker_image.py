"""Structural checks for the Docker image and compose example.

A full build-and-run smoke test lives in ``scripts/docker-smoke.sh`` because
it needs the Docker daemon. The assertions here keep the Dockerfile and
``docker-compose.yml`` honest about the requirements from the plan:

- slim Python 3.12 base
- port 8085 exposed
- ``/data`` mounted as a volume
- ``uvicorn`` is the default command, wired to the FastAPI factory
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "docker-smoke.sh"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_uses_python_312_slim_base() -> None:
    content = _dockerfile()
    assert "FROM python:3.12-slim" in content


def test_dockerfile_exposes_default_http_port() -> None:
    assert "EXPOSE 8085" in _dockerfile()


def test_dockerfile_declares_data_volume() -> None:
    content = _dockerfile()
    assert 'VOLUME ["/data"]' in content


def test_dockerfile_default_command_runs_uvicorn_factory() -> None:
    content = _dockerfile()
    assert "uvicorn" in content
    assert "telegram_assistant.http_api.app:create_app" in content
    assert "--factory" in content
    assert "--port" in content and "8085" in content


def test_dockerfile_installs_ffmpeg() -> None:
    """The image must ship ``ffmpeg``/``ffprobe``.

    ``messages send --rich-markdown`` probes local media for duration and
    dimensions, extracts a video preview frame, and converts an animated
    ``.gif`` to mp4. Without those binaries large videos render as an empty
    rectangle in the Telegram clients and a ``.gif`` is rejected outright,
    so the container would silently behave worse than a host install.
    """
    content = _dockerfile()
    assert "ffmpeg" in content, "Dockerfile must apt-install ffmpeg (it also provides ffprobe)"


def test_dockerfile_runs_as_non_root_user() -> None:
    content = _dockerfile()
    assert "USER app" in content


def test_dockerignore_excludes_data_directory() -> None:
    assert DOCKERIGNORE.exists(), ".dockerignore must exist to keep secrets out of the image"
    content = DOCKERIGNORE.read_text(encoding="utf-8")
    assert "data/" in content
    assert ".venv/" in content


def test_compose_mounts_data_and_publishes_8085() -> None:
    payload = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = payload["services"]["telegram-assistant"]
    assert "8085:8085" in service["ports"]
    assert any(
        isinstance(v, str) and v.endswith(":/data")
        for v in service["volumes"]
    )
    healthcheck = service.get("healthcheck") or {}
    test_clause = healthcheck.get("test")
    assert test_clause, "compose service must declare a healthcheck"
    joined = " ".join(test_clause) if isinstance(test_clause, list) else test_clause
    assert "/health" in joined


def test_smoke_script_is_present_and_executable() -> None:
    assert SMOKE_SCRIPT.exists(), "smoke script must exist for manual verification"
    mode = SMOKE_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "smoke script must be executable"


def test_smoke_script_checks_ffmpeg_binaries() -> None:
    """The structural check above only reads the Dockerfile; the smoke run proves
    the binaries are actually on PATH inside the built image."""
    content = SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "ffmpeg" in content and "ffprobe" in content


@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker CLI not available; structural checks already cover the Dockerfile shape",
)
def test_dockerfile_parses_with_docker_buildx_check() -> None:
    """When Docker is available, parse-check the Dockerfile syntax.

    ``docker buildx build --check`` lints the Dockerfile without producing an
    image — it catches syntax issues, deprecated instructions, and missing
    stages. Skipped when Docker isn't installed so the unit-test suite stays
    portable.
    """
    result = subprocess.run(  # noqa: S603 - controlled invocation
        [
            "docker",
            "buildx",
            "build",
            "--check",
            "-f",
            str(DOCKERFILE),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(
            "docker buildx build --check failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
