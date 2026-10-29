from __future__ import annotations

import json
from pathlib import Path

from coding_agent.cli import main
from coding_agent.docker_tests import TestResult
from coding_agent.github_ops import IssueDetails
from tests.conftest import (
    FakeFunctionCall,
    FakeOpenAIClient,
    FakeResponse,
    FakeUsage,
    run_git,
)


ISSUE = IssueDetails(
    number=963,
    title="Fix addition",
    body="Addition is wrong.\n\n## Acceptance criteria\n- [ ] add works\n",
    state="OPEN",
    url="https://github.com/acme/demo/issues/963",
    author="alice",
    labels=("bug",),
    comments=(),
    acceptance_criteria=("add works",),
    dependencies=(),
)


class FakeGitHub:
    def __init__(self, *, issue: IssueDetails = ISSUE) -> None:
        self.issue = issue
        self.fetch_calls: list[int] = []
        self.pr_calls: list[dict] = []
        self.check_calls = 0

    def check_available(self) -> None:
        self.check_calls += 1

    def fetch_issue(self, number: int) -> IssueDetails:
        self.fetch_calls.append(number)
        assert number == self.issue.number
        return self.issue

    def create_draft_pr(self, *, branch: str, base: str, title: str, body: str) -> str:
        self.pr_calls.append(
            {"branch": branch, "base": base, "title": title, "body": body}
        )
        return "https://github.com/acme/demo/pull/100"


class FakeDocker:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[tuple[Path, str]] = []

    def run(self, project: Path, *, profile: str = "pytest", test_paths=()) -> TestResult:
        self.calls.append((Path(project), profile))
        if not self.success:
            return TestResult(
                profile=profile,
                command=("python", "-m", "pytest"),
                exit_code=1,
                stdout="1 failed\n",
                stderr="",
                duration_seconds=1.0,
                failed_tests=("tests/test_app.py::test_add",),
                failed_count=1,
            )
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


def plan_response(text: str = "PLAN: change app.py and run tests") -> FakeResponse:
    return FakeResponse(output=[], output_text=text)


def tool_response(name: str, args: dict, call_id: str = "call_1") -> FakeResponse:
    return FakeResponse(
        output=[
            FakeFunctionCall(
                name=name, arguments=json.dumps(args), call_id=call_id
            )
        ],
        output_text="",
    )


def final_response(text: str = "Implementation complete") -> FakeResponse:
    return FakeResponse(output=[], output_text=text)


def make_approver(*, allow_plan=True, allow_write=True, allow_push=True):
    prompts: list[str] = []

    def approve(prompt: str) -> bool:
        prompts.append(prompt)
        if "approve this plan" in prompt:
            return allow_plan
        if "write " in prompt:
            return allow_write
        if "push branch" in prompt:
            return allow_push
        return False

    approve.prompts = prompts  # type: ignore[attr-defined]
    return approve


def _setup_remote(git_repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    run_git(tmp_path, "init", "--bare", str(bare))
    run_git(git_repo, "remote", "add", "origin", str(bare))
    return bare


def _run_main(
    git_repo: Path,
    tmp_path: Path,
    responses: list[FakeResponse],
    *,
    approver=None,
    docker=None,
    github=None,
    extra: list[str] | None = None,
) -> tuple[int, FakeOpenAIClient, FakeDocker, FakeGitHub, list[str]]:
    client = FakeOpenAIClient(responses)
    gh = github or FakeGitHub()
    docker = docker or FakeDocker()
    echo_lines: list[str] = []
    argv = [
        str(git_repo),
        "--issue",
        "963",
        "--worktree-root",
        str(tmp_path / "worktrees"),
        "--cost-log-dir",
        str(tmp_path / "costs"),
        "--model",
        "gpt-5.4",
        "--max-cost",
        "2.00",
        *(extra or []),
    ]
    code = main(
        argv,
        client=client,
        github=gh,
        docker=docker,
        approver=approver or make_approver(),
        echo=echo_lines.append,
        check_docker=False,
    )
    return code, client, docker, gh, echo_lines


def test_full_integration_commit_push_and_draft_pr(
    git_repo: Path, tmp_path: Path
) -> None:
    bare = _setup_remote(git_repo, tmp_path)
    source_head = run_git(git_repo, "rev-parse", "HEAD").stdout.strip()
    responses = [
        plan_response(),
        tool_response(
            "write_file",
            {
                "path": "app.py",
                "content": "def add(a, b):\n    return a + b + 1\n",
            },
        ),
        tool_response(
            "submit_summary",
            {
                "summary": "Correct the addition result.",
                "pr_title": "Fix addition",
                "pr_body": "This fixes the addition bug.\n\nCloses #963",
            },
            call_id="call_2",
        ),
        final_response(),
    ]
    docker = FakeDocker()
    gh = FakeGitHub()
    code, client, docker, gh, _lines = _run_main(
        git_repo, tmp_path, responses, docker=docker, github=gh
    )
    assert code == 0
    assert len(client.responses.calls) == 4
    assert docker.calls
    worktree_path = docker.calls[0][0]
    assert worktree_path != git_repo
    assert not worktree_path.is_relative_to(git_repo)
    assert "return a + b + 1" in (worktree_path / "app.py").read_text()
    # Original branch and working tree are untouched.
    assert run_git(git_repo, "branch", "--show-current").stdout.strip() == "main"
    assert run_git(git_repo, "rev-parse", "HEAD").stdout.strip() == source_head
    assert "return a + b" in (git_repo / "app.py").read_text()
    assert run_git(git_repo, "status", "--porcelain").stdout.strip() == ""
    # Commit exists on the agent branch and was pushed.
    branch = run_git(worktree_path, "branch", "--show-current").stdout.strip()
    assert branch.startswith("agent/issue-963-")
    message = run_git(worktree_path, "log", "-1", "--format=%B").stdout
    assert "Refs #963" in message
    assert "Closes #963" not in message
    assert branch in run_git(bare, "branch", "--list").stdout
    assert gh.pr_calls
    pr = gh.pr_calls[0]
    assert pr["branch"] == branch
    assert pr["base"] == "main"
    assert "Closes #963" not in pr["body"]
    assert "Refs #963" in pr["body"]


def test_plan_denial_creates_no_branch_or_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    approver = make_approver(allow_plan=False)
    code, _client, _docker, _gh, _lines = _run_main(
        git_repo, tmp_path, [plan_response()], approver=approver
    )
    assert code == 0
    assert len(approver.prompts) == 1  # type: ignore[attr-defined]
    assert not (tmp_path / "worktrees").exists()
    assert run_git(git_repo, "branch", "--list").stdout.strip().splitlines() == ["* main"]
    assert run_git(git_repo, "status", "--porcelain").stdout.strip() == ""


def test_push_denial_keeps_commit_but_does_not_push(
    git_repo: Path, tmp_path: Path
) -> None:
    bare = _setup_remote(git_repo, tmp_path)
    responses = [
        plan_response(),
        tool_response(
            "write_file",
            {"path": "app.py", "content": "def add(a, b):\n    return a + b + 0\n"},
        ),
        final_response(),
    ]
    approver = make_approver(allow_push=False)
    code, _client, docker, gh, _lines = _run_main(
        git_repo, tmp_path, responses, approver=approver
    )
    assert code == 0
    worktree_path = docker.calls[0][0]
    assert run_git(worktree_path, "log", "-1", "--format=%s").stdout.strip()
    assert gh.pr_calls == []
    # Only the base branch exists on the remote (no push happened).
    assert "agent/" not in run_git(bare, "branch", "--list").stdout


def test_failing_tests_block_commit(git_repo: Path, tmp_path: Path) -> None:
    responses = [
        plan_response(),
        tool_response(
            "write_file",
            {"path": "app.py", "content": "def add(a, b):\n    return 42\n"},
        ),
        final_response(),
    ]
    docker = FakeDocker(success=False)
    code, _client, docker, gh, _lines = _run_main(
        git_repo, tmp_path, responses, docker=docker
    )
    assert code == 1
    worktree_path = docker.calls[0][0]
    assert (worktree_path / "app.py").read_text().endswith("return 42\n")
    status = run_git(worktree_path, "status", "--porcelain").stdout
    assert "app.py" in status
    assert run_git(worktree_path, "log", "-1", "--format=%s").stdout.strip() == "Initial commit"
    assert gh.pr_calls == []
    assert run_git(git_repo, "branch", "--show-current").stdout.strip() == "main"


def test_dry_run_has_no_side_effects(git_repo: Path, tmp_path: Path) -> None:
    client = FakeOpenAIClient([plan_response("DRY PLAN")])
    gh = FakeGitHub()
    echo_lines: list[str] = []
    called = False

    def approver(_prompt: str) -> bool:
        nonlocal called
        called = True
        return True

    code = main(
        [
            str(git_repo),
            "--issue",
            "963",
            "--dry-run",
            "--worktree-root",
            str(tmp_path / "worktrees"),
            "--cost-log-dir",
            str(tmp_path / "costs"),
            "--model",
            "gpt-5.4",
        ],
        client=client,
        github=gh,
        docker=None,
        approver=approver,
        echo=echo_lines.append,
        check_docker=False,
    )
    assert code == 0
    assert called is False
    assert len(client.responses.calls) == 1
    assert not (tmp_path / "worktrees").exists()
    assert not (tmp_path / "costs").exists()
    assert gh.pr_calls == []
    assert run_git(git_repo, "status", "--porcelain").stdout.strip() == ""
    assert run_git(git_repo, "branch", "--show-current").stdout.strip() == "main"
    assert any("DRY RUN" in line for line in echo_lines)


def test_no_changes_means_no_commit(git_repo: Path, tmp_path: Path) -> None:
    responses = [plan_response(), final_response("No changes needed")]
    code, _client, docker, gh, _lines = _run_main(git_repo, tmp_path, responses)
    assert code == 1
    assert docker.calls == []
    assert gh.pr_calls == []
    worktrees = list((tmp_path / "worktrees").rglob("agent__*"))
    assert worktrees
    assert run_git(worktrees[0], "log", "-1", "--format=%s").stdout.strip() == "Initial commit"


def test_api_error_before_worktree(git_repo: Path, tmp_path: Path) -> None:
    class BrokenResponses:
        def create(self, **kwargs):
            raise RuntimeError("API unavailable")

    class BrokenClient:
        responses = BrokenResponses()

    gh = FakeGitHub()
    code = main(
        [
            str(git_repo),
            "--issue",
            "963",
            "--worktree-root",
            str(tmp_path / "worktrees"),
            "--cost-log-dir",
            str(tmp_path / "costs"),
        ],
        client=BrokenClient(),
        github=gh,
        docker=FakeDocker(),
        approver=make_approver(),
        echo=lambda _line: None,
        check_docker=False,
    )
    assert code == 3
    assert not (tmp_path / "worktrees").exists()
    assert run_git(git_repo, "branch", "--show-current").stdout.strip() == "main"


def test_interrupt_during_write_keeps_worktree_and_no_commit(
    git_repo: Path, tmp_path: Path
) -> None:
    responses = [
        plan_response(),
        tool_response(
            "write_file",
            {"path": "app.py", "content": "def add(a, b):\n    return a + b + 2\n"},
        ),
    ]

    def approver(prompt: str) -> bool:
        if "approve this plan" in prompt:
            return True
        if "write " in prompt:
            raise KeyboardInterrupt
        return False

    code, _client, _docker, _gh, _lines = _run_main(
        git_repo, tmp_path, responses, approver=approver
    )
    assert code == 130
    worktrees = list((tmp_path / "worktrees").rglob("agent__*"))
    assert len(worktrees) == 1
    assert run_git(worktrees[0], "log", "-1", "--format=%s").stdout.strip() == "Initial commit"
    assert run_git(git_repo, "status", "--porcelain").stdout.strip() == ""


def test_budget_exceeded_stops_before_edit(git_repo: Path, tmp_path: Path) -> None:
    expensive = FakeUsage(input_tokens=1_000_000, output_tokens=100_000)
    responses = [
        FakeResponse(output=[], output_text="PLAN", usage=expensive),
        plan_response("should not be reached"),
    ]
    code, client, _docker, _gh, _lines = _run_main(
        git_repo, tmp_path, responses, extra=["--max-cost", "2.00"]
    )
    assert code == 2
    assert len(client.responses.calls) == 1
    worktrees = list((tmp_path / "worktrees").rglob("agent__*"))
    assert len(worktrees) == 1
    assert run_git(worktrees[0], "log", "-1", "--format=%s").stdout.strip() == "Initial commit"


def test_cost_log_inside_target_repo_is_rejected(
    git_repo: Path, tmp_path: Path
) -> None:
    code, _client, _docker, _gh, _lines = _run_main(
        git_repo,
        tmp_path,
        [],
        extra=["--cost-log-dir", str(git_repo / "costs")],
    )
    assert code == 2
    assert not (git_repo / "costs").exists()
    assert run_git(git_repo, "status", "--porcelain").stdout.strip() == ""


def test_unknown_model_fails_before_api(git_repo: Path, tmp_path: Path) -> None:
    code = main(
        [
            str(git_repo),
            "task",
            "--model",
            "not-a-real-model",
            "--cost-log-dir",
            str(tmp_path / "costs"),
        ],
        echo=lambda _line: None,
    )
    assert code == 2
    assert not (tmp_path / "costs").exists()
