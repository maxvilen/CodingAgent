from __future__ import annotations

from coding_agent.command import git_github_env, sanitized_subprocess_env


def test_git_env_drops_ai_keys_but_keeps_github_auth(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    env = git_github_env()
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["GH_TOKEN"] == "gh-token"


def test_container_env_drops_all_secret_like_variables(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("NORMAL_SETTING", "kept")
    env = sanitized_subprocess_env()
    assert "OPENAI_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["NORMAL_SETTING"] == "kept"
