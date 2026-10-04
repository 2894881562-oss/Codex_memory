"""Local input boundaries and best-effort secret redaction (not a DLP guarantee)."""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

SECRET_NAMES = {
    "auth.json",
    "credentials.json",
    "credential",
    "credentials",
    "cookies",
    "cookies.sqlite",
    "login data",
    "id_rsa",
    "id_ed25519",
    "cap_sid",
    "collector.token",
    "credential.json",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    ".git-credentials",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".sandbox-secrets",
    ".sandbox",
    "browser",
    "dist",
    "build",
    ".next",
    ".cache",
    ".ruff_cache",
    ".test-data",
    ".pytest_cache",
    "backups",
    "exports",
    "handoffs",
    "archives",
    "backup-staging",
    "local-state",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
}
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})(?![A-Za-z0-9_-])"
)
_ASSIGNMENT = re.compile(
    r"""(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|authorization)["']?\s*[:=]\s*)(?:"[^"\n]+"|'[^'\n]+'|[^\s,;\n]{8,})"""
)
_BEARER = re.compile(r"(?i)(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----.*?-----END (?:[A-Z ]*PRIVATE KEY)-----", re.S
)


def redact(text: str) -> str:
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", str(text))
    text = _TOKEN.sub("[REDACTED TOKEN]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _ASSIGNMENT.sub(lambda m: m.group(1) + '"[REDACTED]"', text)


def redact_object(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): redact_object(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_object(v) for v in value]
    return value


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def label(value: str, name: str = "label", allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是文本")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} 必须为 1..256 个可打印字符")
    return value


def bounded_int(value: int, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low}..{high} 范围内")
    return value


def excluded_file(path: Path) -> bool:
    names = {p.lower() for p in path.parts}
    filename = path.name.lower()
    return (
        bool(names & EXCLUDED_DIRS)
        or any(part.endswith(".egg-info") for part in names)
        or filename in SECRET_NAMES
        or filename == ".env"
        or filename.startswith(".env.")
        or path.suffix.lower() in {".pem", ".key", ".pfx", ".p12"}
    )


def safe_relative(name: str) -> Path:
    normalized = name.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(
            p in {"", ".", ".."}
            or any(c in p for c in ':\x00<>"|?*')
            or any(ord(c) < 32 for c in p)
            or p.endswith((" ", "."))
            or p.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for p in parts
        )
    ):
        raise ValueError("不安全的归档路径")
    return Path(*parts)


def is_link(path: Path) -> bool:
    """Exclude Windows junctions/reparse points as well as ordinary symlinks."""
    try:
        stat = path.lstat()
        return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def ensure_collector_token(data_dir: Path) -> str:
    state = Path(data_dir).resolve() / "local-state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "collector.token"
    try:
        with path.open("x", encoding="ascii") as stream:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            token = secrets.token_urlsafe(32)
            stream.write(token)
            return token
    except FileExistsError:
        token = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
            raise ValueError("本地采集令牌格式无效，请检查 local-state/collector.token") from None
        return token
