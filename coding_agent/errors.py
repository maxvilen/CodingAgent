"""User-facing error types.

Messages in these exceptions are safe to print: they must never contain API
keys, tokens, file contents, or other secrets.
"""

from __future__ import annotations


class AgentError(RuntimeError):
    """Base class for expected, user-facing failures."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class ConfigError(AgentError):
    exit_code = 2


class GitError(AgentError):
    exit_code = 1


class GitHubError(AgentError):
    exit_code = 2


class DockerUnavailable(ConfigError):
    exit_code = 2


class DockerTestError(AgentError):
    exit_code = 1


class PricingError(ConfigError):
    exit_code = 2


class BudgetExceeded(AgentError):
    exit_code = 2


class AgentApiError(AgentError):
    exit_code = 3


class SecretDetected(AgentError):
    exit_code = 1
