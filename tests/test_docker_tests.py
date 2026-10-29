from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coding_agent.docker_tests import (
    DockerLimits,
    DockerTestRunner,
    PytestProfile,
)
from coding_agent.errors import DockerTestError, DockerUnavailable
from tests.conftest import FakeCommandRunner, result


PASS_OUTPUT = (
    "tests/test_app.py::test_add PASSED                             [100%]\n"
    "============================== 1 passed in 0.01s ==============================\n"
)
FAIL_OUTPUT = (
    "tests/test_app.py::test_add FAILED                             [100%]\n"
    "============================== 1 failed in 0.01s ==============================\n"
)


class DockerFake:
    def __init__(self, *, build_code: int = 0, run_code: int = 0, run_output: str = PASS_OUTPUT, timeout_run: bool = False):
        self.build_code = build_code
        self.run_code = run_code
        self.run_output = run_output
        self.timeout_run = timeout_run
        self.build_context: Path | None = None
        self.build_dockerfile: str = ""
        self.build_files: list[str] = []
        self.run_args: list[str] = []
        self.run_mount: Path | None = None
        self.run_mount_files: list[str] = []
        self.cleanup_calls: list[list[str]] = []
        self.host_env: dict[str, str] | None = None

    def __call__(self, args, cwd, timeout, env):
        if args[1] == "build":
            self.build_context = Path(args[-1])
            dockerfile = Path(args[args.index("--file") + 1])
            self.build_dockerfile = dockerfile.read_text()
            self.build_files = sorted(
                str(p.relative_to(self.build_context / "workspace"))
                for p in (self.build_context / "workspace").rglob("*")
                if p.is_file()
            )
            self.host_env = env
            return result(
                args,
                returncode=self.build_code,
                stdout="" if self.build_code == 0 else "build failed",
                stderr="" if self.build_code == 0 else "dependency installation error",
            )
        if args[1] == "run":
            self.run_args = args
            self.host_env = env
            for index, value in enumerate(args):
                if value == "--mount":
                    mount = args[index + 1]
                    self.run_mount = Path(mount.split("src=", 1)[1].split(",", 1)[0])
                    self.run_mount_files = sorted(
                        str(p.relative_to(self.run_mount))
                        for p in self.run_mount.rglob("*")
                        if p.is_file()
                    )
            if self.timeout_run:
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            return result(args, returncode=self.run_code, stdout=self.run_output)
        if args[1] in {"rm", "image"}:
            self.cleanup_calls.append(args)
            return result(args)
        return result(args, returncode=1, stderr="unexpected")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    (project / ".env").write_text(
        "OPENAI_API_KEY=" + "sk-" + "should-not-be-copied\n"
    )
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside\n")
    (project / "symlink").symlink_to(outside)
    return project


def test_docker_run_has_isolation_and_resource_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    fake = DockerFake()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-secret")
    runner = DockerTestRunner(
        docker_path="/usr/bin/docker",
        runner=fake,
        limits=DockerLimits(cpus="0.5", memory="256m", pids=64, timeout_seconds=42),
    )
    result_obj = runner.run(project, profile="pytest")
    assert result_obj.success
    assert result_obj.passed_count == 1

    args = fake.run_args
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in args
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    expected_user = (
        "10001:10001" if os.getuid() == 0 else f"{os.getuid()}:{os.getgid()}"
    )
    assert args[args.index("--user") + 1] == expected_user
    assert not args[args.index("--user") + 1].startswith("0:")
    assert args[args.index("--cpus") + 1] == "0.5"
    assert args[args.index("--memory") + 1] == "256m"
    assert args[args.index("--pids-limit") + 1] == "64"
    assert args[args.index("--tmpfs") + 1].startswith("/tmp:")
    assert not any("docker.sock" in arg for arg in args)
    assert not any("OPENAI_API_KEY" in arg for arg in args)
    assert not any(str(Path.home()) in arg for arg in args)
    assert fake.host_env is not None
    assert "OPENAI_API_KEY" not in fake.host_env
    assert fake.run_mount is not None
    assert fake.run_mount != project
    assert not fake.run_mount.is_relative_to(project)
    assert ".env" not in fake.run_mount_files
    assert ".git/config" not in [path.replace("\\", "/") for path in fake.run_mount_files]
    assert "symlink" not in fake.run_mount_files
    assert "app.py" in fake.run_mount_files
    # The original project is untouched and the temporary copy is gone.
    assert not (project / "agent-write-test.txt").exists()
    assert fake.run_mount is not None and not fake.run_mount.exists()
    assert any(call[1] == "rm" for call in fake.cleanup_calls)
    assert any(call[1:3] == ["image", "rm"] for call in fake.cleanup_calls)
    assert "pytest==9.2.0" in fake.build_dockerfile
    assert "USER 10001:10001" in fake.build_dockerfile


def test_docker_build_failure_cleans_up(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fake = DockerFake(build_code=1)
    runner = DockerTestRunner(docker_path="/usr/bin/docker", runner=fake)
    with pytest.raises(DockerTestError, match="image build failed") as exc:
        runner.run(project)
    assert "dependency installation error" in str(exc.value)
    assert fake.run_args == []
    assert fake.build_context is not None and not fake.build_context.exists()
    assert any(call[1:3] == ["image", "rm"] for call in fake.cleanup_calls)


def test_docker_timeout_kills_container_and_cleans_up(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fake = DockerFake(timeout_run=True)
    runner = DockerTestRunner(
        docker_path="/usr/bin/docker",
        runner=fake,
        limits=DockerLimits(timeout_seconds=3),
    )
    result_obj = runner.run(project)
    assert result_obj.timed_out
    assert not result_obj.success
    assert any(call[1] == "rm" for call in fake.cleanup_calls)
    assert fake.build_context is not None and not fake.build_context.exists()


def test_docker_missing_gives_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coding_agent.docker_tests.shutil.which", lambda _name: None)
    runner = DockerTestRunner(runner=DockerFake())
    project = _project(tmp_path)
    with pytest.raises(DockerUnavailable, match="Docker was not found") as exc:
        runner.run(project)
    assert "brew install --cask docker" in str(exc.value)
    assert "never falls back" in str(exc.value)


def test_docker_daemon_missing_gives_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "coding_agent.docker_tests.shutil.which", lambda _name: "/usr/bin/docker"
    )

    def handler(args, cwd, timeout, env):
        return result(args, returncode=1, stderr="Cannot connect to the Docker daemon")

    runner = DockerTestRunner(runner=FakeCommandRunner(handler))
    with pytest.raises(DockerUnavailable, match="daemon is not available") as exc:
        runner.ensure_available()
    assert "Docker Desktop" in str(exc.value)


def test_failed_tests_are_reported(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fake = DockerFake(run_code=1, run_output=FAIL_OUTPUT)
    runner = DockerTestRunner(docker_path="/usr/bin/docker", runner=fake)
    result_obj = runner.run(project)
    assert not result_obj.success
    assert result_obj.failed_count == 1
    assert "tests/test_app.py::test_add" in result_obj.failed_tests
    assert "1 failed" in result_obj.summary()


def test_no_tests_collected_is_not_success(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fake = DockerFake(run_code=5, run_output="no tests ran in 0.01s\n")
    runner = DockerTestRunner(docker_path="/usr/bin/docker", runner=fake)
    result_obj = runner.run(project)
    assert not result_obj.success
    assert result_obj.passed_count == 0


def test_unsafe_test_paths_rejected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runner = DockerTestRunner(docker_path="/usr/bin/docker", runner=DockerFake())
    with pytest.raises(DockerTestError, match="escapes the project"):
        runner.run(project, test_paths=("../outside.py",))
    with pytest.raises(DockerTestError, match="Unsafe test path"):
        runner.run(project, test_paths=("--collect-only",))


def test_profile_parser() -> None:
    profile = PytestProfile()
    parsed = profile.parse(PASS_OUTPUT, "", 0)
    assert parsed["passed_count"] == 1
    assert parsed["passed_tests"] == ("tests/test_app.py::test_add",)
    parsed = profile.parse(FAIL_OUTPUT, "", 1)
    assert parsed["failed_count"] == 1
