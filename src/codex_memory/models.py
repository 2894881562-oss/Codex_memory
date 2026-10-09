"""Stable importer/storage contract. IDs are source IDs, scoped by account label."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    id: str
    role: str
    text: str
    timestamp: str = ""
    parent_id: str | None = None
    branch: str = ""
    kind: str = "message"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    id: str
    source: str
    account: str
    title: str
    messages: list[Message] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    project: str = ""
    cwd: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportBatch:
    conversations: list[Conversation] = field(default_factory=list)
    # bytes are archived intact, never interpreted as instructions or executed.
    artifacts: list[tuple[str, bytes, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0
    warnings_omitted: int = 0
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportReport:
    conversations: int = 0
    messages: int = 0
    artifacts: int = 0
    unchanged: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    warnings_omitted: int = 0
    coverage: dict[str, Any] = field(default_factory=dict)
    conversations_seen: int = 0
    messages_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)
