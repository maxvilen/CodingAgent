from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.errors import GitHubError
from coding_agent.github_ops import (
    GitHubClient,
    extract_acceptance_criteria,
)
from tests.conftest import FakeCommandRunner, result


ISSUE_PAYLOAD = {
    "number": 963,
    "title": "Fix login flow",
    "body": (
        "The login flow fails.\n\n"
        "## Acceptance criteria\n"
        "- [ ] Users can log in\n"
        "- [ ] Errors are shown\n\n"
        "Depends on #42 and PR #7.\n"
        "See https://github.com/acme/demo/issues/12\n"
    ),
    "state": "OPEN",
    "url": "https://github.com/acme/demo/issues/963",
    "author": {"login": "alice"},
    "labels": [{"name": "bug"}, {"name": "priority:high"}],
    "comments": [
        {
            "author": {"login": "bob"},
            "body": "Also see #42. Comment body.",
            "createdAt": "2026-01-01T00:00:00Z",
        }
    ],
}


def _github_handler(args: list[str], cwd: Path | None, timeout: float, env: object):
    if args[0] == "git":
        return result(args, stdout="git@example.com:acme/demo.git")
    assert args[0] == "gh"
    command = args[1:]
    if command == ["auth", "status"]:
        return result(args, stdout="Logged in")
    if command[:2] == ["issue", "view"] and command[2] == "963":
        return result(args, stdout=json.dumps(ISSUE_PAYLOAD))
    if command[:2] == ["issue", "view"] and command[2] == "42":
        return result(
            args,
            stdout=json.dumps(
                {
                    "number": 42,
                    "title": "Auth refactor",
                    "state": "CLOSED",
                    "url": "https://github.com/acme/demo/issues/42",
                }
            ),
        )
    if command[:2] == ["pr", "view"] and command[2] == "7":
        return result(
            args,
            stdout=json.dumps(
                {
                    "number": 7,
                    "title": "Login PR",
                    "state": "OPEN",
                    "url": "https://github.com/acme/demo/pull/7",
                }
            ),
        )
    if command[:2] == ["issue", "view"] and command[2] == "12":
        return result(args, returncode=1, stderr="not found")
    if command[:2] == ["pr", "create"]:
        return result(args, stdout="https://github.com/acme/demo/pull/99\n")
    return result(args, returncode=1, stderr=f"unexpected: {command}")


def test_fetch_issue_includes_comments_labels_criteria_and_dependencies(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setattr(
        "coding_agent.github_ops.shutil.which", lambda _name: "/usr/local/bin/gh"
    )
    runner = FakeCommandRunner(_github_handler)
    client = GitHubClient(git_repo, runner=runner)
    client.check_available()
    issue = client.fetch_issue(963)
    assert issue.number == 963
    assert issue.title == "Fix login flow"
    assert issue.labels == ("bug", "priority:high")
    assert issue.author == "alice"
    assert len(issue.comments) == 1
    assert issue.acceptance_criteria == ("Users can log in", "Errors are shown")
    dependency_titles = {dep.title for dep in issue.dependencies}
    assert "Auth refactor" in dependency_titles
    assert "Login PR" in dependency_titles
    assert any(dep.kind == "pr" for dep in issue.dependencies)
    # All external commands used argument lists, never a shell string.
    for args, _cwd, _timeout, _env in runner.calls:
        assert isinstance(args, list)
    assert any("--json" in args for args, *_ in runner.calls)


def test_missing_gh_gives_installation_instructions(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setattr("coding_agent.github_ops.shutil.which", lambda _name: None)
    client = GitHubClient(git_repo, runner=FakeCommandRunner(_github_handler))
    with pytest.raises(GitHubError, match="not installed") as exc:
        client.check_available()
    assert "brew install gh" in str(exc.value)
    assert "gh auth login" in str(exc.value)


def test_unauthenticated_gh_gives_login_instructions(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setattr(
        "coding_agent.github_ops.shutil.which", lambda _name: "/usr/local/bin/gh"
    )

    def handler(args, cwd, timeout, env):
        if args[1:] == ["auth", "status"]:
            return result(args, returncode=1, stderr="not logged in")
        return result(args, stdout="")

    client = GitHubClient(git_repo, runner=FakeCommandRunner(handler))
    with pytest.raises(GitHubError, match="not authenticated") as exc:
        client.check_available()
    assert "gh auth login" in str(exc.value)


def test_create_draft_pr_uses_safe_args_and_neutralizes_close_keywords(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    monkeypatch.setattr(
        "coding_agent.github_ops.shutil.which", lambda _name: "/usr/local/bin/gh"
    )
    runner = FakeCommandRunner(_github_handler)
    client = GitHubClient(git_repo, runner=runner)
    client.check_available()
    url = client.create_draft_pr(
        branch="agent/issue-963-fix",
        base="main",
        title="Fix login",
        body="Closes #963\nFixes #42",
    )
    assert url.endswith("/pull/99")
    command = runner.calls[-1][0]
    assert command[:3] == ["gh", "pr", "create"]
    assert "--draft" in command
    assert "--title" in command
    assert "--body" in command
    body = command[command.index("--body") + 1]
    assert "Closes #963" not in body
    assert "Fixes #42" not in body
    assert "Refs #963" in body
    assert "Refs #42" in body


def test_unsafe_pr_branch_rejected(git_repo: Path) -> None:
    client = GitHubClient(git_repo)
    with pytest.raises(GitHubError, match="Unsafe branch"):
        client.create_draft_pr(
            branch="agent/-evil", base="main", title="x", body="y"
        )
    with pytest.raises(GitHubError, match="only be created from agent/"):
        client.create_draft_pr(
            branch="feature/evil", base="main", title="x", body="y"
        )


def test_acceptance_criteria_extraction() -> None:
    body = (
        "# Issue\n\n## Acceptanskriterier\n1. First\n2. Second\n\n"
        "## Notes\n- [ ] unchecked anywhere\n"
    )
    assert extract_acceptance_criteria(body) == ("First", "Second")
    assert extract_acceptance_criteria("Intro\n- [ ] only checkbox\n") == (
        "only checkbox",
    )
