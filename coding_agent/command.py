"""Small subprocess wrapper. Argument lists only; never uses a shell."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = 60,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    argv = [str(arg) for arg in args]
    started = time.monotonic()
    completed = subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
        shell=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=dict(env) if env is not None else None,
        check=False,
    )
    return CommandResult(
        args=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=time.monotonic() - started,
    )


def git_github_env() -> dict[str, str]:
    """Environment for git/gh without AI-provider credentials.

    Git hooks are skipped at commit time, but Git credential helpers and gh
    plugins are trusted host tooling. Do not let them inherit the OpenAI key.
    """

    prefix = ("OPENAI_", "ANTHROPIC_", "AZURE_OPENAI_")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(prefix)
    }
    # Fail fast instead of hanging on a credential prompt in a non-interactive
    # subprocess. gh must never open an interactive prompt either.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def sanitized_subprocess_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for Docker/Git subprocesses without common secret variables.

    The container itself never inherits host environment variables (only
    explicitly supplied ``-e`` values are passed), but keeping secrets out of
    the client process environment is an additional layer.
    """

    secret_fragments = (
        "API_KEY",
        "APIKEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "PRIVATE_KEY",
        "OPENAI_",
        "ANTHROPIC_",
        "AWS_",
        "AZURE_",
        "GITHUB_",
        "GH_",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.upper() for fragment in secret_fragments)
    }
    if extra:
        env.update(extra)
    return env
