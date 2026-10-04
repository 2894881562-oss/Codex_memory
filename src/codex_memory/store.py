"""Versioned SQLite records plus immutable SHA-256 addressed source blobs."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
INSERT INTO schema_info(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_info);
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, external_id TEXT NOT NULL, source TEXT NOT NULL, account TEXT NOT NULL,
 title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, project TEXT NOT NULL,
 cwd TEXT NOT NULL, metadata TEXT NOT NULL, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), external_id TEXT NOT NULL,
 role TEXT NOT NULL, text TEXT NOT NULL, timestamp TEXT NOT NULL, parent_id TEXT, branch TEXT NOT NULL,
 kind TEXT NOT NULL, metadata TEXT NOT NULL, ordinal INTEGER NOT NULL, fingerprint TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, ordinal);
CREATE INDEX IF NOT EXISTS conversations_project ON conversations(project, updated_at);
CREATE TABLE IF NOT EXISTS message_revisions (
 id INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id), fingerprint TEXT NOT NULL,
 text TEXT NOT NULL, metadata TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE(message_id, fingerprint));
CREATE TABLE IF NOT EXISTS artifacts (
 hash TEXT PRIMARY KEY, size INTEGER NOT NULL, mime TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_sources (
 id INTEGER PRIMARY KEY, hash TEXT NOT NULL REFERENCES artifacts(hash), source TEXT NOT NULL,
 account TEXT NOT NULL, name TEXT NOT NULL, imported_at TEXT NOT NULL,
 UNIQUE(hash, source, account, name));
CREATE TABLE IF NOT EXISTS checkpoints (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL, completed TEXT NOT NULL,
 decisions TEXT NOT NULL, next_steps TEXT NOT NULL, blockers TEXT NOT NULL, account TEXT NOT NULL,
 source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS checkpoints_project ON checkpoints(project, created_at);
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, account TEXT NOT NULL,
 project TEXT NOT NULL, last_run TEXT NOT NULL DEFAULT '', last_report TEXT NOT NULL DEFAULT '{}',
 UNIQUE(kind, path, account, project));
CREATE TABLE IF NOT EXISTS import_runs (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, account TEXT NOT NULL, created_at TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_files (
 source_id TEXT NOT NULL REFERENCES sources(id), path TEXT NOT NULL, signature TEXT NOT NULL,
 PRIMARY KEY(source_id,path));
CREATE TABLE IF NOT EXISTS project_files (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, account TEXT NOT NULL, relative_path TEXT NOT NULL,
 root TEXT NOT NULL, hash TEXT NOT NULL REFERENCES artifacts(hash), size INTEGER NOT NULL,
 text TEXT, updated_at TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1, UNIQUE(project, account, root, relative_path));
CREATE TABLE IF NOT EXISTS project_file_revisions (
 id INTEGER PRIMARY KEY, file_id TEXT NOT NULL REFERENCES project_files(id), hash TEXT NOT NULL REFERENCES artifacts(hash),
 recorded_at TEXT NOT NULL, UNIQUE(file_id, hash));
CREATE TABLE IF NOT EXISTS project_snapshots (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, account TEXT NOT NULL, root TEXT NOT NULL,
 git_head TEXT NOT NULL, git_branch TEXT NOT NULL, git_status TEXT NOT NULL,
 created_at TEXT NOT NULL, fingerprint TEXT NOT NULL, UNIQUE(root, account, project, fingerprint));
"""


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.db_path = self.data_dir / "memory.sqlite3"
        self.blob_dir = self.data_dir / "archive" / "sha256"
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=30) as con:
            existing = con.execute("SELECT name FROM sqlite_master WHERE name='schema_info'").fetchone()
            if existing:
                row = con.execute("SELECT version FROM schema_info").fetchone()
                if not row or row[0] != SCHEMA_VERSION:
                    raise ValueError("不兼容的数据库版本，请使用匹配的软件版本")
                # Opening a reader must not rerun DDL or contend with an in-progress bulk import.
                if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    con.execute("PRAGMA journal_mode=WAL")
                self._ready = True
                return
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            try:
                con.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(message_id UNINDEXED, text, tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                pass
        self._ready = True

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def blob_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("归档哈希无效")
        return self.blob_dir / digest[:2] / digest

    def archive(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"既有归档损坏: {digest}") from None
        return digest
