"""Isolated test execution in Docker.

Only a sanitized, writable copy of the project is mounted. The container runs
as an unprivileged user, without network, with CPU/memory/PID/time limits, and
without the host home directory, Docker socket, SSH keys or API keys.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .command import CommandResult, run_command, sanitized_subprocess_env
from .errors import DockerTestError, DockerUnavailable
from .security import (
    clip,
    is_sensitive_path,
    is_within,
    redact_secrets,
    safe_for_terminal,
)

DOCKER_INSTALL_HINT = (
    "Install Docker and start the Docker daemon:\n"
    "  macOS:  brew install --cask docker  (then open Docker Desktop)\n"
    "          or install Docker Desktop from https://www.docker.com/products/docker-desktop/\n"
    "  Linux:  https://docs.docker.com/engine/install/\n"
    "Verify with: docker info\n"
    "The agent never falls back to running tests directly on the host."
)

_PYTEST_LINE = re.compile(
    r"^(\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_SUMMARY = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TestResult:
    __test__ = False

    profile: str
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    passed_tests: tuple[str, ...] = ()
    failed_tests: tuple[str, ...] = ()
    skipped_tests: tuple[str, ...] = ()
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    timed_out: bool = False
    image: str = ""

    @property
    def success(self) -> bool:
        return (
            not self.timed_out
            and self.exit_code == 0
            and self.failed_count == 0
            and (self.passed_count > 0 or bool(self.passed_tests))
        )

    def summary(self) -> str:
        if self.timed_out:
            return (
                f"Docker tests timed out after {self.duration_seconds:.1f}s "
                f"(profile {self.profile}, exit code {self.exit_code})"
            )
        return (
            f"Tests: {self.passed_count} passed, {self.failed_count} failed, "
            f"{self.skipped_count} skipped (exit code {self.exit_code}, "
            f"{self.duration_seconds:.1f}s)"
        )


class TestProfile:
    """Extensible test-profile contract.

    Add future React/TypeScript support by implementing a profile and
    registering it with :class:`DockerTestRunner`.
    """

    name = "generic"
    base_image = "python:3.12-slim"

    def dockerfile_setup(self) -> str:
        raise NotImplementedError

    def command(self, test_paths: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, exit_code: int) -> dict[str, object]:
        raise NotImplementedError


class PytestProfile(TestProfile):
    name = "pytest"
    base_image = "python:3.12-slim"

    def dockerfile_setup(self) -> str:
        return r"""ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "pytest==9.2.0"
WORKDIR /build
COPY . /build
RUN set -eu; \
    if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi; \
    if [ -f requirements-dev.txt ]; then pip install --no-cache-dir -r requirements-dev.txt; fi; \
    if [ -f pyproject.toml ] || [ -f setup.py ]; then pip install --no-cache-dir .; fi
RUN groupadd --gid 10001 agent && \
    useradd --uid 10001 --gid 10001 --home-dir /home/agent \
    --create-home --shell /usr/sbin/nologin agent
WORKDIR /workspace
USER 10001:10001
"""

    def command(self, test_paths: Sequence[str]) -> list[str]:
        return [
            "python",
            "-m",
            "pytest",
            "-v",
            "-rA",
            "--color=no",
            "-p",
            "no:cacheprovider",
            *test_paths,
        ]

    def parse(
        self, stdout: str, stderr: str, exit_code: int
    ) -> dict[str, object]:
        passed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        seen: set[tuple[str, str]] = set()
        for line in (stdout + "\n" + stderr).splitlines():
            match = _PYTEST_LINE.match(line.strip())
            if not match:
                continue
            name, status = match.group(1), match.group(2)
            key = (name, status)
            if key in seen:
                continue
            seen.add(key)
            if status in {"PASSED", "XPASS"}:
                passed.append(name)
            elif status in {"FAILED", "ERROR", "XFAIL"}:
                failed.append(name)
            else:
                skipped.append(name)
        counts = {
            "passed": len(passed),
            "failed": len(failed),
            "skipped": len(skipped),
        }
        for match in _SUMMARY.finditer(stdout + "\n" + stderr):
            count = int(match.group(1))
            label = match.group(2).lower()
            if label == "passed":
                counts["passed"] = max(counts["passed"], count)
            elif label in {"failed", "error", "errors"}:
                counts["failed"] = max(counts["failed"], count)
            elif label == "skipped":
                counts["skipped"] = max(counts["skipped"], count)
        return {
            "passed_tests": tuple(passed),
            "failed_tests": tuple(failed),
            "skipped_tests": tuple(skipped),
            "passed_count": counts["passed"],
            "failed_count": counts["failed"],
            "skipped_count": counts["skipped"],
        }


DEFAULT_PROFILES: dict[str, TestProfile] = {"pytest": PytestProfile()}


@dataclass(frozen=True)
class DockerLimits:
    cpus: str = "1.0"
    memory: str = "512m"
    pids: int = 128
    timeout_seconds: int = 300
    build_timeout_seconds: int = 600


class DockerTestRunner:
    def __init__(
        self,
        *,
        docker_path: str | None = None,
        runner: Callable[..., CommandResult] = run_command,
        limits: DockerLimits | None = None,
        profiles: dict[str, TestProfile] | None = None,
    ) -> None:
        self.docker_path = docker_path
        self._runner = runner
        self.limits = limits or DockerLimits()
        self.profiles = dict(profiles or DEFAULT_PROFILES)

    @staticmethod
    def _container_user() -> str:
        """Use the host's non-root uid so bind-mounted test files stay removable.

        If the host process is root, fall back to the image's unprivileged
        account (uid/gid 10001).
        """

        try:
            uid, gid = os.getuid(), os.getgid()
        except AttributeError:  # pragma: no cover - non-POSIX host
            return "10001:10001"
        if uid == 0:
            return "10001:10001"
        return f"{uid}:{gid}"

    def _docker(
        self, args: Sequence[str], *, timeout: float
    ) -> CommandResult:
        if not self.docker_path:
            raise DockerUnavailable(DOCKER_INSTALL_HINT)
        return self._runner(
            [self.docker_path, *args],
            cwd=None,
            timeout=timeout,
            env=sanitized_subprocess_env(),
        )

    def ensure_available(self) -> None:
        if self.docker_path is None:
            self.docker_path = shutil.which("docker")
        if not self.docker_path:
            raise DockerUnavailable(
                "Docker was not found on PATH. " + DOCKER_INSTALL_HINT
            )
        try:
            result = self._docker(["info"], timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise DockerUnavailable(
                "Docker did not respond within 30 seconds. " + DOCKER_INSTALL_HINT
            ) from exc
        if not result.ok:
            detail = safe_for_terminal(
                redact_secrets(result.stderr or result.stdout), 1_500
            )
            raise DockerUnavailable(
                "Docker is installed but the daemon is not available. "
                + DOCKER_INSTALL_HINT
                + (f"\n\ndocker info said:\n{detail}" if detail else "")
            )

    def _copy_project(self, source: Path, destination: Path) -> None:
        source = source.resolve()
        if not source.is_dir():
            raise DockerTestError(f"Test project directory does not exist: {source}")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            relative_root = root_path.relative_to(source)
            kept_dirs: list[str] = []
            for dirname in dirs:
                rel = relative_root / dirname
                if is_sensitive_path(rel) or dirname == ".env" or dirname.startswith(".env."):
                    continue
                if (root_path / dirname).is_symlink():
                    continue
                target = destination / rel
                target.mkdir(parents=True, exist_ok=True)
                kept_dirs.append(dirname)
            dirs[:] = kept_dirs
            for filename in files:
                rel = relative_root / filename
                if (
                    is_sensitive_path(rel)
                    or filename == ".env"
                    or filename.startswith(".env.")
                ):
                    continue
                src = root_path / filename
                if src.is_symlink() or not src.is_file():
                    continue
                target = destination / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, target)
                except OSError as exc:
                    raise DockerTestError(
                        f"Could not copy project file {rel} into the test copy: {exc}"
                    ) from exc
        # The copy is ephemeral and may be written by the container user.
        # Never chmod the original repository.
        for root, _dirs, files in os.walk(destination):
            os.chmod(root, os.stat(root).st_mode | 0o777)
            for filename in files:
                path = Path(root) / filename
                os.chmod(path, os.stat(path).st_mode | 0o666)

    def _write_dockerfile(self, context: Path, profile: TestProfile) -> Path:
        dockerfile = context / "Dockerfile.ai-coding-agent"
        content = (
            f"FROM {profile.base_image}\n"
            f"{profile.dockerfile_setup()}\n"
            "LABEL ai-coding-agent.test-container=\"true\"\n"
        )
        dockerfile.write_text(content, encoding="utf-8")
        return dockerfile

    def _validate_test_paths(
        self, project_copy: Path, test_paths: Sequence[str]
    ) -> list[str]:
        if len(test_paths) > 20:
            raise DockerTestError("Too many test paths (maximum 20)")
        validated: list[str] = []
        for raw in test_paths:
            value = str(raw).strip()
            if not value or value.startswith("-") or Path(value).is_absolute():
                raise DockerTestError(f"Unsafe test path: {raw!r}")
            base = value.split("::", 1)[0]
            if not base or base in {".", ".."}:
                raise DockerTestError(f"Unsafe test path: {raw!r}")
            if ".." in Path(base).parts:
                raise DockerTestError(f"Test path escapes the project: {raw!r}")
            candidate = (project_copy / base).resolve()
            if not is_within(project_copy, candidate):
                raise DockerTestError(f"Test path escapes the project: {raw!r}")
            if not candidate.exists():
                raise DockerTestError(f"Test path does not exist: {raw!r}")
            if is_sensitive_path(base):
                raise DockerTestError(f"Refusing to access sensitive test path: {raw!r}")
            validated.append(value)
        return validated

    def run(
        self,
        project_dir: Path | str,
        *,
        profile: str = "pytest",
        test_paths: Sequence[str] = (),
    ) -> TestResult:
        profile_impl = self.profiles.get(profile)
        if profile_impl is None:
            raise DockerTestError(
                f"Unknown test profile '{profile}'. Available: "
                + ", ".join(sorted(self.profiles))
            )
        if self.docker_path is None:
            self.docker_path = shutil.which("docker")
        if not self.docker_path:
            raise DockerUnavailable(
                "Docker was not found on PATH. " + DOCKER_INSTALL_HINT
            )

        source = Path(project_dir).expanduser().resolve()
        context = Path(tempfile.mkdtemp(prefix="ai-coding-agent-test-"))
        image = f"ai-coding-agent-test:{os.getpid()}-{uuid.uuid4().hex[:10]}"
        container = f"ai-coding-agent-test-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        try:
            project_copy = context / "workspace"
            self._copy_project(source, project_copy)
            paths = self._validate_test_paths(project_copy, test_paths)
            dockerfile = self._write_dockerfile(context, profile_impl)

            try:
                build = self._docker(
                    [
                        "build",
                        "--tag",
                        image,
                        "--file",
                        str(dockerfile),
                        str(context),
                    ],
                    timeout=self.limits.build_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise DockerTestError(
                    "Docker image build timed out after "
                    f"{self.limits.build_timeout_seconds}s"
                ) from exc
            if not build.ok:
                detail = safe_for_terminal(
                    redact_secrets((build.stdout + "\n" + build.stderr)[-8_000:]),
                    8_000,
                )
                raise DockerTestError(
                    f"Docker image build failed for profile '{profile}'.\n{detail}"
                )

            container_args = [
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                "--cpus",
                self.limits.cpus,
                "--memory",
                self.limits.memory,
                "--memory-swap",
                self.limits.memory,
                "--pids-limit",
                str(self.limits.pids),
                "--ulimit",
                "nofile=256:256",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=128m",
                "--mount",
                f"type=bind,src={project_copy},dst=/workspace",
                "--workdir",
                "/workspace",
                "--user",
                self._container_user(),
                "--env",
                "HOME=/tmp",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONPYCACHEPREFIX=/tmp/pycache",
                "--env",
                "TMPDIR=/tmp",
                "--env",
                "CI=1",
                image,
                *profile_impl.command(paths),
            ]
            timed_out = False
            try:
                result = self._docker(
                    container_args, timeout=self.limits.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                self._cleanup_container(container)
                result = CommandResult(
                    args=tuple(container_args),
                    returncode=124,
                    stdout="",
                    stderr=(
                        f"Test container exceeded the {self.limits.timeout_seconds}s "
                        "time limit and was terminated."
                    ),
                    duration_seconds=time.monotonic() - started,
                )

            parsed = profile_impl.parse(result.stdout, result.stderr, result.returncode)
            duration = time.monotonic() - started
            return TestResult(
                profile=profile,
                command=tuple(profile_impl.command(paths)),
                exit_code=result.returncode,
                stdout=clip(safe_for_terminal(result.stdout, 500_000), 500_000),
                stderr=clip(safe_for_terminal(result.stderr, 500_000), 500_000),
                duration_seconds=duration,
                passed_tests=parsed["passed_tests"],  # type: ignore[arg-type]
                failed_tests=parsed["failed_tests"],  # type: ignore[arg-type]
                skipped_tests=parsed["skipped_tests"],  # type: ignore[arg-type]
                passed_count=int(parsed["passed_count"]),
                failed_count=int(parsed["failed_count"]),
                skipped_count=int(parsed["skipped_count"]),
                timed_out=timed_out,
                image=image,
            )
        finally:
            self._cleanup_container(container)
            self._cleanup_image(image)
            shutil.rmtree(context, ignore_errors=True)

    def _cleanup_container(self, container: str) -> None:
        try:
            self._docker(["rm", "--force", container], timeout=30)
        except Exception:
            pass

    def _cleanup_image(self, image: str) -> None:
        try:
            self._docker(["image", "rm", "--force", image], timeout=60)
        except Exception:
            pass


__all__ = [
    "DOCKER_INSTALL_HINT",
    "DockerLimits",
    "DockerTestRunner",
    "PytestProfile",
    "TestProfile",
    "TestResult",
]
