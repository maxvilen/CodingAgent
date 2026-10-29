from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from coding_agent.command import CommandResult


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {completed.stderr or completed.stdout}"
        )
    return completed


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.name", "Test User")
    run_git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "Initial commit")
    return repo


@dataclass
class FakeCommandRunner:
    handler: Callable[[list[str], Path | None, float, dict[str, str] | None], CommandResult]
    calls: list[tuple[list[str], Path | None, float, dict[str, str] | None]] = field(
        default_factory=list
    )

    def __call__(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 60,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append((list(args), cwd, timeout, env))
        return self.handler(list(args), cwd, timeout, env)


def result(
    args: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    duration: float = 0.01,
) -> CommandResult:
    return CommandResult(
        args=tuple(args),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
    )


# ---------------------------------------------------------------- fake OpenAI
@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    total_tokens: int = 150
    input_tokens_details: Any = None
    output_tokens_details: Any = None

    def __post_init__(self) -> None:
        if self.input_tokens_details is None:
            self.input_tokens_details = {"cached_tokens": 10}
        if self.output_tokens_details is None:
            self.output_tokens_details = {"reasoning_tokens": 5}


@dataclass
class FakeFunctionCall:
    name: str
    arguments: str
    call_id: str = "call_1"
    type: str = "function_call"


@dataclass
class FakeResponse:
    output: list[Any] = field(default_factory=list)
    output_text: str = ""
    status: str = "completed"
    usage: Any = field(default_factory=FakeUsage)
    incomplete_details: Any = None


class FakeResponsesAPI:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Fake OpenAI client ran out of scripted responses")
        return self.responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = FakeResponsesAPI(responses)
