"""GitHub integration through the existing ``gh`` CLI authentication."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .command import CommandResult, git_github_env, run_command
from .errors import GitError, GitHubError
from .git_ops import validate_branch_name
from .security import (
    clip,
    neutralize_closing_keywords,
    redact_secrets,
    safe_for_terminal,
)

_LOCAL_REF = re.compile(r"(?<![\w/])#(\d{1,8})\b")
_URL_REF = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/(\d{1,8})"
)
_ACCEPTANCE_HEADING = re.compile(
    r"(?i)\b(acceptance criteria|acceptanskriterier|definition of done|"
    r"krav|requirements)\b"
)
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$")


@dataclass(frozen=True)
class IssueComment:
    author: str
    body: str
    created_at: str = ""


@dataclass(frozen=True)
class Dependency:
    number: int
    kind: str
    title: str
    state: str
    url: str
    error: str = ""


@dataclass(frozen=True)
class IssueDetails:
    number: int
    title: str
    body: str
    state: str
    url: str
    author: str
    labels: tuple[str, ...]
    comments: tuple[IssueComment, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[Dependency, ...]

    def summary_for_prompt(self, limit: int = 24_000) -> str:
        labels = ", ".join(self.labels) if self.labels else "(none)"
        comments = "\n".join(
            f"[{comment.author}] {redact_secrets(comment.body)}"
            for comment in self.comments
        ) or "(none)"
        criteria = "\n".join(
            f"- {redact_secrets(item)}" for item in self.acceptance_criteria
        ) or "(none extracted)"
        deps = (
            "\n".join(
                f"- {dep.kind} #{dep.number}: {redact_secrets(dep.title)} "
                f"[{dep.state}]"
                + (f" (fetch error: {dep.error})" if dep.error else "")
                for dep in self.dependencies
            )
            or "(none detected)"
        )
        text = (
            f"Issue #{self.number}: {redact_secrets(self.title)}\n"
            f"State: {self.state}\n"
            f"URL: {self.url}\n"
            f"Author: {self.author}\n"
            f"Labels: {labels}\n\n"
            f"Body:\n{redact_secrets(self.body or '(empty)')}\n\n"
            f"Comments:\n{comments}\n\n"
            f"Acceptance criteria (extracted, may be incomplete):\n{criteria}\n\n"
            f"Detected issue/PR dependencies:\n{deps}\n"
        )
        return clip(text, limit)


def _run_failure(result: CommandResult) -> str:
    output = (result.stderr or result.stdout or "").strip()
    return safe_for_terminal(redact_secrets(output), 3_000)


class GitHubClient:
    """Thin, testable wrapper around `gh`. Never passes a shell."""

    def __init__(
        self,
        repo: Path | str,
        *,
        runner: Callable[..., CommandResult] = run_command,
        gh_executable: str | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self._runner = runner
        self._gh = gh_executable or "gh"
        self._validated = False

    def _run(self, args: list[str], timeout: float = 60) -> CommandResult:
        return self._runner(
            [self._gh, *args],
            cwd=self.repo,
            timeout=timeout,
            env=git_github_env(),
        )

    def check_available(self) -> None:
        """Check gh installation, authentication and a usable remote."""

        if shutil.which(self._gh) is None:
            raise GitHubError(
                "GitHub CLI (gh) is not installed. Install it with:\n"
                "  macOS:  brew install gh\n"
                "  Linux:  https://github.com/cli/cli#installation\n"
                "Then authenticate with: gh auth login\n"
                "The agent never asks for or stores a GitHub token itself."
            )
        auth = self._run(["auth", "status"], timeout=30)
        if not auth.ok:
            raise GitHubError(
                "GitHub CLI is not authenticated. Run:\n"
                "  gh auth login\n"
                "The agent reuses your existing gh authentication.",
                hint=safe_for_terminal(redact_secrets(auth.stderr or ""), 1_000),
            )
        remote = self._runner(
            ["git", "-C", str(self.repo), "remote", "get-url", "origin"],
            cwd=self.repo,
            timeout=30,
            env=git_github_env(),
        )
        if not remote.ok:
            raise GitHubError(
                "No Git remote named 'origin' is configured. The agent needs "
                "a GitHub remote to fetch issues and create pull requests."
            )
        self._validated = True

    def fetch_issue(self, number: int, *, max_dependencies: int = 6) -> IssueDetails:
        if not isinstance(number, int) or number < 1:
            raise GitHubError("Issue number must be a positive integer")
        if shutil.which(self._gh) is None:
            self.check_available()  # raises the installation instructions
        result = self._run(
            [
                "issue",
                "view",
                str(number),
                "--json",
                "number,title,body,state,labels,comments,url,author,createdAt,updatedAt",
            ],
            timeout=60,
        )
        if not result.ok:
            raise GitHubError(
                f"Could not fetch issue #{number}: {_run_failure(result)}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(
                f"GitHub returned invalid JSON for issue #{number}"
            ) from exc
        return self._parse_issue(payload, number, max_dependencies=max_dependencies)

    def _parse_issue(
        self, payload: dict[str, Any], fallback_number: int, *, max_dependencies: int
    ) -> IssueDetails:
        try:
            number = int(payload.get("number") or fallback_number)
        except (TypeError, ValueError) as exc:
            raise GitHubError("GitHub issue payload has an invalid number") from exc
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "")
        state = str(payload.get("state") or "UNKNOWN")
        url = str(payload.get("url") or "")
        author = _author_login(payload.get("author"))
        labels = tuple(
            str(label.get("name") if isinstance(label, dict) else label)
            for label in (payload.get("labels") or [])
            if label
        )
        comments = tuple(
            IssueComment(
                author=_author_login(comment.get("author")),
                body=str(comment.get("body") or ""),
                created_at=str(comment.get("createdAt") or ""),
            )
            for comment in (payload.get("comments") or [])
            if isinstance(comment, dict)
        )
        acceptance_items: list[str] = list(extract_acceptance_criteria(body))
        for comment in comments:
            for item in extract_acceptance_criteria(comment.body):
                if item not in acceptance_items:
                    acceptance_items.append(item)
        acceptance = tuple(acceptance_items[:40])
        dependencies = self._find_dependencies(
            number, body, comments, max_dependencies=max_dependencies
        )
        return IssueDetails(
            number=number,
            title=title,
            body=body,
            state=state,
            url=url,
            author=author,
            labels=labels,
            comments=comments,
            acceptance_criteria=acceptance,
            dependencies=dependencies,
        )

    def _find_dependencies(
        self,
        own_number: int,
        body: str,
        comments: tuple[IssueComment, ...],
        *,
        max_dependencies: int,
    ) -> tuple[Dependency, ...]:
        text = body + "\n" + "\n".join(c.body for c in comments)
        found: list[tuple[str, str, int]] = []  # (repo-or-empty, kind, number)
        seen: set[tuple[str, str, int]] = set()
        for match in _URL_REF.finditer(text):
            repo, kind, number = match.group(1), match.group(2), int(match.group(3))
            key = (repo, "pr" if kind == "pull" else "issue", number)
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
            if len(found) >= max_dependencies:
                break
        for match in _LOCAL_REF.finditer(text):
            number = int(match.group(1))
            if number == own_number:
                continue
            key = ("", "unknown", number)
            if any(k[2] == number and not k[0] for k in seen):
                continue
            seen.add(key)
            found.append(key)
            if len(found) >= max_dependencies:
                break

        dependencies: list[Dependency] = []
        for repo, kind, number in found:
            target = f"{repo}#{number}" if repo else f"#{number}"
            if kind == "unknown":
                dep = self._fetch_dependency(target, number, "issue")
                if dep.error:
                    dep = self._fetch_dependency(target, number, "pr")
            else:
                dep = self._fetch_dependency(target, number, kind)
            dependencies.append(dep)
        return tuple(dependencies)

    def _fetch_dependency(self, target: str, number: int, kind: str) -> Dependency:
        command = "pr" if kind == "pr" else "issue"
        display_target = target if target.startswith("#") else target
        arg = display_target.lstrip("#") if target.startswith("#") else target
        result = self._run(
            [command, "view", arg, "--json", "number,title,state,url"], timeout=45
        )
        if not result.ok:
            return Dependency(
                number=number,
                kind=kind,
                title="",
                state="UNKNOWN",
                url="",
                error=_run_failure(result)[:300],
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return Dependency(
                number=number,
                kind=kind,
                title="",
                state="UNKNOWN",
                url="",
                error="invalid JSON",
            )
        return Dependency(
            number=int(payload.get("number") or number),
            kind=kind,
            title=str(payload.get("title") or ""),
            state=str(payload.get("state") or "UNKNOWN"),
            url=str(payload.get("url") or ""),
        )

    def create_draft_pr(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        if not branch.startswith("agent/"):
            raise GitHubError(
                "Draft PRs may only be created from agent/ branches"
            )
        try:
            validate_branch_name(branch)
        except GitError as exc:
            raise GitHubError(str(exc)) from exc
        if (
            not base
            or base.startswith("-")
            or any(ch.isspace() for ch in base)
            or ".." in base
        ):
            raise GitHubError(f"Unsafe base branch name: {base!r}")
        if not self._validated:
            self.check_available()
        title = safe_for_terminal(
            neutralize_closing_keywords(title), 120
        ).replace("\n", " ").strip()
        if not title:
            raise GitHubError("Pull request title cannot be empty")
        body = safe_for_terminal(neutralize_closing_keywords(body), 20_000)
        result = self._run(
            [
                "pr",
                "create",
                "--draft",
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ],
            timeout=120,
        )
        if not result.ok:
            raise GitHubError(
                f"Could not create draft PR: {_run_failure(result)}"
            )
        for line in reversed(result.stdout.splitlines()):
            candidate = line.strip()
            if candidate.startswith("https://github.com/"):
                return candidate
        return safe_for_terminal(result.stdout.strip(), 1_000)


def _author_login(author: Any) -> str:
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return str(author or "")


def extract_acceptance_criteria(body: str, limit: int = 40) -> tuple[str, ...]:
    """Best-effort extraction of acceptance criteria from Markdown.

    The result is a reading aid for the human and the model, not an
    authoritative contract. Issue text remains untrusted input.
    """

    lines = body.splitlines()
    criteria: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            in_section = bool(_ACCEPTANCE_HEADING.search(heading))
            continue
        if in_section:
            if not stripped:
                continue
            if not re.match(r"^(?:[-*+]|\d+[.)])\s+", stripped):
                continue
            cleaned = re.sub(r"^[-*+]\s*(?:\[[ xX]\]\s*)?", "", stripped)
            cleaned = re.sub(r"^\d+[.)]\s*", "", cleaned)
            if cleaned:
                criteria.append(cleaned[:800])
                if len(criteria) >= limit:
                    break
    if not criteria:
        for line in lines:
            match = _CHECKBOX.match(line)
            if match:
                criteria.append(match.group(1).strip()[:800])
                if len(criteria) >= limit:
                    break
    return tuple(criteria)


__all__ = [
    "Dependency",
    "GitHubClient",
    "IssueComment",
    "IssueDetails",
    "extract_acceptance_criteria",
]
