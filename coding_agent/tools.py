"""Explicitly allowed agent tools. There is no arbitrary shell tool."""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .command import git_github_env, run_command
from .docker_tests import DockerTestRunner, TestResult
from .errors import AgentError, DockerTestError, SecretDetected
from .git_ops import GitRepo
from .security import (
    clip,
    is_sensitive_path,
    is_within,
    redact_secrets,
    safe_for_terminal,
    secret_kinds,
)

MAX_READ = 30_000
MAX_OUTPUT = 12_000
MAX_WRITE = 120_000


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "strict": False,
    }


ANALYSIS_TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "list_files",
        "List tracked and untracked (not ignored) repository files, optionally filtered by substring.",
        {"filter": _string("Filename substring, or an empty string for all files")},
        ["filter"],
    ),
    _tool(
        "read_file",
        "Read a UTF-8 text file from the repository. Secrets and binary files are blocked.",
        {"path": _string("Repository-relative path")},
        ["path"],
    ),
    _tool(
        "search_code",
        "Search tracked source files with git grep using a literal fixed string.",
        {"query": _string("Literal search string (1-200 characters)")},
        ["query"],
    ),
    _tool(
        "git_status",
        "Show the current Git branch, status, and diff summary. Read-only.",
        {},
    ),
)

EDIT_TOOLS: tuple[dict[str, Any], ...] = ANALYSIS_TOOLS + (
    _tool(
        "write_file",
        "Replace a text file with complete new contents. Requires explicit user approval; writes only inside the worktree.",
        {
            "path": _string("Repository-relative path"),
            "content": _string("Complete new UTF-8 file contents"),
        },
        ["path", "content"],
    ),
    _tool(
        "run_tests",
        "Run tests in the isolated Docker test container. No network, non-root, resource-limited.",
        {
            "profile": _string("Test profile; currently 'pytest'"),
            "test_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional repository-relative pytest paths/node IDs; empty runs the full suite",
            },
        },
        ["profile"],
    ),
    _tool(
        "submit_summary",
        "Record the final change summary and proposed draft-PR title/body. Does not create a PR.",
        {
            "summary": _string("Concise summary of implemented changes"),
            "pr_title": _string("Proposed pull request title"),
            "pr_body": _string("Proposed pull request description (issue reference is added later)"),
        },
        ["summary", "pr_title", "pr_body"],
    ),
)


@dataclass(frozen=True)
class ChangeSummary:
    summary: str
    pr_title: str
    pr_body: str


class RepositoryTools:
    def __init__(
        self,
        root: Path,
        *,
        phase: str,
        docker: DockerTestRunner | None = None,
        approver: Callable[[str], bool] | None = None,
        echo: Callable[[str], None] = print,
        max_read: int = MAX_READ,
        max_output: int = MAX_OUTPUT,
    ) -> None:
        if phase not in {"analysis", "edit"}:
            raise ValueError("phase must be 'analysis' or 'edit'")
        self.root = Path(root).expanduser().resolve()
        self.phase = phase
        self.docker = docker
        self.approver = approver or (lambda _prompt: False)
        self.echo = echo
        self.max_read = max_read
        self.max_output = max_output
        self.touched_files: set[str] = set()
        self.last_test_result: TestResult | None = None
        self.summary: ChangeSummary | None = None

    @property
    def specs(self) -> tuple[dict[str, Any], ...]:
        return EDIT_TOOLS if self.phase == "edit" else ANALYSIS_TOOLS

    # ------------------------------------------------------------- validation
    def _resolve(
        self,
        relative: str,
        *,
        must_be_file: bool = False,
        allow_missing: bool = False,
    ) -> Path:
        if not isinstance(relative, str):
            raise AgentError("Path must be a string")
        relative = relative.strip()
        if (
            not relative
            or len(relative) > 500
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in relative)
            or Path(relative).is_absolute()
        ):
            raise AgentError("Invalid repository-relative path")
        raw = self.root / relative
        if raw.is_symlink():
            raise AgentError("Symbolic links are not allowed")
        resolved = raw.resolve()
        if not is_within(self.root, resolved):
            raise AgentError("Path escapes the worktree")
        try:
            rel = resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise AgentError("Path escapes the worktree") from exc
        if is_sensitive_path(rel):
            raise AgentError("Sensitive files and directories are blocked")
        if must_be_file:
            if not resolved.exists() or not resolved.is_file():
                if not allow_missing:
                    raise AgentError("File does not exist")
        return resolved

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _output(self, text: str) -> str:
        return clip(safe_for_terminal(redact_secrets(text)), self.max_output)

    # --------------------------------------------------------------- read-only
    def list_files(self, args: dict[str, Any]) -> str:
        filter_text = str(args.get("filter") or "").lower()
        result = run_command(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            timeout=30,
            env=git_github_env(),
        )
        if not result.ok:
            raise AgentError("Could not list repository files")
        lines = []
        for name in result.stdout.splitlines():
            if not name or is_sensitive_path(name):
                continue
            if filter_text and filter_text not in name.lower():
                continue
            lines.append(name)
            if len(lines) >= 2_000:
                lines.append("[... file list truncated ...]")
                break
        return self._output("\n".join(lines) or "No matching files")

    def read_file(self, args: dict[str, Any]) -> str:
        path = self._resolve(str(args.get("path") or ""), must_be_file=True)
        try:
            if path.stat().st_size > 500_000:
                return "File is too large to read (limit 500 KB)"
            data = path.read_bytes()
        except OSError as exc:
            raise AgentError(f"Could not read file: {exc.strerror or exc}") from exc
        if b"\x00" in data[:4_096]:
            return "Binary file blocked"
        text = data.decode("utf-8", errors="replace")
        if len(text) > self.max_read:
            text = text[: self.max_read] + "\n[... truncated ...]"
        return self._output(text)

    def search_code(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "")
        if not query or len(query) > 200 or any(ord(ch) < 32 for ch in query):
            raise AgentError("Search query must be 1-200 printable characters")
        result = run_command(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "grep",
                "-n",
                "-F",
                "-e",
                query,
                "--",
            ],
            timeout=30,
            env=git_github_env(),
        )
        if result.returncode == 1:
            return "No matches"
        if result.returncode != 0:
            raise AgentError("Search failed")
        filtered: list[str] = []
        for line in result.stdout.splitlines():
            path = line.split(":", 1)[0]
            if is_sensitive_path(path):
                continue
            filtered.append(line)
            if len(filtered) >= 300:
                filtered.append("[... results truncated ...]")
                break
        return self._output("\n".join(filtered) or "No matches")

    def git_status(self, _args: dict[str, Any]) -> str:
        repo = GitRepo(self.root)
        return self._output(
            repo.status_short() + "\n" + repo.diff_stat()
        )

    # ------------------------------------------------------------------- edits
    def write_file(self, args: dict[str, Any]) -> str:
        if self.phase != "edit":
            raise AgentError("Writes are not allowed in analysis mode")
        relative = str(args.get("path") or "")
        content = args.get("content")
        if not isinstance(content, str):
            raise AgentError("File content must be a string")
        if len(content) > MAX_WRITE:
            raise AgentError(f"File content too large (limit {MAX_WRITE} characters)")
        if "\x00" in content:
            raise AgentError("Binary content is not supported")
        path = self._resolve(relative, allow_missing=True)
        rel = self._relative(path)
        if path.exists() and not path.is_file():
            raise AgentError("Target is not a regular file")
        kinds = secret_kinds(content)
        if kinds:
            raise SecretDetected(
                "Refusing to write potential secret material "
                f"({', '.join(sorted(kinds))}). Use environment variables or "
                "a documented placeholder instead."
            )
        branch = GitRepo(self.root).current_branch()
        GitRepo(self.root).assert_agent_branch(branch)

        old = ""
        if path.exists():
            try:
                old = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise AgentError(f"Could not read existing file: {exc}") from exc
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        if not diff:
            return f"No change for {rel}"
        self.echo("\nProposed file change:")
        # Show the complete diff for explicit approval (up to the write-size
        # limit), rather than hiding the tail behind the normal tool-output cap.
        self.echo(safe_for_terminal(redact_secrets(diff), 150_000))
        if not self.approver(f"write {rel}"):
            return "Denied by user; the file was not written."

        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode if path.exists() else None
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.agent-", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.chmod(temp_name, mode if mode is not None else 0o644)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        self.touched_files.add(rel)
        added = sum(
            1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        self.echo(f"Wrote {rel} (+{added}/-{removed}); not committed.")
        return f"Wrote {rel} (+{added}/-{removed}). Changes are not committed yet."

    def run_tests(self, args: dict[str, Any]) -> str:
        if self.phase != "edit":
            raise AgentError("Tests can only be run in edit mode")
        if self.docker is None:
            raise DockerTestError("Docker test runner is not configured")
        profile = str(args.get("profile") or "pytest")
        raw_paths = args.get("test_paths") or []
        if not isinstance(raw_paths, list) or any(
            not isinstance(item, str) for item in raw_paths
        ):
            raise AgentError("test_paths must be an array of strings")
        result = self.docker.run(
            self.root, profile=profile, test_paths=tuple(raw_paths)
        )
        self.last_test_result = result
        self.echo(result.summary())
        tail = "\n".join(
            part
            for part in (
                result.summary(),
                f"Exit code: {result.exit_code}",
                ("FAILED: " + ", ".join(result.failed_tests))
                if result.failed_tests
                else "",
                "--- stdout ---",
                result.stdout[-6_000:],
                "--- stderr ---",
                result.stderr[-3_000:],
            )
            if part
        )
        return self._output(tail)

    def submit_summary(self, args: dict[str, Any]) -> str:
        if self.phase != "edit":
            raise AgentError("Summary submission is only allowed in edit mode")
        summary = str(args.get("summary") or "").strip()
        pr_title = str(args.get("pr_title") or "").strip()
        pr_body = str(args.get("pr_body") or "").strip()
        if not summary or not pr_title or not pr_body:
            raise AgentError("summary, pr_title and pr_body must be non-empty")
        if len(summary) > 4_000 or len(pr_title) > 200 or len(pr_body) > 20_000:
            raise AgentError("Summary/PR fields are too long")
        self.summary = ChangeSummary(summary=summary, pr_title=pr_title, pr_body=pr_body)
        return "Summary recorded. No pull request was created."

    # ---------------------------------------------------------------- dispatch
    def execute(self, name: str, args: dict[str, Any]) -> str:
        handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_code": self.search_code,
            "git_status": self.git_status,
        }
        if self.phase == "edit":
            handlers.update(
                {
                    "write_file": self.write_file,
                    "run_tests": self.run_tests,
                    "submit_summary": self.submit_summary,
                }
            )
        handler = handlers.get(name)
        if handler is None:
            raise AgentError(f"Unknown or disallowed tool: {name}")
        return handler(args)


__all__ = [
    "ANALYSIS_TOOLS",
    "EDIT_TOOLS",
    "ChangeSummary",
    "RepositoryTools",
]
