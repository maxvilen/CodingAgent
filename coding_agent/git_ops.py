"""Safe Git and worktree operations.

The source working tree is never checked out to an agent branch and never
committed to. All writes happen in a separate ``git worktree``.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .command import CommandResult, git_github_env, run_command
from .errors import GitError, SecretDetected
from .security import (
    is_sensitive_path,
    is_within,
    neutralize_closing_keywords,
    redact_secrets,
    safe_for_terminal,
    sanitize_remote_url,
    slugify,
)

DEFAULT_PROTECTED_BRANCHES = frozenset({"main", "master"})
_BRANCH_RE = re.compile(r"^agent/[a-z0-9][a-z0-9._/-]{0,108}$")
_BAD_BRANCH_PARTS = ("..", "//", "@{", "\\")


def default_state_dir() -> Path:
    configured = os.environ.get("AI_CODING_AGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "ai-coding-agent"


def default_worktree_root() -> Path:
    return default_state_dir() / "worktrees"


def _safe_error(result: CommandResult) -> str:
    message = (result.stderr or result.stdout or "").strip()
    return safe_for_terminal(redact_secrets(message), 4_000)


@dataclass(frozen=True)
class WorktreeInfo:
    source: Path
    path: Path
    branch: str
    base_ref: str
    base_branch: str
    source_branch: str
    source_head: str
    source_dirty: tuple[str, ...] = ()

    @property
    def had_dirty_source(self) -> bool:
        return bool(self.source_dirty)


@dataclass(frozen=True)
class CommitInfo:
    hash: str
    short_hash: str
    subject: str
    files: tuple[str, ...]
    stat: str
    branch: str


@dataclass(frozen=True)
class PushInfo:
    branch: str
    remote: str
    output: str


def validate_branch_name(name: str) -> str:
    if (
        not name
        or len(name) > 120
        or not _BRANCH_RE.match(name)
        or any(part in name for part in _BAD_BRANCH_PARTS)
        or name.endswith("/")
        or name.endswith(".")
        or name.endswith(".lock")
        or any(part.startswith(".") for part in name.split("/"))
    ):
        raise GitError(
            "Unsafe branch name. Agent branches must look like "
            "'agent/issue-963-fix' and contain only lowercase letters, digits, "
            "'-', '_', '.' and '/'."
        )
    return name


def make_branch_name(title: str, *, issue: int | None = None) -> str:
    slug = slugify(title or "task", max_length=48)
    if issue is not None:
        name = f"agent/issue-{int(issue)}-{slug}"
    else:
        name = f"agent/task-{slug}"
    return validate_branch_name(name[:120].rstrip("-."))


class GitRepo:
    def __init__(self, path: Path | str) -> None:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_dir():
            raise GitError(f"Repository path is not a directory: {candidate}")
        result = self._run_at(candidate, ["rev-parse", "--is-inside-work-tree"])
        if not result.ok or result.stdout.strip() != "true":
            raise GitError(f"Not a valid Git working tree: {candidate}")
        root_result = self._run_at(candidate, ["rev-parse", "--show-toplevel"])
        if not root_result.ok:
            raise GitError(f"Could not determine Git repository root: {candidate}")
        self.path = Path(root_result.stdout.strip()).resolve()
        self.name = self.path.name or "repository"
        self._protected = set(DEFAULT_PROTECTED_BRANCHES)

    @staticmethod
    def _run_at(path: Path, args: Sequence[str], timeout: float = 60) -> CommandResult:
        try:
            return run_command(
                [
                    "git",
                    "-C",
                    str(path),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    *args,
                ],
                cwd=path,
                timeout=timeout,
                env=git_github_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"Git command timed out: git {' '.join(args)}") from exc

    def _run(self, args: Sequence[str], timeout: float = 60) -> CommandResult:
        return self._run_at(self.path, args, timeout=timeout)

    def _checked(self, args: Sequence[str], timeout: float = 60) -> str:
        result = self._run(args, timeout=timeout)
        if not result.ok:
            raise GitError(f"Git command failed: {_safe_error(result)}")
        return result.stdout

    # ------------------------------------------------------------------ state
    def current_branch(self, cwd: Path | None = None) -> str:
        result = self._run_at(cwd or self.path, ["branch", "--show-current"])
        if not result.ok:
            raise GitError(f"Could not read current branch: {_safe_error(result)}")
        return result.stdout.strip() or "HEAD"

    def head(self, cwd: Path | None = None) -> str:
        return self._checked_at(cwd or self.path, ["rev-parse", "HEAD"]).strip()

    def _checked_at(self, cwd: Path, args: Sequence[str]) -> str:
        result = self._run_at(cwd, args)
        if not result.ok:
            raise GitError(f"Git command failed: {_safe_error(result)}")
        return result.stdout

    def dirty_files(self, cwd: Path | None = None) -> tuple[str, ...]:
        result = self._run_at(cwd or self.path, ["status", "--porcelain"])
        if not result.ok:
            raise GitError(f"Could not inspect Git status: {_safe_error(result)}")
        return tuple(line for line in result.stdout.splitlines() if line.strip())

    def status_short(self, cwd: Path | None = None) -> str:
        target = cwd or self.path
        result = self._run_at(
            target, ["status", "--short", "--branch"]
        )
        if not result.ok:
            raise GitError(f"Could not inspect Git status: {_safe_error(result)}")
        return safe_for_terminal(redact_secrets(result.stdout), 8_000)

    def diff_stat(self, cwd: Path | None = None) -> str:
        target = cwd or self.path
        result = self._run_at(target, ["diff", "--no-ext-diff", "--stat"])
        if not result.ok:
            raise GitError(f"Could not inspect diff: {_safe_error(result)}")
        return safe_for_terminal(redact_secrets(result.stdout), 8_000)

    def diff_preview(self, cwd: Path | None = None, limit: int = 20_000) -> str:
        target = cwd or self.path
        result = self._run_at(
            target, ["diff", "--no-ext-diff", "--text"]
        )
        if not result.ok:
            raise GitError(f"Could not inspect diff: {_safe_error(result)}")
        return safe_for_terminal(redact_secrets(result.stdout), limit)

    def changed_files(self, cwd: Path | None = None) -> tuple[str, ...]:
        target = cwd or self.path
        result = self._run_at(
            target, ["status", "--porcelain", "--untracked-files=all"]
        )
        if not result.ok:
            raise GitError(f"Could not inspect changes: {_safe_error(result)}")
        files: list[str] = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.strip().strip('"'))
        return tuple(files)

    def branch_exists(self, branch: str) -> bool:
        result = self._run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        return result.ok

    def _rev_exists(self, ref: str) -> bool:
        result = self._run(["rev-parse", "--verify", "--quiet", ref])
        return result.ok

    def is_protected(self, branch: str) -> bool:
        return branch in self._protected or not branch.startswith("agent/")

    def assert_agent_branch(self, branch: str, base_branch: str | None = None) -> str:
        if branch in self._protected or (base_branch and branch == base_branch):
            raise GitError(
                f"Refusing to modify protected branch '{branch}'. "
                "Agent changes are only allowed on agent/ branches."
            )
        validate_branch_name(branch)
        if self.is_protected(branch):
            raise GitError(
                f"Refusing to modify protected branch '{branch}'. "
                "Agent changes are only allowed on agent/ branches."
            )
        return branch

    def default_base(self) -> tuple[str, str]:
        """Return (ref to branch from, PR base name)."""

        remote_head = self._run(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]
        )
        if remote_head.ok:
            value = remote_head.stdout.strip()
            if value.startswith("origin/"):
                name = value[len("origin/") :]
                if name and self._rev_exists(f"refs/remotes/origin/{name}"):
                    if self._rev_exists(f"refs/heads/{name}"):
                        return name, name
                    return f"origin/{name}", name
        for name in ("main", "master"):
            if self._rev_exists(f"refs/heads/{name}"):
                return name, name
            if self._rev_exists(f"refs/remotes/origin/{name}"):
                return f"origin/{name}", name
        branch = self.current_branch()
        if branch != "HEAD":
            return branch, branch
        raise GitError(
            "Could not determine a base branch. Use --base-branch or create "
            "a local main/master branch."
        )

    def resolve_base(self, requested: str | None) -> tuple[str, str]:
        if requested is None:
            return self.default_base()
        requested = requested.strip()
        if not requested or requested.startswith("-") or any(
            part in requested for part in _BAD_BRANCH_PARTS
        ):
            raise GitError(f"Unsafe base branch name: {requested!r}")
        result = self._run(["check-ref-format", "--branch", requested])
        if not result.ok:
            raise GitError(f"Invalid base branch name: {requested}")
        if self._rev_exists(f"refs/heads/{requested}"):
            return requested, requested
        if self._rev_exists(f"refs/remotes/origin/{requested}"):
            return f"origin/{requested}", requested
        raise GitError(f"Base branch does not exist: {requested}")

    def worktree_for_branch(self, branch: str) -> Path | None:
        result = self._run(["worktree", "list", "--porcelain"])
        if not result.ok:
            return None
        current_path: Path | None = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = Path(line[len("worktree ") :])
            elif line == f"branch refs/heads/{branch}" and current_path is not None:
                return current_path
        return None

    def allocate_branch(self, title: str, issue: int | None = None) -> str:
        base = make_branch_name(title, issue=issue)
        stem = base
        for counter in range(1, 100):
            candidate = stem if counter == 1 else f"{stem}-{counter}"
            if len(candidate) > 120:
                candidate = f"{stem[:116]}-{counter}"
            validate_branch_name(candidate)
            if not self.branch_exists(candidate) and not self.worktree_for_branch(
                candidate
            ):
                return candidate
        raise GitError("Could not allocate a unique agent branch name")

    # --------------------------------------------------------------- worktree
    def create_worktree(
        self,
        *,
        title: str,
        issue: int | None = None,
        base_branch: str | None = None,
        worktree_root: Path | None = None,
    ) -> WorktreeInfo:
        base_ref, base_name = self.resolve_base(base_branch)
        branch = self.allocate_branch(title, issue=issue)
        source_branch = self.current_branch()
        source_head = self.head()
        dirty = self.dirty_files()

        root = (worktree_root or default_worktree_root()).expanduser().resolve()
        repo_key = (
            f"{slugify(self.name, 30)}-"
            f"{hashlib.sha256(str(self.path).encode()).hexdigest()[:10]}"
        )
        path = root / repo_key / branch.replace("/", "__")
        if is_within(self.path, path):
            raise GitError(
                "Worktree path must be outside the source working tree: "
                f"{path}"
            )
        if path.exists():
            raise GitError(f"Worktree path already exists: {path}")
        root_existed = root.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not root_existed:
                os.chmod(root, 0o700)
        except OSError as exc:
            raise GitError(
                f"Could not create worktree directory {path.parent}: {exc}"
            ) from exc

        result = self._run(
            ["worktree", "add", "-b", branch, str(path), base_ref], timeout=120
        )
        if not result.ok:
            raise GitError(f"Could not create Git worktree: {_safe_error(result)}")
        try:
            actual_branch = self.current_branch(cwd=path)
            if actual_branch != branch:
                raise GitError(
                    f"Worktree branch verification failed: expected {branch}, "
                    f"got {actual_branch}"
                )
            if self.dirty_files(cwd=path):
                raise GitError("New worktree is unexpectedly dirty")
        except Exception:
            # Best effort rollback only for the branch/path we just created.
            self._run(["worktree", "remove", "--force", str(path)])
            self._run(["branch", "-D", branch])
            raise

        return WorktreeInfo(
            source=self.path,
            path=path,
            branch=branch,
            base_ref=base_ref,
            base_branch=base_name,
            source_branch=source_branch,
            source_head=source_head,
            source_dirty=dirty,
        )

    def verify_source_unchanged(self, info: WorktreeInfo) -> None:
        current = self.current_branch()
        head = self.head()
        if current != info.source_branch or head != info.source_head:
            raise GitError(
                "The source repository branch or HEAD changed during the run. "
                "Refusing to continue."
            )

    # ---------------------------------------------------------------- commit
    def _validate_commit_file(self, cwd: Path, relative: str) -> Path:
        if (
            not relative
            or relative.startswith(("-", ":"))
            or Path(relative).is_absolute()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in relative)
        ):
            raise GitError(f"Invalid file path for staging: {relative!r}")
        raw_candidate = cwd / relative
        if raw_candidate.is_symlink():
            raise GitError(f"Refusing to stage a symbolic link: {relative}")
        candidate = raw_candidate.resolve()
        if not is_within(cwd, candidate):
            raise GitError(f"Refusing to stage a path outside the worktree: {relative}")
        if is_sensitive_path(relative):
            raise SecretDetected(
                f"Refusing to stage sensitive file: {relative}. Secrets must "
                "never be committed."
            )
        if not candidate.exists():
            raise GitError(f"Cannot stage missing file: {relative}")
        if not candidate.is_file():
            raise GitError(f"Cannot stage non-file path: {relative}")
        return candidate

    def _git_identity(self) -> tuple[str, str]:
        name = self._run(["config", "--get", "user.name"])
        email = self._run(["config", "--get", "user.email"])
        if not name.ok or not email.ok or not name.stdout.strip() or not email.stdout.strip():
            raise GitError(
                "Git author identity is not configured. Run:\n"
                "  git config --global user.name \"Your Name\"\n"
                "  git config --global user.email \"you@example.com\""
            )
        return name.stdout.strip(), email.stdout.strip()

    def commit_changes(
        self,
        info: WorktreeInfo,
        files: Iterable[str],
        *,
        subject: str,
        body: str = "",
        issue: int | None = None,
    ) -> CommitInfo:
        cwd = info.path
        branch = self.current_branch(cwd=cwd)
        self.assert_agent_branch(branch, info.base_branch)
        self._git_identity()

        paths: list[str] = []
        seen: set[str] = set()
        for raw in files:
            relative = str(raw).replace(os.sep, "/")
            if relative.startswith("./"):
                relative = relative[2:]
            if relative in seen:
                continue
            self._validate_commit_file(cwd, relative)
            if is_sensitive_path(relative):
                raise SecretDetected(f"Refusing to stage sensitive path: {relative}")
            paths.append(relative)
            seen.add(relative)
        if not paths:
            raise GitError("No task files to commit")

        changed = set(self.changed_files(cwd=cwd))
        relevant = [path for path in paths if path in changed]
        if not relevant:
            raise GitError("No changes to commit for the task files")
        for path in relevant:
            # --literal-pathspecs prevents glob/pathspec magic in a filename
            # from staging other files.
            result = self._run_at(
                cwd, ["--literal-pathspecs", "add", "--", path]
            )
            if not result.ok:
                raise GitError(f"Could not stage {path}: {_safe_error(result)}")

        staged_result = self._run_at(cwd, ["diff", "--cached", "--name-only"])
        if not staged_result.ok:
            raise GitError(f"Could not inspect staged files: {_safe_error(staged_result)}")
        staged = tuple(
            line.strip() for line in staged_result.stdout.splitlines() if line.strip()
        )
        unexpected = sorted(set(staged) - set(relevant))
        if unexpected:
            self._run_at(cwd, ["reset", "-q"])
            raise GitError(
                "Unexpected files were already staged; commit aborted: "
                + ", ".join(unexpected[:10])
            )

        diff_result = self._run_at(
            cwd, ["diff", "--cached", "--no-ext-diff", "--unified=0", "--text"]
        )
        if diff_result.ok:
            kinds = sorted(secret_kinds(diff_result.stdout))
            if kinds:
                self._run_at(cwd, ["reset", "-q"])
                raise SecretDetected(
                    "Potential secret material detected in staged changes "
                    f"({', '.join(kinds)}). Commit aborted and index reset. "
                    "Remove the secret and use environment variables instead."
                )

        subject = safe_for_terminal(
            neutralize_closing_keywords(subject or "Agent changes"), 72
        ).replace("\n", " ").strip()
        if not subject:
            subject = "Agent changes"
        body_parts = []
        if body.strip():
            body_parts.append(
                safe_for_terminal(neutralize_closing_keywords(body), 8_000).strip()
            )
        if issue is not None:
            body_parts.append(f"Refs #{int(issue)}")
        body_parts.append("Generated by ai-coding-agent.")
        full_body = "\n\n".join(part for part in body_parts if part)

        commit = self._run_at(
            cwd,
            [
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-verify",
                "-m",
                subject,
                "-m",
                full_body,
            ],
            timeout=120,
        )
        if not commit.ok:
            raise GitError(f"Git commit failed: {_safe_error(commit)}")

        commit_hash = self._checked_at(cwd, ["rev-parse", "HEAD"]).strip()
        short_hash = self._checked_at(cwd, ["rev-parse", "--short", "HEAD"]).strip()
        stat_result = self._run_at(
            cwd,
            [
                "show",
                "--no-ext-diff",
                "--stat",
                "--format=%h %s",
                "--no-renames",
                "HEAD",
            ],
        )
        stat = (
            safe_for_terminal(redact_secrets(stat_result.stdout), 8_000)
            if stat_result.ok
            else ""
        )
        return CommitInfo(
            hash=commit_hash,
            short_hash=short_hash,
            subject=subject,
            files=tuple(relevant),
            stat=stat,
            branch=branch,
        )

    def remote_url(self, remote: str = "origin") -> str:
        result = self._run(["remote", "get-url", remote])
        if not result.ok:
            raise GitError(
                f"No Git remote named '{remote}' is configured in this repository. "
                "Add one before pushing."
            )
        return sanitize_remote_url(result.stdout.strip())

    def push(self, info: WorktreeInfo, *, remote: str = "origin") -> PushInfo:
        branch = self.current_branch(cwd=info.path)
        self.assert_agent_branch(branch, info.base_branch)
        self.remote_url(remote)
        result = self._run_at(
            info.path, ["push", "--no-verify", "-u", remote, branch], timeout=180
        )
        if not result.ok:
            raise GitError(f"Git push failed: {_safe_error(result)}")
        return PushInfo(
            branch=branch,
            remote=remote,
            output=safe_for_terminal(redact_secrets(result.stdout), 4_000),
        )

    def show_commit(self, cwd: Path, rev: str = "HEAD") -> str:
        result = self._run_at(
            cwd,
            [
                "show",
                "--no-ext-diff",
                "--stat",
                "--format=%h %s",
                "--no-renames",
                rev,
            ],
        )
        if not result.ok:
            raise GitError(f"Could not show commit: {_safe_error(result)}")
        return safe_for_terminal(redact_secrets(result.stdout), 8_000)


def secret_kinds(text: str) -> set[str]:
    # Imported lazily to keep the public Git module small and avoid a circular
    # import if security helpers grow.
    from .security import secret_kinds as _secret_kinds

    return _secret_kinds(text)


__all__ = [
    "CommitInfo",
    "GitRepo",
    "PushInfo",
    "WorktreeInfo",
    "default_state_dir",
    "default_worktree_root",
    "make_branch_name",
    "validate_branch_name",
]
