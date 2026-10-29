from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.docker_tests import TestResult
from coding_agent.errors import AgentError, SecretDetected
from coding_agent.git_ops import GitRepo
from coding_agent.tools import RepositoryTools
from tests.conftest import run_git


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, tuple[str, ...]]] = []

    def run(self, project, *, profile="pytest", test_paths=()):
        self.calls.append((Path(project), profile, tuple(test_paths)))
        return TestResult(
            profile=profile,
            command=("python", "-m", "pytest"),
            exit_code=0,
            stdout="1 passed\n",
            stderr="",
            duration_seconds=1.0,
            passed_tests=("tests/test_app.py::test_add",),
            passed_count=1,
        )


def _edit_tools(git_repo: Path, tmp_path: Path, approver, docker=None):
    repo = GitRepo(git_repo)
    info = repo.create_worktree(
        title="Tool test", issue=11, worktree_root=tmp_path / "worktrees"
    )
    tools = RepositoryTools(
        info.path,
        phase="edit",
        docker=docker,
        approver=approver,
        echo=lambda _text: None,
    )
    return repo, info, tools


def test_read_file_blocks_sensitive_and_traversal(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / ".env").write_text("OPENAI_API_KEY=sk-nope\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (git_repo / "link").symlink_to(outside)
    tools = RepositoryTools(git_repo, phase="analysis", echo=lambda _t: None)
    assert "def add" in tools.read_file({"path": "app.py"})
    for path in [".env", "../outside.txt", "link", ".git/config", "bad\x1b[31mname"]:
        with pytest.raises(AgentError):
            tools.read_file({"path": path})


def test_binary_files_are_blocked(git_repo: Path) -> None:
    (git_repo / "binary.bin").write_bytes(b"\x00\x01\x02")
    tools = RepositoryTools(git_repo, phase="analysis", echo=lambda _t: None)
    assert tools.read_file({"path": "binary.bin"}) == "Binary file blocked"


def test_list_files_and_search_filter_secrets(git_repo: Path) -> None:
    (git_repo / ".env").write_text("OPENAI_API_KEY=sk-nope\n")
    token = "sk-" + "z" * 48
    (git_repo / "config.py").write_text(f'TOKEN = "{token}"\n')
    run_git(git_repo, "add", "config.py")
    run_git(git_repo, "commit", "-m", "Add config")
    tools = RepositoryTools(git_repo, phase="analysis", echo=lambda _t: None)
    listed = tools.list_files({"filter": ""})
    assert "app.py" in listed
    assert ".env" not in listed
    matches = tools.search_code({"query": token})
    assert token not in matches
    assert "[REDACTED:openai-key]" in matches


def test_write_requires_approval_and_only_touches_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    prompts: list[str] = []

    def deny(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    _, info, tools = _edit_tools(git_repo, tmp_path, deny)
    result = tools.write_file({"path": "app.py", "content": "changed\n"})
    assert "Denied" in result
    assert (info.path / "app.py").read_text().startswith("def add")
    assert tools.touched_files == set()
    assert prompts


def test_approved_write_tracks_file(git_repo: Path, tmp_path: Path) -> None:
    prompts: list[str] = []

    def approve(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    _, info, tools = _edit_tools(git_repo, tmp_path, approve)
    result = tools.write_file(
        {"path": "src/new_file.py", "content": "VALUE = 1\n"}
    )
    assert "Wrote src/new_file.py" in result
    assert (info.path / "src" / "new_file.py").read_text() == "VALUE = 1\n"
    assert tools.touched_files == {"src/new_file.py"}
    assert any("write src/new_file.py" in prompt for prompt in prompts)
    assert not (git_repo / "src" / "new_file.py").exists()


def test_write_blocks_secret_content_and_traversal(
    git_repo: Path, tmp_path: Path
) -> None:
    _, info, tools = _edit_tools(git_repo, tmp_path, lambda _p: True)
    with pytest.raises(SecretDetected):
        tools.write_file(
            {"path": "config.py", "content": 'KEY = "sk-' + "q" * 48 + '"\n'}
        )
    with pytest.raises(AgentError, match="escapes the worktree"):
        tools.write_file({"path": "../escape.py", "content": "x = 1\n"})
    assert not (info.path / "config.py").exists()
    assert tools.touched_files == set()


def test_analysis_phase_has_no_write_tool(git_repo: Path) -> None:
    tools = RepositoryTools(git_repo, phase="analysis", echo=lambda _t: None)
    with pytest.raises(AgentError, match="Unknown or disallowed"):
        tools.execute("write_file", {"path": "app.py", "content": "x"})


def test_run_tests_uses_docker_copy_and_remembers_result(
    git_repo: Path, tmp_path: Path
) -> None:
    docker = FakeDocker()
    _, info, tools = _edit_tools(git_repo, tmp_path, lambda _p: True, docker=docker)
    output = tools.run_tests({"profile": "pytest", "test_paths": ["tests/test_app.py"]})
    assert "1 passed" in output
    assert docker.calls == [
        (info.path, "pytest", ("tests/test_app.py",))
    ]
    assert tools.last_test_result is not None


def test_submit_summary(git_repo: Path, tmp_path: Path) -> None:
    _, _, tools = _edit_tools(git_repo, tmp_path, lambda _p: True)
    output = tools.submit_summary(
        {
            "summary": "Changed login",
            "pr_title": "Fix login",
            "pr_body": "Body",
        }
    )
    assert "recorded" in output.lower()
    assert tools.summary is not None
    assert tools.summary.pr_title == "Fix login"


def test_git_status_read_only(git_repo: Path) -> None:
    tools = RepositoryTools(git_repo, phase="analysis", echo=lambda _t: None)
    output = tools.git_status({})
    assert "main" in output
