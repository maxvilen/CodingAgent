from __future__ import annotations

import re

from coding_agent.security import (
    is_sensitive_path,
    neutralize_closing_keywords,
    redact_secrets,
    safe_for_terminal,
    sanitize_remote_url,
    secret_kinds,
    slugify,
)


def test_sensitive_paths_blocked() -> None:
    blocked = [
        ".env",
        ".env.local",
        "config/.env.production",
        "secrets/api.txt",
        "credentials.json",
        "deploy/id_rsa",
        "deploy/server.pem",
        "node_modules/pkg/index.js",
        ".git/config",
        ".ssh/config",
        ".venv/bin/python",
    ]
    for path in blocked:
        assert is_sensitive_path(path), path


def test_safe_templates_and_normal_paths_allowed() -> None:
    for path in [
        ".env.example",
        ".env.sample",
        "README.md",
        "app.py",
        "src/config.py",
        ".gitignore",
    ]:
        assert not is_sensitive_path(path), path


def test_redaction_and_secret_detection() -> None:
    openai_key = "sk-" + "a" * 40
    github_token = "ghp_" + "b" * 36
    private_key = (
        "-----BEGIN "
        + "PRIVATE KEY-----\n"
        + "not-a-real-key\n"
        + "-----END "
        + "PRIVATE KEY-----"
    )
    text = f"key={openai_key} token={github_token}\n{private_key}"
    kinds = secret_kinds(text)
    assert "openai-key" in kinds
    assert "github-token" in kinds
    assert "private-key" in kinds
    redacted = redact_secrets(text)
    assert openai_key not in redacted
    assert github_token not in redacted
    assert "not-a-real-key" not in redacted
    assert "[REDACTED:openai-key]" in redacted


def test_terminal_output_strips_ansi_and_secrets() -> None:
    key = "sk-" + "c" * 40
    text = f"\x1b[31mred\x1b[0m\x1b]0;title\x07 {key}\x00"
    safe = safe_for_terminal(text)
    assert "sk-" not in safe
    assert "\x1b" not in safe
    assert "\x00" not in safe
    assert "red" in safe


def test_closing_keywords_neutralized() -> None:
    text = "This fixes #123 and Closes #9. Resolves: owner/repo#7."
    neutral = neutralize_closing_keywords(text)
    assert not re.search(r"(?i)\b(fix(es|ed)?|clos(e|es|ed)|resolv(e|es|ed))\s+#", neutral)
    assert "Refs #123" in neutral
    assert "Refs #9" in neutral
    assert "Refs owner/repo#7" in neutral


def test_remote_url_credentials_removed() -> None:
    assert (
        sanitize_remote_url("https://token123@github.com/acme/repo.git")
        == "https://github.com/acme/repo.git"
    )
    assert (
        sanitize_remote_url("https://user:password@github.com:443/acme/repo.git")
        == "https://github.com:443/acme/repo.git"
    )
    assert sanitize_remote_url("git@github.com:acme/repo.git") == (
        "git@github.com:acme/repo.git"
    )
    text = safe_for_terminal("remote https://secret@github.com/acme/repo.git")
    assert "secret@" not in text
    assert "https://github.com/acme/repo.git" in text


def test_slugify() -> None:
    assert slugify("Fix: Login-Problem åäö!") == "fix-login-problem-aao"
    assert slugify("") == "task"
    assert len(slugify("x" * 100, max_length=10)) <= 10
