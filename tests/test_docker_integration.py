from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coding_agent.docker_tests import DockerLimits, DockerTestRunner
from coding_agent.errors import DockerUnavailable


@pytest.mark.docker
def test_real_docker_container_isolated_from_host_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real container verification: non-root, no network, no host secrets."""

    if shutil.which("docker") is None:
        pytest.skip("Docker is not installed")
    runner = DockerTestRunner(
        docker_path=shutil.which("docker"),
        limits=DockerLimits(timeout_seconds=180, build_timeout_seconds=600),
    )
    try:
        runner.ensure_available()
    except DockerUnavailable as exc:
        pytest.skip(f"Docker daemon not available: {exc}")

    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "OPENAI_API_KEY=" + "sk-" + "container-must-not-see\n"
    )
    (project / "README.md").write_text("# temp project\n")
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_isolation.py").write_text(
        """
import os
import socket
from pathlib import Path


def test_runs_as_non_root():
    assert os.geteuid() != 0


def test_api_key_not_in_environment():
    assert "OPENAI_API_KEY" not in os.environ


def test_no_network():
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=2)
    except OSError:
        pass
    else:
        raise AssertionError("container unexpectedly has network access")


def test_workspace_is_writable():
    Path("/workspace/agent-write-test.txt").write_text("ok")
    assert Path("/workspace/agent-write-test.txt").read_text() == "ok"


def test_env_file_and_docker_socket_absent():
    assert not Path("/workspace/.env").exists()
    assert not Path("/var/run/docker.sock").exists()
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "OPENAI_API_KEY", "sk-" + "host-secret-that-must-not-leak"
    )
    result = runner.run(project, profile="pytest")
    assert result.success, result.stdout + result.stderr
    assert result.passed_count >= 5
    assert result.failed_count == 0
    # The original project was never used as the container workspace.
    assert not (project / "agent-write-test.txt").exists()
    assert (project / ".env").read_text().startswith("OPENAI_API_KEY=")
    # Only the sanitized copy was removed, not the original.
    assert project.exists()
