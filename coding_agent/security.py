"""Security helpers: path validation, secret blocking, redaction, terminal safety."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Names that are never read from, written to, or staged by the agent.
_SENSITIVE_DIR_NAMES = {
    ".git",
    ".ssh",
    ".aws",
    ".gnupg",
    ".azure",
    ".docker",
    ".kube",
    "secrets",
    "credentials",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    "coverage",
}

# Explicitly safe templates that often contain only variable names.
_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}

_SENSITIVE_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
    ".ppk",
}

_SENSITIVE_EXACT = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}

# High-confidence secret formats. Used only for redaction/refusal; matches
# are never printed with their value.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r"[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    ("openai-key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "generic-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret)"
            r"\s*[:=]\s*['\"][A-Za-z0-9_\-./+]{24,}['\"]"
        ),
    ),
)

_ANSI_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_SINGLE = re.compile(r"\x1b[@-Z\\-_]")
_CLOSING_KEYWORD = re.compile(
    r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"((?:[\w.-]+/[\w.-]+)?#\d+)"
)


def is_sensitive_path(path: str | Path) -> bool:
    """Return True when a path must not be exposed to the model or commits."""

    parts = [p.lower() for p in Path(path).parts]
    if not parts:
        return False
    for part in parts[:-1]:
        if part in _SENSITIVE_DIR_NAMES:
            return True
    name = parts[-1]
    if name in _SENSITIVE_DIR_NAMES:
        return True
    if name in _SAFE_ENV_TEMPLATES:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SENSITIVE_EXACT:
        return True
    if name.startswith("credentials.") or name.startswith("secrets."):
        return True
    if name.startswith("id_rsa") or name.startswith("id_ed25519"):
        return True
    return Path(name).suffix in _SENSITIVE_SUFFIXES


def secret_kinds(text: str) -> set[str]:
    """Return high-confidence secret categories found in text."""

    found = {name for name, pattern in _SECRET_PATTERNS if pattern.search(text)}
    return found


def redact_secrets(text: str) -> str:
    """Replace high-confidence secrets with a stable marker."""

    redacted = text
    for name, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return redacted


def sanitize_remote_url(url: str) -> str:
    """Remove credentials embedded in an HTTP(S) remote URL."""

    value = url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return redact_secrets(value)
    if parsed.scheme not in {"http", "https"} or not (
        parsed.username or parsed.password
    ):
        return redact_secrets(value)
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def redact_url_credentials(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return sanitize_remote_url(match.group(0))

    return _URL_RE.sub(replace, text)


def safe_for_terminal(text: str, limit: int = 20_000) -> str:
    """Strip terminal control sequences and redact secrets before printing."""

    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_SINGLE.sub("", text)
    text = "".join(
        ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127)
    )
    return redact_url_credentials(redact_secrets(text))[:limit]


def clip(text: str, limit: int, note: str = "\n[... truncated ...]") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(note))] + note


def is_within(root: Path, candidate: Path) -> bool:
    """Resolve both paths and check that candidate stays inside root."""

    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        return resolved_candidate == resolved_root or resolved_candidate.is_relative_to(
            resolved_root
        )
    except ValueError:
        return False


def slugify(text: str, max_length: int = 40) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = "task"
    return slug[:max_length].strip("-") or "task"


def neutralize_closing_keywords(text: str) -> str:
    """Prevent PR text from auto-closing issues when later merged."""

    return _CLOSING_KEYWORD.sub(lambda m: f"Refs {m.group(2)}", text)
